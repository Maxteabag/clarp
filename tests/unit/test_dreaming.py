"""Dreaming scheduler, timezone, prompt, and digest/no-op contracts."""
from __future__ import annotations

import time
import pathlib
from datetime import datetime, timezone

from lib import agents as agents_db
from lib import dreaming, location, message_store
from lib.protocol import AgentState


def _agent(session: str = "domi", *, enabled: bool = True) -> str:
    aid = agents_db.create_agent(
        persona=session.capitalize(), voice_id="v", cwd="/tmp", session=session)
    if enabled:
        agents_db.update_agent(aid, dreaming_enabled=True)
    return aid


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _wait_for(predicate, *, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_prompt_is_ideation_only_and_has_noop_marker():
    prompt = dreaming.dreaming_prompt_text()
    assert "Deep Dreaming contract" in prompt
    assert "grounded overnight investigation" in prompt
    assert "anti-promote already-fixed ideas" in prompt
    assert "Output altitude is a spectrum" in prompt
    assert "no shared-tree" in prompt
    assert str(dreaming.DREAM_PLANNED_ROUNDS) in prompt
    assert dreaming.DREAM_DIRECTION_COUNT == 3
    assert dreaming.DREAM_PLANNED_ROUNDS == 7


def test_dreaming_settings_default_clamp_and_stage_scaling():
    settings = dreaming.get_settings()
    assert settings.as_dict()["dreams_per_night"] == 1
    assert settings.direction_count == 3
    assert settings.planned_rounds == 7
    assert settings.target_token_budget == 70_000
    assert settings.stage_target_tokens()["seed"] == 10_000

    updated = dreaming.update_settings({
        "dreams_per_night": 99,
        "direction_count": 5,
        "target_token_budget": 140_000,
    })
    assert updated.dreams_per_night == 5
    assert updated.direction_count == 5
    assert updated.planned_rounds == 9
    assert updated.target_token_budget == 140_000
    assert updated.stage_target_tokens()["seed"] == 20_000
    assert updated.stage_target_tokens()["fanout"] == 20_000

    clamped = dreaming.update_settings({
        "dreams_per_night": 0,
        "direction_count": 99,
        "target_token_budget": 10,
    })
    assert clamped.dreams_per_night == 1
    assert clamped.direction_count == 8
    assert clamped.target_token_budget == 30_000


def test_pending_agents_are_opt_in_idle_and_inside_local_3am_window():
    enabled = _agent("domi", enabled=True)
    _agent("mike", enabled=False)

    def oslo(_session: str, _now: float) -> dreaming.ResolvedTimeZone:
        return dreaming.ResolvedTimeZone(
            tz=timezone.utc, name="UTC", source="test")

    pending = dreaming.pending_dreaming_agents(
        now=_ts(2026, 6, 24, 3, 15), timezone_resolver=oslo)
    assert [a["agent_id"] for a in pending] == [enabled]
    assert pending[0]["dreaming_local_date"] == "2026-06-24"

    assert dreaming.pending_dreaming_agents(
        now=_ts(2026, 6, 24, 2, 59), timezone_resolver=oslo) == []
    assert dreaming.pending_dreaming_agents(
        now=_ts(2026, 6, 24, 4, 0), timezone_resolver=oslo) == []

    agents_db.record_state(enabled, AgentState.THINKING)
    assert dreaming.pending_dreaming_agents(
        now=_ts(2026, 6, 25, 3, 0), timezone_resolver=oslo) == []


def test_pending_agents_use_configured_dreams_per_night_counter():
    enabled = _agent("domi", enabled=True)
    dreaming.update_settings({"dreams_per_night": 2})

    def oslo(_session: str, _now: float) -> dreaming.ResolvedTimeZone:
        return dreaming.ResolvedTimeZone(
            tz=timezone.utc, name="UTC", source="test")

    agent = agents_db.get_by_agent_id(enabled)
    first = dreaming.create_dream_run(
        agent,
        local_date="2026-06-24",
        timezone_name="UTC",
        timezone_source="test",
    )
    dreaming.db.conn().execute(
        "UPDATE dream_runs SET status = 'completed' WHERE run_id = ?",
        (first["run_id"],),
    )

    pending = dreaming.pending_dreaming_agents(
        now=_ts(2026, 6, 24, 3, 15), timezone_resolver=oslo)
    assert [a["agent_id"] for a in pending] == [enabled]

    second = dreaming.create_dream_run(
        agent,
        local_date="2026-06-24",
        timezone_name="UTC",
        timezone_source="test",
    )
    dreaming.db.conn().execute(
        "UPDATE dream_runs SET status = 'completed' WHERE run_id = ?",
        (second["run_id"],),
    )

    assert dreaming.pending_dreaming_agents(
        now=_ts(2026, 6, 24, 3, 30), timezone_resolver=oslo) == []


def test_routine_heartbeat_or_leader_tick_busy_state_does_not_block_dreaming():
    enabled = _agent("arnold", enabled=True)
    agents_db.bind_backend_session(enabled, "live-session")
    message_store.record_user_message(
        agent_id=enabled,
        backend_session_id="live-session",
        text="Automated leader check",
        client_msg_id="trace-leader",
        origin="leader_tick",
    )
    agents_db.record_state(enabled, AgentState.TOOL, {
        "phase": "tool_started",
        "backend_session_id": "live-session",
    })

    def utc(_session: str, _now: float) -> dreaming.ResolvedTimeZone:
        return dreaming.ResolvedTimeZone(
            tz=timezone.utc, name="UTC", source="test")

    pending = dreaming.pending_dreaming_agents(
        now=_ts(2026, 6, 24, 3, 15), timezone_resolver=utc)
    assert [a["agent_id"] for a in pending] == [enabled]


def test_real_user_or_team_busy_state_still_blocks_dreaming():
    enabled = _agent("arnold", enabled=True)
    agents_db.bind_backend_session(enabled, "live-session")
    message_store.record_user_message(
        agent_id=enabled,
        backend_session_id="live-session",
        text="Domi reports a blocker to Arnold.",
        client_msg_id="trace-agent",
        origin="agent",
    )
    agents_db.record_state(enabled, AgentState.TOOL, {
        "phase": "tool_started",
        "backend_session_id": "live-session",
    })

    def utc(_session: str, _now: float) -> dreaming.ResolvedTimeZone:
        return dreaming.ResolvedTimeZone(
            tz=timezone.utc, name="UTC", source="test")

    assert dreaming.pending_dreaming_agents(
        now=_ts(2026, 6, 24, 3, 15), timezone_resolver=utc) == []


def test_scheduler_runs_deep_investigation_ledger_to_one_visible_digest():
    aid = _agent("domi")
    location.set_location("domi", 0, 0, ts=1)
    sent: list[tuple[str, str]] = []
    now = _ts(2026, 6, 24, 3, 5)

    scheduler = dreaming.DreamingScheduler(
        send_dream=lambda session, text: sent.append((session, text)),
        now=lambda: now,
        # The nightly path now varies its seeding recipe; this test is about
        # the round chain, so pin the control arm and let the strategy-specific
        # shapes be covered separately.
        recipe_chooser=lambda: ("control", "full"),
    )

    assert scheduler.run_once() == 1
    assert sent[0][0] == "domi"
    assert "stage=SEED" in sent[0][1]
    assert agents_db.get_by_session("domi")["dreaming_last_local_date"] == "2026-06-24"

    bsid = "backend-1"

    def complete_sent_round() -> None:
        run = dreaming.list_dream_runs(session="domi")[0]
        dream_round = next(r for r in run["rounds"] if r["status"] == "sent")
        prompt = sent[-1][1]
        if dream_round["stage"] == "synthesize":
            assistant = (
                "Dream Digest\n\n"
                "Ranked ideas:\n1. Make autonomy auditable.\n\n"
                f"DREAM_DIGEST_DONE run_id={run['run_id']} "
                f"round_id={dream_round['round_id']}"
            )
        else:
            marker = (
                f"DREAM_STAGE_OUTPUT run_id={run['run_id']} "
                f"round_id={dream_round['round_id']} "
                f"stage={dream_round['stage'].upper()}"
            )
            if dream_round["stage"] == "seed":
                slate = "\n".join(
                    f"D{i} [new]: Direction {i} with enough detail to parse"
                    for i in range(1, 4)
                )
                assistant = f"{marker}\n{slate}"
            else:
                assistant = (
                    f"{marker}\n"
                    "Evidence status: confirmed\n"
                    "Altitude: verified\n"
                    f"Evidence summary: Deep investigation output for {dream_round['stage']}."
                )
        message_store.store_transcript_turns(
            agent_id=aid,
            backend_session_id=bsid,
            source_file="/tmp/transcript.jsonl",
            turns=[
                {"role": "user", "text": prompt, "timestamp": str(len(sent) * 2)},
                {"role": "assistant", "text": assistant, "timestamp": str(len(sent) * 2 + 1)},
            ],
        )

    for _ in range(dreaming.DREAM_PLANNED_ROUNDS):
        complete_sent_round()
        run = dreaming.list_dream_runs(session="domi")[0]
        if run["status"] == "completed":
            break
        assert scheduler.run_once() == 1

    run = dreaming.list_dream_runs(session="domi")[0]
    assert run["status"] == "completed"
    assert run["completed_rounds"] == dreaming.DREAM_PLANNED_ROUNDS
    assert len(run["threads"]) == dreaming.DREAM_DIRECTION_COUNT
    assert len(run["rounds"]) == dreaming.DREAM_PLANNED_ROUNDS
    assert [r["stage"] for r in run["rounds"]].count("fanout") == dreaming.DREAM_DIRECTION_COUNT
    assert [r["stage"] for r in run["rounds"]].count("iterate") == dreaming.DREAM_ITERATION_THREAD_COUNT
    assert run["final_digest"] == "Dream Digest\n\nRanked ideas:\n1. Make autonomy auditable."
    assert "stage=SEED" in run["rounds"][0]["prompt"]
    assert "prompt_preview" in run["rounds"][0]
    assert "response_preview" in run["rounds"][0]
    visible = message_store.list_messages(agent_id=aid, backend_session_id=bsid)
    assert [m["text"] for m in visible] == [
        "Automated dreaming run",
        "Dream Digest\n\nRanked ideas:\n1. Make autonomy auditable.",
    ]
    assert visible[0]["automated"] is True
    assert visible[0]["automation_kind"] == "dreaming"


def test_custom_dream_settings_shape_new_runs_without_mutating_defaults():
    aid = _agent("domi")
    dreaming.update_settings({
        "direction_count": 5,
        "target_token_budget": 140_000,
    })
    run = dreaming.create_dream_run(
        agents_db.get_by_session("domi"),
        local_date="2026-06-24",
        timezone_name="UTC",
        timezone_source="test",
    )
    assert run["planned_directions"] == 5
    assert run["planned_rounds"] == 9
    assert run["target_tokens"] == 140_000

    seed = dreaming.list_dream_runs(session="domi")[0]["rounds"][0]
    assert seed["target_tokens"] == 20_000
    assert "exactly 5 active candidate directions" in seed["prompt"]
    dreaming.record_round_output(
        agent_id=aid,
        run_id=run["run_id"],
        round_id=seed["round_id"],
        stage="seed",
        response="\n".join(
            f"D{i} [new]: Direction {i} with enough detail to parse"
            for i in range(1, 6)
        ),
    )
    assert dreaming.next_round_for_run(run)["stage"] == "fanout"

    listed = dreaming.list_dream_runs(session="domi")[0]
    fanouts = [r for r in listed["rounds"] if r["stage"] == "fanout"]
    assert len(listed["threads"]) == 5
    assert len(fanouts) == 5
    assert {r["target_tokens"] for r in fanouts} == {20_000}
    assert dreaming.DREAM_DIRECTION_COUNT == 3
    assert dreaming.DREAM_TARGET_TOKEN_BUDGET == 70_000


def test_completed_round_kicks_next_round_without_scheduler_poll():
    aid = _agent("domi")
    location.set_location("domi", 0, 0, ts=1)
    sent: list[tuple[str, str]] = []
    now = _ts(2026, 6, 24, 3, 5)
    scheduler = dreaming.DreamingScheduler(
        send_dream=lambda session, text: sent.append((session, text)),
        now=lambda: now,
        chain_delay_sec=0.01,
        chain_retry_sec=0.01,
        chain_attempts=20,
        # This test drives the seed -> fanout -> ... chain, so the recipe has
        # to be fixed; the nightly path otherwise draws an arm at random and
        # the roleplay arm opens with a different first stage.
        recipe_chooser=lambda: ("control", "full"),
    )

    def complete_current_round() -> None:
        run = dreaming.list_dream_runs(session="domi")[0]
        dream_round = next(r for r in run["rounds"] if r["status"] == "sent")
        marker = (
            f"DREAM_STAGE_OUTPUT run_id={run['run_id']} "
            f"round_id={dream_round['round_id']} "
            f"stage={dream_round['stage'].upper()}"
        )
        if dream_round["stage"] == "seed":
            body = "\n".join(
                f"D{i} [new]: Direction {i} with enough detail to parse"
                for i in range(1, 4)
            )
        else:
            body = (
                "Evidence status: confirmed\n"
                "Altitude: verified\n"
                f"Evidence summary: Deep investigation output for {dream_round['stage']}."
            )
        message_store.store_transcript_turns(
            agent_id=aid,
            backend_session_id="backend-1",
            source_file="/tmp/transcript.jsonl",
            turns=[
                {"role": "user", "text": sent[-1][1], "timestamp": str(len(sent) * 2)},
                {"role": "assistant", "text": f"{marker}\n{body}", "timestamp": str(len(sent) * 2 + 1)},
            ],
        )

    try:
        assert scheduler.run_once() == 1
        assert len(sent) == 1
        assert "stage=SEED" in sent[-1][1]

        complete_current_round()
        assert _wait_for(lambda: len(sent) == 2)
        assert "stage=FANOUT" in sent[-1][1]

        complete_current_round()
        assert _wait_for(lambda: len(sent) == 3)
        assert "stage=FANOUT" in sent[-1][1]
    finally:
        scheduler.stop()


def test_later_round_prompts_include_bounded_prior_ledger_context():
    aid = _agent("domi")
    run = dreaming.create_dream_run(
        agents_db.get_by_session("domi"),
        local_date="2026-06-24",
        timezone_name="UTC",
        timezone_source="test",
    )
    seed = dreaming.list_dream_runs(session="domi")[0]["rounds"][0]
    dreaming.record_round_output(
        agent_id=aid,
        run_id=run["run_id"],
        round_id=seed["round_id"],
        stage="seed",
        response="\n".join([
            "D1 [new]: Consolidate automated-origin classifiers.",
            "D2 [new]: Clean old dream worktrees safely.",
            "D3 [new]: Make synthesis cite prior evidence.",
        ]),
    )

    fanout = dreaming.next_round_for_run(run)
    assert fanout is not None
    assert "Prior dream ledger context" in fanout["prompt"]
    assert "Consolidate automated-origin classifiers" in fanout["prompt"]

    dreaming.record_round_output(
        agent_id=aid,
        run_id=run["run_id"],
        round_id=fanout["round_id"],
        stage="fanout",
        response="\n".join([
            "Evidence status: confirmed",
            "Altitude: verified",
            "Evidence summary: Five origin classifiers diverge and cause notification drift.",
            "Artifact: /var/tmp/dream-origin-classifier",
            "Guardrail refused: make deploy-detached | forbidden during dreams",
            "Detailed finding: user_notifications, message_store, dreaming snapshots, team_store, and native display each define automation differently.",
        ]),
    )

    # Complete remaining fanouts so the selected iteration round can be created.
    for dream_round in dreaming.list_dream_runs(session="domi")[0]["rounds"]:
        if dream_round["status"] == "queued" and dream_round["stage"] == "fanout":
            dreaming.record_round_output(
                agent_id=aid,
                run_id=run["run_id"],
                round_id=dream_round["round_id"],
                stage="fanout",
                response=(
                    "Evidence status: speculative\n"
                    "Altitude: idea\n"
                    "Evidence summary: Secondary thread needs later validation."
                ),
            )

    iterate = dreaming.next_round_for_run(run)
    assert iterate is not None
    assert iterate["stage"] == "iterate"
    assert "Five origin classifiers diverge" in iterate["prompt"]
    assert "guardrail-refusals=yes" in iterate["prompt"]
    assert len(iterate["prompt"]) < 12_000

    dreaming.record_round_output(
        agent_id=aid,
        run_id=run["run_id"],
        round_id=iterate["round_id"],
        stage="iterate",
        response=(
            "Evidence status: confirmed\n"
            "Altitude: worktree\n"
            "Evidence summary: A shared helper removes four duplicate origin sets."
        ),
    )
    completeness = dreaming.next_round_for_run(run)
    dreaming.record_round_output(
        agent_id=aid,
        run_id=run["run_id"],
        round_id=completeness["round_id"],
        stage="completeness",
        response="Missed angle: viewer copy should explain automation-kind labels.",
    )

    synthesize = dreaming.next_round_for_run(run)
    assert synthesize is not None
    assert synthesize["stage"] == "synthesize"
    assert "A shared helper removes four duplicate origin sets" in synthesize["prompt"]
    assert "Missed angle: viewer copy should explain automation-kind labels" in synthesize["prompt"]
    assert len(synthesize["prompt"]) < 12_000


def test_stale_sent_round_is_requeued_and_dispatched(monkeypatch):
    aid = _agent("domi")
    location.set_location("domi", 0, 0, ts=1)
    sent: list[tuple[str, str]] = []
    now = _ts(2026, 6, 24, 3, 5)
    monkeypatch.setenv("CLAUDE_PWA_DREAM_SENT_RECOVERY_SEC", "60")
    scheduler = dreaming.DreamingScheduler(
        send_dream=lambda session, text: sent.append((session, text)),
        now=lambda: now,
    )

    assert scheduler.run_once() == 1
    run = dreaming.list_dream_runs(session="domi")[0]
    first = run["rounds"][0]
    stale_sent_at = int(now * 1000) - 120_000
    dreaming.db.conn().execute(
        "UPDATE dream_rounds SET sent_at = ? WHERE round_id = ?",
        (stale_sent_at, first["round_id"]),
    )

    assert scheduler.run_once() == 1
    retried = dreaming.list_dream_runs(session="domi")[0]["rounds"][0]
    assert len(sent) == 2
    assert sent[0][1] == sent[1][1]
    assert retried["status"] == "sent"
    assert retried["sent_at"] > stale_sent_at


def test_isolated_dream_dispatch_uses_snapshot_without_touching_live_chat(monkeypatch):
    aid = _agent("dreamtest")
    agent = agents_db.get_by_session("dreamtest")
    agents_db.bind_backend_session(aid, "live-session")
    message_store.record_user_message(
        agent_id=aid,
        backend_session_id="live-session",
        client_msg_id="u1",
        text="Real work context: compare Rio dentist replies and BJJ schedules.",
        origin="user",
    )
    run = dreaming.create_dream_run(
        agent,
        local_date="2026-06-24",
        timezone_name="UTC",
        timezone_source="test",
    )
    dream_round = dreaming.list_dream_runs(session="dreamtest")[0]["rounds"][0]
    captured: dict = {}

    def fake_spawn_turn(backend: str, **kwargs):
        captured["backend"] = backend
        captured.update(kwargs)
        marker = (
            f"DREAM_STAGE_OUTPUT run_id={run['run_id']} "
            f"round_id={dream_round['round_id']} stage=SEED"
        )
        kwargs["on_result"]({
            "_assistant_text": (
                f"{marker}\n"
                "D1 [new]: Preserve clean dream isolation\n"
                "D2 [new]: Improve snapshot quality\n"
                "D3 [new]: Add timeout recovery"
            )
        })
        return object()

    monkeypatch.setattr(dreaming.backends, "spawn_turn", fake_spawn_turn)
    monkeypatch.setattr(dreaming, "_dream_scratch_cwd", lambda _agent, _prompt: pathlib.Path("/tmp/dream-scratch"))

    assert dreaming.dispatch_isolated_dream(agent, dream_round["prompt"]) is True

    assert captured["backend_session_id"] != "live-session"
    assert captured["is_new_session"] is True
    assert captured["isolated"] is True
    assert captured["hook_session"] == ""
    assert captured["cwd"] == pathlib.Path("/tmp/dream-scratch")
    assert captured["agent_id"] == aid
    assert "CLARP_DREAM_ISOLATED_CONTEXT" in captured["text"]
    assert "Real work context: compare Rio dentist replies" in captured["text"]
    assert message_store.list_messages(
        agent_id=aid, backend_session_id="live-session",
    )[0]["text"] == (
        "Real work context: compare Rio dentist replies and BJJ schedules."
    )
    updated = dreaming.list_dream_runs(session="dreamtest")[0]["rounds"][0]
    assert updated["status"] == "completed"
    assert "Preserve clean dream isolation" in updated["response"]


def test_grounded_seed_anti_promotes_fixed_items_and_records_evidence():
    aid = _agent("dreamtest")
    run = dreaming.create_dream_run(
        agents_db.get_by_session("dreamtest"),
        local_date="2026-06-24",
        timezone_name="UTC",
        timezone_source="test",
    )
    seed = dreaming.list_dream_runs(session="dreamtest")[0]["rounds"][0]
    dreaming.record_round_output(
        agent_id=aid,
        run_id=run["run_id"],
        round_id=seed["round_id"],
        stage="seed",
        response="\n".join([
            "Already-fixed, anti-promoted:",
            "- `D1 [already-fixed]: Heartbeats should skip active sessions` is skipped.",
            "D2 [new]: Verify dream guardrails are queryable.",
            "D3 [new, refuted]: Tighten digest altitude labels.",
            "D4 [uncertain]: Check whether viewer needs artifact links.",
        ]),
    )

    assert dreaming.next_round_for_run(run)["stage"] == "fanout"
    listed = dreaming.list_dream_runs(session="dreamtest")[0]
    threads = listed["threads"]
    assert [t["status"] for t in threads].count("anti_promoted") == 1
    assert [t["status"] for t in threads].count("planned") == 3
    fixed = next(t for t in threads if t["status"] == "anti_promoted")
    assert fixed["evidence_status"] == "refuted"
    assert "already fixed" in fixed["evidence_summary"].lower()
    refuted = next(t for t in threads if "altitude labels" in t["title"])
    assert refuted["evidence_status"] == "refuted"
    assert len([r for r in listed["rounds"] if r["stage"] == "fanout"]) == 3

    fanout = next(r for r in dreaming.list_dream_runs(session="dreamtest")[0]["rounds"] if r["stage"] == "fanout")
    dreaming.record_round_output(
        agent_id=aid,
        run_id=run["run_id"],
        round_id=fanout["round_id"],
        stage="fanout",
        response="\n".join([
            "Evidence status: confirmed",
            "Altitude: verified",
            "Evidence summary: Existing tests prove the guardrail metadata is inspectable.",
            "Artifact: none",
            "Guardrail refused: make deploy-detached | deploys are forbidden during dreams",
        ]),
    )
    thread = next(
        t for t in dreaming.list_dream_runs(session="dreamtest")[0]["threads"]
        if t["thread_id"] == fanout["thread_id"]
    )
    assert thread["evidence_status"] == "confirmed"
    assert thread["altitude"] == "verified"
    assert "metadata is inspectable" in thread["evidence_summary"]
    assert "make deploy-detached" in thread["guardrail_refusals"]


def test_dream_guardrail_classifier_refuses_forbidden_ops():
    assert dreaming.classify_dream_operation("rg dreaming server/lib")[0] is True
    allowed, reason = dreaming.classify_dream_operation("make deploy-detached")
    assert allowed is False
    assert "forbidden operation" in reason
    allowed, reason = dreaming.classify_dream_operation("git push origin main")
    assert allowed is False
    assert "forbidden operation" in reason


def test_timezone_resolution_prefers_session_location_then_latest_fallback(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("MAPS_API_KEY", raising=False)

    location.set_location("rachel", 59.9139, 10.7522, ts=10)
    location.set_location("domi", 40.7128, -74.0060, ts=20)

    domi = dreaming.resolve_user_timezone("domi")
    assert domi.source == "longitude-offset-fallback"
    assert domi.name == "UTC-05:00"
    assert domi.location["lat"] == 40.7128

    latest = dreaming.resolve_user_timezone("unknown")
    assert latest.source == "longitude-offset-fallback"
    assert latest.location["session"] == "domi"


def test_timezone_resolution_records_host_fallback_without_location(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("MAPS_API_KEY", raising=False)

    resolved = dreaming.resolve_user_timezone("domi")

    assert resolved.source == "host-fallback"
    assert resolved.name


def test_dream_prompt_and_noop_are_tagged_but_digest_is_visible():
    aid = _agent("domi")
    bsid = "backend-1"
    run = dreaming.create_dream_run(
        agents_db.get_by_session("domi"),
        local_date="2026-06-24",
        timezone_name="UTC",
        timezone_source="test",
    )
    prompt = run["rounds"][0]["prompt"] if "rounds" in run else dreaming.list_dream_runs(session="domi")[0]["rounds"][0]["prompt"]
    message_store.store_transcript_turns(
        agent_id=aid,
        backend_session_id=bsid,
        source_file="/tmp/transcript.jsonl",
        turns=[
            {"role": "user", "text": prompt, "timestamp": "1"},
            {"role": "assistant", "text": f"DREAMING_OK run_id={run['run_id']}", "timestamp": "2"},
        ],
    )
    visible = message_store.list_messages(agent_id=aid, backend_session_id=bsid)
    assert [m["text"] for m in visible] == [
        "Automated dreaming run",
        "Dreaming check: no action needed.",
    ]
    assert all(m["automated"] for m in visible)

    digest = (
        "Dream Digest\n\nFresh ideas:\n- Try a narrower state machine.\n"
        "DREAM_DIGEST_DONE run_id=dream_missing round_id=dround_missing"
    )
    message_store.store_transcript_turns(
        agent_id=aid,
        backend_session_id=bsid,
        source_file="/tmp/transcript.jsonl",
        turns=[
            {"role": "user", "text": prompt, "timestamp": "3"},
            {"role": "assistant", "text": digest, "timestamp": "4"},
        ],
    )
    visible = message_store.list_messages(agent_id=aid, backend_session_id=bsid)
    assert [m["text"] for m in visible] == [
        "Automated dreaming run",
        "Dream Digest\n\nFresh ideas:\n- Try a narrower state machine."
    ]
    assert visible[0]["automated"] is True
    assert visible[1]["automated"] is True
    assert visible[1]["automation_kind"] == "dreaming"


def test_snapshot_strips_all_routine_origins_not_just_some():
    """Regression: the dream snapshot must drop heartbeat, dreaming AND
    leader_tick chatter. leader_tick used to leak through because the snapshot
    filter hard-coded {dreaming, heartbeat}; it now uses the canonical
    origins.ROUTINE_AUTOMATION_ORIGINS set, so all three are stripped."""
    aid = _agent("dreamtest")
    agent = agents_db.get_by_session("dreamtest")
    bsid = "live-session"
    agents_db.bind_backend_session(aid, bsid)

    message_store.record_user_message(
        agent_id=aid, backend_session_id=bsid, client_msg_id="real",
        text="User: please compare the dentist replies.", origin="user")
    for kind in ("heartbeat", "leader_tick", "dreaming"):
        message_store.record_user_message(
            agent_id=aid, backend_session_id=bsid, client_msg_id=f"auto-{kind}",
            text=f"Automated {kind} check", origin=kind)

    snapshot = dreaming._recent_real_context_snapshot(agent)

    assert "User: please compare the dentist replies." in snapshot
    # None of the routine-automation origins may appear in the dream context.
    assert "origin=leader_tick" not in snapshot
    assert "origin=heartbeat" not in snapshot
    assert "origin=dreaming" not in snapshot
    assert "Automated leader_tick check" not in snapshot


# --- seeding strategies, context dose, and the parse honesty fix ------------

def test_markdown_headed_slate_parses_instead_of_falling_back():
    """A `### D1 [new]: ...` slate is the shape models actually emit.

    The original pattern accepted only a bare or bulleted `D1:`, so a
    well-formed heading slate parsed as zero directions and the run silently
    branched on whatever prose the fallback scraped up.
    """
    seed = "\n".join([
        "## Active directions",
        "### D1 [new]: Build the stability reconciler so drifted state heals",
        "### D2 [uncertain]: The iOS composer churn is the same disease",
        "**D3 [new]:** A third focus store lives outside set_focus",
        "## Anti-promoted",
        "- D4 [already-fixed]: The restart marker already ships today",
    ])
    items = dreaming._parse_slate_items(seed)
    titles = [item["title"] for item in items]
    assert len(titles) == 4
    assert titles[0].startswith("Build the stability reconciler")
    assert titles[2].startswith("A third focus store")
    assert [item["anti_promoted"] for item in items] == [False, False, False, True]
    assert all(item["origin_note"] == "" for item in items)


def test_bold_metadata_fields_are_extracted():
    text = "\n".join([
        "**Evidence status:** confirmed",
        "**Altitude:** worktree",
        "- Evidence summary: the reconciler is wired and its tests pass",
        "### Guardrail refused: make deploy | deploys are forbidden",
    ])
    meta = dreaming._parse_thread_metadata(text)
    assert meta["evidence_status"] == "confirmed"
    assert meta["altitude"] == "worktree"
    assert meta["evidence_summary"].startswith("the reconciler is wired")
    assert meta["guardrail_refusals"] == ["make deploy | deploys are forbidden"]


def test_failed_slate_parse_is_recorded_rather_than_fabricated():
    """A degraded parse must be visible, not disguised as a healthy run."""
    _agent("dozy")
    run = dreaming.create_dream_run(
        agents_db.get_by_session("dozy"),
        local_date="2026-06-24", timezone_name="UTC", timezone_source="test")
    dreaming.record_round_output(
        agent_id=agents_db.get_by_session("dozy")["agent_id"],
        run_id=run["run_id"],
        round_id=dreaming.list_dream_runs(session="dozy")[0]["rounds"][0]["round_id"],
        stage="seed",
        response="I looked around and nothing in particular stood out to me.",
    )
    dreaming._ensure_next_rounds(run["run_id"])
    threads = dreaming.list_dream_runs(session="dozy")[0]["threads"]
    assert threads, "a degraded parse still has to produce threads to work on"
    assert any(t["origin_note"] for t in threads), "padding must be tagged"
    assert dreaming.get_run(run["run_id"])["last_error"]


def test_roleplay_runs_the_blind_day_before_it_sees_the_product():
    _agent("dreamer")
    run = dreaming.create_dream_run(
        agents_db.get_by_session("dreamer"),
        local_date="2026-06-24", timezone_name="UTC", timezone_source="test",
        seed_strategy="roleplay", context_dose="none")
    assert run["seed_strategy"] == "roleplay"
    assert run["seed_material"] in dreaming.dream_seeds.ROLEPLAY_PERSONAS

    first = dreaming.list_dream_runs(session="dreamer")[0]["rounds"][0]
    assert first["stage"] == "roleplay_day"
    assert "stage=ROLEPLAY_DAY" in first["prompt"]
    # The blind constraint is the whole point of the arm.
    assert "You do not know what this machine is for" in first["prompt"]
    assert "Do not propose solutions" in first["prompt"]

    dreaming.record_round_output(
        agent_id=agents_db.get_by_session("dreamer")["agent_id"],
        run_id=run["run_id"], round_id=first["round_id"],
        stage="roleplay_day",
        response="07:00 handover. 09:20 rewrote the same note on paper twice.",
    )
    dreaming._ensure_next_rounds(run["run_id"])
    rounds = dreaming.list_dream_runs(session="dreamer")[0]["rounds"]
    reveal = next(r for r in rounds if r["stage"] == "seed")
    assert "role-play reveal" in reveal["prompt"]
    assert "Now read the actual project" in reveal["prompt"]


def test_context_dose_none_withholds_the_session_snapshot():
    _agent("doze")
    agent = agents_db.get_by_session("doze")
    withheld = dreaming._dose_snapshot(agent, "none")
    assert "withheld" in withheld
    assert "Recent real conversation snapshot:\n-" not in withheld
    full = dreaming._dose_snapshot(agent, "full")
    assert "withheld" not in full


def test_manual_run_bypasses_the_nightly_window():
    _agent("dodo")
    run = dreaming.start_manual_run("dodo", seed_strategy="lenses",
                                    context_dose="fragments")
    assert run["seed_strategy"] == "lenses"
    assert run["context_dose"] == "fragments"
    assert run["seed_material"], "the lens arm must record which lenses it drew"
    # A second concurrent run on one agent would interleave two ledgers.
    try:
        dreaming.start_manual_run("dodo")
    except ValueError as e:
        assert "already has an active dream run" in str(e)
    else:
        raise AssertionError("expected a refusal for a concurrent manual run")

    # A manual run counts against the night's quota, so the nightly scheduler
    # cannot follow it with a second randomly-seeded run for the same agent.
    agent = agents_db.get_by_session("dodo")
    assert agent["dreaming_last_local_date"] == run["local_date"]
    assert dreaming.dream_runs_for_agent_date(
        agent["agent_id"], run["local_date"]) == 1


def test_completed_digest_reaches_the_chat_and_an_artifact():
    """The night's one visible output has to leave the ledger.

    Dreams run in an isolated backend session that is deliberately never
    written to the chat read model, so for five real runs the digest was
    recorded and then seen by nobody. Delivery is the whole point of the
    feature, so it gets a test rather than a hope.
    """
    from lib import artifacts, message_store

    aid = _agent("dozer")
    bsid = "backend-dream"
    agents_db.bind_backend_session(aid, bsid)
    run = dreaming.create_dream_run(
        agents_db.get_by_session("dozer"),
        local_date="2026-06-24", timezone_name="UTC", timezone_source="test",
        seed_strategy="foreign", context_dose="none")
    run_id = run["run_id"]
    round_id = dreaming.list_dream_runs(session="dozer")[0]["rounds"][0]["round_id"]

    digest = "Dream Digest\n\nOne ranked idea, with evidence and a next step."
    dreaming.record_final_digest(
        agent_id=aid, run_id=run_id, round_id=round_id, digest=digest)

    rows = [m for m in message_store.list_messages(
        agent_id=aid, backend_session_id=bsid, limit=50)
        if m.get("origin") == "dreaming"]
    assert len(rows) == 1, "exactly one visible row per night"
    assert digest in rows[0]["text"]

    published = [a for a in artifacts.list_artifacts(session="dozer")
                 if a.get("reference_id") == run_id]
    assert len(published) == 1
    assert published[0]["type"] == "document"
    # The recipe travels with the digest, otherwise a ranked set of ideas
    # cannot be attributed to the seeding arm that produced it.
    payload = artifacts.get(published[0]["artifact_id"])["payload"]
    assert payload["seed_strategy"] == "foreign"
    assert payload["context_dose"] == "none"
    assert payload["content"] == digest


def test_digest_delivery_is_idempotent():
    """A retried transcript import must not post the digest twice."""
    from lib import message_store

    aid = _agent("dozy2")
    bsid = "backend-dream-2"
    agents_db.bind_backend_session(aid, bsid)
    run = dreaming.create_dream_run(
        agents_db.get_by_session("dozy2"),
        local_date="2026-06-24", timezone_name="UTC", timezone_source="test")
    run_id = run["run_id"]

    from lib import artifacts

    for _ in range(3):
        dreaming._deliver_digest(
            run_id=run_id, agent_id=aid, digest="Dream Digest\n\nSame text.")

    rows = [m for m in message_store.list_messages(
        agent_id=aid, backend_session_id=bsid, limit=50)
        if m.get("origin") == "dreaming"]
    assert len(rows) == 1
    # The artifact side has to be idempotent too. This assertion is the one
    # that matters: the message insert is guarded by message_id, so a broken
    # artifact path would otherwise stay hidden behind a swallowed exception.
    published = [a for a in artifacts.list_artifacts(session="dozy2")
                 if a.get("reference_id") == run_id]
    assert len(published) == 1


# --- worktree harvest ------------------------------------------------------

def _git_repo(tmp_path):
    """A throwaway repo with one commit, usable as a worktree source."""
    import subprocess
    root = tmp_path / "src"
    root.mkdir()
    def run(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t.invalid")
    run("config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    return root


def test_harvest_commits_dream_work_to_a_branch_then_removes_the_worktree(
        tmp_path, monkeypatch):
    """The built code has to outlive the disposable directory it was built in."""
    import subprocess
    root = _git_repo(tmp_path)
    aid = _agent("dharvest")
    run = dreaming.create_dream_run(
        agents_db.get_by_session("dharvest"),
        local_date="2026-09-06", timezone_name="UTC", timezone_source="test",
        seed_strategy="foreign")
    run_id = run["run_id"]
    agents_db.update_agent(aid, cwd=str(root))

    worktrees = tmp_path / "wt"
    worktrees.mkdir()
    target = worktrees / run_id
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach",
                    str(target), "HEAD"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (target / "invention.py").write_text("# what the dream built\n")
    monkeypatch.setattr(dreaming, "_dream_worktree_path", lambda rid: target)

    branch = dreaming.harvest_worktree(run_id)

    assert branch.startswith("dream/2026-09-06-foreign-")
    assert dreaming.get_run(run_id)["artifact_branch"] == branch
    # The work is on a real branch in the source repo...
    listed = subprocess.run(
        ["git", "-C", str(root), "show", f"{branch}:invention.py"],
        capture_output=True, text=True)
    assert listed.returncode == 0
    assert "what the dream built" in listed.stdout
    # ...and the disposable directory is gone.
    assert not target.exists()


def test_harvest_of_a_clean_worktree_records_no_branch(tmp_path, monkeypatch):
    import subprocess
    root = _git_repo(tmp_path)
    _agent("dclean")
    run = dreaming.create_dream_run(
        agents_db.get_by_session("dclean"),
        local_date="2026-09-06", timezone_name="UTC", timezone_source="test")
    run_id = run["run_id"]
    agents_db.update_agent(agents_db.get_by_session("dclean")["agent_id"],
                           cwd=str(root))
    worktrees = tmp_path / "wt"
    worktrees.mkdir()
    target = worktrees / run_id
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach",
                    str(target), "HEAD"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    monkeypatch.setattr(dreaming, "_dream_worktree_path", lambda rid: target)

    assert dreaming.harvest_worktree(run_id) == ""
    assert dreaming.get_run(run_id)["artifact_branch"] == ""
    assert not target.exists()


def test_harvest_keeps_the_worktree_when_committing_fails(tmp_path, monkeypatch):
    """Losing disk is a smaller problem than losing the only copy of the work."""
    import subprocess
    root = _git_repo(tmp_path)
    _agent("dfail")
    run = dreaming.create_dream_run(
        agents_db.get_by_session("dfail"),
        local_date="2026-09-06", timezone_name="UTC", timezone_source="test")
    run_id = run["run_id"]
    worktrees = tmp_path / "wt"
    worktrees.mkdir()
    target = worktrees / run_id
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach",
                    str(target), "HEAD"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (target / "precious.py").write_text("# unreviewed but real\n")
    monkeypatch.setattr(dreaming, "_dream_worktree_path", lambda rid: target)

    real_run = subprocess.run

    def explode(args, **kwargs):
        if "commit" in args:
            raise subprocess.SubprocessError("commit refused")
        return real_run(args, **kwargs)

    monkeypatch.setattr(dreaming.subprocess, "run", explode)
    dreaming.harvest_worktree(run_id)

    assert target.exists(), "a failed harvest must not delete the work"
    assert (target / "precious.py").exists()
