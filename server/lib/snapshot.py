"""Agent dashboard read model."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any


from . import (background_jobs, agent_goals, agents as agents_db, avatar_settings, backend_usage, backends,
               compaction, db,
               config, message_store, model_avatars, persona_avatar_generation, team_store,
               turn_queue, scheduler, janitors, quiet_agents)
from . import reconcile, turn_lifecycle
from .clock import now_ms
from .session_models import agent_model
from . import personas as persona_store
from .avatar_urls import (versioned_avatar_url, janitor_avatar_url,
                          static_persona_avatar_url)
from .log import log, log_exception
from .activity import state_activity_event
from .claude_transcript import context_tokens_from_jsonl, find_latest_jsonl
from .protocol import AgentBackend, AgentState


def _exhausted_backends() -> dict[str, dict[str, Any]]:
    try:
        return backend_usage.exhausted_backends()
    except Exception as exc:  # noqa: BLE001
        log_exception("snapshotBackendQuotaFail", exc)
        return {}


def _backend_quota(backend: str, exhausted: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Why this agent's next turn is expected to fail, or None when it is not.

    An agent never switches provider on its own when its quota runs out: the
    turn fails with the provider's own message and waits for the reset.
    """
    return exhausted.get(backends.normalize(backend)) or None


def _compacting_check() -> Any:
    """One ``(session, observed_kind) -> bool`` for the whole snapshot.

    ``compaction.is_compacting`` asks the external runtime for its status on
    every call, which made the snapshot one socket round trip per agent. The
    runtime's status is already fetched (and briefly cached) for the liveness
    check, so read the compacting set from it once. When the runtime cannot
    answer, fall back to the persisted state exactly as ``is_compacting``
    does; when this process owns its turns, ask ``compaction`` directly.
    """
    if getattr(backends, "_RUNTIME_CLIENT", None) is None:
        return lambda session, kind: compaction.is_compacting(session)
    try:
        compacting = set(backends.runtime_status().get("compactions") or ())
    except Exception:  # noqa: BLE001 - logged once per window by backends
        return lambda session, kind: kind == AgentState.COMPACTING
    return lambda session, kind: session in compacting


from . import tool_explanation_settings  # noqa: E402


def _live_activities() -> dict[str, Any]:
    from . import backends, live_hub
    if backends._RUNTIME_CLIENT is not None:
        try:
            result = backends.runtime_status().get("live_activities")
        except Exception:  # noqa: BLE001 - an outage is logged by runtime_status
            return {}
        return result if isinstance(result, dict) else {}
    hub = live_hub.current()
    return hub.activities() if hub is not None else {}


