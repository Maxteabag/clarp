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
STRUCTURAL_FLOOR_SEC = 10.0
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
    and Janitor failures. With the agents they belong to: ``items`` holds one
    agent id per badge item (empty when it names none)."""
    from . import artifacts, attention_index, janitor_attention
    items: list[str] = []
    for decision in artifacts.attention(include_questions=True):
        items.append(str(decision.get("agent_id") or ""))
    janitors = janitor_attention.pending()
    janitor_ids = {str(item.get("artifact_id")) for item in janitors}
    for item in janitors:
        items.append(str(item.get("agent_id") or ""))
    cursor = ""
    with attention_index.page_sequence() as read_page:
        for _page in range(50):
            page = read_page(limit=200, cursor=cursor)
            for artifact in page["artifacts"]:
                if artifact.get("attention_bucket") == "blocking" and not artifact.get("archived_at") \
                        and str(artifact.get("artifact_id")) not in janitor_ids \
                        and artifact.get("type") != "workflow_run":
                    items.append(str(artifact.get("agent_id") or ""))
            cursor = page.get("next_cursor") or ""
            if not cursor:
                break
    return {"count": len(items), "agent_ids": {agent for agent in items if agent}, "items": items}


def _attention_items(attention: dict[str, Any]) -> list[str]:
    """One agent id per badge item. An older caller without ``items`` gives
    each named agent one item and the rest of ``count`` none."""
    if attention.get("items") is not None:
        return [str(agent or "") for agent in attention["items"]]
    named = sorted(str(agent) for agent in attention.get("agent_ids") or () if agent)
    return named + [""] * max(0, int(attention.get("count") or 0) - len(named))


def _clean_attention(attention: dict[str, Any]) -> dict[str, Any]:
    clean = {"count": max(0, int(attention.get("count") or 0)),
             "agent_ids": set(attention.get("agent_ids") or ())}
    if attention.get("items") is not None:
        clean["items"] = [str(agent or "") for agent in attention["items"]]
    return clean


# --- helpers roll up into their top-level agent ----------------------------------
#
# The same rule as the app's chip (clarp-ios AgentWorkActivityPolicy) and its
# chat list nesting: a helper is ``role == "helper"`` with a
# ``parent_agent_id``, which names its parent by agent id (else session). A
# helper never counts or shows on its own; its work and its wait belong to its
# top-level agent. A helper whose parent is not listed (archived, deleted, a
# Janitor) is its own top level, and so is everyone in a parent cycle.

def _is_helper(row: dict[str, Any]) -> bool:
    return str(row.get("role") or "") == "helper" and bool(row.get("parent_agent_id"))


def _listed(row: dict[str, Any]) -> bool:
    """On the chip at all: not archived and not a quiet agent (Janitor)."""
    return not row.get("archived_at") and not row.get("is_janitor")


def top_level_owners(roster: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Each listed agent's top-level agent id. ``roster`` maps agent id to its
    ``agents`` row (``session``, ``role``, ``parent_agent_id``)."""
    alias: dict[str, str] = {}
    for agent_id in roster:
        alias.setdefault(agent_id, agent_id)
    for agent_id, row in roster.items():
        if row.get("session"):
            alias.setdefault(str(row["session"]), agent_id)

    def parent(agent_id: str) -> str | None:
        row = roster[agent_id]
        if not _is_helper(row):
            return None
        found = alias.get(str(row["parent_agent_id"]))
        return found if found and found != agent_id else None

    owners: dict[str, str] = {}
    for agent_id in roster:
        seen, top = {agent_id}, agent_id
        while (up := parent(top)) is not None:
            if up in seen:
                top = agent_id
                break
            seen.add(up)
            top = up
        owners[agent_id] = top
    return owners


def _newest_first(row: dict[str, Any]) -> tuple:
    return (-(row["turn_started_ms"] or row["started_at_ms"] or 0), row["persona"])


def _waiting(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "state": "waiting", "headline": "Waiting for you", "tool": None,
            "started_at_ms": None, "turn_started_ms": None}


