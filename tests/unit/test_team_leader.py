"""Phase 4: the autonomous team-leader tick decision.

pending_leader_ticks() is the pure decision: which leaders get an automated
check now, and why. The background scheduler just delivers what it returns.
"""
from __future__ import annotations

from lib import agents as agents_db
from lib import db
from lib import message_store
from lib import team_leader, team_store
from lib.protocol import AgentState


def _team_with_leader(tmp_path):
    leader = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")
    worker = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    team = team_store.create_team("Ops")
    team_store.add_member(team["team_id"], leader)
    team_store.add_member(team["team_id"], worker)
    team_store.set_leader(team["team_id"], leader)
    return team, leader, worker


def _activity(agent_id: str, *, now: float, age_sec: int,
              origin: str = "user") -> None:
    rec = message_store.record_user_message(
        agent_id=agent_id,
        backend_session_id=f"bs-{agent_id}",
        client_msg_id=f"{agent_id}-{origin}-{age_sec}",
        text=f"{origin} activity",
        origin=origin,
    )
    db.conn().execute(
        "UPDATE messages SET timestamp = '', updated_at = ? WHERE message_id = ?",
        (int((now - age_sec) * 1000), rec["id"]),
    )


def test_tick_when_a_member_is_stalled(tmp_path):
    _team, _leader, worker = _team_with_leader(tmp_path)
    agents_db.record_state(worker, AgentState.INTERRUPTED, {"reason": "overloaded"})

    ticks = team_leader.pending_leader_ticks()
    assert len(ticks) == 1
    assert ticks[0]["leader_session"] == "lena"
    assert "stalled" in ticks[0]["reason"]


def test_no_tick_when_team_nudging_is_disabled(tmp_path):
    team, _leader, worker = _team_with_leader(tmp_path)
    agents_db.record_state(worker, AgentState.INTERRUPTED, {"reason": "overloaded"})
    team_store.set_nudge_enabled(team["team_id"], False)

    assert team_leader.pending_leader_ticks() == []


def test_no_tick_when_team_is_calm(tmp_path):
    _team, _leader, worker = _team_with_leader(tmp_path)
    agents_db.record_state(worker, AgentState.IDLE)
    assert team_leader.pending_leader_ticks() == []


def test_no_tick_when_leader_is_busy(tmp_path):
    _team, leader, worker = _team_with_leader(tmp_path)
    agents_db.record_state(worker, AgentState.INTERRUPTED)
    agents_db.record_state(leader, AgentState.THINKING)  # leader mid-turn
    assert team_leader.pending_leader_ticks() == []


def test_no_tick_when_leader_has_recent_real_activity(tmp_path):
    _team, leader, worker = _team_with_leader(tmp_path)
    agents_db.record_state(worker, AgentState.INTERRUPTED)
    now = 10_000.0
    _activity(
        leader,
        now=now,
        age_sec=team_leader.DEFAULT_LEADER_TICK_QUIET_PERIOD_SEC - 1,
        origin="user",
    )

    assert team_leader.pending_leader_ticks(now=now) == []


def test_tick_after_leader_quiet_period(tmp_path):
    _team, leader, worker = _team_with_leader(tmp_path)
    agents_db.record_state(worker, AgentState.INTERRUPTED)
    now = 10_000.0
    _activity(
        leader,
        now=now,
        age_sec=team_leader.DEFAULT_LEADER_TICK_QUIET_PERIOD_SEC + 1,
        origin="user",
    )

    ticks = team_leader.pending_leader_ticks(now=now)
    assert len(ticks) == 1
    assert ticks[0]["leader_agent_id"] == leader


def test_automation_activity_does_not_block_leader_tick(tmp_path):
    _team, leader, worker = _team_with_leader(tmp_path)
    agents_db.record_state(worker, AgentState.INTERRUPTED)
    now = 10_000.0
    _activity(leader, now=now, age_sec=1, origin="agent")

    ticks = team_leader.pending_leader_ticks(now=now)
    assert len(ticks) == 1
    assert ticks[0]["leader_agent_id"] == leader