class SharedSnapshot:
    """One snapshot build at a time, shared by every request that waited for it.

    Each app polls the snapshot and reloads it on every roster or state
    event, so several requests usually arrive together. Building one per
    request multiplied the same full-fleet projection by the client count
    and, under load, pushed requests past the clients' 60 s timeout. A
    request reuses a body whose build started after the request arrived:
    that build saw every write the request could have expected to see.

    The full snapshot and the one without archived agents are different
    bodies; each is shared among the requests that asked for it. The listing
    of archived agents is a third projection, shared the same way and also by
    every page that continues a walk (archived_page). One lock lets a single
    build of any of them run at a time, so a request for any of the three
    waits while another one's build runs: a snapshot behind a listing of
    every archived agent included.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[bool, tuple[float, Any, bytes]] = {}
        self._archived: _ArchivedListing | None = None

    def body(self, ctx, *, include_archived: bool = True) -> bytes:
        arrived = time.monotonic()
        with self._lock:
            self._release_expired_listing(arrived)
            last = self._last.get(include_archived)
            if last is not None and last[0] >= arrived and last[1] is ctx:
                return last[2]
            started = time.monotonic()
            # The default makes the very call it always made.
            snapshot = (build_agent_snapshot(ctx) if include_archived
                        else build_agent_snapshot(ctx, include_archived=False))
            body = json.dumps(snapshot).encode()
            self._last[include_archived] = (started, ctx, body)
            return body

    def archived_page(self, ctx, *, limit: int = None, position=None) -> dict[str, Any]:
        """One page of the archived agents, cut from a shared listing.

        The listing is every archived agent's row, built once and held for
        ARCHIVED_LISTING_TTL_S. A request without a position (the first page)
        asks for a current listing: it shares one whose build started after
        the request arrived, like ``body``, and otherwise builds one, which
        replaces the held one. A request with a position (any later page) is
        cut from whichever listing is held, with no build at all, whoever's
        first page built it: a position is a place in the order, not a place
        in one listing, so it means the same in every listing. Builds are
        therefore one per first page, however many clients page at once.
        Only when no listing is held (it expired, or the Host restarted) does
        a later page build one.
        """
        limit = _page_limit(ARCHIVED_PAGE_DEFAULT if limit is None else limit)
        arrived = time.monotonic()
        with self._lock:
            self._release_expired_listing(arrived)
            listing = self._archived
            if listing is None or listing.ctx is not ctx or (position is None and listing.started < arrived):
                started = time.monotonic()
                rows, order = build_archived_listing(ctx)
                listing = self._archived = _ArchivedListing(started, ctx, rows, order)
            return _cut_page(listing.rows, listing.order, limit, position)

    def _release_expired_listing(self, now: float) -> None:
        """Drop a listing held longer than ARCHIVED_LISTING_TTL_S, and its
        rows with it. Called with the lock held by every snapshot and listing
        request, so the memory goes at the first request after the expiry;
        nothing runs on a timer, and a Host no client asks keeps it."""
        held = self._archived
        if held is not None and now - held.started > ARCHIVED_LISTING_TTL_S:
            self._archived = None


@dataclass(frozen=True)
class _ArchivedListing:
    started: float                     # time.monotonic() when its build began
    ctx: Any
    rows: list[dict[str, Any]]         # every archived agent's row, most recently archived first
    order: list[tuple[int, str]]       # (archived_at, agent_id) of each row


SHARED = SharedSnapshot()


# One page of GET /agents/archived.
ARCHIVED_PAGE_DEFAULT = 50
ARCHIVED_PAGE_MAX = 200
# How long a listing is held for the pages that follow a first page.
ARCHIVED_LISTING_TTL_S = 300.0
# A whole number as it is written: ASCII digits, no leading zero, sign, space,
# underscore or other script.
_LIMIT_TEXT = re.compile(r"[1-9][0-9]{0,2}")


def _not_archived(agent_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in agent_rows if not a.get("archived_at")]


def _archived_order(agent: dict[str, Any]) -> tuple[int, str]:
    """Where an archived agent stands in the listing. ``archived_at`` is an
    integer in every row the Host writes; anything else in that column still
    marks the agent archived and sorts last, so it cannot break the listing."""
    value = agent.get("archived_at")
    return (value if isinstance(value, int) and not isinstance(value, bool) else 0), str(agent["agent_id"])


def _page_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= ARCHIVED_PAGE_MAX:
        raise ValueError("invalid archived agents limit")
    return limit


def parse_archived_query(limit: str, cursor: str) -> tuple[int, tuple[int, str] | None]:
    """The query of GET /agents/archived as (limit, position), or ValueError:
    ``limit`` is 1 to ARCHIVED_PAGE_MAX written in plain digits without a
    leading zero, and ``cursor`` is one this Host issued. Empty means the
    default page from the start. Nothing is read from the database here."""
    if limit and not _LIMIT_TEXT.fullmatch(limit):
        raise ValueError("invalid archived agents limit")
    size = _page_limit(int(limit) if limit else ARCHIVED_PAGE_DEFAULT)
    return size, (_decode_cursor(cursor) if cursor else None)


@backends.runtime_status_scope(timeout=2.0)
def build_agent_snapshot(ctx, *, include_archived: bool = True) -> dict[str, Any]:
    """Reconcile liveness and project the dashboard from batched database reads.

    With ``include_archived=False`` the archived agents are left out of
    ``agents`` and their rows are not built: no transcript read, no message
    preview and no per-agent query is spent on them, which on a long-lived
    Host is most of the work (1,019 of 1,375 agents on 10 Nov 2026). Only an
    archived agent that still looks active is reconciled (_reconcile_unlisted).
    The rows that remain are exactly the rows the full snapshot has for those
    agents, counts of archived children included; ``archived_count`` says how
    many were left out and ``archived_parents`` which of them still have a
    child in the list (_archived_parents); SharedSnapshot.archived_page pages
    them. The full snapshot, the default, is unchanged: clients that do not
    ask keep getting
    every agent.
    """
    snapshot, agent_rows = _project(ctx, None if include_archived else _not_archived)
    if not include_archived:
        snapshot["archived_count"] = sum(1 for a in agent_rows if a.get("archived_at"))
        snapshot["archived_parents"] = _archived_parents(agent_rows)
    return snapshot


def _archived_parents(agent_rows: list[dict[str, Any]]) -> list[str]:
    """The ids of the archived agents that an agent left in the list names as
    its parent.

    A client that has every agent hides a helper whose parent is archived and
    shows one whose parent is gone. Without the archived agents it sees the
    same thing in both cases, a ``parent_agent_id`` that is not in the list,
    and this is what tells them apart: an id here is an archived parent, any
    other missing parent has been deleted. ``agent_rows`` holds no deleted
    agent, so a deleted parent is never listed. Sorted, so equal fleets give
    equal bytes.
    """
    archived = {a["agent_id"] for a in agent_rows if a.get("archived_at")}
    return sorted({a["parent_agent_id"] for a in agent_rows
                   if not a.get("archived_at") and a.get("parent_agent_id") in archived})


@backends.runtime_status_scope(timeout=2.0)
def build_archived_listing(ctx) -> tuple[list[dict[str, Any]], list[tuple[int, str]]]:
    """Every archived agent's row, most recently archived first, and each
    row's place in that order.

    A row is the row the full snapshot has for that agent, built the same
    way, so a client reads it with the decoder it already has, and the
    snapshot without archived agents plus this listing is the full snapshot's
    agent list.
    """
    archived: list[dict[str, Any]] = []

    def select(agent_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        archived.extend(sorted((a for a in agent_rows if a.get("archived_at")),
                               key=_archived_order, reverse=True))
        return archived

    snapshot, _agent_rows = _project(ctx, select)
    return snapshot["agents"], [_archived_order(a) for a in archived]


def build_archived_agents(ctx, *, limit: int = ARCHIVED_PAGE_DEFAULT, cursor: str = "") -> dict[str, Any]:
    """One page of the archived agents from a listing built for this call.
    The route goes through SharedSnapshot.archived_page, which holds one
    listing for the pages that follow; this is the same page without the
    sharing, for callers that want exactly one. Raises ValueError for a
    limit outside 1 to ARCHIVED_PAGE_MAX or a cursor this Host did not issue."""
    limit = _page_limit(limit)
    position = _decode_cursor(cursor) if cursor else None
    rows, order = build_archived_listing(ctx)
    return _cut_page(rows, order, limit, position)


def _cut_page(rows: list[dict[str, Any]], order: list[tuple[int, str]],
              limit: int, position: tuple[int, str] | None) -> dict[str, Any]:
    """The ``limit`` rows after ``position`` (the last agent of the previous
    page; None: from the start). The position is a place in the order, not an
    offset, so it still means the same agent boundary in a later listing."""
    start = 0 if position is None else sum(1 for place in order if place >= position)
    end = start + limit
    return {
        "agents": rows[start:end],
        "next_cursor": _encode_cursor(order[end - 1]) if end < len(rows) else None,
        "total": len(rows),
    }


def _encode_cursor(position: tuple[int, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(position)).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[int, str]:
    """The position a cursor names: (archived_at, agent_id) of the last agent
    of the page that issued it. Nothing in it names a listing."""
    if len(cursor) > 512:
        raise ValueError("invalid archived agents cursor")
    try:
        archived_at, agent_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is the caller's error
        raise ValueError("invalid archived agents cursor") from exc
    if isinstance(archived_at, bool) or not isinstance(archived_at, int) or not isinstance(agent_id, str):
        raise ValueError("invalid archived agents cursor")
    return archived_at, agent_id


def _reconcile_unlisted(agent_rows: list[dict[str, Any]], selected: list[dict[str, Any]],
                        states: dict[str, dict[str, Any]], runtimes: dict[str, dict[str, Any]],
                        runtime_status: dict[str, Any] | None) -> None:
    """Reconcile the archived agents this build leaves out that still look active.

    Building a row is what reconciles an agent (a stuck busy state, a ghost
    session, a leaked slot). Once clients stop asking for archived agents no
    build would do that for them any more, and archiving does not stop a
    turn: an agent archived mid-turn whose turn then died would stay busy
    until someone restored it or opened the archive. So an archived agent
    whose last state is a busy one, that has a live handle, or that the turn
    dispatcher still holds something for, is reconciled here without building
    its row. On a Host at rest that is no agent at all. What is still left to
    the archive listing or a restore is an idle archived agent whose bound
    session has lost its transcript.

    ``runtime_status`` is the external runtime's status this build read, or
    None when this process dispatches its own turns. The caller does not call
    this when that status could not be read: nothing is reconciled blind.
    """
    listed = {a["agent_id"] for a in selected}
    if runtime_status is None:
        # A slot in flight in this process. A live turn also has a handle; a
        # leaked slot has only this.
        from . import turn_dispatch
        held = turn_dispatch.agents_in_flight()
    else:
        # The runtime's slots already count as handles (backends.active_handles);
        # work queued behind no owner shows nowhere else.
        held = set(runtime_status.get("queued") or {})
    for a in agent_rows:
        agent_id = a["agent_id"]
        if agent_id in listed or not a.get("archived_at"):
            continue
        backend = a.get("backend") or AgentBackend.CLAUDE
        state = states.get(agent_id, {})
        try:
            if not (state.get("kind") in turn_lifecycle.BUSY or agent_id in held
                    or backends.active_handles(backend, agent_id)):
                continue
            reconcile.reconcile_agent(
                agent_id, backend, observed_state=state,
                bound_session=runtimes.get(agent_id, {}).get('backend_session_id') or '')
        except Exception as e:  # noqa: BLE001
            log_exception("snapshotReconcileFail", e, detail=agent_id)


def _project(ctx, select) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The snapshot for the agents ``select`` picks from all agent rows (None:
    every agent), and all agent rows. Whatever one agent's row says about
    other agents (its helper counts, the roster) is computed over all of
    them, so a row does not depend on which other agents were selected."""
    rows = []
    is_compacting = _compacting_check()
    focus = agents_db.get_focus()
    team_memberships = team_store.memberships_by_agent()
    queue_states = turn_queue.states()
    goals = agent_goals.by_agent()
    active_jobs, active_avatar_statuses = background_jobs.dashboard_active()
    states = agents_db.dashboard_states()
    live_activities = _live_activities()
    runtimes = agents_db.dashboard_runtimes()
    # Every agent's preview here, as always, when every agent is wanted;
    # otherwise only the selected agents', once the selection is known.
    messages = message_store.dashboard_messages() if select is None else None
    schedules: dict[str, list] = {}
    for schedule in scheduler.list_schedules():
        schedules.setdefault(schedule['agent_id'], []).append(schedule)
    # Model portraits are projected whether or not the preference is on, so
    # toggling it in an app is instant instead of waiting for a snapshot.
    cfg = config.load()
    # Without a server context there is no bundled art to point at, so the
    # projection simply offers no model portraits.
    static_root = getattr(ctx, "static", None)
    model_avatar_root = (static_root / "avatars" / "models") if static_root else None
    default_models: dict[str, str] = {}
    visible_labels = janitors.visible_labels()
    janitor_templates = {row["agent_id"]: row["template_id"] for row in
                         db.conn().execute("SELECT agent_id,template_id FROM janitor_configs")}
    exhausted = _exhausted_backends()
    from . import agent_ring_live
    try:
        call_profiles = agent_ring_live.user_call_profiles(strict=False)
    except agent_ring_live.CallError:
        call_profiles = None  # uncertainty must refuse explicitly, never silently start Normal
    agent_rows = agents_db.list_agents()
    persona_rows = persona_store.list_all()
    personas_by_key = {
        str(row["name"]).strip().casefold(): row
        for row in persona_rows if str(row.get("name") or "").strip()
    }
    selected = agent_rows if select is None else select(agent_rows)
    if messages is None:
        messages = message_store.dashboard_messages(agent_ids={a["agent_id"] for a in selected})
    tree = helper_tree(agent_rows, states, visible_labels)
    child_count = tree.child_count
    running_children = tree.running_children
    helper_sessions = tree.helper_sessions
    helper_activity = tree.helper_activity
    reconcile_available = True
    runtime_status = None                 # the external runtime's, when there is one
    if backends._RUNTIME_CLIENT is not None:
        try:
            runtime_status = backends.runtime_status()
        except Exception as exc:  # noqa: BLE001 - scoped status already logs once
            reconcile_available = False
            reconcile.forget_observations(a["agent_id"] for a in agent_rows)
            log("snapshotReconcileSkipped",
                f"count={len(agent_rows)} error_type={type(exc).__name__}")
    if select is not None and reconcile_available:
        _reconcile_unlisted(agent_rows, selected, states, runtimes, runtime_status)
    for a in selected:
        agent_id = a["agent_id"]
        backend = a.get("backend") or AgentBackend.CLAUDE
        # Re-derive truth from reality before reading derived state (INV1-3):
        # a stuck busy row, a ghost session or a phantom in-flight slot is
        # repaired here, at read time, not on the next send.
        state = states.get(agent_id, {})
        rt = runtimes.get(agent_id, {})
        bsid = rt.get('backend_session_id') or ''
        if reconcile_available:
            try:
                repaired = reconcile.reconcile_agent(
                    agent_id, backend, observed_state=state, bound_session=bsid)
                if 'state' in repaired:
                    # Repairs are rare writes. Re-read their clocks so this very
                    # response still reflects recovery, including unread timing.
                    state = agents_db.latest_state(agent_id) or {}
                    state['turn_started_at'] = agents_db.turn_started_at(agent_id)
                    state['last_turn_end'] = agents_db.last_turn_end(agent_id)
                if 'ghost_session' in repaired:
                    bsid = ''
            except Exception as e:  # noqa: BLE001
                log_exception("snapshotReconcileFail", e, detail=agent_id)
        active = bool(backends.active_handles(backend, agent_id))
        latest_state = state.get("kind")
        if active and latest_state not in {"thinking", "tool", "compacting", "background"}:
            latest_state = "thinking"
        # Two different things keep an agent busy after its turn ends. A
        # SUB-AGENT is a Clarp helper agent it created (role helper), counted
        # from its running children. A BACKGROUND PROCESS is a durable job
        # that is not merely the watcher mirroring one of those helpers.
        # Either shows the agent as background so the apps draw the running
        # indicator instead of idle.
        processes = background_jobs.background_processes(
            active_jobs.get(agent_id, []), helper_sessions.get(agent_id, set()))
        sub_agents = running_children.get(agent_id, 0)
        if (processes or sub_agents) and latest_state not in {"thinking", "tool", "compacting", "background"}:
            latest_state = "background"
        status_text = shown_status(a, state, visible_labels,
                                   helper_activity.get(agent_id, []), processes)
        turn_started_at = int(state.get('turn_started_at') or 0)
        if active and not turn_started_at:
            turn_started_at = int(rt.get('open_turn_started_at') or 0)
        if active and turn_started_at <= 0:
            # Busy with no recorded turn start (a handle adopted mid-turn):
            # "since now" is honest; 0 read as 1970 in a client's timer.
            turn_started_at = now_ms()
        message = messages.get(agent_id, {})
        message_head = message.get('head', {'preview': '', 'message_id': ''})
        # Agree with /log's contract: no bound backend session means an empty
        # conversation at revision 0. Querying with an empty session id
        # dropped the WHERE clause and returned the MAX over every
        # conversation, so the client saw a head it could never reach and
        # reloaded the full transcript on every poll (audit bug D1).
        head_revision = message.get('revisions', {}).get(bsid, 0) if bsid else 0
        # Context-window occupancy from the transcript, only for an adapter
        # that declares a context window (Claude). Codex/agy auto-compact in
        # their own loops, so an empty gauge there correctly signals "managed
        # automatically". Computed from the last assistant message's usage,
        # not the cumulative result event.
        context_tokens = None
        context_window = backends.by_id(backend).context_window
        if context_window is not None and bsid:
            j = find_latest_jsonl(bsid)
            if j is not None:
                context_tokens = context_tokens_from_jsonl(j)
        mcp_servers = _agent_mcp_list(a.get("mcp_servers"))
        # Only an Agent still wearing its bundled persona portrait can wear a
        # model variant of it; an uploaded or generated portrait is the
        # user's own choice and is never swapped out from under them.
        model_avatar_url = ""
        if model_avatar_root is not None and not str(a.get("avatar_path") or ""):
            resolved_backend = backends.normalize(backend)
            if resolved_backend not in default_models:
                default_models[resolved_backend] = backends.default_model_effort(
                    resolved_backend, cfg)[0]
            model_avatar_url = model_avatars.url_for(
                a["persona"], backend, a.get("model") or "",
                root=model_avatar_root,
                default_model=default_models[resolved_backend])
        resolved_avatar_url = janitor_avatar_url(bool(a.get("is_janitor")),
            janitor_templates.get(agent_id), static_root=static_root) or versioned_avatar_url(
            "/avatars", agent_id, str(a.get("avatar_path") or "")) or (
            "" if a.get("avatar_symbol") else static_persona_avatar_url(
                a["persona"], backend, static_root=static_root))
        rows.append({
            "call_mode": ("unavailable" if call_profiles is None else
                          "agent_live_v1" if a.get("session") in call_profiles else "classic"),
            "agent_id":       agent_id,
            "persona":        a["persona"],
            "voice_id":       a["voice_id"],
            "avatar_symbol":  a.get("avatar_symbol") or "",
            "avatar_url":     resolved_avatar_url,
            "avatar_generation_status": persona_avatar_generation.avatar_generation_status_for_agent(
                a,
                personas_by_key=personas_by_key,
                active_by_persona_id=active_avatar_statuses,
            ),
            "model_avatar_url": model_avatar_url,
            "cwd":            a["cwd"],
            "session":        a["session"],
            "backend":        backend,
            "model":          agent_model(a, bsid),
            "effort":         a.get("effort") or "",
            "mcp_servers":    mcp_servers,
            "schedules":      schedules.get(agent_id, []),
            "is_janitor": bool(a.get("is_janitor")),
            # Never unread, in Updates, Ctrl+J or "agents working" by itself.
            "quiet": quiet_agents.is_quiet(a),
            "parent_agent_id": a.get("parent_agent_id") or None,
            "role": a.get("role") or "agent",
            "helper_state": a.get("helper_state") or None,
            "child_count": child_count.get(agent_id, 0),
            "running_children": running_children.get(agent_id, 0),
            "interaction_capabilities": agents_db.interaction_capabilities(a),
            "heartbeat_enabled": bool(a.get("heartbeat_enabled")),
            "dreaming_enabled": bool(a.get("dreaming_enabled")),
            "muted":          bool(a.get("muted")),
            "archived_at":    a.get("archived_at"),
            "backend_session_id": bsid,
            "alive":          True,
            "busy":           active or state.get('kind') in AgentState.busy_states(),
            "focused":        agent_id == focus,
            # An agent with no user messages yet has no message activity, and
            # a bare 0 sorted it below every idle chat — so an agent spawned by
            # another agent was invisible in Chats while it was already working.
            # Floor the value at creation time: a new agent enters the list at
            # its own age and decays normally, and operational state still never
            # reorders an established conversation.
            # Chat ordering uses its own presentation clock. ``activity`` is
            # retained as the scheduler's user-engagement clock for callers
            # that need it; never let routine/tool/import state reorder chats.
            "last_activity":  max(int(message.get('chat_activity', 0) or 0),
                                  int(a.get("created_at") or 0)),
            "last_turn_end":  int(state.get('last_turn_end') or 0),
            # Eager last-message preview for the agent-list overview, so the
            # client shows it without opening each chat.
            "last_message":   message_head["preview"],
            "last_completed_message": message.get("completed_head", {}).get("preview", ""),
            # Version the eager preview against the same canonical conversation
            # used by /log. Clients can now prove that an instant cached chat is
            # behind the overview instead of displaying two silently divergent
            # projections.
            "conversation_id": bsid,
            "head_revision": head_revision,
            "last_message_id": message_head["message_id"],
            "turn_started_at": turn_started_at,
            "latest_state":   latest_state,
            "status_text":    status_text,
            "background_jobs": {"count": len(processes), "sub_agents": sub_agents},
            "team_ids":       team_memberships.get(agent_id, []),
            "latest_state_ts": state.get("ts"),
            "context_tokens": context_tokens,
            # Window the tokens fill, as the adapter declares it; the native
            # gauge divides tokens by this. None for codex/agy (they
            # auto-compact, so no gauge).
            "context_window": context_window,
            "compacting":     is_compacting(a["session"], state.get("kind")),
            "queued_turn_count": queue_states.get(agent_id, {}).get("count", 0),
            "queued_turn_revision": queue_states.get(agent_id, {}).get("revision", 0),
            "queue_paused": bool(queue_states.get(agent_id, {}).get("paused", False)),
            "goal":           agent_goals.public(goals.get(agent_id)),
            # Advisory only: a send is never refused, because an account
            # switch may still serve it.
            "backend_quota": _backend_quota(backend, exhausted),
            "activity":       state_activity_event(
                agent_id=agent_id,
                session=a["session"],
                persona=a["persona"],
                kind=state.get("kind") or "",
                ts=int(state.get("ts") or 0),
                detail=state.get("detail") if isinstance(state.get("detail"), dict) else {},
            ) if state.get("kind") else None,
            # The live-items status line (docs/live-items.md §1.3), when the
            # hub has seen this agent since it started.
            "live_activity":  live_activities.get(agent_id),
        })
    roster = []
    roster_keys = set()
    for name in [
        *(row["name"] for row in persona_rows),
        # Janitors are maintenance identities, never chat targets, so their
        # personas must not appear in the roster clients offer for switching
        # or starting an agent.
        # From every agent, selected or not: the roster is the same list
        # whichever agents a client asked for.
        *(str(a["persona"]) for a in agent_rows
          if a.get("persona") and not a.get("is_janitor")),
    ]:
        key = name.strip().casefold()
        if key and key not in roster_keys:
            roster.append(name.strip())
            roster_keys.add(key)
    from . import settings_store
    projected_settings = settings_store.get_many([avatar_settings.KEY, *tool_explanation_settings.KEYS])
    return {
        "agents": rows,
        "focus": focus,
        "roster": roster,
        "personas": [
            persona_store.public(row, active_avatar_statuses=active_avatar_statuses)
            for row in persona_rows
        ],
        # Whether clients should prefer the model portrait where one exists.
        "model_avatars": avatar_settings.get(stored=projected_settings)["model_avatars"],
        # The menu of MCP servers an agent can be granted (from ~/.claude.json).
        "available_mcp_servers": sorted(config.read_global_mcp_servers().keys()),
        # Whether tool rows carry plain-language explanations (Host setting).
        "tool_explanations": tool_explanation_settings.get(stored=projected_settings),
    }, agent_rows


