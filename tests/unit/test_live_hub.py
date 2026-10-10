"""The Host's live hub (docs/live-items.md §3-5): what it emits, replayed
through the reference reducer a client runs, always lands on the same state
its GET /live snapshot reports; text growth is coalesced, structural changes
go out at once, and the status line follows the items."""
from __future__ import annotations

import copy

from lib.live_hub import LiveHub
from lib.live_items import LiveView


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.timers: list[list] = []

    def ms(self):
        return int(self.now * 1000)

    def schedule(self, delay, fn):
        timer = [self.now + delay, fn, False]
        self.timers.append(timer)

        class _Handle:
            def cancel(self_inner):
                timer[2] = True
        return _Handle()

    def advance(self, seconds):
        self.now += seconds
        for timer in [t for t in self.timers if not t[2] and t[0] <= self.now]:
            timer[2] = True
            timer[1]()


def _hub():
    clock, events = FakeClock(), []
    hub = LiveHub(sink=lambda ev: events.append(copy.deepcopy(ev)),
                  clock_ms=clock.ms, schedule=clock.schedule)
    return hub, clock, events


def _client(hub, events, conv="conv-1"):
    view = LiveView()
    view.apply_snapshot({"conv": conv, "epoch": hub.epoch, "lseq": 0,
                         "activity": {"state": "idle"}, "turn": None, "items": []})
    for event in events:
        assert view.apply_event(event) == [], event
    return view


def _same_state(view, snapshot):
    assert view.lseq == snapshot["lseq"]
    assert view.items() == snapshot["items"]
    assert view.activity == snapshot["activity"]
    assert view.turn == snapshot["turn"]


def test_a_turn_replayed_by_a_client_matches_the_snapshot():
    hub, clock, events = _hub()
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-1")
    hub.message_text("a1", "cl:msg_1:0", "Let me look", phase="commentary", row_id="live-1")
    clock.advance(0.03)
    hub.message_text("a1", "cl:msg_1:0", "Let me look at the parser.")
    clock.advance(0.2)
    hub.done("a1", "cl:msg_1:0")
    hub.tool_start("a1", "cl:toolu_1", name="Bash", call_id="toolu_1", category="exec",
                   label="npm test", command="npm test")
    hub.tool_output("a1", "cl:toolu_1", ["ok 1", "ok 2"], total_lines=2)
    clock.advance(0.3)
    hub.done("a1", "cl:toolu_1", status="completed", patch={"tool": {"output": {"exit_code": 0}}})
    hub.end_turn("a1", status="completed")

    snapshot = hub.snapshot(session="rachel")
    assert snapshot["epoch"] == hub.epoch
    _same_state(_client(hub, events), snapshot)
    message, tool = snapshot["items"]
    assert message["text"] == "Let me look at the parser."
    assert tool["tool"]["output"]["tail"] == ["ok 1", "ok 2"]
    assert snapshot["turn"]["status"] == "completed"
    assert snapshot["turn"]["worked_ms"] == snapshot["turn"]["ended_at_ms"] - snapshot["turn"]["started_at_ms"]
    assert snapshot["activity"]["state"] == "idle"
    assert [e["lseq"] for e in events] == list(range(1, len(events) + 1))


def test_text_growth_is_coalesced_and_structure_is_immediate():
    hub, clock, events = _hub()
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-1")
    before = len(events)
    hub.message_text("a1", "m", "a")              # creates the item: immediate
    assert len(events) == before + 1
    for n in range(2, 30):
        hub.message_text("a1", "m", "a" * n)       # growth: waits for the window
    assert len(events) == before + 1
    clock.advance(0.1)
    assert len(events) == before + 2
    appends = [op for op in events[-1]["ops"] if op["op"] == "append"]
    assert len(appends) == 1 and appends[0]["chunk"] == "a" * 28
    hub.tool_start("a1", "t", name="Read", call_id="t", category="read", label="a.py")
    assert events[-1]["ops"][0]["op"] == "upsert"


