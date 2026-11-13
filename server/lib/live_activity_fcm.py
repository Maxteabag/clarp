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
  nothing, and revoking the device removes its registration.
- Latest wins, all the way: the pusher hands over a state without waiting
  (``offer``); one worker sends, and while it waits on Google newer states only
  replace the one waiting. Each device holds at most one state.
- Every device has its own budget, counted apart from the iPhone's and kept in
  the database with the day count, so a restart neither refills nor forgets it:
  a burst of 10 refilled evenly to 300 a UTC day, and at most 300 updates a UTC
  day.
- An update is HIGH for a structural change or a rising ``needs_you`` (it changes
  what the notification shows), NORMAL otherwise.
- The end: when the fleet goes quiet the quiet state (``working`` 0,
  ``needs_you`` 0) goes out as ``event: "end"``, only to a device that was sent an
  update since its last end (one end per shown notification), at NORMAL
  priority (it shows the user nothing; Google demotes high-priority messages
  that do not), without taking from the burst and even with the day's updates
  spent. So a day stays within about twice the budget.
- A send that fails (Google 5xx, a network error, a 429 after the transport's own
  retries) keeps that state owed and retries it with a back-off from 60 s
  doubling to 30 min, until it is delivered or a newer state replaces it.
- After a Host start, a device that was last sent an update (it may still show
  agents at work) is owed one message with the present state, sent
  ``START_GRACE_SEC`` after the start unless a state was offered by then.
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
START_GRACE_SEC = 30.0
_PREFIX = "live_activity_fcm.device."
_BUDGET_KEY = "live_activity_fcm.budget.v1"


