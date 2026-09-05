"""Agent CRUD over the SQLite state store.

This is the primary interface. `agent_store.{load,save}_agents` delegates to
here for HTTP paths that operate on a complete session map, while domain code
uses stable `agent_id` values directly.
"""
from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import origins
from . import voice_verbosity as voice_verbosity_lib
from .db import conn, now_ms
from .protocol import AgentBackend, AgentState, ClipStatus, TurnSource


def _new_agent_id() -> str:
    return secrets.token_hex(8)


# ---- read paths --------------------------------------------------------

def list_agents() -> list[dict[str, Any]]:
    """All live agents. Each row carries agent_id + session."""
    c = conn()
    rows = c.execute("""
        SELECT agent_id, persona, voice_id, cwd, session, backend, created_at,
               model, effort, mcp_servers, heartbeat_enabled,
               dreaming_enabled, dreaming_last_local_date, muted,
               custom_status, avatar_symbol, avatar_path, personality,
               voice_verbosity, archived_at
          FROM agents
         WHERE deleted_at IS NULL
         ORDER BY created_at
    """).fetchall()
    return [dict(r) for r in rows]


def get_by_session(session: str) -> dict[str, Any] | None:
    c = conn()
    row = c.execute("""
        SELECT agent_id, persona, voice_id, cwd, session, backend, created_at,
               model, effort, mcp_servers, heartbeat_enabled,
               dreaming_enabled, dreaming_last_local_date, muted,
               custom_status, avatar_symbol, avatar_path, personality,
               voice_verbosity, archived_at
          FROM agents
         WHERE session = ? AND deleted_at IS NULL
    """, (session,)).fetchone()
    return dict(row) if row else None


def session_exists(session: str) -> bool:
    """True if ANY agent row uses this session — including soft-deleted
    ones. Used when minting a unique session id so a fresh agent never
    collides with (and resurrects) a previously deleted one."""
    row = conn().execute(
        "SELECT 1 FROM agents WHERE session = ? LIMIT 1", (session,),
    ).fetchone()
    return row is not None


def get_by_backend_session(backend_session_id: str) -> dict[str, Any] | None:
    """Find an agent via the current live runtime row's session UUID.

    The backend session may belong to Claude, Codex, or agy."""
    c = conn()
    row = c.execute("""
        SELECT a.agent_id, a.persona, a.voice_id, a.cwd, a.session,
               a.backend, a.created_at, a.model, a.effort, a.mcp_servers,
               a.heartbeat_enabled, a.dreaming_enabled,
               a.dreaming_last_local_date, a.muted, a.custom_status,
               a.avatar_symbol, a.avatar_path, a.personality,
               a.voice_verbosity, a.archived_at
          FROM agents a
          JOIN runtimes r ON r.agent_id = a.agent_id
         WHERE r.backend_session_id = ?
           AND r.ended_at IS NULL
           AND a.deleted_at IS NULL
         ORDER BY r.started_at DESC
         LIMIT 1
    """, (backend_session_id,)).fetchone()
    return dict(row) if row else None


def resolve_for_hook(*, backend_session_id: str | None,
                     session: str | None) -> dict[str, Any] | None:
    """Find the agent that fired a hook.

    Order:
      1. backend_session_id from the hook payload — always reliable, comes from
         Claude Code directly.
      2. session — identity hint for the first prompt of a new agent
         (no backend_session_id in the live runtime row yet).
    """
    if backend_session_id:
        a = get_by_backend_session(backend_session_id)
        if a:
            return a
    if session:
        return get_by_session(session)
    return None


def get_by_agent_id(agent_id: str) -> dict[str, Any] | None:
    c = conn()
    row = c.execute("""
        SELECT agent_id, persona, voice_id, cwd, session, backend, created_at,
               model, effort, mcp_servers, heartbeat_enabled,
               dreaming_enabled, dreaming_last_local_date, muted,
               custom_status, avatar_symbol, avatar_path, personality,
               voice_verbosity, archived_at
          FROM agents
         WHERE agent_id = ? AND deleted_at IS NULL
    """, (agent_id,)).fetchone()
    return dict(row) if row else None