class HelperTree:
    """Helper counts per parent, from the agent rows already read."""

    def __init__(self) -> None:
        self.child_count: dict[str, int] = {}
        self.running_children: dict[str, int] = {}
        self.helper_sessions: dict[str, set[str]] = {}
        # What each running helper is doing, so the parent's status line can
        # say it instead of repeating the count the badge already shows.
        self.helper_activity: dict[str, list[dict[str, Any]]] = {}


def helper_tree(agent_rows: list[dict[str, Any]], states: dict[str, dict[str, Any]],
                visible_labels: dict[str, Any]) -> HelperTree:
    tree = HelperTree()
    for a in agent_rows:
        parent_id = a.get("parent_agent_id")
        if parent_id:
            tree.child_count[parent_id] = tree.child_count.get(parent_id, 0) + 1
            if (a.get("role") or "agent") == "helper":
                tree.helper_sessions.setdefault(parent_id, set()).add(str(a.get("session") or ""))
            if a.get("helper_state") == "running":
                tree.running_children[parent_id] = tree.running_children.get(parent_id, 0) + 1
                tree.helper_activity.setdefault(parent_id, []).append(
                    _helper_activity(a, states.get(a["agent_id"], {}), visible_labels))
    return tree


def shown_status(agent: dict[str, Any], state: dict[str, Any], visible_labels: dict[str, Any],
                 helpers: list[dict[str, Any]], processes: list[dict[str, Any]]) -> str | None:
    """The status line the apps show for ``agent``, or None.

    Agent-authored free-text status persists separately from run state so it
    survives leader turn transitions. Older BACKGROUND rows may still carry a
    detail label, so keep that as a compatibility fallback. Without either,
    running helpers or processes describe the activity.
    """
    agent_id = agent["agent_id"]
    detail = state.get("detail")
    if not isinstance(detail, dict):
        detail = {}
    text = str(visible_labels.get(agent_id, agent.get("custom_status")) or "").strip() or None
    if text is None and agent_id not in visible_labels and state.get("kind") == "background":
        text = str(detail.get("label") or "").strip() or None
    if (processes or helpers) and text is None:
        text = _work_summary(helpers, processes)
    return text