def test_the_status_line_follows_the_items():
    hub, clock, events = _hub()
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-1")

    def activity():
        return hub.snapshot(session="rachel")["activity"]

    assert activity()["state"] == "thinking"
    assert activity()["turn_started_ms"] == hub.snapshot(session="rachel")["turn"]["started_at_ms"]
    hub.reasoning_text("a1", "r", "**Reading the parser**\n\nIt splits")
    assert (activity()["state"], activity()["headline"]) == ("thinking", "Thinking: Reading the parser")
    hub.done("a1", "r")
    hub.message_text("a1", "m", "Here")
    assert activity()["state"] == "responding"
    hub.done("a1", "m")
    clock.advance(1)
    hub.tool_start("a1", "t1", name="Bash", call_id="c1", category="exec", label="npm test")
    hub.tool_start("a1", "t2", name="Grep", call_id="c2", category="search", label="foo")
    status = activity()
    assert status["state"] == "tool"
    assert status["tool"]["call_id"] == "c2" and status["running_tools"] == 2
    assert status["tool"]["started_at_ms"] == clock.ms()
    hub.done("a1", "t2")
    assert activity()["tool"]["call_id"] == "c1"
    hub.done("a1", "t1")
    assert activity()["state"] == "thinking"


def test_an_interrupted_turn_settles_everything_still_running():
    hub, clock, events = _hub()
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-1")
    hub.message_text("a1", "m", "Half a sen")
    hub.tool_start("a1", "t", name="Bash", call_id="c", category="exec", label="sleep 9")
    clock.advance(2)
    hub.end_turn("a1", status="interrupted")
    snapshot = hub.snapshot(session="rachel")
    assert {item["status"] for item in snapshot["items"]} == {"interrupted"}
    assert all(item["ended_at_ms"] == clock.ms() for item in snapshot["items"])
    assert snapshot["activity"]["state"] == "interrupted"
    assert snapshot["turn"]["worked_ms"] == 2000
    _same_state(_client(hub, events), snapshot)


def test_explore_tools_share_a_group_until_something_else_happens():
    hub, clock, events = _hub()
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-1")
    hub.tool_start("a1", "r1", name="Read", call_id="r1", category="read", label="a.py")
    hub.tool_start("a1", "s1", name="Grep", call_id="s1", category="search", label="x")
    hub.tool_start("a1", "x1", name="Bash", call_id="x1", category="exec", label="make")
    hub.tool_start("a1", "r2", name="Read", call_id="r2", category="read", label="b.py")
    groups = {item["id"]: item["tool"]["group"] for item in hub.snapshot(session="rachel")["items"]}
    assert groups == {"r1": "explore:r1", "s1": "explore:r1", "x1": None, "r2": "explore:r2"}


def test_a_new_turn_starts_with_only_its_own_items():
    hub, clock, events = _hub()
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-1")
    hub.message_text("a1", "m1", "first")
    hub.end_turn("a1", status="completed")
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-2")
    snapshot = hub.snapshot(session="rachel")
    assert snapshot["items"] == [] and snapshot["turn"]["turn_id"] == "tr-2"
    _same_state(_client(hub, events), snapshot)


def test_sse_subscribers_get_item_ops_only_for_the_chats_they_asked_for(tmp_path):
    import json

    from lib.audio_stream import AudioStream

    stream = AudioStream(tmp_path)
    open_chat = stream.subscribe(live={"rachel"})
    roster_only = stream.subscribe(live=set())
    everything = stream.subscribe(live={"*"})
    old_client = stream.subscribe()
    event = {"type": "live", "agent_id": "a1", "session": "rachel", "conv": "c1",
             "epoch": "e", "lseq": 3, "server_now_ms": 1,
             "ops": [{"op": "append", "conv": "c1", "id": "m", "kind": "message", "rev": 2,
                      "field": "text", "chunk": "hi"},
                     {"op": "status", "conv": "c1", "activity": {"state": "responding"}}]}
    stream.broadcast_live(event)
    other = {**event, "agent_id": "a2", "session": "mike", "conv": "c2"}
    stream.broadcast_live(other)

    def received(q):
        out = []
        while not q.empty():
            out.append(json.loads(q.get_nowait()))
        return out

    assert [len(e["ops"]) for e in received(open_chat)] == [2, 1]
    assert [[op["op"] for op in e["ops"]] for e in received(roster_only)] == [["status"], ["status"]]
    assert [len(e["ops"]) for e in received(everything)] == [2, 2]
    assert received(old_client) == []
    # Live events are never stored for replay.
    assert not any(e["type"] == "live" for e in stream.recent())


