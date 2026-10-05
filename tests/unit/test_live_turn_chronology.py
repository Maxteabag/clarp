"""Live turns follow the provider's real turns in any chronology
(docs/live-items.md §1.2): a preempted or killed turn settles even without a
stop, a new prompt over an unsettled turn settles it and opens its own turn,
and items always carry the turn that produced them.

Replays the real Host log of 2026-10-05 (jasper-050b): the Claude process was
killed (exit 137, preempted) and two new prompts started new provider turns
with no terminal state in between; the hub kept the first turn open forever.
"""
from __future__ import annotations

import copy

import pytest

from lib import agents as agents_db
from lib import live_hub, turn_lifecycle
from lib.live_hub import LiveHub
from lib.live_items import LiveView
from lib.turn_lifecycle import TurnEvent


@pytest.fixture
def hub():
    events = []
    hub = LiveHub(sink=lambda event: events.append(copy.deepcopy(event)))
    hub.events = events
    live_hub.install(hub)
    yield hub
    live_hub.install(None)


def _agent(session="jasper", backend="claude"):
    agent_id = agents_db.create_agent(persona="Jasper", voice_id="v", cwd="/tmp",
                                      session=session, backend=backend)
    agents_db.start_runtime(agent_id, session)
    return agent_id


def _prompt(agent_id, trace):
    """What a /send records: the turn row, then SPAWN_STARTED with its trace
    (also over a turn that is still busy: preemption writes nothing between)."""
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id=trace)
    turn_lifecycle.try_transition(agent_id, TurnEvent.SPAWN_STARTED,
                                  {"source": "pwa", "dispatch": "claude", "trace_id": trace})


def _tool(agent_id, call, *, finish=True, trace=None):
    detail = {"phase": "tool_started", "call_id": call, "tool": "Bash",
              "input": {"command": "sleep 30"}, "status": "running"}
    if trace:
        detail["trace_id"] = trace
    turn_lifecycle.try_transition(agent_id, TurnEvent.TOOL_STARTED, detail)
    if finish:
        turn_lifecycle.try_transition(agent_id, TurnEvent.TOOL_FINISHED,
                                      {**detail, "phase": "tool_finished", "status": "ok"})


def _turn_ops(hub):
    return [(op["turn"]["turn_id"], op["turn"]["status"]) for event in hub.events
            for op in event["ops"] if op["op"] == "turn"]


def _client_state(hub, agent_id):
    view = LiveView()
    view.apply_snapshot({"epoch": hub.epoch, "lseq": 0, "activity": {"state": "idle"},
                         "turn": None, "items": []})
    for event in hub.events:
        if event["agent_id"] == agent_id:
            assert view.apply_event(event) == []
    return view


def test_a_new_prompt_over_an_unsettled_turn_settles_it_and_opens_its_own(hub):
    agent_id = _agent()
    _prompt(agent_id, "73894fe2")
    _tool(agent_id, "toolu_a1")
    _tool(agent_id, "toolu_a2", finish=False)          # running when the process is killed
    _prompt(agent_id, "11b1a361")                       # preempting prompt: no stop, no exit row
    snapshot = hub.snapshot(agent_id=agent_id)
    assert snapshot["turn"]["turn_id"] == "11b1a361" and snapshot["turn"]["status"] == "running"
    assert ("73894fe2", "interrupted") in _turn_ops(hub)
    settled = next(op["turn"] for event in hub.events for op in event["ops"]
                   if op["op"] == "turn" and op["turn"]["turn_id"] == "73894fe2"
                   and op["turn"]["status"] == "interrupted")
    assert settled["worked_ms"] is not None and settled["ended_at_ms"] is not None
    interrupted = [op["id"] for event in hub.events for op in event["ops"]
                   if op["op"] == "done" and op["status"] == "interrupted"]
    assert "cl:toolu_a2" in interrupted
    _tool(agent_id, "toolu_b1", finish=False)
    [item] = hub.snapshot(agent_id=agent_id)["items"]
    assert (item["id"], item["turn_id"]) == ("cl:toolu_b1", "11b1a361")
    # A third prompt over the second (the real Host had two in a row).
    _prompt(agent_id, "255d8ac0")
    assert ("11b1a361", "interrupted") in _turn_ops(hub)
    assert hub.snapshot(agent_id=agent_id)["turn"]["turn_id"] == "255d8ac0"
    view = _client_state(hub, agent_id)
    assert view.turn["turn_id"] == "255d8ac0" and view.items() == hub.snapshot(agent_id=agent_id)["items"]


