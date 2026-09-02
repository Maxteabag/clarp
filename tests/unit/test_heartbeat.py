"""Heartbeat scheduler, prompt, and no-op suppression contracts."""
from __future__ import annotations

from lib import agents as agents_db
from lib import db
from lib import heartbeat, leader_memory, message_store, task_plans, team_store
from lib.protocol import AgentState


def _agent(session: str = "domi", *, enabled: bool = True) -> str:
    aid = agents_db.create_agent(
        persona=session.capitalize(), voice_id="v", cwd="/tmp", session=session)
    if enabled:
        agents_db.update_agent(aid, heartbeat_enabled=True)
    return aid


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


def _heartbeat_test_env(monkeypatch, *, base: int = 40, cap: int = 160,
                        dormant_after: int = 5) -> None:
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS", raising=False)
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_INTERVAL_SEC", str(base))
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_BACKOFF_CAP_SEC", str(cap))
    monkeypatch.setenv(
        "CLAUDE_PWA_HEARTBEAT_DORMANT_AFTER_NOOPS", str(dormant_after),
    )
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", "0")


def _scheduler(now_ref: dict[str, float], sent: list[tuple[str, str]]
               ) -> heartbeat.HeartbeatScheduler:
    return heartbeat.HeartbeatScheduler(
        send_heartbeat=lambda session, text: sent.append((session, text)),
        now=lambda: now_ref["now"],
    )


def test_prompt_mirrors_openclaw_contract():
    prompt = heartbeat.heartbeat_prompt_text()
    assert "Read HEARTBEAT.md if it exists (workspace context)." in prompt
    assert "Do not infer or repeat old tasks from prior chats." in prompt
    assert "HEARTBEAT_OK" in prompt
    assert "take no action" in prompt
    assert "Audit your visible custom status and durable background jobs" in prompt
    assert "Do not clear genuine active work" in prompt


def test_prompt_includes_current_durable_plan_and_requires_continuation():
    aid = _agent("theo")
    plan = task_plans.create(
        session="theo",
        title="Finish heartbeat settings",
        items=[
            {"id": "research", "title": "Research OpenClaw"},
            {"id": "implement", "title": "Implement configurable cadence"},
        ],
    )
    task_plans.update_item(
        next(item["item_id"] for item in plan["items"]
             if item["title"] == "Implement configurable cadence"),
        "in_progress",
    )

    prompt = heartbeat.heartbeat_prompt_text(agents_db.get_by_agent_id(aid))

    assert "Durable plan: Finish heartbeat settings" in prompt
    assert "[pending] Research OpenClaw" in prompt
    assert "[in_progress] Implement configurable cadence" in prompt
    assert "do not reply HEARTBEAT_OK merely because no new chat message arrived" in prompt


def test_computer_backoff_policy_applies_to_every_enabled_agent(monkeypatch):
    for name in (
        "CLAUDE_PWA_HEARTBEAT_INTERVAL_SEC",
        "CLAUDE_PWA_HEARTBEAT_BACKOFF_CAP_SEC",
        "CLAUDE_PWA_HEARTBEAT_DORMANT_AFTER_NOOPS",
    ):
        monkeypatch.delenv(name, raising=False)
    aid = _agent("cadence")
    state = heartbeat._state_for(aid)  # noqa: SLF001
    state.noop_streak = 3

    heartbeat.update_settings({
        "heartbeat_interval_sec": 600,
        "heartbeat_backoff_strategy": "linear",
        "heartbeat_backoff_cap_sec": 2100,
        "heartbeat_dormant_after_noops": 0,
    })
    agent = agents_db.get_by_agent_id(aid)
    assert heartbeat._effective_interval_sec(state, agent) == 2100  # noqa: SLF001
    assert heartbeat._dormant_after_noops(agent) == 0  # noqa: SLF001


def test_pending_agents_are_opt_in_and_idle(monkeypatch):
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS", raising=False)
    enabled = _agent("domi", enabled=True)
    _agent("mike", enabled=False)

    pending = heartbeat.pending_heartbeat_agents(now=1_000.0)
    assert [a["agent_id"] for a in pending] == [enabled]

    agents_db.record_state(enabled, AgentState.THINKING)
    assert heartbeat.pending_heartbeat_agents(now=2_000.0) == []