def favorite_paths(limit: int = 5) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 5), 20))
    rows = conn().execute("""
        SELECT path, use_count, first_used_at, last_used_at
          FROM path_usage
         ORDER BY use_count DESC, last_used_at DESC, path ASC
         LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---- write paths -------------------------------------------------------

def create_agent(*, persona: str, voice_id: str, cwd: str,
                 session: str, backend: str = AgentBackend.CLAUDE,
                 model: str = "", effort: str = "") -> str:
    """Insert or resurrect an agent row. Returns the agent_id.

    If a soft-deleted row exists with the same `session`, it's
    resurrected (deleted_at cleared, fields refreshed) and that row's
    agent_id is reused. Hard-deleting isn't an option — the agent_id
    is referenced by FKs in state_log / runtimes / turns / clips, and
    a DELETE would fail with FOREIGN KEY constraint failed. Resurrection
    keeps the agent's history without forcing the user to invent a new
    session name.

    Raises sqlite3.IntegrityError only if a LIVE agent already owns the
    name — caller should relaunch or pick a different session.
    """
    c = conn()
    ghost = c.execute(
        "SELECT agent_id FROM agents "
        "WHERE session = ? AND deleted_at IS NOT NULL",
        (session,),
    ).fetchone()
    if ghost is not None:
        agent_id = ghost["agent_id"]
        c.execute("""
            UPDATE agents SET deleted_at = NULL, persona = ?, voice_id = ?,
                   cwd = ?, backend = ?, created_at = ?, model = ?, effort = ?,
                   custom_status = ''
             WHERE agent_id = ?
        """, (persona, voice_id, cwd, backend, now_ms(), model, effort, agent_id))
        record_state(agent_id, AgentState.SPAWNED,
                     {"session": session, "resurrected": True})
        return agent_id

    agent_id = _new_agent_id()
    c.execute("""
        INSERT INTO agents (agent_id, persona, voice_id, cwd, session, backend,
                            created_at, model, effort)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (agent_id, persona, voice_id, cwd, session, backend, now_ms(),
          model, effort))
    record_state(agent_id, AgentState.SPAWNED, {"session": session})
    return agent_id


def update_voice(agent_id: str, voice_id: str) -> None:
    conn().execute(
        "UPDATE agents SET voice_id = ? WHERE agent_id = ?",
        (voice_id, agent_id),
    )


def set_custom_status(agent_id: str, status: str | None) -> None:
    """Persist free-text agent status shown while the agent is not busy."""
    conn().execute(
        "UPDATE agents SET custom_status = ? WHERE agent_id = ?",
        ((status or "").strip(), agent_id),
    )


def set_archived(agent_id: str, archived: bool) -> None:
    conn().execute(
        "UPDATE agents SET archived_at=? WHERE agent_id=? AND deleted_at IS NULL",
        (now_ms() if archived else None, agent_id),
    )