def content_state(agents: dict[str, dict[str, Any]], now_ms: int,
                  attention: dict[str, Any] | None = None,
                  roster: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The chip's content: top-level agents only. ``agents`` are the agents
    seen on the live stream; ``roster`` the ``agents`` rows by id (an agent
    seen live but not in it is listed by what ``agents`` knows of it).

    ``needs_you`` counts each top-level agent with a badge item or a wait
    (its own or a helper's) once, plus badge items that name no listed agent.
    """
    attention = attention or {}
    rows_by_id = {agent_id: row for agent_id, row in (roster or {}).items()}
    for agent_id, info in agents.items():
        rows_by_id.setdefault(agent_id, info)
    listed = {agent_id: row for agent_id, row in rows_by_id.items() if _listed(row)}
    owners = top_level_owners(listed)
    items = _attention_items(attention)
    flagged = {agent for agent in items if agent in listed}
    other = sum(1 for agent in items if agent not in listed)

    def row_of(agent_id: str) -> dict[str, Any]:
        info = agents.get(agent_id)
        if info is None:
            row = listed[agent_id]
            info = {"session": row.get("session"), "persona": row.get("persona"), "activity": {}}
        return _row(agent_id, info)

    teams: dict[str, list[str]] = {}
    for agent_id in listed:
        if agent_id in agents or agent_id in flagged:
            teams.setdefault(owners[agent_id], []).append(agent_id)
    tops: list[tuple[dict[str, Any], bool]] = []
    for owner, members in teams.items():
        top = row_of(owner)
        rows = [row_of(member) for member in members if member != owner]
        needs = any(member in flagged for member in members) \
            or any(row["state"] in NEEDS_YOU for row in rows + [top])
        busy = sorted((row for row in rows if row["state"] in WORKING), key=_newest_first)
        if top["state"] not in WORKING and busy:
            newest = busy[0]
            top = {**top, "state": newest["state"], "tool": None,
                   "headline": "1 sub-agent working" if len(busy) == 1
                   else f"{len(busy)} sub-agents working",
                   "started_at_ms": newest["started_at_ms"],
                   "turn_started_ms": newest["turn_started_ms"]}
        tops.append((top, needs))
    working = sorted((top for top, _ in tops if top["state"] in WORKING), key=_newest_first)
    waiting = [_waiting(top) for top in sorted(
        (top for top, needs in tops if needs and top["state"] not in WORKING), key=_newest_first)]
    return {"working": len(working), "needs_you": other + sum(1 for _, needs in tops if needs),
            "decisions": len(items),
            "lead": working[0] if working else None,
            "agents": (waiting + working)[:MAX_ROWS], "updated_at_ms": now_ms}


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
                 roster: Callable[[], list[dict[str, Any]]] | None = None,
                 background: Callable[[Callable[[], None]], Any] | None = None):
        from .live_pacing import _thread_timer
        self._send = send
        self._clock = clock
        self._schedule = schedule or _thread_timer
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._agents: dict[str, dict[str, Any]] = {}
        self._quiet: set[str] = set()
        self._sent_key: tuple | None = None
        self._sent_needs = 0
        self._last_sent = 0.0
        self._timer = None
        self._timer_due = 0.0
        self._attention_source = attention
        self._attention: dict[str, Any] | None = None
        self._attention_at = 0.0
        self._attention_refresh = attention_refresh_sec
        # The agents rows (lineage, archived) the roll-up reads, by agent id:
        # refreshed with the badge, and learned at once for an agent first
        # seen live together with its parent chain.
        self._roster_source = roster
        self._roster: dict[str, dict[str, Any]] = {}
        # Where the badge query runs: in production a thread, so the hub's
        # lock never waits on the database; inline when not given.
        self._background = background
        self._refreshing = False

    def set_attention(self, attention: dict[str, Any]) -> None:
        """The Updates badge changed (decisions, blocking artifacts, Janitor)."""
        with self._lock:
            self._attention = _clean_attention(attention)
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
                self._roster = self._read_roster()
            else:
                self._refreshing = True
                self._background(self._refresh_attention)
        return self._attention or {"count": 0, "agent_ids": set()}

    def _read_attention(self) -> dict[str, Any]:
        try:
            return _clean_attention(self._attention_source() or {})
        except Exception:  # noqa: BLE001 - a count, never a failure
            return self._attention or {"count": 0, "agent_ids": set()}

    def _read_roster(self) -> dict[str, dict[str, Any]]:
        if self._roster_source is None:
            return self._roster
        try:
            return {str(row["agent_id"]): row for row in self._roster_source() or ()
                    if row.get("agent_id")}
        except Exception:  # noqa: BLE001 - the last roster, never a failure
            return self._roster

    def _refresh_attention(self) -> None:
        fresh = self._read_attention()
        roster = self._read_roster()
        with self._lock:
            self._refreshing = False
            self._roster = roster
        self.set_attention(fresh)

    def observe(self, event: dict[str, Any]) -> None:
        if event.get("type") != "live":
            return
        statuses = [op for op in event.get("ops") or [] if op.get("op") == "status"]
        if not statuses:
            return
        agent_id = str(event.get("agent_id") or "")
        with self._lock:
            if agent_id in self._quiet:
                return
            info = self._agents.get(agent_id)
            if info is None:
                info = self._identity(agent_id, event)
                if info.pop("quiet"):
                    # A Janitor's work is not "agents working" (quiet_agents).
                    self._quiet.add(agent_id)
                    return
                self._learn_lineage(agent_id, info)
                self._agents[agent_id] = info
            info["activity"] = statuses[-1].get("activity") or {}
            self._consider()

    @staticmethod
    def _identity(agent_id: str, event: dict[str, Any]) -> dict[str, Any]:
        from . import agents as agents_db, quiet_agents
        try:
            agent = agents_db.get_by_agent_id(agent_id) or {}
        except Exception as exc:  # noqa: BLE001 - never break the live stream
            from .log import log_exception
            log_exception("liveActivityAgentLookupFail", exc, detail=f"agent={agent_id}")
            agent = {}
        return {"session": event.get("session") or "",
                "persona": str(agent.get("persona") or ""),
                "role": agent.get("role"), "parent_agent_id": agent.get("parent_agent_id"),
                "archived_at": agent.get("archived_at"),
                "quiet": quiet_agents.is_quiet(agent)}

    def _learn_lineage(self, agent_id: str, info: dict[str, Any]) -> None:
        """A helper first seen live rolls up at once: fetch the ancestors the
        roster does not hold yet (bounded; a cycle or a gap stops it)."""
        if agent_id not in self._roster:
            self._roster[agent_id] = {"agent_id": agent_id, **info}
        row = self._roster[agent_id]
        known = {r.get("session"): r for r in self._roster.values() if r.get("session")}
        for _step in range(16):
            if not _is_helper(row):
                return
            parent = str(row["parent_agent_id"])
            if parent in self._roster or parent in known:
                row = self._roster.get(parent) or known[parent]
                continue
            from . import identity
            try:
                found = identity.lookup(parent)
            except Exception:  # noqa: BLE001 - an orphan then, never the stream
                return
            if not found:
                return
            self._roster[str(found["agent_id"])] = found
            row = found

    def _consider(self) -> None:
        state = content_state(self._agents, int(self._clock() * 1000), self._current_attention(),
                              self._roster)
        lead = state["lead"] or {}
        shape = (state["working"], state["needs_you"], lead.get("agent_id"))
        detail = shape + (lead.get("headline"), lead.get("tool"),
                          tuple((r["agent_id"], r["state"]) for r in state["agents"]))
        if detail == self._sent_key:
            return
        urgent = state["needs_you"] > self._sent_needs
        structural = self._sent_key is None or shape != self._sent_key[:3]
        now = self._clock()
        # A structural change waits at least STRUCTURAL_FLOOR_SEC after the
        # last push, routine churn min_interval (APNS-N1: 2 s used the relay
        # budget up); either way the latest state goes out when the timer fires.
        floor = min(STRUCTURAL_FLOOR_SEC, self._min_interval) if structural else self._min_interval
        if urgent or now - self._last_sent >= floor:
            self._push(state, detail, "10" if urgent else "5")
            return
        due = self._last_sent + floor
        if self._timer is not None and due < self._timer_due:
            self._cancel_timer()
        if self._timer is None:
            self._timer_due = due
            self._timer = self._schedule(max(0.0, due - now), self._fire)

    def _cancel_timer(self) -> None:
        try:
            self._timer.cancel()
        except Exception:  # noqa: BLE001 - a timer that already ran
            pass
        self._timer = None

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            self._consider()

    def _push(self, state: dict[str, Any], detail: tuple, priority: str) -> None:
        if self._timer is not None:
            self._cancel_timer()
        self._sent_key = detail
        self._sent_needs = state["needs_you"]
        self._last_sent = self._clock()
        try:
            self._send(payload(state, self._clock()), priority)
        except Exception as exc:  # noqa: BLE001 - best effort, never the stream
            from .log import log_exception
            log_exception("liveActivityPushFail", exc)
