"""The Scheduled task Janitor (`scheduled-prompt`): owner-written instructions run
as a quiet turn in the Janitor's own session on `schedule@1`, close with
worked/idle/failed, and back off while idle (janitor_adaptive)."""
from __future__ import annotations

import pytest

from lib import agents, backends, db, janitors
from lib.janitor_runner import JanitorRunner, SQLiteSource

INSTRUCTIONS = "Run the testflight-feedback-monitor skill.\nMONITOR_CONFIG_JSON\n{\"version\": 1}"
QUARTER = 900_000


class Source(SQLiteSource):
    """The real store and database; the test decides when a turn ended."""
    def __init__(self):
        self.ended = {}

    def terminal(self, run):
        kind = self.ended.get(run["run_id"])
        return {"kind": kind, "detail": "{}"} if kind else None


@pytest.fixture
def feedback(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])
    agents.create_agent(persona="Feedback Janitor", voice_id="", cwd="/tmp/feedback",
                        session="feedback", backend="codex")
    config = janitors.create("feedback", "scheduled-prompt", options={"instructions": INSTRUCTIONS},
                             attachments=[{"trigger_id": "schedule", "trigger_version": 1,
                                           "config": {"cron": "*/15 * * * *", "timezone": "UTC"}}])
    return janitors.set_enabled("feedback", config["revision"], True)


@pytest.fixture
def lane(feedback):
    source, calls, clock = Source(), [], [QUARTER // 2]

    def dispatch(run, prompt):
        calls.append((run, prompt))
        return True

    def tick():  # a fresh runner every tick: progress must survive restarts
        return JanitorRunner(dispatch, source=source, clock=lambda: clock[0]).tick()

    return source, calls, clock, tick


def _next_run():
    return janitors.get("feedback")["cadence"][0]["next_run_at"]


def _occurrence(lane, activity, summary="One line"):
    source, calls, clock, tick = lane
    clock[0] = _next_run()
    assert tick() == 1
    run, prompt = calls[-1]
    if activity:
        janitors.report_activity(run["run_id"], activity, summary)
    source.ended[run["run_id"]] = "done"
    tick()
    return janitors.get_run(run["run_id"]), prompt


def test_the_converted_agent_is_a_quiet_janitor_with_its_workspace_and_cadence(feedback):
    agent = agents.get_by_session("feedback")
    assert agent["is_janitor"] and agent["cwd"] == "/tmp/feedback"
    [lane] = feedback["cadence"]
    assert (lane["base_interval_seconds"], lane["max_interval_seconds"]) == (900, 86400)


def test_each_occurrence_runs_the_instructions_once_and_idle_runs_back_off(lane):
    source, calls, clock, tick = lane
    tick()
    assert _next_run() == QUARTER and calls == []
    run, prompt = _occurrence(lane, "idle", "No new TestFlight feedback")
    assert INSTRUCTIONS in prompt and run["candidates"] == []
    assert f"clarp-admin janitor outcome {run['run_id']} --status STATUS --summary SUMMARY" in prompt
    assert (run["status"], run["outcome"], run["activity"]) == ("completed", "completed", "idle")
    gaps = []
    for _ in range(3):
        _occurrence(lane, "idle")
        gaps.append((_next_run() - clock[0]) // 60_000)
    assert gaps == [60, 120, 240]
    lane_view = janitors.get("feedback")["cadence"][0]
    assert lane_view["current_interval_seconds"] == 14400 and lane_view["idle_streak"] == 4


def test_work_resets_to_every_fifteen_minutes_and_silence_counts_as_work(lane):
    source, calls, clock, tick = lane
    tick()
    _occurrence(lane, "idle")
    _occurrence(lane, "idle")
    assert janitors.get("feedback")["cadence"][0]["current_interval_seconds"] == 3600
    _occurrence(lane, "worked", "Filed two clarp-ios issues")
    assert _next_run() - clock[0] == QUARTER
    _occurrence(lane, "idle")
    _occurrence(lane, None)  # never reported: worked, cadence back to the base
    assert _next_run() - clock[0] == QUARTER


def test_a_failed_turn_keeps_the_interval_and_ends_the_run_as_an_error(lane):
    source, calls, clock, tick = lane
    tick()
    _occurrence(lane, "idle")
    clock[0] = _next_run()
    assert tick() == 1
    run, _ = calls[-1]
    source.ended[run["run_id"]] = "error"
    tick()
    assert janitors.get_run(run["run_id"])["status"] == "failed"
    view = janitors.get("feedback")["cadence"][0]
    assert (view["current_interval_seconds"], view["last_activity"]) == (1800, "failed")


def test_no_second_run_while_one_is_still_working(lane):
    source, calls, clock, tick = lane
    tick()
    clock[0] = _next_run()
    assert tick() == 1
    clock[0] += 3 * QUARTER
    tick(); tick()
    assert len(calls) == 1
    assert len({r["run_id"] for r in janitors.list_runs("feedback")}) == 1


def test_enabling_needs_instructions_and_scheduled_tasks_never_overlap(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])
    for session in ("one", "two", "empty"):
        agents.create_agent(persona=session, voice_id="", cwd="/tmp", session=session, backend="codex")
    attachments = [{"trigger_id": "schedule", "config": {"cron": "0 * * * *", "timezone": "UTC"}}]
    for session in ("one", "two"):
        config = janitors.create(session, "scheduled-prompt", options={"instructions": "Tidy " + session},
                                 attachments=attachments)
        assert janitors.set_enabled(session, config["revision"], True)["enabled"]
    empty = janitors.create("empty", "scheduled-prompt", attachments=attachments)
    with pytest.raises(janitors.JanitorError, match="instructions"):
        janitors.set_enabled("empty", empty["revision"], True)


@pytest.mark.parametrize("instructions, valid", [
    ("Line one\n\tLine two", True), ("x" * 8000, True), ("x" * 8001, False), ("bell\x07", False)])
def test_instructions_are_bounded_text(instructions, valid):
    if valid:
        janitors.validate_configuration("scheduled-prompt", options={"instructions": instructions})
    else:
        with pytest.raises(janitors.JanitorError):
            janitors.validate_configuration("scheduled-prompt", options={"instructions": instructions})


def test_only_the_schedule_trigger_is_offered():
    template = janitors.template("scheduled-prompt")
    assert template["supported_trigger_ids"] == ["schedule"]
    with pytest.raises(janitors.JanitorError):
        janitors.validate_configuration("scheduled-prompt", attachments=[{"trigger_id": "agent-work-completed"}])


def test_a_tick_just_after_the_due_time_does_not_push_the_next_run_a_quarter_later(lane):
    source, calls, clock, tick = lane
    tick()
    clock[0] = _next_run() + 144  # the runner noticed the 00:00 occurrence at 00:00:00.144
    occurrence = clock[0] - 144
    assert tick() == 1
    run, _ = calls[-1]
    janitors.report_activity(run["run_id"], "idle", "Nothing new")
    source.ended[run["run_id"]] = "done"
    clock[0] += 24_000
    tick()
    assert _next_run() == occurrence + 2 * QUARTER  # 00:30, not 00:45