def test_pending_agents_skip_unavailable_terminal_states(monkeypatch):
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS", raising=False)
    waiting = _agent("waiting")
    interrupted = _agent("interrupted")
    healthy = _agent("healthy")

    agents_db.record_state(waiting, AgentState.WAITING, {"reason": "approval"})
    agents_db.record_state(interrupted, AgentState.INTERRUPTED, {
        "reason": "usage_limit",
        "message": "Usage limit reached",
    })

    pending = heartbeat.pending_heartbeat_agents(now=1_000.0)
    assert [a["agent_id"] for a in pending] == [healthy]


def test_recent_real_activity_skips_heartbeat(monkeypatch):
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS", raising=False)
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", raising=False)
    heartbeat.update_settings({"heartbeat_interval_sec": 600})
    aid = _agent("domi")
    now = 10_000.0
    _activity(
        aid,
        now=now,
        age_sec=599,
        origin="user",
    )

    assert heartbeat.pending_heartbeat_agents(now=now) == []


def test_quiet_real_activity_allows_heartbeat(monkeypatch):
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS", raising=False)
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", raising=False)
    heartbeat.update_settings({"heartbeat_interval_sec": 600})
    aid = _agent("domi")
    now = 10_000.0
    _activity(
        aid,
        now=now,
        age_sec=601,
        origin="user",
    )

    assert [a["agent_id"] for a in heartbeat.pending_heartbeat_agents(now=now)] == [aid]


def test_automation_activity_does_not_reset_heartbeat_quiet_timer(monkeypatch):
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS", raising=False)
    aid = _agent("domi")
    now = 10_000.0
    _activity(aid, now=now, age_sec=1, origin="heartbeat")

    assert [a["agent_id"] for a in heartbeat.pending_heartbeat_agents(now=now)] == [aid]


def test_scheduler_doubles_noop_interval_until_cap(monkeypatch):
    _heartbeat_test_env(monkeypatch, base=40, cap=160)
    aid = _agent("domi")
    sent: list[tuple[str, str]] = []
    now_ref = {"now": 1_000.0}
    scheduler = _scheduler(now_ref, sent)

    assert scheduler.run_once() == 1
    assert sent[0][0] == "domi"
    assert sent[0][1].startswith(heartbeat.HEARTBEAT_PROMPT)
    assert scheduler.run_once() == 0

    heartbeat.record_heartbeat_noop(aid)
    now_ref["now"] = 1_079.0
    assert scheduler.run_once() == 0
    now_ref["now"] = 1_080.0
    assert scheduler.run_once() == 1

    heartbeat.record_heartbeat_noop(aid)
    now_ref["now"] = 1_239.0
    assert scheduler.run_once() == 0
    now_ref["now"] = 1_240.0
    assert scheduler.run_once() == 1


def test_restart_recovery_wakes_every_active_runtime_once(monkeypatch):
    _heartbeat_test_env(monkeypatch)
    enabled = _agent("enabled", enabled=True)
    disabled = _agent("disabled", enabled=False)
    stopped = _agent("stopped", enabled=True)
    archived = _agent("archived", enabled=True)
    agents_db.start_runtime(enabled, "enabled")
    agents_db.start_runtime(disabled, "disabled")
    agents_db.start_runtime(archived, "archived")
    agents_db.set_archived(archived, True)
    # A created Agent without a live runtime is stopped/inactive.
    assert agents_db.current_runtime_id(stopped) is None
    sent: list[tuple[str, str]] = []
    now_ref = {"now": 1_000.0}
    scheduler = _scheduler(now_ref, sent)

    assert scheduler.run_restart_recovery_once() == 2
    assert [session for session, _ in sent] == ["enabled", "disabled"]
    assert all(text.startswith(heartbeat.RESTART_HEARTBEAT_PREFIX)
               for _, text in sent)
    assert scheduler.run_restart_recovery_once() == 0
    assert len(sent) == 2


