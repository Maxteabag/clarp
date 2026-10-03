"""ActivityKit Live Activity pushes for the fleet (docs/live-items.md §8).

A phone that shows the "agents working" Live Activity registers its
activity push token (``POST /devices/live-activity``). This module keeps the
tokens, folds the ``status`` ops of the live stream into one aggregate
content state (working, needs_you, the lead agent with its tool and start
time, up to five rows), and pushes it with ``apns-push-type: liveactivity``.

Apple budgets these pushes, so routine churn (a tool after a tool) goes out
at most every ``min_interval`` per Host, at priority 5; a change that needs
the owner, a turn starting or ending, or a new lead goes out at once (needs
the owner: priority 10).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from . import settings_store

_PREFIX = "live_activity."
KINDS = frozenset({"agents-working", "agents"})
WORKING = frozenset({"thinking", "responding", "tool", "compacting", "limited"})
NEEDS_YOU = frozenset({"waiting"})
MAX_ROWS = 5
STALE_AFTER_SEC = 15 * 60


# --- registry -----------------------------------------------------------------

def register(*, token: str, activity_id: str = "", kind: str = "agents-working",
             environment: str = "") -> dict[str, Any]:
    token = str(token or "").strip().lower()
    if not token or any(c not in "0123456789abcdef" for c in token) or len(token) > 400:
        raise ValueError("token must be a hex ActivityKit push token")
    kind = str(kind or "agents-working")
    if kind not in KINDS:
        raise ValueError("kind must be agents-working")
    if environment and environment not in {"sandbox", "production", "development"}:
        raise ValueError("environment must be sandbox or production")
    activity_id = str(activity_id or "").strip()[:128] or token[:32]
    key = _PREFIX + activity_id
    previous = _load(settings_store.get(key))
    row = {"token": token, "activity_id": activity_id, "kind": "agents-working",
           "environment": environment or (previous or {}).get("environment") or "",
           "registered_at": int(time.time() * 1000)}
    settings_store.set_text(key, json.dumps(row))
    return row


def unregister(*, activity_id: str = "", token: str = "") -> int:
    removed = 0
    for row in tokens():
        if (activity_id and row["activity_id"] == activity_id) or \
                (token and row["token"] == str(token).strip().lower()):
            settings_store.delete(_PREFIX + row["activity_id"])
            removed += 1
    return removed


def tokens() -> list[dict[str, Any]]:
    rows = [_load(value) for value in settings_store.values_with_prefix(_PREFIX)]
    return [row for row in rows if row]


def _load(raw: str | None) -> dict[str, Any] | None:
    try:
        row = json.loads(raw or "")
    except ValueError:
        return None
    return row if isinstance(row, dict) and row.get("token") else None


# --- content state --------------------------------------------------------------

def _row(agent_id: str, info: dict[str, Any]) -> dict[str, Any]:
    activity = info.get("activity") or {}
    tool = activity.get("tool") if isinstance(activity.get("tool"), dict) else None
    started = (tool or {}).get("started_at_ms") or activity.get("since_ms") \
        or activity.get("turn_started_ms")
    return {"agent_id": agent_id, "session": info.get("session") or "",
            "persona": info.get("persona") or info.get("session") or "",
            "state": activity.get("state") or "idle", "headline": activity.get("headline"),
            "tool": (tool or {}).get("name"),
            "started_at_ms": int(started) if started else None,
            "turn_started_ms": activity.get("turn_started_ms")}


def content_state(agents: dict[str, dict[str, Any]], now_ms: int) -> dict[str, Any]:
    rows = [_row(agent_id, info) for agent_id, info in agents.items()]
    working = [r for r in rows if r["state"] in WORKING]
    needing = [r for r in rows if r["state"] in NEEDS_YOU]
    working.sort(key=lambda r: r["turn_started_ms"] or r["started_at_ms"] or 0, reverse=True)
    return {"working": len(working), "needs_you": len(needing),
            "lead": working[0] if working else None,
            "agents": (needing + working)[:MAX_ROWS], "updated_at_ms": now_ms}


def payload(state: dict[str, Any], now: float) -> dict[str, Any]:
    return {"aps": {"timestamp": int(now), "event": "update", "content-state": state,
                    "stale-date": int(now) + STALE_AFTER_SEC}}


# --- pusher -----------------------------------------------------------------------

class LiveActivityPusher:
    """Watches live events; pushes the aggregate when it changes."""

    def __init__(self, send: Callable[[dict[str, Any], str], Any], *,
                 clock: Callable[[], float] = time.time, schedule=None,
                 min_interval: float = 15.0):
        from .live_pacing import _thread_timer
        self._send = send
        self._clock = clock
        self._schedule = schedule or _thread_timer
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._agents: dict[str, dict[str, Any]] = {}
        self._sent_key: tuple | None = None
        self._sent_needs = 0
        self._last_sent = 0.0
        self._timer = None

    def observe(self, event: dict[str, Any]) -> None:
        if event.get("type") != "live":
            return
        statuses = [op for op in event.get("ops") or [] if op.get("op") == "status"]
        if not statuses:
            return
        agent_id = str(event.get("agent_id") or "")
        with self._lock:
            info = self._agents.setdefault(agent_id, self._identity(agent_id, event))
            info["activity"] = statuses[-1].get("activity") or {}
            self._consider()

    @staticmethod
    def _identity(agent_id: str, event: dict[str, Any]) -> dict[str, Any]:
        persona = ""
        try:
            from . import agents as agents_db
            persona = str((agents_db.get_by_agent_id(agent_id) or {}).get("persona") or "")
        except Exception:  # noqa: BLE001
            pass
        return {"session": event.get("session") or "", "persona": persona}

    def _consider(self) -> None:
        state = content_state(self._agents, int(self._clock() * 1000))
        lead = state["lead"] or {}
        shape = (state["working"], state["needs_you"], lead.get("agent_id"))
        detail = shape + (lead.get("headline"), lead.get("tool"),
                          tuple((r["agent_id"], r["state"]) for r in state["agents"]))
        if detail == self._sent_key:
            return
        urgent = state["needs_you"] > self._sent_needs
        structural = self._sent_key is None or shape != self._sent_key[:3]
        now = self._clock()
        if urgent or (structural and now - self._last_sent >= 2.0) \
                or now - self._last_sent >= self._min_interval:
            self._push(state, detail, "10" if urgent else "5")
            return
        if self._timer is None:
            wait = max(0.0, self._min_interval - (now - self._last_sent))
            self._timer = self._schedule(wait, self._fire)

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            self._consider()

    def _push(self, state: dict[str, Any], detail: tuple, priority: str) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._timer = None
        self._sent_key = detail
        self._sent_needs = state["needs_you"]
        self._last_sent = self._clock()
        try:
            self._send(payload(state, self._clock()), priority)
        except Exception as exc:  # noqa: BLE001 - best effort, never the stream
            from .log import log_exception
            log_exception("liveActivityPushFail", exc)
