"""Autonomous team-leader loop.

A team can name one member as leader (see team_store.set_leader). This module
decides, each tick, which leaders should get an automated "team check" prompt —
and a background worker delivers it. The leader's per-turn brief
(team_store._leader_section) carries the live member states, so on each tick it
can assign work and unstick stalled teammates via the self-prompt skill.

Guardrails keep it bounded and out of the user's way:
  - Never tick a busy leader (it's mid-turn — possibly talking to the user).
  - Only tick when there's something to act on: a stalled teammate
    (interrupted/waiting) or unread team activity. Nothing to do → no tick,
    so an idle team costs nothing.
  - A coarse interval (default 10 min) caps frequency; the user can stop it any
    time by clearing the leader.
"""
from __future__ import annotations

import os
import re
import threading
import time

from . import agents as agents_db
from . import team_store
from .log import log, log_exception
from .protocol import AgentState

LEADER_NOOP = "LEADER_NOOP"
LEADER_NOOP_ACK_MAX_CHARS = 300
DEFAULT_LEADER_TICK_QUIET_PERIOD_SEC = 10 * 60

TICK_PROMPT = (
    "[Automated team check] Review your team's status (members and their current "
    "state are in your brief). Follow Leader Standing Orders v2: decide what "
    "matters, delegate execution to workers, track delegated runs, and capture "
    "any genuine user judgment calls. If a teammate is stalled, blocked, or "
    "waiting, prompt them with the next bounded task using the self-prompt skill "
    "with --from your own session so it comes from you. If there is no valuable "
    "action, keep it quiet: reply only with LEADER_NOOP. Do not implement the "
    "work yourself and do not duplicate work teammates are already doing."
)

_TOKEN_EDGE_RE = re.compile(
    rf"(?:^{re.escape(LEADER_NOOP)}\W*|\W*{re.escape(LEADER_NOOP)}\W*$)"
)


def contains_leader_noop(text: str) -> bool:
    return LEADER_NOOP in str(text or "")


def should_skip_leader_tick_prompt(text: str) -> bool:
    return str(text or "").strip() == TICK_PROMPT


def strip_leader_noop(text: str) -> tuple[bool, str]:
    """Suppress quiet leader no-op acknowledgements.

    Mirrors the HEARTBEAT_OK contract: a token at the start or end is removed.
    If only a small acknowledgement remains, the assistant message is hidden.
    A token embedded in substantive content is left alone.
    """
    raw = str(text or "")
    if LEADER_NOOP not in raw:
        return False, raw
    stripped = raw.strip()
    if not stripped:
        return True, ""
    if not stripped.startswith(LEADER_NOOP) and not stripped.endswith(LEADER_NOOP):
        return False, raw
    remaining = _TOKEN_EDGE_RE.sub("", stripped).strip()
    remaining = re.sub(r"\s+", " ", remaining)
    if len(remaining) <= LEADER_NOOP_ACK_MAX_CHARS:
        return True, ""
    return False, remaining


def record_leader_noop(agent_id: str) -> None:
    if agent_id:
        log("teamLeaderNoop", f"agent={agent_id}")


def pending_leader_ticks(*, now: float | None = None) -> list[dict]:
    """Which team leaders should get an autonomous check now, and why.

    Pure read over the DB (no side effects), so it's directly testable. Returns
    [{team_id, team_name, leader_agent_id, leader_session, reason}]."""
    now = time.time() if now is None else now
    out: list[dict] = []
    for team in team_store.list_teams():  # active teams only
        if not team.get("leader_enabled") or not team.get("nudge_enabled"):
            continue
        leader_id = (team.get("leader_agent_id") or "").strip()
        if not leader_id:
            continue
        # Mid-turn (or the user's actively prompting it) — don't preempt.
        if agents_db.is_busy(leader_id):
            continue
        recent = _recent_real_activity_reason(leader_id, now)
        if recent:
            log("teamLeaderSkip",
                f"{team['name']} leader={leader_id} reason={recent}")
            continue
        members = [m for m in team.get("member_agent_ids", []) if m != leader_id]
        if not members:
            continue
        stalled = [
            mid for mid in members
            if (agents_db.latest_state(mid) or {}).get("kind")
            in (AgentState.INTERRUPTED, AgentState.WAITING)
        ]
        digest, _ = team_store.pending_digest(leader_id)
        if not stalled and not digest:
            continue  # nothing to act on — skip, so an idle team costs nothing
        leader = agents_db.get_by_agent_id(leader_id) or {}
        session = (leader.get("session") or "").strip()
        if not session:
            continue
        reasons = []
        if stalled:
            reasons.append(f"{len(stalled)} stalled")
        if digest:
            reasons.append("new team activity")
        out.append({
            "team_id": team["team_id"],
            "team_name": team["name"],
            "leader_agent_id": leader_id,
            "leader_session": session,
            "reason": ", ".join(reasons),
        })
    return out


def _quiet_period_sec() -> int:
    raw = os.environ.get("CLAUDE_PWA_TEAM_LEADER_QUIET_PERIOD_SEC", "").strip()
    if not raw:
        return DEFAULT_LEADER_TICK_QUIET_PERIOD_SEC
    try:
        return max(0, int(raw))
    except ValueError:
        log("teamLeaderQuietPeriodInvalid", raw)
        return DEFAULT_LEADER_TICK_QUIET_PERIOD_SEC


def _recent_real_activity_reason(agent_id: str, now: float) -> str:
    quiet_period = _quiet_period_sec()
    if quiet_period <= 0:
        return ""
    try:
        from . import message_store
        last_ms = message_store.last_real_message_activity(agent_id=agent_id)
    except Exception as e:  # noqa: BLE001
        log_exception("teamLeaderQuietActivityFail", e, detail=agent_id)
        return "activity-unknown"
    if not last_ms:
        return ""
    age = now - (last_ms / 1000.0)
    if age < quiet_period:
        return f"recent-activity:{max(0, int(age))}s"
    return ""


class TeamLeaderScheduler:
    """Periodically deliver an automated team check to leaders that need one."""

    def __init__(self, *, send_tick, interval_sec: float = 600.0):
        # send_tick(leader_session: str, text: str) -> None
        self._send_tick = send_tick
        self.interval_sec = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="team-leader-scheduler",
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def run_once(self) -> int:
        ticks = pending_leader_ticks()
        for tick in ticks:
            try:
                current = team_store.get_team(tick["team_id"])
                if not current or current["archived_at"] is not None or not current["leader_enabled"] or not current["nudge_enabled"] or current["leader_agent_id"] != tick["leader_agent_id"]:
                    continue
                self._send_tick(tick["leader_session"], TICK_PROMPT)
                log("teamLeaderTick",
                    f"{tick['team_name']} -> {tick['leader_session']} "
                    f"({tick['reason']})")
            except Exception as e:  # noqa: BLE001
                log_exception("teamLeaderTickFail", e,
                              detail=str(tick.get("leader_session") or ""))
        return len(ticks)

    def _loop(self) -> None:
        # A short initial delay so a fresh boot settles before the first tick.
        if self._stop.wait(min(60.0, self.interval_sec)):
            return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                log_exception("teamLeaderSchedulerFail", e)
            if self._stop.wait(self.interval_sec):
                break
