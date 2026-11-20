"""Opt-in reset transport and a bounded, persisted Host admission lane."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid

from . import config, db, settings_store
from .log import log_fault

TIMEOUT_SECONDS = 150
CHECK_INTERVAL_MS = 10 * 60_000
MAX_RECONCILE_ATTEMPTS = 6
DAY_MS = 24 * 60 * 60_000


def _kill_group(process):
    """Only the session this call created; include descendants after parent exit."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# The broker is started with what it needs to run and nothing of the Host's own (R145, both
# providers' reset commands): its home, PATH, the XDG directories and the locale. Never the Host's
# keys, tokens or session names.
BROKER_ENV = frozenset({"HOME", "PATH", "LANG"})
BROKER_ENV_PREFIXES = ("LC_", "XDG_")


def broker_env(env=None) -> dict[str, str]:
    env = os.environ if env is None else env
    return {key: value for key, value in env.items()
            if key in BROKER_ENV or key.startswith(BROKER_ENV_PREFIXES)}


def attempt(command_field: str, models, *, accounts=(), provider="claude", stop_event=None, cancelled=None,
            reconcile_only=False) -> dict:
    """Run the provider's reset broker once. A receipt is verified only when it names one of
    ``accounts``, the provider's saved aliases as the keeper's read gave them (R145): never a
    pattern of another provider's names."""
    saved = {str(alias) for alias in accounts or () if isinstance(alias, str) and alias}
    command = getattr(config.load(), command_field, ()) if command_field else ()
    if not command:
        return {"status": "disabled", "verified": False, "needs_reconcile": reconcile_only}
    process = None
    try:
        # The supported broker is POSIX-only; do not silently leak a Windows tree.
        if os.name != "posix":
            raise OSError("reset process groups unavailable")
        if (stop_event and stop_event.is_set()) or (cancelled and cancelled()):
            return {"status": "cancelled", "verified": False, "needs_reconcile": reconcile_only}
        process = subprocess.Popen(list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True, env=broker_env())
        deadline = time.monotonic() + TIMEOUT_SECONDS
        request = {"models": models}
        if reconcile_only:
            request["reconcile_only"] = True
        payload = json.dumps(request)
        while True:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("banked-reset", TIMEOUT_SECONDS)
            if (stop_event and stop_event.is_set()) or (cancelled and cancelled()):
                raise InterruptedError("reset owner changed")
            try:
                stdout, _ = process.communicate(input=payload, timeout=.25)
                break
            except subprocess.TimeoutExpired:
                payload = None  # communicate retains its original input across retries.
        receipt = json.loads(stdout)
        if not isinstance(receipt, dict):
            raise ValueError("unreadable reset receipt")
        verified = (process.returncode == 0 and receipt.get("verified") is True
                    and receipt.get("status") in ({"reconciled"} if reconcile_only else {"redeemed", "reconciled"})
                    and isinstance(receipt.get("account"), str)
                    and receipt["account"] in saved)
        status = receipt.get("status")
        allowed = {"no_pending", "cancelled", "disabled", "not_applicable", "no_owned_reset_accounts", "busy", "cooldown",
                   "unverified", "no_eligible_grant", "eligible", "redeemed", "reconciled"}
        if status not in allowed or process.returncode or (status in {"redeemed", "reconciled"} and not verified):
            status = "unverified"
        left = receipt.get("resets_left")
        # What the broker CLAIMS was spent counts against the cap, verified or not (R145): a banked
        # reset cannot be given back, so the Host never asks again on the strength of a doubt.
        claimed = receipt.get("status") in {"redeemed", "reconciled"}
        return {"status": status, "verified": verified, "claimed": claimed,
                "account": receipt.get("account") if verified else None,
                "resets_left": left if verified and type(left) is int and left >= 0 else None,
                "needs_reconcile": status in {"unverified", "cancelled"} or (reconcile_only and not (verified or status == "no_pending"))}
    except InterruptedError as exc:
        log_fault(f"{provider}BankedResetCancelled", exc)
        return {"status": "cancelled", "verified": False, "needs_reconcile": reconcile_only or process is not None}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        log_fault(f"{provider}BankedResetCommandFailed", exc)
        return {"status": "unverified", "verified": False, "needs_reconcile": reconcile_only or process is not None}
    finally:
        if process is not None:
            _kill_group(process)
            process.communicate()  # Reap our parent and close the private pipes.