class RegistrationError(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status, self.error = status, error


# --- registry ------------------------------------------------------------------------
#
# One settings row per device: {"principal", "platform", "kind", "registered_at", "shown"}. ``shown``
# is whether the last message delivered to it was an update (its notification may show agents at work):
# an end is owed only then, and after a restart that is what the start check reads.

def _read(principal: str) -> dict[str, Any] | None:
    raw = settings_store.get(_PREFIX + principal)
    if raw is None:
        return None
    try:
        row = json.loads(raw)
    except ValueError as exc:
        log_exception("liveActivityFcmRowUnreadable", exc, detail=f"principal={principal}")
        return None
    return row if isinstance(row, dict) else None


def register(principal: str, kind: Any = KIND) -> dict[str, Any]:
    """The device asks for the agents-working summary on its FCM push row. ``principal`` is the
    authenticated paired device; the caller has checked it is full-scope and not revoked."""
    from . import apns, live_activity
    if str(kind or KIND) not in live_activity.KINDS:
        raise RegistrationError(400, "kind must be agents-working")
    if not apns.live_fcm_rows(principal):
        raise RegistrationError(409, "fcm_push_token_required")
    previous = _read(principal) or {}
    row = {"principal": principal, "platform": "fcm", "kind": KIND, "registered_at": int(time.time() * 1000),
           "shown": bool(previous.get("shown"))}
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


def _set_shown(principal: str, shown: bool) -> None:
    row = _read(principal)
    if row is not None and bool(row.get("shown")) != shown:
        row["shown"] = shown
        settings_store.set_text(_PREFIX + principal, json.dumps(row, separators=(",", ":")))


def recipients() -> dict[str, dict[str, Any]]:
    """Each registered device that may receive now: a paired full-scope device, not revoked, that
    still owns at least one live FCM row. ``{principal: {"tokens": [...], "shown": bool}}``."""
    from . import apns
    out = {}
    for row in registrations():
        principal = row["principal"]
        if not apns.owner_can_receive(principal):
            continue
        tokens = apns.live_fcm_rows(principal)
        if tokens:
            out[principal] = {"tokens": tokens, "shown": bool(row.get("shown"))}
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


# --- the durable budget, per device ----------------------------------------------------------
#
# One settings row: {"version": 1, "day": <UTC day>, "counts": {principal: n}, "buckets": {principal:
# [level, at]}}. ``counts`` restart at every UTC day; a bucket refills with wall time and is kept
# across days and restarts.

def _load_budget(now: float) -> dict[str, Any]:
    raw = settings_store.get(_BUDGET_KEY)
    try:
        state = json.loads(raw) if raw else None
    except ValueError as exc:
        log_exception("liveActivityFcmBudgetUnreadable", exc)
        state = None
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("counts"), dict) \
            or not isinstance(state.get("buckets"), dict) or not isinstance(state.get("day"), int):
        state = {"version": 1, "day": int(now // 86400), "counts": {}, "buckets": {}}
    if state["day"] != int(now // 86400):
        state["day"], state["counts"] = int(now // 86400), {}
    return state


def _level(state: dict[str, Any], principal: str, now: float, burst: float, refill: float) -> float:
    bucket = state["buckets"].get(principal)
    if not (isinstance(bucket, list) and len(bucket) == 2 and all(isinstance(v, (int, float)) for v in bucket)):
        return burst
    level, at = bucket
    return max(0.0, min(burst, level + max(0.0, now - at) / refill))


def reserve(principal: str, now: float, *, update: bool, day_limit: int, burst: float,
            refill: float) -> tuple[bool, float]:
    """Count one send for ``principal`` and answer (allowed, wait). An update takes one from the
    device's burst and needs the day's count below ``day_limit``; an end takes neither (it is
    counted). Raises when it cannot be committed: then nothing is sent."""
    from . import db
    with db.busy_timeout_at_most(20):
        with settings_store.transaction():
            state = _load_budget(now)
            level = _level(state, principal, now, burst, refill)
            count = state["counts"].get(principal, 0)
            count = count if isinstance(count, int) and count >= 0 else 0
            if update:
                if level < 1:
                    return False, (1 - level) * refill
                if count >= day_limit:
                    return False, (state["day"] + 1) * 86400 - now
                state["buckets"][principal] = [level - 1, now]
            state["counts"][principal] = count + 1
            settings_store.set_text(_BUDGET_KEY, json.dumps(state, separators=(",", ":"), sort_keys=True),
                                    updated_at=int(time.time() * 1000))
    return True, 0.0


def budget(principal: str, now: float, *, burst: float, refill: float) -> tuple[int, float]:
    """(today's count, the burst left) for one device (tests and diagnostics)."""
    state = _load_budget(now)
    return int(state["counts"].get(principal, 0)), _level(state, principal, now, burst, refill)


def forget_budget(principal: str) -> None:
    """Drop a device's budget row entries (revocation)."""
    from . import db
    with db.busy_timeout_at_most(20):
        with settings_store.transaction():
            raw = settings_store.get(_BUDGET_KEY)
            if not raw:
                return
            state = _load_budget(time.time())
            had_count = state["counts"].pop(principal, None) is not None
            had_bucket = state["buckets"].pop(principal, None) is not None
            if not (had_count or had_bucket):
                return
            settings_store.set_text(_BUDGET_KEY, json.dumps(state, separators=(",", ":"), sort_keys=True))


def forget(principal: str) -> None:
    """A revoked device: its request and its budget go."""
    unregister(principal)
    forget_budget(principal)


# --- the sender ----------------------------------------------------------------------------

class _Device:
    __slots__ = ("pending", "paused_until", "spent_until", "floor", "timer", "timer_due")

    def __init__(self):
        self.pending: tuple[dict, bool] | None = None   # the one state owed to this device, and its priority
        self.paused_until = 0.0                          # a failed send's back-off: nothing goes
        self.spent_until = 0.0                           # the burst or the day is spent: only an end goes
        self.floor = 0.0
        self.timer = None
        self.timer_due = 0.0


class FcmActivitySender:
    """Sends the pusher's states to the registered Android phones (see the module docstring).

    ``offer(state, high)`` is the pusher's second sink (LiveActivityPusher ``send_fcm``): it only
    records the state and wakes the worker. ``inline=True`` (tests) sends on the offering thread."""

    BURST, REFILL_SEC = 10.0, 86_400 / 300          # apns._LiveActivityGate's
    FLOOR_MIN_SEC, FLOOR_MAX_SEC = 60.0, 1800.0

    def __init__(self, *, config: Callable[[], Any] | None = None, transport=None,
                 clock: Callable[[], float] = time.time, schedule=None, day_limit: int | None = None,
                 inline: bool = False):
        from . import apns
        self._config = config
        self._transport = transport
        self._clock = clock
        self._schedule = schedule
        self._day_limit = apns._LA_DAY_LIMIT if day_limit is None else day_limit
        self._inline = inline
        self._lock = threading.Lock()          # state below; never held over the network
        self._send_lock = threading.Lock()     # one sender at a time
        self._latest: tuple[dict, bool] | None = None
        self._offered = False
        self._closed = False
        self._devices: dict[str, _Device] = {}
        self._start_timer = None
        self._work_pending = False
        self._working = False

    # -- the pusher's side ----------------------------------------------------------------

    def offer(self, state: dict[str, Any], high: bool) -> dict[str, int] | None:
        """The newest fleet state. Replaces one not yet taken (its HIGH is kept: the phone has not
        seen that change either). Never raises; never waits on the network unless inline."""
        with self._lock:
            if self._closed:
                return None
            if self._latest is not None:
                high = high or self._latest[1]
            self._latest = (state, high)
            self._offered = True
        return self._kick()

    send = offer

    def after_start(self, present: Callable[[], dict[str, Any]], grace: float = START_GRACE_SEC) -> None:
        """A Host start: a device last sent an update is owed the present state. Unless the pusher
        offers one first, ``present`` is offered after ``grace`` seconds (the live hub has resynced
        by then)."""
        def check():
            with self._lock:
                if self._closed or self._offered:
                    return
            try:
                state = present()
            except Exception as exc:  # noqa: BLE001 - the next fleet change sends it anyway
                log_exception("liveActivityFcmStartStateFail", exc)
                return
            self.offer(state, True)
        self._start_timer = self._timer(grace, check)

    def close(self) -> None:
        """The server stops: no more sends, no timers."""
        with self._lock:
            self._closed = True
            timers = [d.timer for d in self._devices.values() if d.timer is not None] + [self._start_timer]
            for device in self._devices.values():
                device.timer = None
        for timer in timers:
            self._cancel(timer)

    # -- the worker -------------------------------------------------------------------------

    def _timer(self, delay: float, fn: Callable[[], None]):
        schedule = self._schedule
        if schedule is None:
            from .live_pacing import _thread_timer as schedule
        return schedule(max(0.0, min(delay, threading.TIMEOUT_MAX)), fn)

    @staticmethod
    def _cancel(timer) -> None:
        if timer is None:
            return
        try:
            timer.cancel()
        except Exception as exc:  # noqa: BLE001 - a timer that already ran
            log("liveActivityFcmTimerCancel", type(exc).__name__)

    def _kick(self) -> dict[str, int] | None:
        if self._inline:
            return self.flush()
        with self._lock:
            self._work_pending = True
            if self._working:
                return None                      # the running worker takes the newest after its send
            self._working = True
        threading.Thread(target=self._work, name="live-activity-fcm", daemon=True).start()
        return None

    def _work(self) -> None:
        from . import db
        while True:
            with self._lock:
                if not self._work_pending:
                    self._working = False
                    return
                self._work_pending = False
            try:
                self.flush()
            except Exception as exc:  # noqa: BLE001 - the next offer or timer tries again
                log_exception("liveActivityFcmWorkFail", exc)
            finally:
                db.close_local()

    def _cfg(self):
        if self._config is not None:
            return self._config()
        from . import config
        return config.load()

    def flush(self) -> dict[str, int]:
        """Send what may go now; arm a timer for what must wait. Never raises."""
        try:
            with self._send_lock:
                return self._flush_locked()
        except Exception as exc:  # noqa: BLE001 - best effort, never the live stream
            log_exception("liveActivityFcmFail", exc)
            return {"sent": 0, "held": 0, "failed": 0}

    def _arm_locked(self, principal: str, device: _Device, due: float) -> None:
        if self._closed:
            return
        if device.timer is not None and device.timer_due <= due:
            return
        self._cancel(device.timer)
        device.timer_due = due

        def fire():
            with self._lock:
                if self._devices.get(principal) is device:
                    device.timer = None
            self._kick()
        device.timer = self._timer(due - self._clock(), fire)

    def _hold(self, principal: str, device: _Device, item: tuple[dict, bool], *, wait: float,
              back_off: bool = False, spent: bool = False) -> None:
        with self._lock:
            now = self._clock()
            if back_off:
                device.floor = self.FLOOR_MIN_SEC if not device.floor else min(device.floor * 2, self.FLOOR_MAX_SEC)
                wait = max(wait, device.floor)
            if spent:
                device.spent_until = max(device.spent_until, now + wait)
            else:
                device.paused_until = max(device.paused_until, now + wait)
            if device.pending is None:                  # a newer state that came meanwhile wins
                device.pending = item
            self._arm_locked(principal, device, now + wait)

    def _flush_locked(self) -> dict[str, int]:
        from . import apns, fcm
        sent = held = failed = 0
        cfg = self._cfg()
        if not apns.fcm_configured(cfg):
            with self._lock:
                self._latest = None
            return {"sent": 0, "held": 0, "failed": 0}
        targets = recipients()
        with self._lock:
            latest, self._latest = self._latest, None
            for principal in list(self._devices):
                if principal not in targets:
                    self._cancel(self._devices.pop(principal).timer)
            for principal in targets:
                device = self._devices.setdefault(principal, _Device())
                if latest is not None:
                    state, high = latest
                    if device.pending is not None:
                        high = high or device.pending[1]
                    device.pending = (state, high)
            now = self._clock()
            ready = []
            for principal, device in self._devices.items():
                if device.pending is None:
                    continue
                state, _high = device.pending
                if is_quiet(state) and not targets[principal]["shown"]:
                    device.pending = None               # nothing shown since its last end: no end owed
                    continue
                if now < device.paused_until:
                    self._arm_locked(principal, device, device.paused_until)
                    continue
                if not is_quiet(state) and now < device.spent_until:
                    self._arm_locked(principal, device, device.spent_until)
                    continue
                ready.append((principal, device, device.pending))
                device.pending = None
        transport = None
        instance = apns._server_instance_id()
        for principal, device, item in ready:
            state, high = item
            end = is_quiet(state)
            try:
                allowed, wait = reserve(principal, now, update=not end, day_limit=self._day_limit,
                                        burst=self.BURST, refill=self.REFILL_SEC)
            except Exception as exc:  # noqa: BLE001 - no committed count, no send
                log_exception("liveActivityFcmBudgetUnavailable", exc, detail=f"principal={principal}")
                self._hold(principal, device, item, wait=60.0)
                held += 1
                continue
            if not allowed:
                self._hold(principal, device, item, wait=max(wait, 1.0), spent=True)
                held += 1
                continue
            transport = transport or self._transport or fcm.Transport(cfg)
            payload = message(state, server_instance_id=instance)
            priority = "10" if high and not end else "5"
            delivered = retry = False
            for row in targets[principal]["tokens"]:
                token = row["token"]
                try:
                    status, reason, _name = transport.send(token, payload, push_type="liveactivity",
                                                           priority=priority, collapse_id=COLLAPSE_KEY)
                except Exception as exc:  # noqa: BLE001 - a network error: owed, retried
                    log_exception("liveActivityFcmSendFail", exc, detail=f"principal={principal} token={token[:12]}…")
                    transport.reset()
                    retry = True
                    continue
                if status == 200:
                    delivered = True
                elif status == 410:
                    apns.disable_token(token, reason or "Unregistered")
                elif reason == "PayloadTooLarge":
                    log("liveActivityFcmTooLarge", f"principal={principal}")
                else:
                    log("liveActivityFcmRejected", f"status={status} reason={reason} principal={principal}")
                    retry = True
            if delivered:
                sent += 1
                with self._lock:
                    device.floor = 0.0
                _set_shown(principal, not end)
            elif retry:
                self._hold(principal, device, item, wait=0.0, back_off=True)
                held += 1
            else:
                failed += 1
        if ready:
            log("liveActivityFcm", f"sent={sent} held={held} failed={failed}")
        return {"sent": sent, "held": held, "failed": failed}
