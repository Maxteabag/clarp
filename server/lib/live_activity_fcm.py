"""The agents-working summary for an Android phone, by FCM (feature live_activity_fcm;
docs/push-android.md "Agents working").

The iPhone's Live Activity gets the fleet's content state by ActivityKit push
(live_activity.LiveActivityPusher → apns.send_live_activity). An Android phone
asks for the same content with ``POST /devices/live-activity {"platform": "fcm",
"kind": "agents-working"}`` and gets it as a data-only FCM message on its own FCM
push row, at the moments the same pusher decides (15 s routine, 10 s structural
floor, a rising ``needs_you`` at once).

- The registration records the paired full-scope device that made it, one per
  device. It sends to that device's live FCM push rows (POST /devices), so
  rotation, ``DELETE /devices``, a take-over by another pairing and revocation
  follow the push row's own rules (PR 275); a revoked or limited device gets
  nothing.
- Every device has its own budget, counted apart from the iPhone's: the APNs
  path's rules (a burst of 10 refilled to 300 a UTC day, latest state wins while
  held; a 429 backs the device off from 60 s doubling to 30 min) with its own
  durable day count per device.
- HIGH priority for a structural change (an agent starting or ending, a new
  lead) or a rising ``needs_you``; NORMAL otherwise. ``collapse_key``
  ``agents-working``: FCM keeps only the latest for an offline phone.
- When the fleet goes quiet the quiet state (``working`` 0, ``needs_you`` 0)
  goes out once with ``event: "end"``: a suspended Android app cannot end its
  notification itself, so this push is never held by the budget.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from . import settings_store
from .log import log, log_exception

KIND = "agents-working"
COLLAPSE_KEY = "agents-working"
_PREFIX = "live_activity_fcm.device."
_DAY_KEY = "live_activity_fcm.day_budget.v1"


class RegistrationError(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status, self.error = status, error


# --- registry ------------------------------------------------------------------------

def register(principal: str, kind: Any = KIND) -> dict[str, Any]:
    """The device asks for the agents-working summary on its FCM push row. ``principal`` is the
    authenticated paired device; the caller has checked it is full-scope and not revoked."""
    from . import apns, live_activity
    if str(kind or KIND) not in live_activity.KINDS:
        raise RegistrationError(400, "kind must be agents-working")
    if not apns.live_fcm_rows(principal):
        raise RegistrationError(409, "fcm_push_token_required")
    row = {"principal": principal, "platform": "fcm", "kind": KIND, "registered_at": int(time.time() * 1000)}
    settings_store.set_text(_PREFIX + principal, json.dumps(row, separators=(",", ":")))
    log("liveActivityFcmRegistered", f"principal={principal}")
    return row


def unregister(principal: str) -> int:
    if settings_store.get(_PREFIX + principal) is None:
        return 0
    settings_store.delete(_PREFIX + principal)
    log("liveActivityFcmUnregistered", f"principal={principal}")
    return 1


def registrations() -> list[dict[str, Any]]:
    rows = []
    for raw in settings_store.values_with_prefix(_PREFIX):
        try:
            row = json.loads(raw)
        except ValueError as exc:
            log_exception("liveActivityFcmRowUnreadable", exc)
            continue
        if isinstance(row, dict) and str(row.get("principal") or "").startswith("device_"):
            rows.append(row)
    return rows


def recipients() -> dict[str, list[dict[str, Any]]]:
    """Each registered device that may receive now, with its live FCM push rows: a paired full-scope
    device, not revoked, that still owns at least one live FCM row."""
    from . import apns
    out = {}
    for row in registrations():
        principal = row["principal"]
        if not apns.owner_can_receive(principal):
            continue
        tokens = apns.live_fcm_rows(principal)
        if tokens:
            out[principal] = tokens
    return out


# --- the message -------------------------------------------------------------------------

def is_quiet(state: dict[str, Any]) -> bool:
    return not state.get("working") and not state.get("needs_you")


def message(state: dict[str, Any], *, server_instance_id: str = "") -> dict[str, Any]:
    """The APNs-shaped payload fcm.data_payload turns into the data map: ``kind``, ``event``
    (``update``, or ``end`` for the quiet state) and ``state``, the APNs content state, which
    becomes one JSON string."""
    payload = {"kind": KIND, "event": "end" if is_quiet(state) else "update", "state": state}
    if server_instance_id:
        payload["server_instance_id"] = server_instance_id
    return payload


# --- the durable day count, per device ------------------------------------------------

def _day_reserve(principal: str, day_limit: int, *, force: bool) -> tuple[bool, float]:
    """Count one send for ``principal`` in today's UTC budget. Over the limit nothing is counted
    and the wait until the next UTC day comes back; ``force`` (the quiet push) is always counted
    and allowed. Raises when the count cannot be committed: then nothing is sent."""
    from . import db
    with db.busy_timeout_at_most(20):
        with settings_store.transaction():
            now = time.time()
            day = int(now // 86400)
            raw = settings_store.get(_DAY_KEY)
            state = json.loads(raw) if raw else {}
            if not isinstance(state, dict) or state.get("version") != 1 or state.get("day") != day \
                    or not isinstance(state.get("counts"), dict):
                state = {"version": 1, "day": day, "counts": {}}
            count = state["counts"].get(principal, 0)
            if not isinstance(count, int) or count < 0:
                count = 0
            if count >= day_limit and not force:
                return False, (day + 1) * 86400 - now
            state["counts"][principal] = count + 1
            settings_store.set_text(_DAY_KEY, json.dumps(state, separators=(",", ":"), sort_keys=True),
                                    updated_at=int(now * 1000))
    return True, 0.0


def day_counts() -> dict[str, int]:
    """Today's sends per device (for tests and diagnostics)."""
    raw = settings_store.get(_DAY_KEY)
    state = json.loads(raw) if raw else {}
    if not isinstance(state, dict) or state.get("day") != int(time.time() // 86400):
        return {}
    return dict(state.get("counts") or {})


# --- the sender ----------------------------------------------------------------------------

class _Device:
    __slots__ = ("level", "paused_until", "spent_until", "floor", "pending", "timer")

    def __init__(self, burst: float, now: float):
        self.level = (burst, now)
        self.paused_until = 0.0      # a 429 back-off: nothing goes
        self.spent_until = 0.0       # the day's budget is spent: only the quiet state goes
        self.floor = 0.0
        self.pending: tuple[dict, bool] | None = None
        self.timer = None


class FcmActivitySender:
    """Sends the pusher's states to the registered Android phones, within each device's budget.

    ``send(state, high)`` is the pusher's second sink (live_activity.LiveActivityPusher
    ``send_fcm``); the composition root runs it off the hub's thread. One state per device is held
    at most (the latest wins) and goes out on a timer when that device's budget allows."""

    BURST, REFILL_SEC = 10.0, 86_400 / 300          # apns._LiveActivityGate's
    FLOOR_MIN_SEC, FLOOR_MAX_SEC = 60.0, 1800.0

    def __init__(self, *, config: Callable[[], Any] | None = None, transport=None,
                 clock: Callable[[], float] = time.monotonic, schedule=None, day_limit: int | None = None):
        from . import apns
        self._config = config
        self._transport = transport
        self._clock = clock
        self._schedule = schedule
        self._day_limit = apns._LA_DAY_LIMIT if day_limit is None else day_limit
        self._lock = threading.Lock()          # the per-device state; never held over the network
        self._send_lock = threading.Lock()     # one ordered sender: an older state never lands after a newer
        self._devices: dict[str, _Device] = {}

    def _cfg(self):
        if self._config is not None:
            return self._config()
        from . import config
        return config.load()

    def send(self, state: dict[str, Any], high: bool) -> dict[str, int]:
        """Offer one content state to every registered device and send what may go now. Never raises."""
        try:
            from . import apns
            if not apns.fcm_configured(self._cfg()):
                return {"sent": 0, "held": 0, "failed": 0}
            targets = recipients()
            with self._lock:
                for principal in list(self._devices):
                    if principal not in targets:
                        self._drop_locked(principal)
                for principal in targets:
                    device = self._devices.setdefault(principal, _Device(self.BURST, self._clock()))
                    device.pending = (state, high)
            return self._flush()
        except Exception as exc:  # noqa: BLE001 - best effort, never the live stream
            log_exception("liveActivityFcmFail", exc)
            return {"sent": 0, "held": 0, "failed": 0}

    def _drop_locked(self, principal: str) -> None:
        device = self._devices.pop(principal)
        if device.timer is not None:
            try:
                device.timer.cancel()
            except Exception as exc:  # noqa: BLE001 - a timer that already ran
                log("liveActivityFcmTimerCancel", f"{type(exc).__name__}")

    def _flush(self) -> dict[str, int]:
        with self._send_lock:
            return self._flush_locked()

    def _take(self) -> list[tuple[str, dict, bool]]:
        """What may go now: for each device with a held state, its bucket allowing (the quiet state
        always may) and no back-off running. The rest wait on one timer per device."""
        ready = []
        with self._lock:
            now = self._clock()
            for principal, device in self._devices.items():
                if device.pending is None:
                    continue
                state, high = device.pending
                level = min(self.BURST, device.level[0] + (now - device.level[1]) / self.REFILL_SEC)
                quiet = is_quiet(state)
                if now < device.paused_until or (not quiet and (level < 1 or now < device.spent_until)):
                    self._arm_locked(principal, device, now, level)
                    continue
                device.level = (max(0.0, level - 1), now)
                device.pending = None
                ready.append((principal, state, high))
        return ready

    def _arm_locked(self, principal: str, device: _Device, now: float, level: float) -> None:
        if device.timer is not None:
            return
        wait = max(device.paused_until - now, device.spent_until - now, (1 - level) * self.REFILL_SEC, 0.0)

        def fire():
            with self._lock:
                if self._devices.get(principal) is device:
                    device.timer = None
            self._flush()
        schedule = self._schedule
        if schedule is None:
            from .live_pacing import _thread_timer as schedule
        device.timer = schedule(min(wait, threading.TIMEOUT_MAX), fire)

    def _hold(self, principal: str, state: dict, high: bool, *, pause: float = 0.0, back_off: bool = False,
              spent: float = 0.0) -> None:
        """Keep a state that did not go (a newer one wins) and wake up when it may."""
        with self._lock:
            device = self._devices.get(principal)
            if device is None:
                return
            now = self._clock()
            if back_off:
                device.floor = self.FLOOR_MIN_SEC if not device.floor else min(device.floor * 2, self.FLOOR_MAX_SEC)
                pause = max(pause, device.floor)
            if pause:
                device.paused_until = max(device.paused_until, now + pause)
            if spent:
                device.spent_until = max(device.spent_until, now + spent)
            if device.pending is None:
                device.pending = (state, high)
            level = min(self.BURST, device.level[0] + (now - device.level[1]) / self.REFILL_SEC)
            self._arm_locked(principal, device, now, level)

    def _flush_locked(self) -> dict[str, int]:
        from . import apns, fcm
        sent = held = failed = 0
        ready = self._take()
        if not ready:
            return {"sent": 0, "held": 0, "failed": 0}
        cfg = self._cfg()
        transport = self._transport or fcm.Transport(cfg)
        instance = apns._server_instance_id()
        for principal, state, high in ready:
            quiet = is_quiet(state)
            try:
                allowed, wait = _day_reserve(principal, self._day_limit, force=quiet)
            except Exception as exc:  # noqa: BLE001 - no committed count, no send
                log_exception("liveActivityFcmBudgetUnavailable", exc, detail=f"principal={principal}")
                self._hold(principal, state, high, pause=60.0)
                held += 1
                continue
            if not allowed:
                log("liveActivityFcmDayLimit", f"principal={principal} wait={wait:.0f}s")
                self._hold(principal, state, high, spent=max(wait, 1.0))
                held += 1
                continue
            payload = message(state, server_instance_id=instance)
            priority = "10" if high else "5"
            for row in apns.live_fcm_rows(principal):
                token = row["token"]
                try:
                    status, reason, _name = transport.send(token, payload, push_type="liveactivity",
                                                           priority=priority, collapse_id=COLLAPSE_KEY)
                except Exception as exc:  # noqa: BLE001
                    log_exception("liveActivityFcmSendFail", exc, detail=f"principal={principal} token={token[:12]}…")
                    transport.reset()
                    failed += 1
                    continue
                if status == 200:
                    sent += 1
                    with self._lock:
                        device = self._devices.get(principal)
                        if device is not None:
                            device.floor = 0.0
                elif status == 410:
                    apns.disable_token(token, reason or "Unregistered")
                    failed += 1
                elif status == 429:
                    log("liveActivityFcmRateLimited", f"principal={principal}")
                    self._hold(principal, state, high, back_off=True)
                    held += 1
                else:
                    log("liveActivityFcmRejected", f"status={status} reason={reason} principal={principal}")
                    failed += 1
        log("liveActivityFcm", f"sent={sent} held={held} failed={failed}")
        return {"sent": sent, "held": held, "failed": failed}
