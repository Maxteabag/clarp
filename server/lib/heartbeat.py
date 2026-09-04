"""Autonomous per-agent heartbeat loop.

Heartbeat is opt-in per agent. The scheduler periodically sends a lightweight
turn to idle enabled agents so they can review standing context and act only
when something useful needs attention.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import agents as agents_db
from . import backends, compaction, settings_store
from .db import conn, now_ms
from .log import log, log_exception
from .protocol import AgentState


HEARTBEAT_OK = "HEARTBEAT_OK"
HEARTBEAT_PROMPT = (
    "This is a continuity check, not a new task. Read HEARTBEAT.md if it exists "
    "(workspace context). Follow it strictly. Review the durable goal/task plan "
    "included below, current commitments, and queued team updates. If a pending "
    "or in-progress plan item can be advanced safely, continue it now; do not "
    "reply HEARTBEAT_OK merely because no new chat message arrived. Do not infer "
    "or repeat old tasks from prior chats. Do not restart completed or stale work. "
    "Audit your visible custom status and durable background jobs: clear a status "
    "that describes completed, failed, or cancelled work, and update any live job "
    "whose displayed state is stale. Do not clear genuine active work. If no current "
    "work can be advanced and nothing needs attention, reply HEARTBEAT_OK and "
    "take no action."
)
RESTART_HEARTBEAT_PREFIX = (
    "The Clarp server has just restarted. Your previous turn may have been "
    "interrupted. Re-establish the current workspace state and continue any "
    "unfinished active commitment without repeating completed work.\n\n"
)
INTERRUPTED_HEARTBEAT_PREFIX = (
    "Your previous turn was interrupted or paused by a system limit. "
    "Re-establish the current workspace state and continue any unfinished "
    "active commitment without repeating completed work.\n\n"
)
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30 * 60
DEFAULT_BACKOFF_CAP_SEC = 60 * 60  # 1 hour maximum heartbeat interval cap
MAX_INTERRUPTED_RETRY_SEC = 11 * 60 * 60  # 11 hours maximum retry duration for interrupted agents
MIN_WAKE_SPACING_SEC = 30
FLOOD_WINDOW_SEC = 60
FLOOD_THRESHOLD = 5
NOOP_BACKOFF_AFTER = 3
NOOP_BACKOFF_MAX_MULTIPLIER = 4
HEARTBEAT_ACK_MAX_CHARS = 300
BACKOFF_STRATEGIES = {"fixed", "linear", "exponential"}
KEY_INTERVAL_SEC = "heartbeat.interval_sec"
KEY_BACKOFF_STRATEGY = "heartbeat.backoff_strategy"
KEY_BACKOFF_CAP_SEC = "heartbeat.backoff_cap_sec"
KEY_DORMANT_AFTER_NOOPS = "heartbeat.dormant_after_noops"


@dataclass(frozen=True)
class HeartbeatSettings:
    interval_sec: int
    backoff_strategy: str
    backoff_cap_sec: int
    dormant_after_noops: int

    def as_dict(self) -> dict:
        return {
            "heartbeat_interval_sec": self.interval_sec,
            "heartbeat_backoff_strategy": self.backoff_strategy,
            "heartbeat_backoff_cap_sec": self.backoff_cap_sec,
            "heartbeat_dormant_after_noops": self.dormant_after_noops,
        }

_TOKEN_EDGE_RE = re.compile(
    rf"(?:^{re.escape(HEARTBEAT_OK)}\W*|\W*{re.escape(HEARTBEAT_OK)}\W*$)"
)


@dataclass
class _HeartbeatState:
    last_started: float = 0.0
    recent_starts: list[float] = field(default_factory=list)
    noop_streak: int = 0
    dormant: bool = False
    last_wake_signal_ms: int = 0


_STATE_LOCK = threading.Lock()
_STATE_BY_AGENT: dict[str, _HeartbeatState] = {}


def reset_for_tests() -> None:
    with _STATE_LOCK:
        _STATE_BY_AGENT.clear()


def _is_user_stopped(latest: dict[str, Any]) -> bool:
    detail = latest.get("detail") or {}
    if not isinstance(detail, dict):
        return False
    return bool(
        detail.get("source") == "user_stop"
        or detail.get("reason") == "interrupted"
    )


def _is_restart_interrupted(latest: dict) -> bool:
    """A turn the previous server process took down with it (issue #11).

    The restart heartbeat already tells the agent its turn may have been cut,
    so the system-limit wording would only be noise on top of it.
    """
    detail = latest.get("detail") or {}
    return isinstance(detail, dict) and detail.get("source") == "server_restart"


def heartbeat_prompt_text(agent: dict | None = None) -> str:
    """Return the bounded heartbeat prompt plus explicit durable plan state."""
    prefix = ""
    if agent:
        agent_id = str(agent.get("agent_id") or "").strip()
        if agent_id:
            latest = agents_db.latest_state(agent_id) or {}
            if (latest.get("kind") == AgentState.INTERRUPTED
                    and not _is_user_stopped(latest)
                    and not _is_restart_interrupted(latest)):
                prefix = INTERRUPTED_HEARTBEAT_PREFIX
    if not agent:
        return prefix + HEARTBEAT_PROMPT
    session = str(agent.get("session") or "").strip()
    if not session:
        return prefix + HEARTBEAT_PROMPT
    try:
        from . import task_plans

        plan = task_plans.active_for_session(session)
    except Exception as e:  # noqa: BLE001
        log_exception("heartbeatPlanLoadFail", e, detail=session)
        plan = None
    if not plan:
        return prefix + HEARTBEAT_PROMPT + "\n\nDurable plan: none active."
    lines = [f"Durable plan: {str(plan.get('title') or '')[:240]}"]
    shown = 0
    for item in plan.get("items") or []:
        for candidate in [item, *(item.get("subtasks") or [])]:
            status = str(candidate.get("status") or "pending")
            if status not in {"pending", "in_progress", "blocked"}:
                continue
            title = str(candidate.get("title") or "").strip()
            if not title:
                continue
            lines.append(f"- [{status}] {title[:300]}")
            shown += 1
            if shown >= 12:
                break
        if shown >= 12:
            break
    if shown == 0:
        lines.append("- No pending, in-progress, or blocked items.")
    return prefix + HEARTBEAT_PROMPT + "\n\n" + "\n".join(lines)


def restart_heartbeat_prompt_text(agent: dict) -> str:
    """Continuity prompt used once for every active runtime after boot."""
    return RESTART_HEARTBEAT_PREFIX + heartbeat_prompt_text(agent)


def restart_heartbeat_agents() -> list[dict]:
    """Agents whose persisted runtime was active when the server restarted.

    This is deliberately independent of the periodic heartbeat opt-in. A
    restart interrupts every server-owned backend process, so all active,
    non-archived sessions need one continuity turn even when periodic autonomy
    is disabled. Stopped/deleted/archived sessions remain untouched.
    """
    return [
        agent for agent in agents_db.list_agents()
        if not agent.get("archived_at")
        and agents_db.current_runtime_id(agent["agent_id"]) is not None
    ]


def heartbeat_enabled(agent: dict) -> bool:
    return bool(agent.get("heartbeat_enabled"))


def get_settings() -> HeartbeatSettings:
    """Return the policy owned by this Computer for every enabled Agent."""
    interval = settings_store.get_int(
        KEY_INTERVAL_SEC,
        default=_env_int(
            "CLAUDE_PWA_HEARTBEAT_INTERVAL_SEC",
            DEFAULT_HEARTBEAT_INTERVAL_SEC,
            minimum=1,
        ),
        minimum=1,
        maximum=86_400,
    )
    strategy = settings_store.get_text(
        KEY_BACKOFF_STRATEGY, default="exponential").strip()
    if strategy not in BACKOFF_STRATEGIES:
        strategy = "exponential"
    cap = settings_store.get_int(
        KEY_BACKOFF_CAP_SEC,
        default=_env_int(
            "CLAUDE_PWA_HEARTBEAT_BACKOFF_CAP_SEC", DEFAULT_BACKOFF_CAP_SEC, minimum=1),
        minimum=interval,
        maximum=86_400,
    )
    dormant_after = settings_store.get_int(
        KEY_DORMANT_AFTER_NOOPS,
        default=_env_int(
            "CLAUDE_PWA_HEARTBEAT_DORMANT_AFTER_NOOPS", 5, minimum=0),
        minimum=0,
        maximum=100,
    )
    return HeartbeatSettings(interval, strategy, cap, dormant_after)


def update_settings(data: dict) -> HeartbeatSettings:
    """Validate and atomically save this Computer's heartbeat policy."""
    if not isinstance(data, dict):
        raise ValueError("heartbeat settings must be an object")
    current = get_settings()

    def integer(name: str, current_value: int) -> int:
        value = data.get(name, current_value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        return value

    interval = integer("heartbeat_interval_sec", current.interval_sec)
    cap = integer("heartbeat_backoff_cap_sec", current.backoff_cap_sec)
    dormant_after = integer(
        "heartbeat_dormant_after_noops", current.dormant_after_noops)
    strategy = data.get("heartbeat_backoff_strategy", current.backoff_strategy)
    if not isinstance(strategy, str) or strategy.strip() not in BACKOFF_STRATEGIES:
        raise ValueError("invalid heartbeat_backoff_strategy")
    strategy = strategy.strip()
    if not 60 <= interval <= 86_400:
        raise ValueError("heartbeat_interval_sec must be 60...86400")
    if not interval <= cap <= 86_400:
        raise ValueError("heartbeat_backoff_cap_sec must be interval...86400")
    if not 0 <= dormant_after <= 100:
        raise ValueError("heartbeat_dormant_after_noops must be 0...100")

    database = conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        settings_store.set_int(KEY_INTERVAL_SEC, interval)
        settings_store.set_text(KEY_BACKOFF_STRATEGY, strategy)
        settings_store.set_int(KEY_BACKOFF_CAP_SEC, cap)
        settings_store.set_int(KEY_DORMANT_AFTER_NOOPS, dormant_after)
        database.execute("COMMIT")
    except Exception:
        database.execute("ROLLBACK")
        raise
    return get_settings()


def pending_heartbeat_agents(*, now: float | None = None) -> list[dict]:
    """Return enabled idle agents currently eligible for a heartbeat."""
    now = time.time() if now is None else now
    if _outside_active_hours(now):
        return []
    due: list[dict] = []
    for agent in agents_db.list_agents():
        if not heartbeat_enabled(agent):
            continue
        agent_id = agent["agent_id"]
        session = (agent.get("session") or "").strip()
        if not session:
            continue
        state = _state_for(agent_id)
        reason = _skip_reason(agent=agent, state=state, now=now)
        if reason:
            log("heartbeatSkip", f"agent={agent_id} session={session} reason={reason}")
            continue
        due.append(agent)
    return due


def record_heartbeat_noop(agent_id: str, *, is_interrupted: bool | None = None) -> None:
    if is_interrupted is not None:
        _record_heartbeat_noop(agent_id, is_interrupted=is_interrupted)
    else:
        _record_heartbeat_noop(agent_id)


def record_heartbeat_noop_once(
    agent_id: str,
    accounting_key: str,
    *,
    is_interrupted: bool | None = None,
) -> None:
    if not _claim_accounting_key(agent_id, accounting_key, "noop"):
        return
    if is_interrupted is not None:
        _record_heartbeat_noop(agent_id, is_interrupted=is_interrupted)
    else:
        _record_heartbeat_noop(agent_id)


def _record_heartbeat_noop(agent_id: str, *, is_interrupted: bool | None = None) -> None:
    if not agent_id:
        return
    if is_interrupted is None:
        latest = agents_db.latest_state(agent_id) or {}
        is_interrupted = latest.get("kind") == AgentState.INTERRUPTED and not _is_user_stopped(latest)
    with _STATE_LOCK:
        state = _state_for_locked(agent_id)
        state.noop_streak += 1
        if not is_interrupted:
            dormant_after = _dormant_after_noops()
            if dormant_after > 0 and state.noop_streak >= dormant_after:
                state.dormant = True
        snapshot = _state_snapshot(state)
    _persist_state_snapshot(agent_id, snapshot)
    log("heartbeatNoop",
        f"agent={agent_id} streak={snapshot.noop_streak} dormant={snapshot.dormant}")


def record_heartbeat_activity(agent_id: str) -> None:
    _record_heartbeat_activity(agent_id)


def record_heartbeat_activity_once(agent_id: str, accounting_key: str) -> None:
    if not _claim_accounting_key(agent_id, accounting_key, "activity"):
        return
    _record_heartbeat_activity(agent_id)


def _record_heartbeat_activity(agent_id: str) -> None:
    if not agent_id:
        return
    with _STATE_LOCK:
        state = _state_for_locked(agent_id)
        _reset_backoff_locked(state)
        snapshot = _state_snapshot(state)
    _persist_state_snapshot(agent_id, snapshot)


def _claim_accounting_key(agent_id: str, accounting_key: str,
                          outcome: str) -> bool:
    key = str(accounting_key or "").strip()
    if not agent_id or not key:
        return True
    try:
        cur = conn().execute(
            """INSERT OR IGNORE INTO heartbeat_accounting (
                   accounting_key, agent_id, outcome, counted_at
               ) VALUES (?, ?, ?, ?)""",
            (key, agent_id, outcome, now_ms()),
        )
        if cur.rowcount == 1:
            return True
        if outcome != "activity":
            return False
        cur = conn().execute(
            """UPDATE heartbeat_accounting
                  SET outcome = ?, counted_at = ?
                WHERE accounting_key = ? AND outcome = 'noop'""",
            (outcome, now_ms(), key),
        )
        return cur.rowcount == 1
    except Exception as e:  # noqa: BLE001
        log_exception("heartbeatAccountingFail", e, detail=agent_id)
        return True


def _reset_backoff_locked(state: _HeartbeatState) -> None:
    state.noop_streak = 0
    state.dormant = False


def should_skip_heartbeat_prompt(text: str) -> bool:
    clean = str(text or "").strip()
    return (
        clean == HEARTBEAT_PROMPT
        or clean.startswith(HEARTBEAT_PROMPT)
        or clean.startswith(RESTART_HEARTBEAT_PREFIX)
        or clean.startswith(INTERRUPTED_HEARTBEAT_PREFIX)
    )


def strip_heartbeat_ack(text: str) -> tuple[bool, str]:
    """Mirror OpenClaw's HEARTBEAT_OK response contract.

    A token at the start or end is stripped. If the remaining text is empty or
    compact enough to be an acknowledgement, the whole assistant message is
    suppressed. A token in the middle is left alone.
    """
    raw = str(text or "")
    if HEARTBEAT_OK not in raw:
        return False, raw
    stripped = raw.strip()
    if not stripped:
        return True, ""
    if not stripped.startswith(HEARTBEAT_OK) and not stripped.endswith(HEARTBEAT_OK):
        return False, raw
    remaining = _TOKEN_EDGE_RE.sub("", stripped).strip()
    remaining = re.sub(r"\s+", " ", remaining)
    if len(remaining) <= HEARTBEAT_ACK_MAX_CHARS:
        return True, ""
    return False, remaining


def is_neutral_heartbeat_status(text: str) -> bool:
    """True for short voice/status preambles before a HEARTBEAT_OK reply."""
    normalized = re.sub(r"\s+", " ", clean := str(text or "").strip()).lower()
    if not normalized:
        return True
    if HEARTBEAT_OK.lower() in normalized:
        return True
    if len(clean) > HEARTBEAT_ACK_MAX_CHARS:
        return False
    return (
        normalized.startswith("checking heartbeat")
        or normalized.startswith("heartbeat check")
        or normalized.startswith("heartbeat pass")
    )


def _state_for(agent_id: str) -> _HeartbeatState:
    with _STATE_LOCK:
        return _state_for_locked(agent_id)


def _state_for_locked(agent_id: str) -> _HeartbeatState:
    state = _STATE_BY_AGENT.get(agent_id)
    if state is None:
        state = _load_persisted_state(agent_id)
        _STATE_BY_AGENT[agent_id] = state
    return state


def _load_persisted_state(agent_id: str) -> _HeartbeatState:
    try:
        row = conn().execute(
            """SELECT last_started, noop_streak, dormant, last_wake_signal_ms
                 FROM heartbeat_state WHERE agent_id = ?""",
            (agent_id,),
        ).fetchone()
    except Exception as e:  # noqa: BLE001
        log_exception("heartbeatStateLoadFail", e, detail=agent_id)
        return _HeartbeatState()
    if row is None:
        return _HeartbeatState()
    return _HeartbeatState(
        last_started=float(row["last_started"] or 0.0),
        noop_streak=int(row["noop_streak"] or 0),
        dormant=bool(row["dormant"]),
        last_wake_signal_ms=int(row["last_wake_signal_ms"] or 0),
    )


def _state_snapshot(state: _HeartbeatState) -> _HeartbeatState:
    return _HeartbeatState(
        last_started=state.last_started,
        recent_starts=list(state.recent_starts),
        noop_streak=state.noop_streak,
        dormant=state.dormant,
        last_wake_signal_ms=state.last_wake_signal_ms,
    )


def _persist_state_snapshot(agent_id: str, state: _HeartbeatState) -> None:
    if not agent_id:
        return
    try:
        conn().execute(
            """INSERT INTO heartbeat_state (
                   agent_id, last_started, noop_streak, dormant,
                   last_wake_signal_ms, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                   last_started = excluded.last_started,
                   noop_streak = excluded.noop_streak,
                   dormant = excluded.dormant,
                   last_wake_signal_ms = excluded.last_wake_signal_ms,
                   updated_at = excluded.updated_at""",
            (
                agent_id,
                float(state.last_started or 0.0),
                int(state.noop_streak),
                1 if state.dormant else 0,
                int(state.last_wake_signal_ms or 0),
                now_ms(),
            ),
        )
    except Exception as e:  # noqa: BLE001
        log_exception("heartbeatStatePersistFail", e, detail=agent_id)


def _skip_reason(*, agent: dict, state: _HeartbeatState, now: float) -> str:
    agent_id = agent["agent_id"]
    session = agent.get("session") or ""
    _wake_from_external_signal(agent_id, state)
    latest = agents_db.latest_state(agent_id) or {}
    latest_kind = latest.get("kind")
    if latest_kind == AgentState.WAITING:
        return str(latest_kind)
    if latest_kind == AgentState.INTERRUPTED and _is_user_stopped(latest):
        return str(latest_kind)
    if agents_db.is_busy(agent_id):
        return "busy"
    if backends.active_handles(agent.get("backend"), agent_id):
        return "active"
    if compaction.is_compacting(session):
        return "compacting"
    recent = _recent_real_activity_reason(agent_id, now)
    if recent:
        return recent
    interrupted_at = (
        float(latest.get("ts") or 0.0) / 1000.0
        if latest_kind == AgentState.INTERRUPTED
        else 0.0
    )
    is_interrupted = latest_kind == AgentState.INTERRUPTED and not _is_user_stopped(latest)
    if is_interrupted and interrupted_at and (now - interrupted_at) >= MAX_INTERRUPTED_RETRY_SEC:
        with _STATE_LOCK:
            state.dormant = True
        return "dormant"
    last_event = max(state.last_started, interrupted_at)
    if last_event and now - last_event < MIN_WAKE_SPACING_SEC:
        return "min-spacing"
    interval = _effective_interval_sec(state, is_interrupted=is_interrupted)
    if last_event and now - last_event < interval:
        return "not-due"
    if _flooded(state, now):
        return "flood"
    if state.dormant:
        return "dormant"
    return ""


def _effective_interval_sec(
    state: _HeartbeatState,
    agent: dict | None = None,
    *,
    is_interrupted: bool = False,
) -> float:
    # Timing policy is Computer-owned; `agent` is accepted so callers can
    # pass the row they already hold.
    base = _base_interval_sec()
    cap = _backoff_cap_sec()
    if is_interrupted:
        return min(base, cap)
    if state.noop_streak <= 0:
        return base
    strategy = _backoff_strategy()
    if strategy == "fixed":
        interval = base
    elif strategy == "linear":
        interval = base * (state.noop_streak + 1)
    else:
        interval = base * (2 ** state.noop_streak)
    return min(cap, interval)


def agent_schedule(agent: dict, *, now: float | None = None) -> dict:
    """Authoritative profile projection for cadence, backoff, and dormancy."""
    current = time.time() if now is None else now
    agent_id = agent["agent_id"]
    current_state = _state_for(agent_id)
    _wake_from_external_signal(agent_id, current_state)
    state = _state_snapshot(_state_for(agent_id))
    latest = agents_db.latest_state(agent_id) or {}
    latest_kind = latest.get("kind")
    is_interrupted = latest_kind == AgentState.INTERRUPTED and not _is_user_stopped(latest)
    interrupted_at = (
        float(latest.get("ts") or 0.0) / 1000.0
        if latest_kind == AgentState.INTERRUPTED
        else 0.0
    )
    if is_interrupted and interrupted_at and (current - interrupted_at) >= MAX_INTERRUPTED_RETRY_SEC:
        with _STATE_LOCK:
            current_state.dormant = True
        state.dormant = True
    interval = int(_effective_interval_sec(state, is_interrupted=is_interrupted))
    next_at = None
    if agent.get("heartbeat_enabled") and not state.dormant:
        if latest_kind == AgentState.WAITING or (
            latest_kind == AgentState.INTERRUPTED and _is_user_stopped(latest)
        ):
            next_at = None
        else:
            last_event = max(state.last_started, interrupted_at)
            next_at = last_event + interval if last_event else current
            quiet_period = _quiet_period_sec()
            if quiet_period > 0:
                try:
                    last_activity_ms = _last_real_message_activity_ms(
                        agent_id)
                except Exception as e:  # noqa: BLE001
                    log_exception(
                        "heartbeatScheduleActivityFail", e,
                        detail=agent_id)
                    last_activity_ms = 0
                if last_activity_ms:
                    next_at = max(
                        next_at, last_activity_ms / 1000.0 + quiet_period)
            next_at = max(current, next_at)
    return {
        "enabled": bool(agent.get("heartbeat_enabled")),
        "last_started_at": int(state.last_started * 1000) if state.last_started else None,
        "next_heartbeat_at": int(next_at * 1000) if next_at is not None else None,
        "effective_interval_sec": interval,
        "noop_streak": state.noop_streak,
        "dormant": state.dormant,
    }


def _base_interval_sec(agent: dict | None = None) -> int:
    return get_settings().interval_sec


def _backoff_strategy(agent: dict | None = None) -> str:
    return get_settings().backoff_strategy


def _backoff_cap_sec(agent: dict | None = None) -> int:
    return get_settings().backoff_cap_sec


def _dormant_after_noops(agent: dict | None = None) -> int:
    return get_settings().dormant_after_noops


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        log("heartbeatEnvInvalid", f"{name}={raw}")
        return default


def _quiet_period_sec() -> int:
    return _env_int(
        "CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC",
        _base_interval_sec(),
        minimum=0,
    )


def _last_real_message_activity_ms(agent_id: str) -> int:
    from . import message_store
    return int(message_store.last_real_message_activity(agent_id=agent_id) or 0)


def _recent_real_activity_reason(agent_id: str, now: float) -> str:
    quiet_period = _quiet_period_sec()
    if quiet_period <= 0:
        return ""
    try:
        last_ms = _last_real_message_activity_ms(agent_id)
    except Exception as e:  # noqa: BLE001
        log_exception("heartbeatQuietActivityFail", e, detail=agent_id)
        return "activity-unknown"
    if not last_ms:
        return ""
    age = now - (last_ms / 1000.0)
    if age < quiet_period:
        return f"recent-activity:{max(0, int(age))}s"
    return ""


def _flooded(state: _HeartbeatState, now: float) -> bool:
    window_start = now - FLOOD_WINDOW_SEC
    state.recent_starts = [ts for ts in state.recent_starts if ts >= window_start]
    return len(state.recent_starts) >= FLOOD_THRESHOLD


def _record_run_start(agent_id: str, now: float) -> None:
    latest_signal = _latest_wake_signal_ms(agent_id)
    with _STATE_LOCK:
        state = _state_for_locked(agent_id)
        state.last_started = now
        state.last_wake_signal_ms = max(state.last_wake_signal_ms, latest_signal)
        state.recent_starts.append(now)
        if len(state.recent_starts) > FLOOD_THRESHOLD + 1:
            state.recent_starts = state.recent_starts[-(FLOOD_THRESHOLD + 1):]
        snapshot = _state_snapshot(state)
    _persist_state_snapshot(agent_id, snapshot)


def _wake_from_external_signal(agent_id: str, state: _HeartbeatState) -> None:
    latest = _latest_wake_signal_ms(agent_id)
    if latest <= 0 or latest <= state.last_wake_signal_ms:
        return
    snapshot: _HeartbeatState | None = None
    with _STATE_LOCK:
        current = _STATE_BY_AGENT.setdefault(agent_id, state)
        if latest <= current.last_wake_signal_ms:
            return
        current.last_wake_signal_ms = latest
        _reset_backoff_locked(current)
        snapshot = _state_snapshot(current)
    if snapshot is not None:
        _persist_state_snapshot(agent_id, snapshot)
    log("heartbeatWake", f"agent={agent_id} signal_ms={latest}")


def _latest_wake_signal_ms(agent_id: str) -> int:
    return max(
        _latest_user_signal_ms(agent_id),
        _latest_team_signal_ms(agent_id),
        _latest_promotion_signal_ms(),
    )


def _latest_user_signal_ms(agent_id: str) -> int:
    try:
        from . import message_store
        return int(message_store.last_real_message_activity(agent_id=agent_id) or 0)
    except Exception as e:  # noqa: BLE001
        log_exception("heartbeatWakeUserFail", e, detail=agent_id)
        return 0


def _latest_team_signal_ms(agent_id: str) -> int:
    try:
        from . import team_store
        return int(team_store.latest_activity_for_agent(agent_id) or 0)
    except Exception as e:  # noqa: BLE001
        log_exception("heartbeatWakeTeamFail", e, detail=agent_id)
        return 0


def _latest_promotion_signal_ms() -> int:
    try:
        from . import leader_memory
        return int(leader_memory.latest_promotion_activity() or 0)
    except Exception as e:  # noqa: BLE001
        log_exception("heartbeatWakePromotionFail", e)
        return 0


def _outside_active_hours(now: float) -> bool:
    """Optional OpenClaw-style quiet-hours gate.

    Set CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS to HH:MM-HH:MM. Omit it for 24/7,
    matching OpenClaw's default when activeHours is unset.
    """
    raw = os.environ.get("CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS", "").strip()
    if not raw:
        return False
    match = re.fullmatch(r"(\d\d:\d\d)-(\d\d:\d\d)", raw)
    if not match:
        log("heartbeatActiveHoursInvalid", raw)
        return False
    start = _minutes(match.group(1), allow_24=False)
    end = _minutes(match.group(2), allow_24=True)
    if start is None or end is None:
        log("heartbeatActiveHoursInvalid", raw)
        return False
    if start == end:
        return True
    current = datetime.fromtimestamp(now).hour * 60 + datetime.fromtimestamp(now).minute
    inside = start <= current < end if end > start else current >= start or current < end
    return not inside


def _minutes(raw: str, *, allow_24: bool) -> int | None:
    try:
        hour_s, minute_s = raw.split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
    except ValueError:
        return None
    if minute < 0 or minute > 59:
        return None
    if hour == 24 and minute == 0 and allow_24:
        return 24 * 60
    if hour < 0 or hour > 23:
        return None
    return hour * 60 + minute


class HeartbeatScheduler:
    """Periodically deliver heartbeat turns to enabled idle agents."""

    def __init__(
        self,
        *,
        send_heartbeat: Callable[[str, str], None],
        poll_interval_sec: float = 60.0,
        now: Callable[[], float] = time.time,
    ):
        self._send_heartbeat = send_heartbeat
        self.poll_interval_sec = poll_interval_sec
        self.now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._restart_recovery_done = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="agent-heartbeat-scheduler",
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def run_once(self) -> int:
        now = self.now()
        sent = 0
        for agent in pending_heartbeat_agents(now=now):
            agent_id = agent["agent_id"]
            session = agent["session"]
            try:
                _record_run_start(agent_id, now)
                self._send_heartbeat(session, heartbeat_prompt_text(agent))
                sent += 1
                log("heartbeatTick", f"agent={agent_id} session={session}")
            except Exception as e:  # noqa: BLE001
                log_exception("heartbeatTickFail", e, detail=session)
        return sent

    def run_restart_recovery_once(self) -> int:
        """Immediately wake each active runtime once for this server process."""
        if self._restart_recovery_done:
            return 0
        self._restart_recovery_done = True
        now = self.now()
        sent = 0
        for agent in restart_heartbeat_agents():
            agent_id = agent["agent_id"]
            session = agent["session"]
            try:
                _record_run_start(agent_id, now)
                self._send_heartbeat(
                    session, restart_heartbeat_prompt_text(agent))
                sent += 1
                log("heartbeatRestartTick",
                    f"agent={agent_id} session={session}")
            except Exception as e:  # noqa: BLE001
                log_exception("heartbeatRestartTickFail", e, detail=session)
        return sent

    def _loop(self) -> None:
        if self._stop.wait(min(60.0, self.poll_interval_sec)):
            return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                log_exception("heartbeatSchedulerFail", e)
            if self._stop.wait(self.poll_interval_sec):
                break