def update_agent(agent_id: str, *, persona: str | None = None,
                 voice_id: str | None = None, cwd: str | None = None,
                 backend: str | None = None, model: str | None = None,
                 effort: str | None = None,
                 mcp_servers: str | None = None,
                 heartbeat_enabled: bool | int | None = None,
                 dreaming_enabled: bool | int | None = None,
                 muted: bool | int | None = None,
                 avatar_symbol: str | None = None,
                 personality: str | None = None,
                 voice_verbosity: int | None = None,
                 avatar_path: str | None = None,
                 dreaming_last_local_date: str | None = None) -> None:
    """Update mutable agent metadata without changing its stable agent_id.

    `model` / `effort` / `mcp_servers` are per-agent overrides read fresh on the
    next turn dispatch, so updating them live re-tunes a running agent without a
    relaunch. `mcp_servers` is a JSON array of MCP server names."""
    fields: list[str] = []
    values: list[Any] = []
    if persona is not None:
        fields.append("persona = ?")
        values.append(persona)
    if voice_id is not None:
        fields.append("voice_id = ?")
        values.append(voice_id)
    if cwd is not None:
        fields.append("cwd = ?")
        values.append(cwd)
    if backend is not None:
        fields.append("backend = ?")
        values.append(backend)
    if model is not None:
        fields.append("model = ?")
        values.append(model)
    if effort is not None:
        fields.append("effort = ?")
        values.append(effort)
    if mcp_servers is not None:
        fields.append("mcp_servers = ?")
        values.append(mcp_servers)
    if heartbeat_enabled is not None:
        fields.append("heartbeat_enabled = ?")
        values.append(1 if bool(heartbeat_enabled) else 0)
    if dreaming_enabled is not None:
        fields.append("dreaming_enabled = ?")
        values.append(1 if bool(dreaming_enabled) else 0)
    if muted is not None:
        fields.append("muted = ?")
        values.append(1 if bool(muted) else 0)
    if avatar_symbol is not None:
        fields.append("avatar_symbol = ?")
        values.append(avatar_symbol)
    if personality is not None:
        fields.append("personality = ?")
        values.append(personality)
    if voice_verbosity is not None:
        fields.append("voice_verbosity = ?")
        values.append(voice_verbosity_lib.clamp(voice_verbosity))
    if avatar_path is not None:
        fields.append("avatar_path = ?")
        values.append(avatar_path)
    if dreaming_last_local_date is not None:
        fields.append("dreaming_last_local_date = ?")
        values.append(dreaming_last_local_date)
    if not fields:
        return
    values.append(agent_id)
    conn().execute(
        f"UPDATE agents SET {', '.join(fields)} WHERE agent_id = ?",
        tuple(values),
    )


def record_path_usage(path: str) -> None:
    path = str(path or "").strip()
    if not path:
        return
    ts = now_ms()
    conn().execute("""
        INSERT INTO path_usage
            (path, use_count, first_used_at, last_used_at)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            use_count = use_count + 1,
            last_used_at = excluded.last_used_at
    """, (path, ts, ts))


def soft_delete(agent_id: str) -> None:
    c = conn()
    c.execute("UPDATE agents SET deleted_at = ? WHERE agent_id = ?",
              (now_ms(), agent_id))
    from . import task_plans
    task_plans.cancel_for_agent(agent_id)
    from . import artifacts
    artifacts.cancel_for_agent(agent_id)
    end_current_runtime(agent_id)
    record_state(agent_id, AgentState.STOPPED, {"reason": "deleted"})


# ---- runtime rows ------------------------------------------------------
#
# A "runtime" is one continuous agent process lifetime. When
# the user relaunches or forks, the agent_id stays the same but a new
# runtime row opens.

def start_runtime(agent_id: str, session: str) -> int:
    """End any previous live runtime for this agent, open a fresh one."""
    end_current_runtime(agent_id)
    cur = conn().execute("""
        INSERT INTO runtimes (agent_id, session, started_at)
        VALUES (?, ?, ?)
    """, (agent_id, session, now_ms()))
    return int(cur.lastrowid or 0)


def end_current_runtime(agent_id: str) -> None:
    conn().execute("""
        UPDATE runtimes SET ended_at = ?
         WHERE agent_id = ? AND ended_at IS NULL
    """, (now_ms(), agent_id))


class SessionAlreadyBound(Exception):
    """Another live runtime already owns this backend_session_id UUID.

    The partial unique index on runtimes(backend_session_id WHERE
    ended_at IS NULL) refuses to let two agents share a UUID — that
    would mean they're appending to the same conversation file. When
    bind_backend_session hits this, it surfaces the conflict by name instead
    of letting the IntegrityError leak with a cryptic message.
    """
    def __init__(self, agent_id: str, backend_session_id: str, owner_agent_id: str):
        self.agent_id = agent_id
        self.backend_session_id = backend_session_id
        self.owner_agent_id = owner_agent_id
        super().__init__(
            f"backend_session_id {backend_session_id} is already bound to "
            f"agent {owner_agent_id}; refusing to bind it to {agent_id}"
        )