def test_late_events_from_a_killed_turn_never_reopen_it(hub):
    agent_id = _agent()
    _prompt(agent_id, "old")
    _prompt(agent_id, "new")
    # The killed process's drain still reports a phase change for its trace.
    turn_lifecycle.try_transition(agent_id, TurnEvent.TEXT_STREAMED,
                                  {"dispatch": "claude", "trace_id": "old", "phase": "thinking"})
    assert hub.snapshot(agent_id=agent_id)["turn"]["turn_id"] == "new"
    # Its stream items are tagged with their turn and dropped.
    hub.message_text(agent_id, "cl:msg_old:0", "late text", turn_id="old")
    hub.tool_start(agent_id, "cl:toolu_old", name="Bash", call_id="toolu_old",
                   category="exec", label="ls", turn_id="old")
    assert hub.snapshot(agent_id=agent_id)["items"] == []
    hub.message_text(agent_id, "cl:msg_new:0", "fresh", turn_id="new")
    assert [i["turn_id"] for i in hub.snapshot(agent_id=agent_id)["items"]] == ["new"]


def test_a_process_killed_without_a_stop_settles_its_turn_as_failed(hub):
    agent_id = _agent()
    _prompt(agent_id, "tr-kill")
    _tool(agent_id, "toolu_k", finish=False)
    # The process died (exit 137) and the dispatcher gave up: no user stop.
    turn_lifecycle.try_transition(agent_id, TurnEvent.PROCESS_EXITED_FAILED,
                                  {"trace_id": "tr-kill", "message": "claude exited rc=137"})
    snapshot = hub.snapshot(agent_id=agent_id)
    assert snapshot["turn"]["status"] == "failed" and snapshot["turn"]["worked_ms"] is not None
    assert [i["status"] for i in snapshot["items"]] == ["interrupted"]
    assert snapshot["activity"]["state"] == "interrupted"


def test_a_queued_prompt_after_a_completed_turn_opens_the_next_turn(hub):
    agent_id = _agent()
    _prompt(agent_id, "first")
    turn_lifecycle.try_transition(agent_id, TurnEvent.PROCESS_EXITED_OK, {"trace_id": "first"})
    _prompt(agent_id, "queued")
    assert ("first", "completed") in _turn_ops(hub)
    assert ("first", "interrupted") not in _turn_ops(hub)
    assert hub.snapshot(agent_id=agent_id)["turn"]["turn_id"] == "queued"


def test_steering_mid_turn_stays_in_the_same_turn(hub):
    agent_id = _agent(session="cody", backend="codex")
    _prompt(agent_id, "steered")
    # A steer appends to the running provider turn: same trace, no new spawn.
    turn_lifecycle.try_transition(agent_id, TurnEvent.TEXT_STREAMED,
                                  {"dispatch": "codex", "trace_id": "steered", "phase": "responding"})
    _tool(agent_id, "call_1", trace="steered")
    assert set(t for t, _s in _turn_ops(hub)) == {"steered"}
    assert hub.snapshot(agent_id=agent_id)["turn"]["status"] == "running"


def test_after_a_runtime_restart_a_new_prompt_opens_its_turn():
    agent_id = _agent()
    _prompt(agent_id, "before-restart")          # no hub: the old runtime saw it
    fresh = LiveHub(sink=lambda _e: None)        # the restarted runtime
    live_hub.install(fresh)
    try:
        turn_lifecycle.try_transition(agent_id, TurnEvent.RESTART_INTERRUPTED,
                                      {"trace_id": "before-restart"})
        assert fresh.snapshot(agent_id=agent_id)["turn"]["status"] == "interrupted"
        _prompt(agent_id, "after-restart")
        snapshot = fresh.snapshot(agent_id=agent_id)
        assert snapshot["turn"]["turn_id"] == "after-restart"
        assert snapshot["turn"]["status"] == "running"
    finally:
        live_hub.install(None)


def test_a_killed_claude_stream_cannot_write_into_the_turn_that_replaced_it(hub):
    from lib.live_claude import ClaudeLiveItems

    agent_id = _agent()
    _prompt(agent_id, "killed")
    old_stream = ClaudeLiveItems(agent_id=agent_id, trace_id="killed", row_id=lambda _k: "live-x")
    _prompt(agent_id, "replacement")
    # The killed process flushes the rest of its stdout after the new turn began.
    old_stream.on_event({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}})
    old_stream.on_event({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "stale"}}})
    old_stream.on_event({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "id": "toolu_stale", "name": "Bash", "input": {}}}})
    snapshot = hub.snapshot(agent_id=agent_id)
    assert snapshot["turn"]["turn_id"] == "replacement"
    assert snapshot["items"] == []