class ResetWorker:
    """One offer, no queue, outside the shared Janitor thread; never switches.

    Admission is persisted before launch, so restarts/concurrent instances cannot
    bypass the ten-minute Host budget. The broker retains uncertain write intent.
    A completion merely schedules a new ordinary guarded read, not a switch.
    """
    def __init__(self, stop_event):
        self.stop_event = stop_event
        self._slot = threading.Lock()
        self.thread = None

    @staticmethod
    def _key(provider):
        return f"hotseat-reset.{provider}.state"

    @staticmethod
    def _spent_key(provider):
        return f"hotseat-reset.{provider}.redeemed"

    def spent_today(self, provider, now=None):
        """The redemptions of the last 24 hours (their times, ms), kept in the settings store so a
        restart does not forget them (R145); None when that record cannot be read (anything but a
        list of whole numbers): the caller then redeems nothing."""
        now = db.now_ms() if now is None else now
        try:
            times = json.loads(settings_store.get_text(self._spent_key(provider), default="[]"))
        except (ValueError, TypeError):
            return None
        if not isinstance(times, list) or not all(type(t) is int for t in times):
            return None
        return [t for t in times if now - DAY_MS < t <= now]

    def capped(self, provider, cap_field):
        """'off' when the provider's cap is 0, 'ledger_unreadable' when its count cannot be read
        (it fails closed), 'daily_cap' when its redemptions of the last 24 hours reach the cap,
        else ''. A provider with no cap field has none."""
        if not cap_field:
            return ""
        cap = getattr(config.load(), cap_field, 0)
        if not cap:
            return "off"
        spent = self.spent_today(provider)
        if spent is None:
            return "ledger_unreadable"
        return "daily_cap" if len(spent) >= cap else ""

    def peek(self, provider, owner):
        try:
            value = json.loads(settings_store.get_text(self._key(provider), default="{}"))
            if (value.get("owner") == owner["agent_id"] and value.get("result")
                    and not value.get("handled")):
                return value
        except (ValueError, TypeError, AttributeError) as exc:
            log_fault("bankedResetStateUnreadable", exc)
        return None

    def reconciliation_due(self, provider, owner, command_field):
        if (not command_field or self.stop_event.is_set() or owner["options"]["mode"] != "automatic"
                or not getattr(config.load(), command_field, ())):
            return None
        try:
            value = json.loads(settings_store.get_text(self._key(provider), default="{}"))
            same_generation = value.get("generation") == owner["generation"]
            capped = same_generation and value.get("reconcile_attempts", 0) >= MAX_RECONCILE_ATTEMPTS
            due_at = value.get("next_at", float('inf'))
            if not same_generation:
                due_at = min(due_at, value.get("started", 0) + CHECK_INTERVAL_MS)
            if (not capped and value.get("owner") == owner["agent_id"] and value.get("id")
                    and due_at <= db.now_ms()
                    and ((value.get("handled") and value.get("needs_reconcile"))
                         or not value.get("result"))):
                return value
        except (ValueError, TypeError, AttributeError) as exc:
            log_fault("bankedResetReconcileStateUnreadable", exc)
        return None

    def ack(self, provider, receipt):
        with settings_store.transaction():
            value = json.loads(settings_store.get_text(self._key(provider), default="{}"))
            if value.get("id") == receipt["id"]:
                value["handled"] = True
                settings_store.set_text(self._key(provider), json.dumps(value))

    def offer(self, provider, field, models, owner, *, accounts=(), cap_field="", reconcile_only=False):
        expected_command=tuple(getattr(config.load(), field, ()))
        if not expected_command:
            return "disabled"
        if owner["options"]["mode"] != "automatic" or self.stop_event.is_set():
            return "disabled"
        cap = "" if reconcile_only else self.capped(provider, cap_field)
        if cap == "off":
            return "disabled"
        if cap:
            return cap                  # the broker is not called: the Host's cap (R145)
        if not self._slot.acquire(blocking=False):
            return "busy"
        launched = False
        try:
            from . import turn_queue
            claim = {"id": uuid.uuid4().hex, "owner": owner["agent_id"],
                     "generation": owner["generation"], "models": list(models),
                     "accounts": [str(alias) for alias in accounts or ()],
                     "reconcile_only": reconcile_only,
                     "stop_generation": turn_queue.user_stop_generation(owner["agent_id"])}
            with settings_store.transaction():
                now = db.now_ms()
                prior = json.loads(settings_store.get_text(self._key(provider), default="{}"))
                same_generation = prior.get("owner") == owner["agent_id"] and prior.get("generation") == owner["generation"]
                due_at = prior.get("next_at", 0)
                if not same_generation:
                    due_at = min(due_at, prior.get("started", 0) + CHECK_INTERVAL_MS)
                if due_at > now:
                    return "cooldown"
                # Unsettled effects cannot be bypassed by an exhaustion offer.
                reconcile_only = bool(reconcile_only or prior.get("needs_reconcile")
                    or (prior.get("id") and not prior.get("result")))
                if reconcile_only and prior.get("accounts"):
                    # The aliases the attempt being reconciled was offered with, and today's.
                    claim["accounts"] = list(dict.fromkeys(list(prior["accounts"]) + claim["accounts"]))
                same_generation = prior.get("owner") == owner["agent_id"] and prior.get("generation") == owner["generation"]
                attempts = prior.get("reconcile_attempts", 0) if same_generation else 0
                if reconcile_only and attempts >= MAX_RECONCILE_ATTEMPTS:
                    return "attention_required"
                attempts = attempts + 1 if reconcile_only else 0
                # A reconcile continues the attempt before it: a redemption it already counted is
                # not counted again.
                claim["counted"] = bool(reconcile_only and prior.get("counted"))
                interval = CHECK_INTERVAL_MS * 2 ** max(0, attempts - 1)
                claim.update(started=now, next_at=now + interval, handled=False,
                             reconcile_only=reconcile_only, reconcile_attempts=attempts)
                settings_store.set_text(self._key(provider), json.dumps(claim))
            self.thread = threading.Thread(target=self._work, args=(provider, field, claim, expected_command),
                                           name="janitor-banked-reset", daemon=True)
            self.thread.start()
            launched = True
            return "checking"
        except Exception as exc:
            log_fault("bankedResetWorkerStartFailed", exc)
            return "unverified"
        finally:
            if not launched:
                self._slot.release()

    def _work(self, provider, field, claim, expected_command):
        from . import janitor_builtins, turn_queue
        last_check = 0
        still_owned = True

        def cancelled():
            nonlocal last_check, still_owned
            now = time.monotonic()
            if now - last_check >= 1:
                last_check = now
                latest = janitor_builtins.resolve("account-hotseat")
                still_owned = bool(tuple(getattr(config.load(),field,())) == expected_command
                    and latest and latest["agent_id"] == claim["owner"]
                    and latest["generation"] == claim["generation"]
                    and latest["options"]["mode"] == "automatic"
                    and turn_queue.user_stop_generation(claim["owner"]) == claim["stop_generation"])
            return not still_owned
        result = {"status": "unverified", "verified": False, "needs_reconcile": True}
        try:
            result = attempt(field, claim["models"], accounts=claim.get("accounts", ()), provider=provider,
                             stop_event=self.stop_event, cancelled=cancelled,
                             reconcile_only=claim.get("reconcile_only", False))
        except Exception as exc:
            log_fault("bankedResetWorkerFailed", exc)
        finally:
            try:
                with settings_store.transaction():
                    value = json.loads(settings_store.get_text(self._key(provider), default="{}"))
                    if value.get("id") == claim["id"]:
                        if result.get("claimed") and not value.get("counted"):
                            # A redemption claimed: counted once, in the same write as its result. A
                            # count that cannot be read is left as it is, so the step stays closed.
                            now = db.now_ms()
                            spent = self.spent_today(provider, now)
                            if spent is not None:
                                settings_store.set_text(self._spent_key(provider), json.dumps(spent + [now]))
                            value["counted"] = True
                        value.update(result=result, completed=db.now_ms(),
                                     needs_reconcile=bool(result.get("needs_reconcile")),
                                     attention_required=bool(result.get("needs_reconcile") and claim.get("reconcile_attempts",0)>=MAX_RECONCILE_ATTEMPTS))
                        settings_store.set_text(self._key(provider), json.dumps(value))
            except Exception as exc:
                log_fault("bankedResetResultStoreFailed", exc)
            finally:
                try:
                    db.close_local()
                except Exception as exc:
                    log_fault("bankedResetConnectionCloseFailed", exc)
                finally:
                    self._slot.release()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