def test_tick_on_unread_team_activity(tmp_path):
    _team, _leader, worker = _team_with_leader(tmp_path)
    agents_db.record_state(worker, AgentState.IDLE)
    team_store.capture_assistant_message(
        agent_id=worker, source_message_id="m1",
        text="<team>finished the parser</team>")

    ticks = team_leader.pending_leader_ticks()
    assert len(ticks) == 1
    assert "new team activity" in ticks[0]["reason"]


def test_no_tick_without_a_leader(tmp_path):
    leader = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")
    worker = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    team = team_store.create_team("Ops")
    team_store.add_member(team["team_id"], leader)
    team_store.add_member(team["team_id"], worker)
    agents_db.record_state(worker, AgentState.INTERRUPTED)
    assert team_leader.pending_leader_ticks() == []  # no leader designated


def test_scheduler_run_once_delivers_ticks(tmp_path):
    _team, _leader, worker = _team_with_leader(tmp_path)
    agents_db.record_state(worker, AgentState.WAITING)
    sent: list[tuple[str, str]] = []
    sched = team_leader.TeamLeaderScheduler(
        send_tick=lambda session, text: sent.append((session, text)))

    delivered = sched.run_once()
    assert delivered == 1
    assert sent[0][0] == "lena"
    assert "[Automated team check]" in sent[0][1]


def test_one_leader_gets_one_consolidated_tick_for_multiple_teams(tmp_path):
    leader = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")
    for index in range(2):
        worker = agents_db.create_agent(
            persona=f"Worker {index}", voice_id="V", cwd=str(tmp_path),
            session=f"worker-{index}")
        team = team_store.create_team(f"Team {index}")
        team_store.add_member(team["team_id"], leader)
        team_store.add_member(team["team_id"], worker)
        team_store.set_leader(team["team_id"], leader)
        agents_db.record_state(worker, AgentState.INTERRUPTED)
    sent = []
    sched = team_leader.TeamLeaderScheduler(
        send_tick=lambda session, text: sent.append((session, text)))

    delivered = sched.run_once()

    assert delivered == 1
    assert [session for session, _text in sent] == ["lena"]




def test_leader_noop_is_suppressed_from_live_and_durable_messages(tmp_path):
    _team, leader, _worker = _team_with_leader(tmp_path)
    bsid = "leader-session"
    agents_db.open_turn(agent_id=leader, source="pwa", trace_id="trace-1")

    live = message_store.upsert_live_assistant_message(
        agent_id=leader,
        backend_session_id=bsid,
        trace_id="trace-1",
        text="LEADER_NOOP",
    )
    assert live is not None
    assert live["text"] == "Automated check: no action needed."
    assert [m["text"] for m in message_store.list_messages(
        agent_id=leader, backend_session_id=bsid)] == [
            "Automated check: no action needed.",
        ]

    bsid2 = "leader-session-2"
    message_store.store_transcript_turns(
        agent_id=leader,
        backend_session_id=bsid2,
        source_file="/tmp/transcript.jsonl",
        turns=[
            {"role": "user", "text": team_leader.TICK_PROMPT, "timestamp": "1"},
            {"role": "assistant", "text": "LEADER_NOOP", "timestamp": "2"},
        ],
    )
    visible = message_store.list_messages(agent_id=leader, backend_session_id=bsid2)
    assert [m["text"] for m in visible] == [
        "Automated leader check",
        "Leader check: no action needed.",
    ]
    assert all(m["automated"] for m in visible)


def test_leader_noop_with_substantive_content_is_kept():
    text = "LEADER_NOOP " + ("x" * (team_leader.LEADER_NOOP_ACK_MAX_CHARS + 1))
    skip, remaining = team_leader.strip_leader_noop(text)
    assert skip is False
    assert remaining.startswith("x")
