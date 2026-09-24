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

from .activity import state_activity_event
from .log import log, log_exception
from .protocol import AgentState, SSEType
from .timing import SERVER_TIMING


def _emit(*a, **kw):
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


_NOTIFY_STOP = object()


class StateLogWatcher:
    INTERVAL_SEC = SERVER_TIMING.state_watcher_poll_sec

    def __init__(self, stream):
        self.stream = stream
        self._last_id = 0
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
            self.stream.broadcast(
                user_notifications.event_payload(notification))
            apns.on_user_notification(notification)

    def _loop(self) -> None:
        from . import db
        # Start watching from the current tail — don't replay history.
        try:
            row = db.conn().execute(
                "SELECT COALESCE(MAX(state_id), 0) AS m FROM state_log"
            ).fetchone()
            self._last_id = int(row["m"]) if row else 0
        except Exception as e:
            log_exception("stateWatcherInitFail", e)
        while not self._stop.wait(self.INTERVAL_SEC):
            try:
                self._poll_once()
            except Exception as e:  # noqa: BLE001 — never let the thread die
                log_exception("stateWatcherTickFail", e)

    def _poll_once(self) -> None:
        from . import db
        cur = db.conn().execute(
            """SELECT s.state_id, s.agent_id, s.ts, s.kind, s.detail,
                      a.persona, a.session, a.custom_status
                 FROM state_log s
                 JOIN agents a ON a.agent_id = s.agent_id
                WHERE s.state_id > ?
                ORDER BY s.state_id""",
            (self._last_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return
        for r in rows:
            detail = None
            if r["detail"]:
                try:
                    detail = json.loads(r["detail"])
                except json.JSONDecodeError:
                    detail = None
            state_event = {
                "type":         SSEType.AGENT_STATE,
                "agent_id":     r["agent_id"],
                "session": r["session"],
                "persona":      r["persona"],
                "kind":         r["kind"],
                "ts":           int(r["ts"]),
                "detail":       detail,
                "status_text":  r["custom_status"] or "",
            }
            self.stream.broadcast(state_event)
            self.stream.broadcast(state_activity_event(
                agent_id=r["agent_id"],
                session=r["session"],
                persona=r["persona"],
                kind=r["kind"],
                ts=int(r["ts"]),
                detail=detail,
            ))
            if r["kind"] == AgentState.DONE:
                self._ensure_notify_worker()
                self._notify_queue.put({
                    "agent_id": r["agent_id"],
                    "session": r["session"],
                    "persona": r["persona"],
                    "done_ts": int(r["ts"]),
                    "detail": detail,
                })
            self._last_id = int(r["state_id"])
        _emit("state_watcher", "broadcast",
              detail={"count": len(rows), "last_id": self._last_id})