# How a running helper's latest state reads in its parent's roll-up.
_HELPER_BUCKETS = {
    "thinking": "working", "tool": "working", "compacting": "working",
    "background": "working", "waiting": "waiting", "interrupted": "waiting",
}


def _helper_activity(helper: dict[str, Any], state: dict[str, Any],
                     visible_labels: dict[str, Any]) -> dict[str, Any]:
    """One running helper as its parent's status line describes it."""
    kind = str(state.get("kind") or "")
    detail = state.get("detail") if isinstance(state.get("detail"), dict) else {}
    text = str(visible_labels.get(helper["agent_id"], helper.get("custom_status")) or "").strip()
    if not text and kind:
        text = str(state_activity_event(
            agent_id=helper["agent_id"], session=helper["session"],
            persona=helper["persona"], kind=kind, ts=int(state.get("ts") or 0),
            detail=detail)["summary"] or "").strip()
    bucket = _HELPER_BUCKETS.get(kind, "idle")
    if detail.get("account_recovery"):
        bucket = "waiting"
    return {"session": str(helper["session"]), "text": text, "bucket": bucket,
            "ts": int(state.get("ts") or 0)}


def _work_summary(helpers: list[dict[str, Any]], processes: list[dict[str, Any]]) -> str:
    """Say what the agent's sub-agents and processes are doing.

    The row's badge already carries the counts (`background_jobs`), so this
    line describes activity: one helper as "slice5-helper: running tests",
    several as a roll-up "3 working, 1 waiting", a process by its latest
    progress line or its title. A bare count is the last resort.
    """
    parts: list[str] = []
    if len(helpers) == 1:
        helper = helpers[0]
        parts.append(f"{helper['session']}: {helper['text']}" if helper["text"] else helper["session"])
    elif helpers:
        buckets: dict[str, int] = {}
        for helper in helpers:
            buckets[helper["bucket"]] = buckets.get(helper["bucket"], 0) + 1
        parts.append(", ".join(f"{buckets[b]} {b}" for b in ("working", "waiting", "idle")
                               if buckets.get(b)))
    if processes:
        # Most recent progress wins; on a tie, the later-started job.
        latest = max(enumerate(processes),
                     key=lambda item: (int(item[1].get("active_at") or 0), item[0]))[1]
        text = str(latest.get("progress_text") or "").strip()
        title = str(latest.get("title") or "").strip()
        described = f"{title}: {text}" if title and text else (text or title)
        if described:
            parts.append(described)
        else:
            noun = "process" if len(processes) == 1 else "processes"
            parts.append(f"{len(processes)} background {noun}")
    return " · ".join(parts)


def _agent_mcp_list(raw: str | None) -> list[str]:
    from .mcp_selection import decode
    return decode(raw)[1]
