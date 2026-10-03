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

_VERBS = {"exec": "Running", "read": "Reading", "list": "Listing", "search": "Searching",
          "edit": "Editing", "write": "Writing", "fetch": "Fetching", "todo": "Planning",
          "agent": "Delegating", "mcp": "Calling", "other": "Calling"}
_STATE_WORDS = {"thinking": "Thinking", "responding": "Responding", "compacting": "Compacting",
                "waiting": "Waiting for you", "limited": "Waiting for the usage limit",
                "interrupted": "Interrupted", "background": "Background work", "idle": "Idle"}
_PATH_CATEGORIES = frozenset({"read", "edit", "write", "list"})


def display_name(persona: str) -> str:
    """A person's name, not a session id (the iOS rule): `clarp-memory-expert`
    and `CLARP-MEMORY-EXPERT` read `Clarp Memory Expert`; `ECIT Rachel` stays."""
    persona = str(persona or "")
    separated = "-" in persona or "_" in persona
    shouted = len(persona) > 3 and persona == persona.upper() and persona != persona.lower() \
        and " " not in persona
    if not (separated or shouted):
        return persona
    words = [w for w in persona.replace("-", " ").replace("_", " ").split(" ") if w]
    return " ".join(w[:1].upper() + w[1:].lower() for w in words)


def _tool_headline(tool: dict[str, Any]) -> str:
    name = str(tool.get("name") or "")
    label = str(tool.get("label") or "")
    category = tool.get("category")
    if not category:
        from .live_tools import classify_tool
        category = classify_tool(name, {})[0]
    if category in _PATH_CATEGORIES and "/" in label:
        label = label.rstrip("/").rsplit("/", 1)[-1]
    verb = _VERBS.get(category, "Calling")
    return f"{verb} {label or name}".strip() if (label or name) else "Using a tool"


def row_headline(activity: dict[str, Any]) -> str:
    """What the agent is doing, in the words the apps use; never empty."""
    state = activity.get("state") or "idle"
    tool = activity.get("tool") if isinstance(activity.get("tool"), dict) else None
    if state == "tool":
        return _tool_headline(tool) if tool else "Using a tool"
    return str(activity.get("headline") or "") or _STATE_WORDS.get(state, "Working")


def _row(agent_id: str, info: dict[str, Any]) -> dict[str, Any]:
    activity = info.get("activity") or {}
    tool = activity.get("tool") if isinstance(activity.get("tool"), dict) else None
    started = (tool or {}).get("started_at_ms") or activity.get("since_ms") \
        or activity.get("turn_started_ms")
    return {"agent_id": agent_id, "session": info.get("session") or "",
            "persona": display_name(info.get("persona") or info.get("session") or ""),
            "state": activity.get("state") or "idle", "headline": row_headline(activity),
            "tool": (tool or {}).get("name"),
            "started_at_ms": int(started) if started else None,
            "turn_started_ms": activity.get("turn_started_ms")}


def badge_attention() -> dict[str, Any]:
    """What the apps' Updates badge counts, unarchived: pending decisions and
    questions, artifacts waiting on the owner (attention bucket `blocking`),
    and Janitor failures. With the agents they belong to."""
    from . import artifacts, attention_index, janitor_attention
    items: list[str] = []
    for decision in artifacts.attention(include_questions=True):
        items.append(str(decision.get("agent_id") or ""))
    janitors = janitor_attention.pending()
    janitor_ids = {str(item.get("artifact_id")) for item in janitors}
    for item in janitors:
        items.append(str(item.get("agent_id") or ""))
    cursor = ""
    for _page in range(50):
        page = attention_index.page(limit=200, cursor=cursor)
        for artifact in page["artifacts"]:
            if artifact.get("attention_bucket") == "blocking" and not artifact.get("archived_at") \
                    and str(artifact.get("artifact_id")) not in janitor_ids \
                    and artifact.get("type") != "workflow_run":
                items.append(str(artifact.get("agent_id") or ""))
        cursor = page.get("next_cursor") or ""
        if not cursor:
            break
    return {"count": len(items), "agent_ids": {agent for agent in items if agent}}


def content_state(agents: dict[str, dict[str, Any]], now_ms: int,
                  attention: dict[str, Any] | None = None) -> dict[str, Any]:
    attention = attention or {"count": 0, "agent_ids": set()}
    decisions = int(attention.get("count") or 0)
    counted = set(attention.get("agent_ids") or ())
    rows = [_row(agent_id, info) for agent_id, info in agents.items()]
    working = [r for r in rows if r["state"] in WORKING]
    needing = [r for r in rows if r["state"] in NEEDS_YOU]
    # A waiting agent that already filed something is counted once.
    waiting_unfiled = [r for r in needing if r["agent_id"] not in counted]
    working.sort(key=lambda r: r["turn_started_ms"] or r["started_at_ms"] or 0, reverse=True)
    return {"working": len(working), "needs_you": len(waiting_unfiled) + decisions,
            "decisions": decisions,
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
                 min_interval: float = 15.0,
                 attention: Callable[[], dict[str, Any]] | None = None,
                 attention_refresh_sec: float = 30.0,
                 background: Callable[[Callable[[], None]], Any] | None = None):
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
        self._attention_source = attention
        self._attention: dict[str, Any] | None = None
        self._attention_at = 0.0
        self._attention_refresh = attention_refresh_sec
        # Where the badge query runs: in production a thread, so the hub's
        # lock never waits on the database; inline when not given.
        self._background = background
        self._refreshing = False

    def set_attention(self, attention: dict[str, Any]) -> None:
        """The Updates badge changed (decisions, blocking artifacts, Janitor)."""
        with self._lock:
            self._attention = {"count": max(0, int(attention.get("count") or 0)),
                               "agent_ids": set(attention.get("agent_ids") or ())}
            self._attention_at = self._clock()
            if self._agents or self._sent_key is not None:
                self._consider()

    def _current_attention(self) -> dict[str, Any]:
        stale = self._clock() - self._attention_at >= self._attention_refresh
        if (self._attention is None or stale) and self._attention_source is not None \
                and not self._refreshing:
            self._attention_at = self._clock()
            if self._background is None:
                self._attention = self._read_attention()
            else:
                self._refreshing = True
                self._background(self._refresh_attention)
        return self._attention or {"count": 0, "agent_ids": set()}

    def _read_attention(self) -> dict[str, Any]:
        try:
            fresh = self._attention_source() or {}
            return {"count": int(fresh.get("count") or 0),
                    "agent_ids": set(fresh.get("agent_ids") or ())}
        except Exception:  # noqa: BLE001 - a count, never a failure
            return self._attention or {"count": 0, "agent_ids": set()}

    def _refresh_attention(self) -> None:
        fresh = self._read_attention()
        with self._lock:
            self._refreshing = False
        self.set_attention(fresh)

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
        state = content_state(self._agents, int(self._clock() * 1000), self._current_attention())
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