def test_restart_recovery_bypasses_cadence_dormancy_and_recent_activity(monkeypatch):
    _heartbeat_test_env(monkeypatch, dormant_after=1)
    aid = _agent("resume", enabled=False)
    agents_db.start_runtime(aid, "resume")
    state = heartbeat._state_for(aid)  # noqa: SLF001
    state.last_started = 999.0
    state.dormant = True
    _activity(aid, now=1_000.0, age_sec=0, origin="user")
    sent: list[tuple[str, str]] = []
    scheduler = _scheduler({"now": 1_000.0}, sent)

    assert scheduler.run_restart_recovery_once() == 1
    assert sent[0][0] == "resume"
    assert heartbeat._state_for(aid).last_started == 1_000.0  # noqa: SLF001


def test_scheduler_flood_guard_still_applies(monkeypatch):
    _heartbeat_test_env(monkeypatch)
    aid = _agent("domi")
    now = 1_000.0
    for i in range(heartbeat.FLOOD_THRESHOLD):
        now += 1
        heartbeat._record_run_start(aid, now)  # noqa: SLF001 - pin flood guard.
    assert heartbeat.pending_heartbeat_agents(now=now + 1) == []


def test_heartbeat_goes_dormant_after_noop_threshold(monkeypatch):
    _heartbeat_test_env(monkeypatch, dormant_after=3)
    aid = _agent("domi")
    sent: list[tuple[str, str]] = []
    now_ref = {"now": 1_000.0}
    scheduler = _scheduler(now_ref, sent)

    assert scheduler.run_once() == 1
    for _ in range(3):
        heartbeat.record_heartbeat_noop(aid)
    now_ref["now"] = 2_000.0

    assert scheduler.run_once() == 0


def test_noop_streak_survives_scheduler_restart(monkeypatch):
    _heartbeat_test_env(monkeypatch, dormant_after=3)
    aid = _agent("domi")

    for _ in range(3):
        heartbeat.record_heartbeat_noop(aid)

    assert heartbeat._state_for(aid).noop_streak == 3  # noqa: SLF001
    assert heartbeat._state_for(aid).dormant is True  # noqa: SLF001

    heartbeat.reset_for_tests()
    reloaded = heartbeat._state_for(aid)  # noqa: SLF001

    assert reloaded.noop_streak == 3
    assert reloaded.dormant is True


def test_transcript_replay_accumulates_noop_streak_once_per_new_turn(monkeypatch):
    _heartbeat_test_env(monkeypatch, dormant_after=3)
    aid = _agent("domi")
    bsid = "backend-replay"
    turns = [
        {"role": "user", "text": "What is the plan?", "timestamp": "1"},
        {"role": "assistant", "text": "We should keep the scope tight.", "timestamp": "2"},
    ]
    noop_calls: list[str] = []
    activity_calls: list[str] = []
    original_noop = heartbeat._record_heartbeat_noop  # noqa: SLF001
    original_activity = heartbeat._record_heartbeat_activity  # noqa: SLF001

    def record_noop(agent_id: str) -> None:
        noop_calls.append(agent_id)
        original_noop(agent_id)

    def record_activity(agent_id: str) -> None:
        activity_calls.append(agent_id)
        original_activity(agent_id)

    monkeypatch.setattr(heartbeat, "_record_heartbeat_noop", record_noop)
    monkeypatch.setattr(heartbeat, "_record_heartbeat_activity", record_activity)

    for i in range(3):
        turns.extend([
            {
                "role": "user",
                "text": heartbeat.HEARTBEAT_PROMPT,
                "timestamp": str(3 + (i * 2)),
            },
            {
                "role": "assistant",
                "text": "Heartbeat check. File, goal, tree.",
                "timestamp": str(4 + (i * 3)),
            },
            {
                "role": "assistant",
                "text": "HEARTBEAT_OK",
                "timestamp": str(5 + (i * 3)),
            },
        ])
        message_store.store_transcript_turns(
            agent_id=aid,
            backend_session_id=bsid,
            source_file="/tmp/replayed-transcript.jsonl",
            turns=list(turns),
        )
        assert len(noop_calls) == i + 1
        assert heartbeat._state_for(aid).noop_streak == i + 1  # noqa: SLF001

    state = heartbeat._state_for(aid)  # noqa: SLF001
    assert state.noop_streak == 3
    assert state.dormant is True
    assert activity_calls == []

    message_store.store_transcript_turns(
        agent_id=aid,
        backend_session_id=bsid,
        source_file="/tmp/replayed-transcript.jsonl",
        turns=list(turns),
    )

    assert len(noop_calls) == 3
    assert heartbeat._state_for(aid).noop_streak == 3  # noqa: SLF001
    assert activity_calls == []

    replayed_with_changed_timestamps = [
        {**turn, "timestamp": f"{turn.get('timestamp')}-replayed"}
        for turn in turns
    ]
    message_store.store_transcript_turns(
        agent_id=aid,
        backend_session_id=bsid,
        source_file="/tmp/replayed-transcript.jsonl",
        turns=replayed_with_changed_timestamps,
    )

    assert len(noop_calls) == 3
    assert heartbeat._state_for(aid).noop_streak == 3  # noqa: SLF001
    assert activity_calls == []


