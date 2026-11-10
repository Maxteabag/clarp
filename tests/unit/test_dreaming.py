"""Dreaming scheduler, timezone, prompt, and digest/no-op contracts."""
from __future__ import annotations

import time
import threading
import pathlib
import urllib.error
from datetime import datetime, timezone

import pytest

from tests.patching import patch_shared
from lib import agents as agents_db
from lib import dream_seeds, dreaming, location, message_store
from lib.protocol import AgentState


@pytest.fixture(autouse=True)
def wikipedia_unreachable(monkeypatch):
    """The foreign seed asks Wikipedia for a random article and falls back to the
    offline pool when it cannot. Tests never reach the network: the transport
    (dream_seeds' urlopen) fails Wikipedia requests deterministically, exactly as
    an offline Host does, and every other URL goes to the real urlopen."""
    real_urlopen = dream_seeds.urllib.request.urlopen

    def urlopen(request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        if "wikipedia.org" in str(url):
            raise urllib.error.URLError("wikipedia is unreachable in tests")
        return real_urlopen(request, *args, **kwargs)
    monkeypatch.setattr(dream_seeds.urllib.request, "urlopen", urlopen)


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


@pytest.mark.parametrize("interruption", ["none", "scheduler_stop", "user_stop", "user_stop_resume", "user_stop_resume_new_wake", "run_end", "retry_error"])
def test_completed_round_kicks_next_round_without_scheduler_poll(monkeypatch, interruption):
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

    advance_sent = threading.Event()
    release_advance = threading.Event()
    wake_threads = []
    real_advance = scheduler._advance_run
    real_start = threading.Thread.start

    def record_start(thread):
        if thread.name.startswith("dreaming-advance-"):
            wake_threads.append(thread)  # child registered before its parent can exit
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", record_start)
    failures, errors = [], []
    patch_shared(monkeypatch, dreaming, "log_exception",
                 lambda event, error, **kw: errors.append((event, str(error))))

    def hold_successful_advance(run):
        if interruption == "retry_error" and advance_sent.is_set() and not failures:
            failures.append(True)
            raise RuntimeError("synthetic retry failure")
        result = real_advance(run)
        if result and len(sent) == 2:
            # The round is durably sent, but its wake worker still owns the
            # pending slot. Completing it now used to lose the next wake.
            advance_sent.set()
            assert release_advance.wait(5), "test releases the pending advance"
        return result

    monkeypatch.setattr(scheduler, "_advance_run", hold_successful_advance)

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
        assert advance_sent.wait(5), "hold the successful advance before pending cleanup"
        assert len(sent) == 2
        assert "stage=FANOUT" in sent[-1][1]

        complete_current_round()
        run_id = dreaming.list_dream_runs(session="domi")[0]["run_id"]
        for _ in range(5):
            scheduler.request_advance(run_id)
        if interruption == "scheduler_stop":
            scheduler.stop()
        elif interruption in {"user_stop", "user_stop_resume", "user_stop_resume_new_wake"}:
            from lib import turn_queue
            turn_queue.set_paused(aid, True, user_stop=True)
            if interruption != "user_stop":
                turn_queue.set_paused(aid, False)  # Resume is not a fresh completion wake
        elif interruption == "run_end":
            dreaming.abandon_run(run_id, harvest=False)
        release_advance.set()
        if interruption == "retry_error":
            assert _wait_for(lambda: bool(errors), timeout=5)
            for thread in wake_threads:
                thread.join(timeout=5)
                assert not thread.is_alive()
            assert errors == [("dreamingAdvanceFail", "synthetic retry failure")]
            assert len(sent) == 2, "a failed retry does not re-arm itself"
            scheduler.request_advance(run_id)  # only a new wake retries that failure
        if interruption in {"none", "retry_error"}:
            assert _wait_for(lambda: len(sent) == 3, timeout=5)
            assert "stage=FANOUT" in sent[-1][1]
        # Drain every worker before teardown Stop: Stop must not hide an old
        # completion token reviving after the user has paused and resumed.
        for thread in wake_threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        expected = 3 if interruption in {"none", "retry_error"} else 2
        assert len(sent) == expected, "a pre-Stop token cannot revive after Resume"
        if interruption == "user_stop_resume_new_wake":
            scheduler.request_advance(run_id)
            assert _wait_for(lambda: len(sent) == 3, timeout=5)
    finally:
        release_advance.set()
        scheduler.stop()
        for thread in wake_threads:
            thread.join(timeout=5)
            assert not thread.is_alive(), "advance workers finish before test teardown"
    expected = 3 if interruption in {"none", "retry_error", "user_stop_resume_new_wake"} else 2
    assert len(sent) == expected, "coalesced wakes respect Stop and cannot double-dispatch"


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
    patch_shared(monkeypatch, dreaming, "_dream_scratch_cwd", lambda _agent, _prompt: pathlib.Path("/tmp/dream-scratch"))

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
    patch_shared(monkeypatch, dreaming, "_dream_worktree_path", lambda rid: target)

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
    patch_shared(monkeypatch, dreaming, "_dream_worktree_path", lambda rid: target)

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
    patch_shared(monkeypatch, dreaming, "_dream_worktree_path", lambda rid: target)

    real_run = subprocess.run

    def explode(args, **kwargs):
        if "commit" in args:
            raise subprocess.SubprocessError("commit refused")
        return real_run(args, **kwargs)

    monkeypatch.setattr(dreaming.subprocess, "run", explode)
    dreaming.harvest_worktree(run_id)

    assert target.exists(), "a failed harvest must not delete the work"
    assert (target / "precious.py").exists()


def test_disabling_dreaming_mid_run_ends_it_instead_of_stranding_it(monkeypatch):
    """Opting out must not leave a run that can neither advance nor be reaped.

    `_advance_run` checks `dreaming_enabled` before recovery, so a run whose
    agent was switched off mid-flight used to sit active forever holding an
    untracked worktree — no advance, no recovery, no harvest.
    """
    aid = _agent("dquit")
    location.set_location("dquit", 0, 0, ts=1)
    now = _ts(2026, 6, 24, 3, 5)
    sent: list[tuple[str, str]] = []
    scheduler = dreaming.DreamingScheduler(
        send_dream=lambda session, text: sent.append((session, text)),
        now=lambda: now,
        recipe_chooser=lambda: ("control", "full"),
    )
    assert scheduler.run_once() == 1
    run = dreaming.list_dream_runs(session="dquit")[0]
    assert run["status"] == "active"

    harvested: list[str] = []
    patch_shared(monkeypatch, dreaming, "harvest_worktree",
                        lambda rid: harvested.append(rid) or "")

    agents_db.update_agent(aid, dreaming_enabled=False)
    assert scheduler.run_once() == 0

    closed = dreaming.get_run(run["run_id"])
    assert closed["status"] == "abandoned"
    assert closed["finished_at"]
    assert "disabled" in (closed["last_error"] or "")
    # Whatever it had already built still goes through the harvest.
    assert harvested == [run["run_id"]]
    # No round is left claiming to be in flight.
    rounds = dreaming.list_dream_runs(session="dquit")[0]["rounds"]
    assert not [r for r in rounds if r["status"] in {"queued", "sent"}]


def test_digest_survives_a_transcript_rebuild():
    """The digest exists only in the database — a rebuild must not erase it.

    Transcript import clears an agent's assistant rows and re-derives them
    from the transcript file. A dream runs in an isolated backend session that
    appears in no transcript, so without an exemption the digest is deleted on
    the agent's very next turn and can never be restored.
    """
    from lib import message_store

    aid = _agent("drebuild")
    bsid = "backend-rebuild"
    agents_db.bind_backend_session(aid, bsid)
    run = dreaming.create_dream_run(
        agents_db.get_by_session("drebuild"),
        local_date="2026-09-06", timezone_name="UTC", timezone_source="test")
    digest = "Dream Digest\n\nSomething worth keeping."
    message_store.record_dream_digest(
        agent_id=aid, backend_session_id=bsid, run_id=run["run_id"],
        text=digest)

    def dream_rows():
        return [m for m in message_store.list_messages(
            agent_id=aid, backend_session_id=bsid, limit=50)
            if m.get("origin") == "dreaming"]

    assert len(dream_rows()) == 1

    # The agent takes an ordinary turn; the transcript knows nothing of the dream.
    message_store.store_transcript_turns(
        agent_id=aid, backend_session_id=bsid,
        source_file="/tmp/transcript.jsonl",
        turns=[{"role": "user", "text": "morning", "timestamp": "1"},
               {"role": "assistant", "text": "morning to you", "timestamp": "2"}])

    surviving = dream_rows()
    assert len(surviving) == 1, "the rebuild deleted a row it cannot re-derive"
    assert digest in surviving[0]["text"]


def test_google_timezone_is_cached_per_location_and_a_failure_retries_later(monkeypatch):
    # AUT-12: resolve_tz runs for every agent on every 300 s poll; with a Maps
    # key each one was a billed, synchronous Google call. The zone id does not
    # depend on the timestamp, so one lookup per location serves 6 h.
    import json as _json
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    calls, fail = [], [False]

    class _Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return _json.dumps({"status": "OK", "timeZoneId": "Europe/Oslo"}).encode()

    def urlopen(url, timeout=None):
        calls.append(url)
        if fail[0]:
            raise urllib.error.URLError("offline")
        return _Reply()

    monkeypatch.setattr(dreaming.urllib.request, "urlopen", urlopen)
    loc = {"lat": 61.1153, "lng": 10.4662, "session": "rachel"}
    t = 1_800_000_000
    for later in (0, 300, 3 * 3600):
        assert dreaming._google_timezone(loc, t + later).name == "Europe/Oslo"
    assert len(calls) == 1
    nearby = {**loc, "lat": 61.1180}                 # same 2-decimal cell (61.12)
    assert dreaming._google_timezone(nearby, t + 600).name == "Europe/Oslo"
    assert len(calls) == 1
    assert dreaming._google_timezone(loc, t + 6 * 3600).name == "Europe/Oslo"
    assert len(calls) == 2

    other = {"lat": 62.4722, "lng": 6.1495, "session": "rachel"}
    fail[0] = True
    assert dreaming._google_timezone(other, t) is None
    assert dreaming._google_timezone(other, t + 300) is None
    assert len(calls) == 3                          # a failure is not retried at once
    fail[0] = False
    assert dreaming._google_timezone(other, t + 600).name == "Europe/Oslo"
    assert len(calls) == 4


def test_a_dream_that_keeps_failing_ends_and_frees_the_next_night(tmp_path, monkeypatch):
    """AUT-06: a failed isolated round was re-sent every 90 minutes for ever,
    and the active run kept the agent from ever dreaming again."""
    from lib import db
    aid = agents_db.create_agent(persona="Dreamer", voice_id="v", cwd=str(tmp_path),
                                 session="dreamer")
    agents_db.start_runtime(aid, "dreamer")
    agents_db.update_agent(aid, dreaming_enabled=True)
    agent = agents_db.get_by_agent_id(aid)
    run = dreaming.create_dream_run(agent, local_date="2026-01-01", timezone_name="UTC",
                                    timezone_source="synthetic")
    callbacks = {}
    patch_shared(monkeypatch, dreaming, "_dream_scratch_cwd", lambda *_: tmp_path)
    monkeypatch.setattr(dreaming.backends, "spawn_turn",
                        lambda _backend, **kw: callbacks.update(kw))
    monkeypatch.setattr(dreaming.backends, "active_handles", lambda *_: [])
    clock = [db.now_ms()]
    monkeypatch.setattr(dreaming.db, "now_ms", lambda: clock[0])
    scheduler = dreaming.DreamingScheduler(
        send_dream=lambda _session, prompt: dreaming.dispatch_isolated_dream(agent, prompt),
        now=lambda: clock[0] / 1000)

    for _ in range(3):
        current = dreaming.active_run_for_agent(aid)
        if current is None:
            break
        scheduler._advance_run(current)
        callbacks["on_error"]("synthetic permanent backend rejection")
        clock[0] += 86_400_000

    assert dreaming.active_run_for_agent(aid) is None
    ended = dreaming.get_run(run["run_id"])
    assert ended["status"] != "active" and ended["last_error"]


def _live_dream_worker(monkeypatch, tmp_path, agent_id, run, *, pid=True):
    """Dispatch one isolated round of ``run`` whose worker is a real process
    (or, with ``pid=False``, a handle that names no process)."""
    import subprocess
    from types import SimpleNamespace
    proc = subprocess.Popen(["sleep", "60"])
    handle = SimpleNamespace(pid=proc.pid, proc=proc) if pid else SimpleNamespace()
    patch_shared(monkeypatch, dreaming, "_dream_scratch_cwd", lambda *_: tmp_path)
    monkeypatch.setattr(dreaming.backends, "spawn_turn", lambda *_a, **_k: handle)
    prompt = dreaming.next_round_for_run(run)["prompt"]
    assert dreaming.dispatch_isolated_dream(agents_db.get_by_agent_id(agent_id), prompt)
    return proc


def _stop(proc):
    proc.kill()
    proc.wait()


def test_a_run_past_its_deadline_is_harvested_only_once_its_worker_stops(tmp_path, monkeypatch):
    """AUT-06 review: the deadline ends the run at once, but its worktree is
    committed and removed only when the run's own dream worker has exited."""
    agent_id, run, _clock = _expired_dream_run(tmp_path, monkeypatch)
    worker = _live_dream_worker(monkeypatch, tmp_path, agent_id, run)
    patch_shared(monkeypatch, dreaming, "_dream_worktree_path", lambda run_id: tmp_path)
    harvested = []
    patch_shared(monkeypatch, dreaming, "harvest_worktree", harvested.append)
    scheduler = dreaming.DreamingScheduler(send_dream=lambda *_: False)
    try:
        scheduler._advance_run(run)
        assert dreaming.get_run(run["run_id"])["status"] == "abandoned"
        assert harvested == []
    finally:
        _stop(worker)
    scheduler.run_once()
    assert harvested == [run["run_id"]]


def test_a_late_digest_does_not_complete_an_abandoned_run(tmp_path, monkeypatch):
    """AUT-06b (reviewer B's control): a synthesis result that arrives after
    the run was abandoned (deadline or opt-out) neither completes the run nor
    delivers its digest."""
    agent_id = agents_db.create_agent(persona="Late", voice_id="v", cwd=str(tmp_path), session="late")
    agents_db.update_agent(agent_id, dreaming_enabled=True)
    run = dreaming.create_dream_run(agents_db.get_by_agent_id(agent_id), local_date="2026-01-01",
                                    timezone_name="UTC", timezone_source="synthetic")
    clock = int(run["started_at"]) + dreaming.DREAM_RUN_DEADLINE_SEC * 1000 + 1
    monkeypatch.setattr(dreaming.db, "now_ms", lambda: clock)
    harvested, delivered = [], []
    patch_shared(monkeypatch, dreaming, "harvest_worktree", harvested.append)
    patch_shared(monkeypatch, dreaming, "_deliver_digest", lambda **kw: delivered.append(kw))
    round_id = dreaming._insert_round(run_id=run["run_id"], stage="synthesize",
                                      round_index=100, target_tokens=100)
    dreaming.mark_round_sent(round_id)
    dreaming.DreamingScheduler(send_dream=lambda *_: False)._advance_run(run)
    assert dreaming.get_run(run["run_id"])["status"] == "abandoned"

    dreaming.record_final_digest(agent_id=agent_id, run_id=run["run_id"],
                                 round_id=round_id, digest="synthetic late digest")

    assert dreaming.get_run(run["run_id"])["status"] == "abandoned"
    assert delivered == []


def _expired_dream_run(tmp_path, monkeypatch, clock=None):
    agent_id = agents_db.create_agent(persona="Expired", voice_id="v", cwd=str(tmp_path), session="expired")
    agents_db.update_agent(agent_id, dreaming_enabled=True)
    run = dreaming.create_dream_run(agents_db.get_by_agent_id(agent_id), local_date="2026-01-01",
                                    timezone_name="UTC", timezone_source="synthetic")
    clock = clock if clock is not None else [int(run["started_at"]) + dreaming.DREAM_RUN_DEADLINE_SEC * 1000 + 1]
    monkeypatch.setattr(dreaming.db, "now_ms", lambda: clock[0])
    return agent_id, run, clock


def test_routine_activity_does_not_hide_a_busy_dream_worker_from_cleanup(tmp_path, monkeypatch):
    """AUT-06 review 2 (B's control): scheduling ignores routine activity so a
    dream is not starved, but a live dream worker still keeps its worktree."""
    agent_id, run, _clock = _expired_dream_run(tmp_path, monkeypatch)
    worker = _live_dream_worker(monkeypatch, tmp_path, agent_id, run)
    agents_db.record_state(agent_id, "thinking", {"origin": "heartbeat"})
    harvested = []
    patch_shared(monkeypatch, dreaming, "harvest_worktree", harvested.append)
    try:
        dreaming.DreamingScheduler(send_dream=lambda *_: False)._advance_run(run)
    finally:
        _stop(worker)
    assert dreaming.get_run(run["run_id"])["status"] == "abandoned"
    assert harvested == []


@pytest.mark.parametrize("runtime_view", ["idle", "unavailable"])
def test_a_runtime_view_without_the_dream_worker_does_not_free_its_worktree(tmp_path, monkeypatch, runtime_view):
    """AUT-06 review 3 (B's control): the dream worker is a Host-local
    process; a separate runtime's activity, idle or unreadable, says nothing
    about it, so cleanup reads the run's own worker."""
    agent_id, run, _clock = _expired_dream_run(tmp_path, monkeypatch)
    worker = _live_dream_worker(monkeypatch, tmp_path, agent_id, run)
    monkeypatch.setattr(dreaming.backends, "_RUNTIME_CLIENT", object())

    def runtime_status():
        if runtime_view == "unavailable":
            raise RuntimeError("synthetic runtime status unavailable")
        return {"active": {}, "spawning": [], "terminals": []}

    monkeypatch.setattr(dreaming.backends, "runtime_status", runtime_status)
    harvested = []
    patch_shared(monkeypatch, dreaming, "harvest_worktree", harvested.append)
    try:
        dreaming.DreamingScheduler(send_dream=lambda *_: False)._advance_run(run)
    finally:
        _stop(worker)
    assert harvested == []


def test_a_dream_worker_of_unknown_identity_defers_cleanup(tmp_path, monkeypatch):
    agent_id, run, _clock = _expired_dream_run(tmp_path, monkeypatch)
    worker = _live_dream_worker(monkeypatch, tmp_path, agent_id, run, pid=False)
    _stop(worker)
    patch_shared(monkeypatch, dreaming, "_dream_worktree_path", lambda run_id: tmp_path)
    harvested = []
    patch_shared(monkeypatch, dreaming, "harvest_worktree", harvested.append)
    scheduler = dreaming.DreamingScheduler(send_dream=lambda *_: False)
    scheduler._advance_run(run)
    scheduler.run_once()
    assert harvested == []


def test_a_deferred_cleanup_survives_a_day_of_downtime(tmp_path, monkeypatch):
    """AUT-06 review 2 (B's control): the cleanup a live worker deferred is
    kept until it succeeds, however long the worker or the Host was away."""
    agent_id, run, clock = _expired_dream_run(tmp_path, monkeypatch)
    worker = _live_dream_worker(monkeypatch, tmp_path, agent_id, run)
    patch_shared(monkeypatch, dreaming, "_dream_worktree_path", lambda _run_id: tmp_path)
    harvested = []
    patch_shared(monkeypatch, dreaming, "harvest_worktree", harvested.append)
    monkeypatch.setattr(dreaming, "pending_dreaming_agents", lambda **_: [])
    scheduler = dreaming.DreamingScheduler(send_dream=lambda *_: False)
    try:
        scheduler._advance_run(run)
        assert harvested == []
    finally:
        _stop(worker)

    clock[0] += 2 * dreaming.DREAM_RUN_DEADLINE_SEC * 1000 + 1
    scheduler.run_once()

    assert harvested == [run["run_id"]]


def test_abandoning_does_not_overwrite_a_completion_that_won(tmp_path, monkeypatch):
    """AUT-06 review 2 (B's control): a digest that completes the run between
    abandonment's read and its write keeps the run completed."""
    agent_id, run, _clock = _expired_dream_run(tmp_path, monkeypatch)
    round_id = dreaming._insert_round(run_id=run["run_id"], stage="synthesize",
                                      round_index=100, target_tokens=100)
    dreaming.mark_round_sent(round_id)
    patch_shared(monkeypatch, dreaming, "harvest_worktree", lambda *_: None)
    patch_shared(monkeypatch, dreaming, "_deliver_digest", lambda **_: None)
    real_get = dreaming.get_run
    raced = []

    def completion_after_the_read(run_id):
        snapshot = real_get(run_id)
        if not raced and snapshot["status"] == "active":
            raced.append(True)
            dreaming.record_final_digest(agent_id=agent_id, run_id=run_id,
                                         round_id=round_id, digest="synthetic completed digest")
        return snapshot

    patch_shared(monkeypatch, dreaming, "get_run", completion_after_the_read)
    dreaming.abandon_run(run["run_id"], harvest=False)

    assert raced and real_get(run["run_id"])["status"] == "completed"


def test_a_live_worker_whose_start_was_unreadable_is_not_taken_for_gone(monkeypatch):
    """AUT-06 review 4 (B's control): an unknown recorded start time is no
    proof that a live pid was reused; only a known, different start is."""
    import os
    real_start = dreaming._process_start
    claim = dreaming._claim_worker("synthetic-unknown-start")
    patch_shared(monkeypatch, dreaming, "_process_start", lambda _pid: None)
    dreaming._fill_worker(claim, os.getpid())
    patch_shared(monkeypatch, dreaming, "_process_start", real_start)
    assert not dreaming._workers_gone("synthetic-unknown-start")


def test_overlapping_dream_spawns_keep_their_own_claims():
    """AUT-06 review 4 (B's control): two spawns of one run each hold their
    own claim; the first one finishing does not clear the second's."""
    import subprocess
    first = dreaming._claim_worker("synthetic-overlap")
    dreaming._claim_worker("synthetic-overlap")            # the second spawn, still under way
    finished = subprocess.Popen(["true"])
    finished.wait()
    dreaming._fill_worker(first, finished.pid)
    assert not dreaming._workers_gone("synthetic-overlap")


def test_a_run_whose_workers_all_exited_is_gone():
    import os
    import subprocess
    claim = dreaming._claim_worker("synthetic-all-exited")
    finished = subprocess.Popen(["true"])
    finished.wait()
    dreaming._fill_worker(claim, finished.pid)
    assert dreaming._workers_gone("synthetic-all-exited")
    live = dreaming._claim_worker("synthetic-live")
    dreaming._fill_worker(live, os.getpid())
    assert not dreaming._workers_gone("synthetic-live")


@pytest.mark.parametrize('delayed_old_request',[False,True],ids=['fresh-only','old-read-after-fresh-write'])
def test_an_older_snapshot_cannot_replace_a_new_generation_wake(monkeypatch,delayed_old_request):
    from lib import turn_queue
    aid=_agent('domi')
    run=dreaming.create_dream_run(agents_db.get_by_agent_id(aid),local_date='2026-06-24',timezone_name='UTC',timezone_source='test',seed_strategy='control',context_dose='full')
    scheduler=dreaming.DreamingScheduler(send_dream=lambda *a:None,chain_delay_sec=0,chain_retry_sec=0,chain_attempts=1)
    first_entered=threading.Event();release_first=threading.Event()
    old_read=threading.Event();release_old=threading.Event();calls=[];workers=[];errors=[]
    start=threading.Thread.start
    def register(thread):
        if thread.name.startswith('dreaming-advance-'):workers.append(thread)
        return start(thread)
    monkeypatch.setattr(threading.Thread,'start',register)
    def advance(run):
        calls.append(1)
        if len(calls)==1:
            first_entered.set()
            assert release_first.wait(5)
        return 1
    monkeypatch.setattr(scheduler,'_advance_run',advance)
    wake_lock = scheduler._wake_lock
    class HeldRequestLock:
        def __enter__(self):
            # At this point a generation-aware request has already read its
            # token. The same gate works on original main, which has no token.
            if threading.current_thread().name == 'old-completion-request':
                old_read.set()
                assert release_old.wait(5)
            return wake_lock.__enter__()
        def __exit__(self, *args):
            return wake_lock.__exit__(*args)
    scheduler._wake_lock = HeldRequestLock()
    def older():
        try:scheduler.request_advance(run['run_id'])
        except BaseException as error:errors.append(type(error).__name__)
    old_thread=None
    try:
        scheduler.request_advance(run['run_id'])
        assert first_entered.wait(5)
        if delayed_old_request:
            old_thread=threading.Thread(target=older,name='old-completion-request')
            old_thread.start();assert old_read.wait(5)
        turn_queue.set_paused(aid,True,user_stop=True)
        turn_queue.set_paused(aid,False)
        scheduler.request_advance(run['run_id'])  # explicit fresh generation wake
        release_old.set()
        if old_thread:
            old_thread.join(5);assert not old_thread.is_alive()
        assert errors==[]
        release_first.set()
        for thread in workers:
            thread.join(5);assert not thread.is_alive()
        assert len(calls)==2, 'fresh post-Stop completion must cause one follow-up despite a delayed old request'
    finally:
        release_old.set();release_first.set()
        if old_thread:old_thread.join(5)
        for thread in workers:thread.join(5)
        scheduler.stop()


# Backlog72: a replayed acknowledgement never targets the currently active run.
def _ack_run_for(agent_id, local_date):
    return dreaming.create_dream_run(
        agents_db.get_by_agent_id(agent_id), local_date=local_date,
        timezone_name="UTC", timezone_source="synthetic", seed_strategy="control")


@pytest.mark.parametrize("history_case", ["first-seen", "changed-row", "unchanged-replay"])
@pytest.mark.parametrize("marker_binding", ["bare-ack", "old-run-id", "claimed-newer-id"])
def test_historical_full_import_ack_never_noops_a_newer_run(monkeypatch, tmp_path,
                                                          history_case, marker_binding):
    from lib import db
    aid = _agent("ack-history")
    clock = [db.now_ms()]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    old_run = _ack_run_for(aid, "2026-06-24")
    old_prompt = dreaming.next_round_for_run(old_run)["prompt"]
    ack = "DREAMING_OK" + (f" run_id={old_run['run_id']}" if marker_binding != "bare-ack" else "")
    turns = [{"role": "user", "text": old_prompt, "timestamp": "1"},
             {"role": "assistant", "text": ack, "timestamp": "2"}]
    def import_old():
        message_store.store_transcript_turns(
            agent_id=aid, backend_session_id="ack-history-transcript",
            source_file=str(tmp_path / "ack-history.jsonl"), turns=turns)
    if history_case != "first-seen":
        if history_case == "changed-row":
            turns[1]["text"] = "Earlier historical assistant output"
        import_old()
        prior = message_store.list_messages(agent_id=aid, backend_session_id="ack-history-transcript")
        assert prior[-1]["text"] == ("Earlier historical assistant output" if history_case == "changed-row"
                                      else "Dreaming check: no action needed.")
    # The original run is terminal before the newer one starts, independently
    # of whether an earlier full import had already observed its marker.
    dreaming.abandon_run(old_run["run_id"], harvest=False)
    clock[0] += 1
    newer = _ack_run_for(aid, "2026-06-25")
    assert newer["started_at"] > old_run["started_at"]
    before = dreaming.get_run(newer["run_id"])
    assert before["status"] == "active"

    turns[1]["text"] = (f"DREAMING_OK run_id={newer['run_id']}"
                         if marker_binding == "claimed-newer-id" else ack)
    if history_case == "unchanged-replay":
        # An entirely unchanged probe may return without a locked pass. This
        # unrelated appended turn forces the real full import to publish while
        # revisiting the unchanged historical ACK; no writer guard is mocked.
        turns.append({"role": "user", "text": "An unrelated later request", "timestamp": "3"})
    import_old()  # Real full import: the locked pass sees the historical ACK.

    assert dreaming.get_run(newer["run_id"]) == before
    assert dreaming.active_run_for_agent(aid)["run_id"] == newer["run_id"]
    visible = message_store.list_messages(agent_id=aid, backend_session_id="ack-history-transcript")
    expected_display = ["Automated dreaming run", "Dreaming check: no action needed."]
    if history_case == "unchanged-replay":
        expected_display.append("An unrelated later request")
    assert [row["text"] for row in visible] == expected_display


@pytest.mark.parametrize("case", ["active", "late", "duplicate", "foreign-agent", "missing-id", "unknown-id",
                                 "quoted-id", "ambiguous-header", "body-conflict", "late-claims-new"])
def test_isolated_ack_is_bound_to_its_dispatch_run(monkeypatch, tmp_path, case):
    from lib import db
    aid = _agent("ack-callback")
    clock = [db.now_ms()]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    run = _ack_run_for(aid, "2026-06-24")
    agent = agents_db.get_by_agent_id(aid)
    prompt = dreaming.next_round_for_run(run)["prompt"]
    other = None
    if case == "foreign-agent":
        other_aid = _agent("ack-other")
        other = _ack_run_for(other_aid, "2026-06-24")
        agent = agents_db.get_by_agent_id(other_aid)
    elif case == "missing-id":
        prompt = dreaming.DREAMING_PROMPT
    elif case == "unknown-id":
        prompt = prompt.replace(run["run_id"], "dream_unknown_ack72")
    elif case == "quoted-id":
        prompt = "Quoted old dream context follows:\n" + prompt
    elif case == "ambiguous-header":
        prompt = prompt.replace("hidden=true]]", "hidden=true run_id=dream_ambiguous_ack72]]", 1)
    elif case == "body-conflict":
        prompt += "\nQuoted historical context: run_id=dream_other_ack72"
    callbacks = []
    def fake_spawn(_backend, **kwargs):
        callbacks.append(kwargs)
        return object()
    patch_shared(monkeypatch, dreaming, "_dream_scratch_cwd", lambda *_: tmp_path)
    monkeypatch.setattr(dreaming.backends, "spawn_turn", fake_spawn)
    assert dreaming.dispatch_isolated_dream(agent, prompt) is True
    assert len(callbacks) == 1
    result = callbacks[0]["on_result"]
    if case == "duplicate":
        result({"_assistant_text": "DREAMING_OK"})
        assert dreaming.get_run(run["run_id"])["status"] == "noop"
    elif case in ("late", "late-claims-new"):
        dreaming.abandon_run(run["run_id"], harvest=False)
    if case in ("late", "duplicate", "late-claims-new"):
        clock[0] += 1
        other = _ack_run_for(aid, "2026-06-25")
    before = dreaming.get_run(run["run_id"])
    other_before = dreaming.get_run(other["run_id"]) if other else None
    clock[0] += 1

    response = (f"DREAMING_OK run_id={other['run_id']}" if case == "late-claims-new"
                else "DREAMING_OK run_id=dream_other_ack72" if case == "body-conflict"
                else "DREAMING_OK")
    result({"_assistant_text": response})

    if case in ("active", "body-conflict"):
        after = dreaming.get_run(run["run_id"])
        assert after["status"] == after["stage"] == "noop"
        assert after["finished_at"] == clock[0]
        # No active run remains; a duplicate cannot rewrite a terminal receipt.
        clock[0] += 1
        result({"_assistant_text": "DREAMING_OK"})
        assert dreaming.get_run(run["run_id"]) == after
    else:
        assert dreaming.get_run(run["run_id"]) == before
    if other:
        assert dreaming.get_run(other["run_id"]) == other_before
        assert other_before["status"] == "active"


def test_bound_ack_loses_to_an_independent_terminal_writer(monkeypatch, tmp_path, capsys):
    import sqlite3
    from lib import db
    aid = _agent("ack-write-race")
    run = _ack_run_for(aid, "2026-06-24")
    callbacks = []
    patch_shared(monkeypatch, dreaming, "_dream_scratch_cwd", lambda *_: tmp_path)
    def fake_spawn(_backend, **kwargs):
        callbacks.append(kwargs)
        return object()
    monkeypatch.setattr(dreaming.backends, "spawn_turn", fake_spawn)
    assert dreaming.dispatch_isolated_dream(
        agents_db.get_by_agent_id(aid), dreaming.next_round_for_run(run)["prompt"])
    original_conn = db.conn
    primary = original_conn()
    observer = sqlite3.connect(primary.execute("PRAGMA database_list").fetchone()["file"],
                               isolation_level=None)
    observer.row_factory = sqlite3.Row
    terminal = []
    class BeforeNoopWrite:
        def __getattr__(self, name):
            return getattr(primary, name)
        def execute(self, sql, parameters=()):
            if sql.lstrip().upper().startswith("UPDATE DREAM_RUNS") and "'noop'" in sql and not terminal:
                assert not primary.in_transaction
                # A real separate SQLite writer commits the terminal state
                # immediately before the original target UPDATE is forwarded.
                stamp = db.now_ms() + 1
                observer.execute("UPDATE dream_runs SET status='abandoned', stage='abandoned', "
                                 "finished_at=?, updated_at=?, last_error='independent terminal' WHERE run_id=?",
                                 (stamp, stamp, run["run_id"]))
                terminal.append(dict(observer.execute(
                    "SELECT * FROM dream_runs WHERE run_id=?", (run["run_id"],)).fetchone()))
            return primary.execute(sql, parameters)
    capsys.readouterr()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(db, "conn", lambda: BeforeNoopWrite())
            callbacks[0]["on_result"]({"_assistant_text": "DREAMING_OK"})
        assert len(terminal) == 1, "the actual noop write was not reached"
        assert dreaming.get_run(run["run_id"]) == terminal[0]
        assert "dreamingNoop" not in capsys.readouterr().err
    finally:
        observer.close()


@pytest.mark.parametrize("exit_reason", ["deadline", "disabled"])
def test_import_only_unbound_ack_keeps_ledger_until_existing_run_exit(monkeypatch, tmp_path, exit_reason):
    from lib import db
    aid = _agent("ack-import-only")
    clock = [db.now_ms()]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    run = _ack_run_for(aid, "2026-06-24")
    prompt = dreaming.next_round_for_run(run)["prompt"]
    before = dreaming.get_run(run["run_id"])
    message_store.store_transcript_turns(
        agent_id=aid, backend_session_id="ack-only-transcript",
        source_file=str(tmp_path / "ack-only.jsonl"), turns=[
            {"role": "user", "text": prompt, "timestamp": "1"},
            {"role": "assistant", "text": f"DREAMING_OK run_id={run['run_id']}", "timestamp": "2"}])
    # The marker body is not dispatch provenance. Import display is preserved,
    # but neither a current-active fallback nor a guessed run id may close it.
    assert dreaming.get_run(run["run_id"]) == before
    if exit_reason == "deadline":
        clock[0] = run["started_at"] + dreaming.DREAM_RUN_DEADLINE_SEC * 1000 + 1
    else:
        agents_db.update_agent(aid, dreaming_enabled=False)
    scheduler = dreaming.DreamingScheduler(send_dream=lambda *_: pytest.fail("expired/disabled run dispatched"))
    try:
        assert scheduler._advance_run(dreaming.get_run(run["run_id"])) == 0
        ended = dreaming.get_run(run["run_id"])
        assert ended["status"] == "abandoned" and ended["finished_at"] == clock[0]
        assert ended["last_error"] == (dreaming._DEADLINE_REASON if exit_reason == "deadline"
                                      else "dreaming disabled while in flight")
    finally:
        scheduler.stop()


@pytest.mark.parametrize("origin, live", [("dreaming", True), ("user", False), ("heartbeat", False)])
def test_ack_live_or_foreign_origin_never_uses_a_body_run_id(origin, live):
    aid = _agent("ack-origin-flags")
    run = _ack_run_for(aid, "2026-06-24")
    before = dreaming.get_run(run["run_id"])
    text = f"DREAMING_OK run_id={run['run_id']}"
    decision = dreaming.process_assistant_text(aid, text, origin=origin, live=live)
    assert dreaming.get_run(run["run_id"]) == before
    assert decision == ((True, "") if origin == "dreaming" else (False, text))