def bind_backend_session(agent_id: str, backend_session_id: str) -> None:
    """Canonical setter for a runtime row's backend_session_id UUID.

    Every code path that learns an agent's claude session UUID — the
    UserPromptSubmit hook, /send's pre-mint, clarp's on_session_init
    callback, and so on — must go through this function. It enforces
    the one-agent-per-live-UUID invariant: if another live runtime
    already owns this UUID, raises SessionAlreadyBound instead of
    silently overwriting.

    Idempotent: setting the same UUID twice is a no-op.

    Opens a live runtime row if there isn't one yet.

    Raises:
        SessionAlreadyBound: another live runtime owns this UUID.
        ValueError: agent_id is empty / unknown.
    """
    if not agent_id:
        raise ValueError("bind_backend_session requires agent_id")
    if not backend_session_id:
        raise ValueError("bind_backend_session requires non-empty backend_session_id")

    c = conn()
    # Ensure a live runtime row exists. record_state and other writers
    # need one anyway.
    if current_runtime_id(agent_id) is None:
        agent = get_by_agent_id(agent_id)
        if not agent:
            raise ValueError(f"bind_backend_session: unknown agent_id {agent_id}")
        start_runtime(agent_id, agent["session"])

    # Idempotent: if the live row already carries this UUID, nothing to do.
    existing = live_backend_session(agent_id)
    if existing == backend_session_id:
        return

    # Check for cross-binding BEFORE attempting the UPDATE. The partial
    # unique index would catch it too, but a pre-check lets us raise a
    # named exception with the conflicting agent.
    owner = c.execute(
        """SELECT agent_id FROM runtimes
            WHERE backend_session_id = ? AND ended_at IS NULL
              AND agent_id != ?""",
        (backend_session_id, agent_id),
    ).fetchone()
    if owner is not None:
        raise SessionAlreadyBound(
            agent_id=agent_id,
            backend_session_id=backend_session_id,
            owner_agent_id=owner["agent_id"],
        )

    c.execute(
        """UPDATE runtimes SET backend_session_id = ?
            WHERE agent_id = ? AND ended_at IS NULL""",
        (backend_session_id, agent_id),
    )


def current_runtime_id(agent_id: str) -> int | None:
    row = conn().execute("""
        SELECT runtime_id FROM runtimes
         WHERE agent_id = ? AND ended_at IS NULL
         ORDER BY started_at DESC LIMIT 1
    """, (agent_id,)).fetchone()
    return int(row["runtime_id"]) if row else None


def live_backend_session(agent_id: str) -> str:
    """Most-recent live runtime's backend_session_id UUID for an agent, or ''
    if no claude session has been bound yet. /send uses this to pick
    `--resume <id>` vs `--continue` when dispatching to clarp."""
    row = conn().execute("""
        SELECT backend_session_id FROM runtimes
         WHERE agent_id = ? AND ended_at IS NULL
         ORDER BY started_at DESC LIMIT 1
    """, (agent_id,)).fetchone()
    return (row["backend_session_id"] or "") if row else ""


# ---- state log ---------------------------------------------------------

def record_state(agent_id: str, kind: str,
                 detail: dict | None = None) -> None:
    if not AgentState.is_valid(kind):
        raise ValueError(f"invalid agent state: {kind}")
    ts = now_ms()
    detail = _state_detail_with_origin(agent_id, kind, detail, ts)
    rt = current_runtime_id(agent_id)
    conn().execute("""
        INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail)
        VALUES (?, ?, ?, ?, ?)
    """, (agent_id, rt, ts, kind,
          json.dumps(detail) if detail else None))


def _state_detail_with_origin(agent_id: str, kind: str,
                              detail: dict | None, ts: int) -> dict | None:
    if detail and detail.get("origin"):
        return detail
    if kind not in {
        AgentState.THINKING,
        AgentState.TOOL,
        AgentState.COMPACTING,
        AgentState.DONE,
        AgentState.IDLE,
    }:
        return detail
    raw_detail = detail or {}
    backend_session_id = str(
        raw_detail.get("backend_session_id") or live_backend_session(agent_id) or "",
    )
    origin = ""
    if backend_session_id:
        try:
            from . import message_store
            origin = message_store.latest_turn_user_origin(
                agent_id=agent_id,
                backend_session_id=backend_session_id,
                done_ts=ts,
            )
        except Exception:
            origin = ""
    if not origin:
        origin = _recent_state_origin(agent_id, ts)
    if not origin:
        return detail
    enriched = dict(detail or {})
    enriched["origin"] = origin
    return enriched