def test_assistant_only_heartbeat_deltas_share_one_accounting_key(monkeypatch):
    """Incremental watcher reads must not count every streamed delta as a run."""
    _heartbeat_test_env(monkeypatch, dormant_after=3)
    aid = _agent("delta-heartbeat")
    bsid = "backend-delta-heartbeat"
    message_store.record_user_message(
        agent_id=aid,
        backend_session_id=bsid,
        client_msg_id="heartbeat-run-1",
        text=heartbeat.HEARTBEAT_PROMPT,
        origin="heartbeat",
    )

    message_store.store_transcript_turns(
        agent_id=aid,
        backend_session_id=bsid,
        source_file="/tmp/heartbeat-assistant-only-deltas.jsonl",
        turns=[
            {
                "id": f"assistant-delta-{index}",
                "role": "assistant",
                "text": "HEARTBEAT_OK",
                "timestamp": str(index),
            }
            for index in range(10)
        ],
    )

    state = heartbeat._state_for(aid)  # noqa: SLF001
    assert state.noop_streak == 1
    assert state.dormant is False


def test_user_turn_wakes_dormant_heartbeat_at_base_interval(monkeypatch):
    _heartbeat_test_env(monkeypatch, base=40, cap=160, dormant_after=3)
    aid = _agent("domi")
    sent: list[tuple[str, str]] = []
    now_ref = {"now": 1_000.0}
    scheduler = _scheduler(now_ref, sent)

    assert scheduler.run_once() == 1
    for _ in range(3):
        heartbeat.record_heartbeat_noop(aid)
    _activity(aid, now=1_001.0, age_sec=0, origin="user")
    now_ref["now"] = 1_039.0
    assert scheduler.run_once() == 0
    now_ref["now"] = 1_040.0

    assert scheduler.run_once() == 1


def test_team_action_wakes_dormant_heartbeat(monkeypatch):
    _heartbeat_test_env(monkeypatch, dormant_after=3)
    aid = _agent("domi")
    teammate = _agent("lena")
    team = team_store.create_team("iOS Development")
    assert team_store.add_member(team["team_id"], aid)
    assert team_store.add_member(team["team_id"], teammate)
    sent: list[tuple[str, str]] = []
    now_ref = {"now": 1_000.0}
    scheduler = _scheduler(now_ref, sent)

    assert scheduler.run_once() == 2
    for _ in range(3):
        heartbeat.record_heartbeat_noop(aid)
    assert team_store.capture_assistant_message(
        agent_id=teammate,
        source_message_id="msg-team-wake",
        text="<team>New delegation landed.</team>",
    ) == 1
    now_ref["now"] = 1_040.0

    due = heartbeat.pending_heartbeat_agents(now=now_ref["now"])
    assert aid in [agent["agent_id"] for agent in due]


