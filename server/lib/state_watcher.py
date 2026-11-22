"""Tail state_log + broadcast each new row over SSE.

Same idea as `AudioStream._watcher`: poll the source-of-truth at a short
interval, send events when new rows appear. Polling sqlite locally is
cheap (a single indexed SELECT against the highest seen state_id).

The hooks write state_log rows from their own subprocesses — they can't
reach into the server's SSE bus directly. The server-side watcher closes
that gap: hook commits a row, watcher picks it up within `INTERVAL_SEC`,
SSE clients see `agent-state`.

Server-driven changes (create / delete / focus) skip this and broadcast
directly from the handler for zero-latency UI updates.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable

from .activity import state_activity_event
from . import coordination, events
from .log import log, log_exception
from .protocol import AgentState
from .timing import SERVER_TIMING


def _emit(*a, **kw):
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


_NOTIFY_STOP = object()


def _public_detail(kind: str, raw: str | None):
    detail = None
    if raw:
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = None
    if (kind == AgentState.IDLE and isinstance(detail, dict)
            and detail.get("reason") == "update_handoff"):
        # Queue-only proof remains stored, never part of public state.
        detail = dict(detail)
        detail.pop("dispatch_not_started", None)
        detail.pop("dispatch_not_started_through_turn_id", None)
    elif (kind == AgentState.INTERRUPTED and isinstance(detail, dict)
          and detail.get("dispatch_not_started") is True):
        detail = dict(detail)
        detail.pop("dispatch_not_started_turn_id", None)
    return detail


class StateLogWatcher:
    WORKING_TTL_S = 5.0

    def _veil(self, agent_id: str, working: dict) -> str:
        """'Working with <Name>' for a coordination turn the agent's chat hides, else ''."""
        if working.get("audience") != coordination.AGENTS:
            return ""
        now = time.monotonic()
        if now - self._hiding_at >= self.WORKING_TTL_S:
            try:
                self._hiding, self._hiding_at = coordination.hiding_agents(), now
            except Exception as exc:  # noqa: BLE001 - never break the state stream
                log_exception("stateHidingAgentsFail", exc)
                # Unknown: veil, the failure mode of hiding is shown words, never leaked ones.
                return coordination.veiled(working)
        return coordination.veiled(working) if agent_id in self._hiding else ""

    def _working_with(self, agent_id: str, kind: str, detail) -> dict:
        """A working state's audience, as the dispatch's 'thinking' state carries it, on every
        state of the running turn: a client that connects mid-turn learns from the next state
        that the agent is working with another agent (feature coordination_working_state)."""
        if kind not in AgentState.busy_states() or not isinstance(detail, dict):
            return {}
        trace = str(detail.get("trace_id") or "")
        if not trace:
            return {}
        known = self._working.get(agent_id)
        if known and known[0] == trace and time.monotonic() - known[1] < self.WORKING_TTL_S:
            return dict(known[2])
        try:
            fields = coordination.working_with(agent_id, trace)
        except Exception as exc:  # noqa: BLE001 - never break the state stream
            log_exception("stateWorkingWithFail", exc, detail=agent_id)
            return {}
        self._working[agent_id] = (trace, time.monotonic(), fields)
        return dict(fields)

    INTERVAL_SEC = SERVER_TIMING.state_watcher_poll_sec

    def __init__(self, stream, *, on_done: Callable[[str], None] | None = None):
        self.stream = stream
        # Told the agent of every DONE row as the poll reads it: the transcript streamer
        # imports the ended turn's transcript then, not after its import window.
        self._on_done = on_done
        self._last_id = 0
        self._last_status: dict[str, str] = {}
        # agent_id -> (trace_id, read at, its working_with fields): a turn's audience, read again
        # after WORKING_TTL_S so a turn the user steps into reads 'user' soon after.
        self._working: dict[str, tuple[str, float, dict]] = {}
        # The chats that hide coordination (lib.coordination.hiding_agents), read again after
        # WORKING_TTL_S: a setting change reaches the stream within seconds.
        self._hiding: set[str] = set()
        self._hiding_at = float("-inf")
        # The database's change stamp at the last whole poll: what a poll broadcasts is
        # new state_log rows and changed agents rows, so while it stands (no commit by
        # anyone) a poll has nothing to say. Four polls a second read the agents table
        # every time otherwise.
        self._seen_stamp: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Completed-turn notification policy settles for up to
        # user_notifications.SETTLE_TIMEOUT_S waiting for the final assistant
        # row. That wait must not stall the agent-state stream, so DONE rows
        # are handed to one FIFO worker: it keeps the per-row order the
        # inline call had, and the poll loop returns immediately.
        self._notify_queue: queue.Queue = queue.Queue()
        self._notify_thread: threading.Thread | None = None
        self._notify_lock = threading.Lock()

    def start(self) -> None:
        self._ensure_notify_worker()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        with self._notify_lock:
            worker = self._notify_thread
            self._notify_thread = None
        if worker and worker.is_alive():
            self._notify_queue.put(_NOTIFY_STOP)
            worker.join(timeout=timeout)

    def wait_for_notifications(self, timeout: float = 5.0) -> bool:
        """Block until every queued completed-turn classification has run.
        Returns False on timeout. Intended for tests and shutdown."""
        deadline = time.monotonic() + timeout
        while self._notify_queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _ensure_notify_worker(self) -> None:
        with self._notify_lock:
            if self._notify_thread and self._notify_thread.is_alive():
                return
            self._notify_thread = threading.Thread(
                target=self._notify_loop, daemon=True,
                name="state-watcher-notify")
            self._notify_thread.start()

    def _notify_loop(self) -> None:
        while True:
            item = self._notify_queue.get()
            try:
                if item is _NOTIFY_STOP:
                    return
                self._classify_completed_turn(**item)
            except Exception as e:  # noqa: BLE001 — never let a push break the worker
                log_exception("userNotificationClassifyFail", e)
            finally:
                self._notify_queue.task_done()

    def _classify_completed_turn(self, *, agent_id, session, persona, done_ts,
                                 detail) -> None:
        from . import apns, user_notifications
        detail_map = detail if isinstance(detail, dict) else {}
        notification = user_notifications.classify_completed_turn(
            agent_id=agent_id,
            session=session,
            persona=persona,
            done_ts=done_ts,
            backend_session_id=str(
                detail_map.get("backend_session_id") or ""),
            trace_id=str(detail_map.get("trace_id") or ""),
        )
        if notification.get("notify"):
            events.broadcast(self.stream, events.as_event(
                user_notifications.event_payload(notification)))
            apns.on_user_notification(notification)

    def prime(self) -> None:
        """Record the current state_log tail and custom_status baseline."""
        from . import db
        c = db.conn()
        row = c.execute(
            "SELECT COALESCE(MAX(state_id), 0) AS m FROM state_log"
        ).fetchone()
        self._last_id = int(row["m"]) if row else 0
        self._last_status = {
            str(r["agent_id"]): str(r["custom_status"] or "")
            for r in c.execute(
                "SELECT agent_id, custom_status FROM agents WHERE deleted_at IS NULL"
            ).fetchall()
        }

    def _loop(self) -> None:
        # Start watching from the current tail — don't replay history.
        try:
            self.prime()
        except Exception as e:
            log_exception("stateWatcherInitFail", e)
        while not self._stop.wait(self.INTERVAL_SEC):
            try:
                self._poll_once()
            except Exception as e:  # noqa: BLE001 — never let the thread die
                log_exception("stateWatcherTickFail", e)

    def _poll_once(self) -> None:
        from . import db
        stamp = db.change_stamp()
        if stamp == self._seen_stamp and stamp[0] >= 0:
            return
        self._poll_rows()
        self._seen_stamp = stamp

    def _poll_rows(self) -> None:
        from . import db
        c = db.conn()
        cur = c.execute(
            """SELECT s.state_id, s.agent_id, s.ts, s.kind, s.detail,
                      a.persona, a.session, a.custom_status
                 FROM state_log s
                 JOIN agents a ON a.agent_id = s.agent_id
                WHERE s.state_id > ?
                ORDER BY s.state_id""",
            (self._last_id,),
        )
        rows = cur.fetchall()
        for r in rows:
            detail = _public_detail(r["kind"], r["detail"])
            working = self._working_with(r["agent_id"], r["kind"], detail)
            veil = self._veil(r["agent_id"], working)
            events.broadcast(self.stream, events.agent_state(
                agent_id=r["agent_id"],
                session=r["session"],
                persona=r["persona"],
                kind=r["kind"],
                ts=int(r["ts"]),
                detail=coordination.veil_detail(detail, veil),
                status_text=r["custom_status"] or "",
                **working,
            ))
            # Clients draw a row's activity from this event: a coordination turn its chat
            # hides shows who the agent works with, never what it does (as the snapshot does).
            events.broadcast(self.stream, events.as_event(coordination.veil_activity(state_activity_event(
                agent_id=r["agent_id"],
                session=r["session"],
                persona=r["persona"],
                kind=r["kind"],
                ts=int(r["ts"]),
                detail=detail,
            ), veil)))
            self._last_status[str(r["agent_id"])] = str(r["custom_status"] or "")
            if r["kind"] == AgentState.DONE:
                if self._on_done is not None:
                    try:
                        self._on_done(str(r["agent_id"]))
                    except Exception as exc:  # noqa: BLE001 - never break the state stream
                        log_exception("stateDoneCallbackFail", exc, detail=str(r["agent_id"]))
                self._ensure_notify_worker()
                self._notify_queue.put({
                    "agent_id": r["agent_id"],
                    "session": r["session"],
                    "persona": r["persona"],
                    "done_ts": int(r["ts"]),
                    "detail": detail,
                })
            self._last_id = int(r["state_id"])
        if rows:
            _emit("state_watcher", "broadcast",
                  detail={"count": len(rows), "last_id": self._last_id})
        self._poll_status_changes(c)

    def _poll_status_changes(self, c) -> None:
        """Broadcast custom_status updates that occurred without a state_log row."""
        live = c.execute(
            """SELECT agent_id, persona, session, custom_status
                 FROM agents
                WHERE deleted_at IS NULL"""
        ).fetchall()
        live_ids = {str(a["agent_id"]) for a in live}
        for stale_id in list(self._last_status):
            if stale_id not in live_ids:
                del self._last_status[stale_id]
        for a in live:
            agent_id = str(a["agent_id"])
            status_text = str(a["custom_status"] or "")
            if self._last_status.get(agent_id, "") == status_text:
                continue
            if c.execute(
                "SELECT 1 FROM state_log WHERE agent_id = ? AND state_id > ? LIMIT 1",
                (agent_id, self._last_id),
            ).fetchone() is not None:
                continue
            self._last_status[agent_id] = status_text
            latest = c.execute(
                """SELECT kind, ts, detail FROM state_log
                    WHERE agent_id = ?
                    ORDER BY ts DESC, state_id DESC LIMIT 1""",
                (agent_id,),
            ).fetchone()
            if latest is None:
                continue
            latest_detail = _public_detail(latest["kind"], latest["detail"])
            working = self._working_with(agent_id, latest["kind"], latest_detail)
            events.broadcast(self.stream, events.agent_state(
                agent_id=agent_id,
                session=a["session"],
                persona=a["persona"],
                kind=latest["kind"],
                ts=int(latest["ts"]),
                detail=coordination.veil_detail(latest_detail, self._veil(agent_id, working)),
                status_text=status_text,
                **working,
            ))
