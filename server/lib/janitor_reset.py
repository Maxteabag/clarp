"""Opt-in reset transport and a bounded, persisted Host admission lane."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid

from . import config, db, settings_store
from .log import log_fault

TIMEOUT_SECONDS = 150
CHECK_INTERVAL_MS = 10 * 60_000


def _kill_group(process):
    """Only the session this call created; include descendants after parent exit."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def attempt(command_field: str, models, *, stop_event=None, cancelled=None) -> dict:
    command = getattr(config.load(), command_field, ()) if command_field else ()
    if not command:
        return {"status": "disabled", "verified": False}
    process = None
    try:
        # The supported broker is POSIX-only; do not silently leak a Windows tree.
        if os.name != "posix":
            raise OSError("reset process groups unavailable")
        if (stop_event and stop_event.is_set()) or (cancelled and cancelled()):
            return {"status": "cancelled", "verified": False}
        process = subprocess.Popen(list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        payload = json.dumps({"models": models})
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
                    and receipt.get("status") in {"redeemed", "reconciled"}
                    and isinstance(receipt.get("account"), str)
                    and re.fullmatch(r"ai(?:[1-9]|1[0-5])", receipt["account"]) is not None)
        status = receipt.get("status")
        allowed = {"disabled", "not_applicable", "no_owned_reset_accounts", "busy", "cooldown",
                   "unverified", "no_eligible_grant", "eligible", "redeemed", "reconciled"}
        if status not in allowed or process.returncode or (status in {"redeemed", "reconciled"} and not verified):
            status = "unverified"
        return {"status": status, "verified": verified,
                "account": receipt.get("account") if verified else None}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        log_fault("claudeBankedResetCommandFailed", exc)
        return {"status": "unverified", "verified": False}
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

    def peek(self, provider, owner):
        try:
            value = json.loads(settings_store.get_text(self._key(provider), default="{}"))
            if (value.get("owner") == owner["agent_id"] and value.get("result")
                    and not value.get("handled")):
                return value
        except (ValueError, TypeError, AttributeError) as exc:
            log_fault("bankedResetStateUnreadable", exc)
        return None

    def ack(self, provider, receipt):
        with settings_store.transaction():
            value = json.loads(settings_store.get_text(self._key(provider), default="{}"))
            if value.get("id") == receipt["id"]:
                value["handled"] = True
                settings_store.set_text(self._key(provider), json.dumps(value))

    def offer(self, provider, field, models, owner):
        if not getattr(config.load(), field, ()):
            return "disabled"
        if owner["options"]["mode"] != "automatic" or self.stop_event.is_set():
            return "disabled"
        if not self._slot.acquire(blocking=False):
            return "busy"
        launched = False
        try:
            from . import turn_queue
            claim = {"id": uuid.uuid4().hex, "owner": owner["agent_id"],
                     "generation": owner["generation"], "models": list(models),
                     "stop_generation": turn_queue.user_stop_generation(owner["agent_id"])}
            with settings_store.transaction():
                now = db.now_ms()
                prior = json.loads(settings_store.get_text(self._key(provider), default="{}"))
                if prior.get("next_at", 0) > now:
                    return "cooldown"
                claim.update(started=now, next_at=now + CHECK_INTERVAL_MS, handled=False)
                settings_store.set_text(self._key(provider), json.dumps(claim))
            self.thread = threading.Thread(target=self._work, args=(provider, field, claim),
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

    def _work(self, provider, field, claim):
        from . import janitor_builtins, turn_queue
        last_check = 0
        still_owned = True

        def cancelled():
            nonlocal last_check, still_owned
            now = time.monotonic()
            if now - last_check >= 1:
                last_check = now
                latest = janitor_builtins.resolve("account-hotseat")
                still_owned = bool(latest and latest["agent_id"] == claim["owner"]
                    and latest["generation"] == claim["generation"]
                    and latest["options"]["mode"] == "automatic"
                    and turn_queue.user_stop_generation(claim["owner"]) == claim["stop_generation"])
            return not still_owned
        result = {"status": "unverified", "verified": False}
        try:
            result = attempt(field, claim["models"], stop_event=self.stop_event, cancelled=cancelled)
        except Exception as exc:
            log_fault("bankedResetWorkerFailed", exc)
        finally:
            try:
                with settings_store.transaction():
                    value = json.loads(settings_store.get_text(self._key(provider), default="{}"))
                    if value.get("id") == claim["id"]:
                        value.update(result=result, completed=db.now_ms())
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