def test_peer_tool_chatter_does_not_wake_dormant_heartbeat(monkeypatch):
    _heartbeat_test_env(monkeypatch, dormant_after=3)
    aid = _agent("domi")
    teammate = _agent("lena")
    teammate_backend = f"bs-{teammate}"
    team = team_store.create_team("iOS Development")
    assert team_store.add_member(team["team_id"], aid)
    assert team_store.add_member(team["team_id"], teammate)
    sent: list[tuple[str, str]] = []
    now_ref = {"now": 1_000.0}
    scheduler = _scheduler(now_ref, sent)

    assert scheduler.run_once() == 2
    for _ in range(3):
        heartbeat.record_heartbeat_noop(aid)
    agents_db.record_state(teammate, AgentState.TOOL, {"tool": "pytest"})
    now_ref["now"] = 2_000.0
    assert aid not in [agent["agent_id"] for agent in heartbeat.pending_heartbeat_agents(
        now=now_ref["now"])]

    message_store.record_user_message(
        agent_id=teammate,
        backend_session_id=teammate_backend,
        client_msg_id="heartbeat-peer-turn",
        text=heartbeat.HEARTBEAT_PROMPT,
        origin="heartbeat",
    )
    agents_db.record_state(teammate, AgentState.IDLE, {
        "backend_session_id": teammate_backend,
        "trace_id": "peer-heartbeat-turn",
    })
    assert (agents_db.latest_state(teammate) or {})["detail"]["origin"] == "heartbeat"
    assert aid not in [agent["agent_id"] for agent in heartbeat.pending_heartbeat_agents(
        now=now_ref["now"])]

    agents_db.bind_backend_session(teammate, teammate_backend)
    agents_db.record_state(teammate, AgentState.IDLE)
    assert (agents_db.latest_state(teammate) or {})["detail"]["origin"] == "heartbeat"
    assert aid not in [agent["agent_id"] for agent in heartbeat.pending_heartbeat_agents(
        now=now_ref["now"])]

    agents_db.record_state(teammate, AgentState.DONE, {
        "backend_session_id": teammate_backend,
        "source": "stop_hook",
        "origin": "heartbeat",
    })
    agents_db.record_state(teammate, AgentState.IDLE)
    assert (agents_db.latest_state(teammate) or {})["detail"]["origin"] == "heartbeat"
    assert aid not in [agent["agent_id"] for agent in heartbeat.pending_heartbeat_agents(
        now=now_ref["now"])]

    agents_db.record_state(teammate, AgentState.THINKING, {"origin": "heartbeat"})
    assert aid not in [agent["agent_id"] for agent in heartbeat.pending_heartbeat_agents(
        now=now_ref["now"])]

    real = message_store.record_user_message(
        agent_id=teammate,
        backend_session_id=teammate_backend,
        client_msg_id="real-peer-turn",
        text="Actual teammate work",
        origin="user",
    )
    db.conn().execute(
        "UPDATE messages SET updated_at = updated_at + 1000 WHERE message_id = ?",
        (real["id"],),
    )
    agents_db.record_state(teammate, AgentState.THINKING, {"trace_id": "real-turn"})
    assert aid in [agent["agent_id"] for agent in heartbeat.pending_heartbeat_agents(
        now=now_ref["now"])]


def test_promotion_wakes_dormant_heartbeat(monkeypatch):
    _heartbeat_test_env(monkeypatch, dormant_after=3)
    aid = _agent("domi")
    sent: list[tuple[str, str]] = []
    now_ref = {"now": 1_000.0}
    scheduler = _scheduler(now_ref, sent)

    assert scheduler.run_once() == 1
    for _ in range(3):
        heartbeat.record_heartbeat_noop(aid)
    fact_id = leader_memory.upsert_user_value_fact(
        statement="Heartbeats should wake on promoted memory.",
        category="preference",
    )
    leader_memory.promote_user_value_fact(fact_id)
    now_ref["now"] = 1_040.0

    assert scheduler.run_once() == 1


def test_heartbeat_action_resets_backoff(monkeypatch):
    _heartbeat_test_env(monkeypatch, base=40, cap=160)
    aid = _agent("domi")
    sent: list[tuple[str, str]] = []
    now_ref = {"now": 1_000.0}
    scheduler = _scheduler(now_ref, sent)

    assert scheduler.run_once() == 1
    heartbeat.record_heartbeat_noop(aid)
    heartbeat.record_heartbeat_noop(aid)
    heartbeat.record_heartbeat_activity(aid)
    now_ref["now"] = 1_040.0

    assert scheduler.run_once() == 1