def _recent_state_origin(agent_id: str, ts: int) -> str:
    """Carry turn origin from hook DONE into immediate tail state rows."""
    row = conn().execute(
        """SELECT ts, detail
             FROM state_log
            WHERE agent_id = ?
              AND ts <= ?
              AND detail IS NOT NULL
            ORDER BY ts DESC, state_id DESC
            LIMIT 1""",
        (agent_id, ts),
    ).fetchone()
    if row is None or ts - int(row["ts"] or 0) > 120_000:
        return ""
    try:
        payload = json.loads(row["detail"] or "{}")
    except json.JSONDecodeError:
        return ""
    return str(payload.get("origin") or "")


def latest_state(agent_id: str) -> dict[str, Any] | None:
    row = conn().execute("""
        SELECT kind, ts, detail FROM state_log
         WHERE agent_id = ?
         ORDER BY ts DESC, state_id DESC LIMIT 1
    """, (agent_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("detail"):
        try:
            d["detail"] = json.loads(d["detail"])
        except json.JSONDecodeError:
            d["detail"] = {}
    return d


def is_busy(agent_id: str) -> bool:
    s = latest_state(agent_id)
    return bool(s) and s["kind"] in AgentState.busy_states()


def last_activity(agent_id: str) -> int:
    """Epoch ms of the most recent real conversation message for this agent.

    Message recency comes from the message's semantic timestamp, not updated_at:
    opening a chat re-imports its transcript cache and must not make an old
    conversation look new. Operational state, audio clips, background jobs, and
    automated watcher turns belong in their own UI surfaces and must never move
    a chat's timestamp or ordering.
    """
    from .message_store import last_real_message_activity
    return last_real_message_activity(agent_id=agent_id)


def turn_started_at(agent_id: str) -> int:
    """Epoch ms of the current (or most recent) turn start.

    Defined as the first busy state event (THINKING / TOOL / COMPACTING)
    after the most recent turn-end event (DONE / IDLE / STOPPED). Drives
    the "thinking…" / "running X… 4s" banner — the client subtracts
    this from now() to show live elapsed time, just like Claude Code's
    CLI spinner.
    """
    row = conn().execute("""
        WITH last_end AS (
            SELECT MAX(ts) AS t FROM state_log
             WHERE agent_id = ?
               AND kind IN (?, ?, ?)
        )
        SELECT MIN(ts) AS t FROM state_log
         WHERE agent_id = ?
           AND kind IN (?, ?, ?)
           AND ts > COALESCE((SELECT t FROM last_end), 0)
    """, (agent_id, AgentState.DONE, AgentState.IDLE, AgentState.STOPPED,
          agent_id, AgentState.THINKING, AgentState.TOOL,
          AgentState.COMPACTING)).fetchone()
    return int(row["t"] or 0)


def last_turn_end(agent_id: str) -> int:
    """Epoch ms of the most recent turn-complete event for this agent.

    The dock/switcher unread badge uses this instead of last_activity
    so it fires only when a turn fully completes — not on every
    streamed chunk while the agent is still typing. Two signals count:
      * explicit DONE from the Stop hook (fires exactly once per turn at
        the natural completion boundary),
      * a busy→IDLE transition (written by the reconciler, the Codex
        runner, and turn-failure handling).
    """
    row = conn().execute("""
        WITH s AS (
            SELECT ts, kind,
                   LAG(kind) OVER (ORDER BY ts, state_id) AS prev_kind
              FROM state_log
             WHERE agent_id = ?
        )
        SELECT MAX(ts) AS t FROM s
         WHERE kind = ?
            OR (kind = ? AND prev_kind IN (?, ?))
    """, (agent_id, AgentState.DONE, AgentState.IDLE,
          AgentState.THINKING, AgentState.TOOL)).fetchone()
    return int(row["t"] or 0)


# ---- turns + clips -----------------------------------------------------

def open_turn(*, agent_id: str, source: str, trace_id: str,
              synthesize_audio: bool = True) -> int:
    if source not in TurnSource.valid():
        raise ValueError(f"invalid turn source: {source}")
    rt = current_runtime_id(agent_id)
    cur = conn().execute("""
        INSERT INTO turns
            (agent_id, runtime_id, source, trace_id, synthesize_audio, started_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (agent_id, rt, source, trace_id, 1 if synthesize_audio else 0, now_ms()))
    return int(cur.lastrowid or 0)


def close_turn(turn_id: int) -> None:
    conn().execute("UPDATE turns SET ended_at = ? WHERE turn_id = ?",
                   (now_ms(), turn_id))


def record_clip(*, agent_id: str, path: str, voice_id: str | None = None,
                trace_id: str | None = None, byte_count: int | None = None,
                turn_id: int | None = None,
                producer_status: str | None = None,
                status: str = ClipStatus.SYNTHESIZED) -> int | None:
    from .clip_store import record_clip as _record_clip
    return _record_clip(
        agent_id=agent_id, path=path, voice_id=voice_id, trace_id=trace_id,
        byte_count=byte_count, turn_id=turn_id, producer_status=producer_status,
        status=status, runtime_id=current_runtime_id,
    )


def mark_clip_producer_status(*, clip_id: int,
                              producer_status: str,
                              byte_count: int | None = None,
                              error: str | None = None) -> bool:
    from .clip_store import mark_clip_producer_status as _mark
    return _mark(
        clip_id=clip_id, producer_status=producer_status,
        byte_count=byte_count, error=error,
    )


def mark_clip_status(*, clip_id: int | None = None,
                     url: str | None = None,
                     status: str,
                     error: str | None = None) -> bool:
    from .clip_store import mark_clip_status as _mark
    return _mark(clip_id=clip_id, url=url, status=status, error=error)


# ---- focus + trace markers --------------------------------------------

_FOCUS_LOCK = threading.RLock()


@contextmanager
def focus_guard() -> Iterator[None]:
    """Serialize focus decisions with consumers that act on the result."""
    with _FOCUS_LOCK:
        yield

def set_focus(agent_id: str | None) -> None:
    with focus_guard():
        conn().execute("""
            INSERT INTO focus (singleton, agent_id, updated_at) VALUES (0, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET agent_id = excluded.agent_id,
                                                 updated_at = excluded.updated_at
        """, (agent_id, now_ms()))


def get_focus() -> str | None:
    with focus_guard():
        row = conn().execute(
            "SELECT agent_id FROM focus WHERE singleton = 0").fetchone()
        return row["agent_id"] if row else None


def get_focus_session() -> str:
    """Session id of the focused agent — the single source of truth for focus,
    resolved live from the focus agent_id. Consumers MUST call this rather than
    caching a copy: a cached focus drifts out of sync (that's what made the
    herald announce the focused agent). Returns '' if no/!unknown focus."""
    with focus_guard():
        fid = get_focus()
        a = get_by_agent_id(fid) if fid else None
        return (a or {}).get("session") or ""


TRACE_TTL_MS = 3600 * 1000


def set_trace(agent_id: str, trace_id: str) -> None:
    conn().execute("""
        INSERT INTO traces (agent_id, trace_id, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET trace_id = excluded.trace_id,
                                            updated_at = excluded.updated_at
    """, (agent_id, trace_id, now_ms()))


def set_trace_for_session(session: str, trace_id: str) -> None:
    agent = get_by_session(session)
    if agent:
        set_trace(agent["agent_id"], trace_id)


def backend_sessions_by_session() -> dict[str, str]:
    rows = conn().execute("""
        SELECT a.session, r.backend_session_id
          FROM agents a
          JOIN runtimes r ON r.agent_id = a.agent_id
         WHERE a.deleted_at IS NULL
           AND r.ended_at IS NULL
           AND r.backend_session_id IS NOT NULL
    """).fetchall()
    return {r["session"]: r["backend_session_id"] for r in rows}


def get_trace(agent_id: str) -> str | None:
    row = conn().execute(
        "SELECT trace_id, updated_at FROM traces WHERE agent_id = ?",
        (agent_id,)).fetchone()
    if not row:
        return None
    if now_ms() - int(row["updated_at"]) > TRACE_TTL_MS:
        return None
    return row["trace_id"]


# ---- turn source -------------------------------------------------------
#
# Replaces the old per-session source marker files. We keep "source" + a
# timestamp on each open turn; "is the current turn local or pwa?" is
# answered by the most recent open turn within a fresh window.

TURN_FRESH_MS = 600 * 1000


def latest_turn_source(agent_id: str) -> str | None:
    row = conn().execute("""
        SELECT source, started_at FROM turns
         WHERE agent_id = ?
         ORDER BY started_at DESC LIMIT 1
    """, (agent_id,)).fetchone()
    if not row:
        return None
    if now_ms() - int(row["started_at"]) > TURN_FRESH_MS:
        return None
    return row["source"]


def active_turn_trace(agent_id: str) -> str:
    row = conn().execute(
        """SELECT trace_id FROM turns
             WHERE agent_id=? AND ended_at IS NULL
             ORDER BY turn_id DESC LIMIT 1""", (agent_id,),
    ).fetchone()
    return (row["trace_id"] or "") if row else ""


def latest_turn_synthesize_audio(agent_id: str) -> bool:
    row = conn().execute("""
        SELECT synthesize_audio, started_at FROM turns
         WHERE agent_id = ?
         ORDER BY started_at DESC, turn_id DESC LIMIT 1
    """, (agent_id,)).fetchone()
    if not row or now_ms() - int(row["started_at"]) > TURN_FRESH_MS:
        return True
    return bool(row["synthesize_audio"])


def enable_latest_turn_audio(agent_id: str) -> None:
    """Upgrade the active turn to speech; never downgrade a voice turn."""
    conn().execute("""
        UPDATE turns SET synthesize_audio = 1
         WHERE turn_id = (
            SELECT turn_id FROM turns WHERE agent_id = ?
             ORDER BY started_at DESC, turn_id DESC LIMIT 1
         )
    """, (agent_id,))


# ---- canonical wire events --------------------------------------------

def record_sse_event(event: dict[str, Any]) -> int:
    from .sse_store import record_sse_event as _record
    return _record(event)


def events_after(event_id: int, limit: int = 500) -> list[dict[str, Any]]:
    from .sse_store import events_after as _events_after
    return _events_after(event_id, limit)


def recent_events(window_ms: int, limit: int = 500) -> list[dict[str, Any]]:
    from .sse_store import recent_events as _recent_events
    return _recent_events(window_ms, limit)


# ---- canonical transcript message read model --------------------------

def store_transcript_turns(*, agent_id: str, backend_session_id: str,
                           source_file: str, turns: list[dict[str, Any]]
                           ) -> list[dict[str, Any]]:
    from .message_store import store_transcript_turns as _store
    return _store(
        agent_id=agent_id, backend_session_id=backend_session_id,
        source_file=source_file, turns=turns,
    )


def record_user_message(*, agent_id: str, backend_session_id: str,
                        client_msg_id: str, text: str,
                        origin: str = "user",
                        sender_agent_id: str | None = None,
                        prompt_admission_id: str = "",
                        trace_id: str = "",
                        ) -> dict[str, Any] | None:
    from .message_store import record_user_message as _record
    return _record(
        agent_id=agent_id, backend_session_id=backend_session_id,
        client_msg_id=client_msg_id, text=text,
        origin=origin, sender_agent_id=sender_agent_id,
        prompt_admission_id=prompt_admission_id, trace_id=trace_id,
    )


def upsert_live_assistant_message(*, agent_id: str, backend_session_id: str,
                                  trace_id: str = "", text: str
                                  ) -> dict[str, Any] | None:
    from .message_store import upsert_live_assistant_message as _upsert
    return _upsert(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        trace_id=trace_id,
        text=text,
    )


def delete_live_assistant_message(*, agent_id: str, backend_session_id: str,
                                  trace_id: str = "") -> bool:
    from .message_store import delete_live_assistant_message as _delete
    return _delete(
        agent_id=agent_id, backend_session_id=backend_session_id,
        trace_id=trace_id,
    )


def finalize_live_assistant_message(*, agent_id: str, backend_session_id: str,
                                    trace_id: str, text: str
                                    ) -> dict[str, Any] | None:
    from .message_store import finalize_live_assistant_message as _finalize
    return _finalize(
        agent_id=agent_id, backend_session_id=backend_session_id,
        trace_id=trace_id, text=text)


def capture_assistant_state(*, agent_id: str,
                            backend_session_id: str) -> dict[str, Any]:
    from .message_store import capture_assistant_state as _capture
    return _capture(agent_id=agent_id, backend_session_id=backend_session_id)


def begin_agy_assistant_turn(*, agent_id: str, backend_session_id: str,
                             trace_id: str,
                             observed_assistant_count: int
                             ) -> dict[str, Any] | None:
    from .message_store import begin_agy_assistant_turn as _begin
    return _begin(
        agent_id=agent_id, backend_session_id=backend_session_id,
        trace_id=trace_id,
        observed_assistant_count=observed_assistant_count)


def commit_agy_assistant_turn(*, agent_id: str, backend_session_id: str,
                              trace_id: str, snapshot: dict[str, Any],
                              terminal_status: str, text: str = ""
                              ) -> dict[str, Any] | None:
    from .message_store import commit_agy_assistant_turn as _commit
    return _commit(
        agent_id=agent_id, backend_session_id=backend_session_id,
        trace_id=trace_id, snapshot=snapshot,
        terminal_status=terminal_status, text=text)


def apply_final_assistant_side_effects(*, agent_id: str,
                                       backend_session_id: str,
                                       trace_id: str,
                                       row: dict[str, Any]) -> None:
    from .message_store import apply_final_assistant_side_effects as _apply
    _apply(agent_id=agent_id, backend_session_id=backend_session_id,
           trace_id=trace_id, row=row)


def restore_assistant_state(*, agent_id: str, backend_session_id: str,
                            trace_id: str, snapshot: dict[str, Any]) -> bool:
    from .message_store import restore_assistant_state as _restore
    return _restore(
        agent_id=agent_id, backend_session_id=backend_session_id,
        trace_id=trace_id, snapshot=snapshot)


def list_messages(*, agent_id: str, backend_session_id: str = "",
                  after_revision: int = 0,
                  before_message_id: str = "",
                  limit: int = 100,
                  include_automated: bool = True) -> list[dict[str, Any]]:
    from .message_store import list_messages as _list
    return _list(
        agent_id=agent_id, backend_session_id=backend_session_id,
        after_revision=after_revision, limit=limit,
        before_message_id=before_message_id,
        include_automated=include_automated,
    )


def latest_message_revision(*, agent_id: str,
                            backend_session_id: str = "") -> int:
    from .message_store import latest_revision
    return latest_revision(
        agent_id=agent_id, backend_session_id=backend_session_id,
    )


def conversation_requires_replace(*, agent_id: str,
                                  backend_session_id: str = "",
                                  after_revision: int = 0) -> bool:
    from .message_store import requires_replace
    return requires_replace(
        agent_id=agent_id, backend_session_id=backend_session_id,
        after_revision=after_revision,
    )


def session_dict() -> dict[str, dict[str, Any]]:
    """Return agents keyed by their user-facing session name."""
    out: dict[str, dict[str, Any]] = {}
    for a in list_agents():
        out[a["session"]] = {
            "name": a["persona"],
            "voice_id": a["voice_id"],
            "cwd": a["cwd"],
            "agent_id": a["agent_id"],
            # Carry session inline so callers do not need to recover it from
            # the map key.
            "session": a["session"],
            "persona": a["persona"],
            "backend": a.get("backend") or AgentBackend.CLAUDE,
        }
    return out