def test_the_runtime_pushes_live_events_and_answers_snapshots_over_its_socket(tmp_path):
    import json
    import threading
    import time

    from lib.audio_stream import AudioStream
    from lib.live_hub import LiveFanout, LiveRelay
    from lib.runtime_bridge import RuntimeClient, RuntimeRPCServer

    class _Dispatch:
        pass

    fanout = LiveFanout()
    hub = LiveHub(sink=fanout.publish)
    socket_path = tmp_path / "rt.sock"
    runtime = RuntimeRPCServer(socket_path, dispatch_service=_Dispatch(),
                               status_provider=lambda: {})
    runtime.live_fanout = fanout
    runtime.live_hub = hub
    threading.Thread(target=runtime.serve_forever, daemon=True).start()
    stream = AudioStream(tmp_path / "audio")
    phone = stream.subscribe(live={"rachel"})
    relay = LiveRelay(socket_path, stream)
    relay.start()
    try:
        deadline = time.monotonic() + 5
        while not fanout.subscribers() and time.monotonic() < deadline:
            time.sleep(0.02)
        hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-1")
        hub.message_text("a1", "m1", "Hello")
        got = json.loads(phone.get(timeout=5))
        assert got["type"] == "live" and got["lseq"] == 1
        snapshot = RuntimeClient(socket_path).live_snapshot(session="rachel")
        assert snapshot["epoch"] == hub.epoch
        assert [item["text"] for item in snapshot["items"]] == ["Hello"]
    finally:
        relay.stop()
        runtime.shutdown()
        runtime.server_close()


def test_a_failed_turn_carries_the_reason_clients_show(monkeypatch):
    # A provider refusal ended the turn after a second; without the reason
    # on the turn, a client could only fold it behind "Worked for 1s".
    from lib import live_hub
    from lib.protocol import AgentState
    hub, clock, events = _hub()
    monkeypatch.setattr(live_hub, "_HUB", hub)
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-1")
    hub.item("a1", "cx:tr-1:compaction", "compaction", {"status": "running"})
    clock.advance(1)
    live_hub.observe_transition("a1", "process_exited_failed", AgentState.INTERRUPTED, {
        "trace_id": "tr-1", "reason": "account_plan",
        "message": "This account's plan does not include this model",
        "error": "The 'gpt-6-astra' model is not supported when using Codex "
                 "with a ChatGPT account."})
    snapshot = hub.snapshot(session="rachel")
    assert snapshot["turn"]["status"] == "failed"
    assert snapshot["turn"]["error"] == {
        "reason": "account_plan",
        "message": "This account's plan does not include this model",
        "detail": "The 'gpt-6-astra' model is not supported when using Codex "
                  "with a ChatGPT account."}
    assert snapshot["activity"]["headline"] == "This account's plan does not include this model"
    assert {item["status"] for item in snapshot["items"]} == {"interrupted"}
    _same_state(_client(hub, events), snapshot)


def test_a_completed_or_stopped_turn_has_no_error():
    hub, _, _ = _hub()
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-1")
    hub.end_turn("a1")
    assert "error" not in hub.snapshot(session="rachel")["turn"]
    hub.begin_turn(agent_id="a1", session="rachel", conv="conv-1", turn_id="tr-2")
    hub.end_turn("a1", status="interrupted")
    assert "error" not in hub.snapshot(session="rachel")["turn"]


def test_an_update_hold_reads_limited_on_a_settled_turn_and_clears():
    events = []
    hub = LiveHub(sink=events.append, clock_ms=lambda: 5000)
    hub.begin_turn(agent_id="a", session="s", conv="c", turn_id="t1")
    hub.set_update_holds({"a"}, "Waiting for the Clarp update")
    assert hub.activities()["a"]["state"] == "thinking"   # a running turn wins

    hub.end_turn("a")
    activity = hub.activities()["a"]
    assert activity["state"] == "limited"
    assert activity["headline"] == "Waiting for the Clarp update"
    assert activity["turn_id"] is None
    assert events[-1]["ops"][-1]["activity"]["state"] == "limited"

    hub.set_update_holds(set(), "Waiting for the Clarp update")
    assert (hub.activities()["a"]["state"], hub.activities()["a"]["headline"]) == (
        "idle", None)