def test_agent_schedule_reports_effective_next_run(monkeypatch):
    _heartbeat_test_env(monkeypatch, base=40, cap=160)
    aid = _agent("schedule")
    agent = agents_db.get_by_agent_id(aid)
    state = heartbeat._state_for(aid)  # noqa: SLF001
    state.last_started = 1_000.0
    state.noop_streak = 2

    value = heartbeat.agent_schedule(agent, now=1_010.0)

    assert value["enabled"] is True
    assert value["effective_interval_sec"] == 160
    assert value["next_heartbeat_at"] == 1_160_000
    assert value["noop_streak"] == 2

    state.dormant = True
    assert heartbeat.agent_schedule(agent, now=1_010.0)["next_heartbeat_at"] is None


def test_agent_schedule_includes_configured_recent_activity_gate(monkeypatch):
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", raising=False)
    heartbeat.update_settings({"heartbeat_interval_sec": 600})
    aid = _agent("schedule-activity")
    agent = agents_db.get_by_agent_id(aid)
    state = heartbeat._state_for(aid)  # noqa: SLF001
    state.last_started = 9_000.0
    _activity(aid, now=10_000.0, age_sec=300, origin="user")

    value = heartbeat.agent_schedule(agent, now=10_000.0)

    assert heartbeat._quiet_period_sec() == 600  # noqa: SLF001
    assert value["next_heartbeat_at"] == 10_300_000
    assert heartbeat.pending_heartbeat_agents(now=10_299.0) == []
    assert [a["agent_id"] for a in heartbeat.pending_heartbeat_agents(
        now=10_301.0)] == [aid]


def test_agent_schedule_applies_activity_wake_before_projection(monkeypatch):
    monkeypatch.delenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", raising=False)
    heartbeat.update_settings({
        "heartbeat_interval_sec": 600,
        "heartbeat_backoff_strategy": "exponential",
        "heartbeat_backoff_cap_sec": 2400,
    })
    aid = _agent("schedule-wake")
    agent = agents_db.get_by_agent_id(aid)
    state = heartbeat._state_for(aid)  # noqa: SLF001
    state.last_started = 9_000.0
    state.noop_streak = 2
    state.dormant = True
    _activity(aid, now=10_000.0, age_sec=300, origin="user")

    value = heartbeat.agent_schedule(agent, now=10_000.0)

    assert value["dormant"] is False
    assert value["noop_streak"] == 0
    assert value["effective_interval_sec"] == 600
    assert value["next_heartbeat_at"] == 10_300_000


def test_active_hours_gate_skips_outside_window(monkeypatch):
    _agent("domi")
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS", "09:00-17:00")
    assert heartbeat.pending_heartbeat_agents(now=0) == []


def test_heartbeat_prompt_and_noop_are_tagged_in_messages():
    aid = _agent("domi")
    bsid = "backend-1"
    message_store.store_transcript_turns(
        agent_id=aid,
        backend_session_id=bsid,
        source_file="/tmp/transcript.jsonl",
        turns=[
            {"role": "user", "text": heartbeat.HEARTBEAT_PROMPT, "timestamp": "1"},
            {"role": "assistant", "text": "HEARTBEAT_OK", "timestamp": "2"},
        ],
    )
    visible = message_store.list_messages(agent_id=aid, backend_session_id=bsid)
    assert [m["text"] for m in visible] == [
        "Automated heartbeat check",
        "Heartbeat check: no action needed.",
    ]
    assert all(m["automated"] for m in visible)
    assert {m["automation_kind"] for m in visible} == {"heartbeat"}
    assert heartbeat._state_for(aid).noop_streak == 1  # noqa: SLF001

    message_store.store_transcript_turns(
        agent_id=aid,
        backend_session_id=bsid,
        source_file="/tmp/transcript.jsonl",
        turns=[
            {"role": "user", "text": heartbeat.HEARTBEAT_PROMPT, "timestamp": "1"},
            {"role": "assistant", "text": "I finished the local cleanup.", "timestamp": "3"},
        ],
    )
    visible = message_store.list_messages(agent_id=aid, backend_session_id=bsid)
    assert [m["text"] for m in visible] == [
        "Automated heartbeat check",
        "I finished the local cleanup.",
    ]
    assert [m["origin"] for m in visible] == ["heartbeat", "heartbeat"]
    assert heartbeat._state_for(aid).noop_streak == 0  # noqa: SLF001
