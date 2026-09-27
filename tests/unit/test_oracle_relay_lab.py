"""Offline Oracle relay lab: scripted GPT-Live turns through the stable engine.

Each scenario drives the real ``oracle_live_stable.Conversation`` with a fake
clock, a list for the upstream socket, a list for the phone and fake agent
tools. Nothing touches the network and a scenario runs in milliseconds.

Adding a scenario: build a ``Lab`` (optionally with agent result rows and a
fake transcript), then script it with ``lab.wait(seconds)`` (time passes, the
Host ticks), ``lab.oracle_speaks(seconds)`` (GPT-Live streams audible audio,
then stops), and ``lab.user("words")`` (a user transcript followed by the
provider's delegation event, routed synchronously). Inspect ``lab.parts()``
for the relayed text parts, ``lab.tools.dispatched`` for work actually sent to
agents, and ``lab.appends()`` for every context append. Seed new scenarios
from a real call: the diagnostics journal and the ``oracle_delegations`` row
give the utterances and the result text; anonymise before committing a
fixture under ``tests/unit/fixtures/oracle_lab/``.
"""
from __future__ import annotations

import array
import base64
import io
import json
import pathlib
import threading

import pytest

from lib import oracle_live_stable as mod, oracle_relay

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "oracle_lab"
THEO_REPLY = (FIXTURES / "cedb186d_theo_reply.md").read_text()
STEP = 0.25


def _audio(value, n=240):
    return base64.b64encode(array.array("h", [value] * n).tobytes()).decode()


class FakeTools:
    """Just enough of AgentTools for the stable Conversation."""

    def __init__(self, rows=(), transcript=()):
        self.lock = threading.Lock()
        self.rows = list(rows)
        self.delegations = {row["delegation_id"] for row in self.rows}
        self.fallback = "theo-97e5"
        self.transcript = list(transcript)
        self.dispatched = []
        self.calls = []

    def results(self):
        return [row for row in self.rows if row["status"] in ("completed", "failed", "cancelled")]

    AGENTS = {"theo": {"session": "theo-97e5", "persona": "Theo", "agent_id": "a-theo"},
              "nadia": {"session": "nadia-1c2d", "persona": "Nadia", "agent_id": "a-nadia"},
              "marcus": {"session": "marcus-5b1a", "persona": "Marcus", "agent_id": "a-marcus"}}

    def resolve(self, name):
        wanted = str(name or "").strip().casefold()
        for agent in self.AGENTS.values():
            if wanted in (agent["session"], agent["persona"].casefold()):
                return dict(agent)
        raise ValueError("Unknown or ambiguous agent; use a session from list_agents")

    def execute(self, name, arguments, call_id):
        self.calls.append((name, dict(arguments)))
        if name == "list_agents":
            return {"agents": [{"name": "Theo", "session": "theo-97e5"}], "oracle_contact": "theo-97e5"}
        if name in ("investigate_with_oracle", "delegate_to_agent"):
            if name == "delegate_to_agent":
                self.resolve(arguments.get("agent"))
            self.dispatched.append(dict(arguments))
            return {"status": "accepted", "operation_id": "new-" + str(len(self.dispatched)),
                    "session": "theo-97e5"}
        if name == "read_agent_transcript":
            self.resolve(arguments.get("agent"))
            return {"agent": "Theo", "messages": self.transcript, "newest_message_age": "2 minutes ago",
                    "truncated": False}
        raise AssertionError("unexpected tool " + name)


class FakeUpstream:
    """The GPT-Live session socket: what the Host sent it, and whether it was closed."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, raw):
        self.sent.append(raw)

    def close(self):
        self.closed = True


class Lab:
    """``rows`` are results of work delegated in this call; ``prior_rows`` are
    the thread's work from earlier calls, known when the call opens, and
    ``completions`` the unread agent completions the attention projection
    reports."""

    def __init__(self, monkeypatch, *, rows=(), prior_rows=(), completions=(), transcript=(),
                 strategy="direct_contact"):
        self.completions = list(completions)
        monkeypatch.setattr(mod.oracle_attention, "pending_decisions", lambda: [])
        monkeypatch.setattr(mod.oracle_attention, "pending_completion_notifications",
                            lambda: list(self.completions))
        self.now = [100.0]
        self.down = []
        self.upstreams = [FakeUpstream()]
        self.sent = self.upstreams[0].sent
        self.tools = FakeTools(prior_rows, transcript)
        self.conv = mod.Conversation(self.upstreams[0], self.down.append,
                                     self.tools, "key", clock=lambda: self.now[0],
                                     delegation_strategy=strategy)
        for row in rows:
            self.tools.rows.append(row)
            self.tools.delegations.add(row["delegation_id"])
        self.ms = 0
        self.delegation = 0

    def close(self):
        self.conv.stop.set()
        self.conv.pool.shutdown(wait=True)

    def wait(self, seconds):
        for _ in range(int(round(seconds / STEP))):
            self.now[0] += STEP
            self.conv.tick()

    def oracle_speaks(self, seconds):
        for _ in range(int(round(seconds / STEP))):
            self.conv.receive({"type": "session.output_audio.delta", "delta": _audio(3000)})
            self.now[0] += STEP
            self.conv.tick()

    def user(self, text):
        self.ms += 5000
        self.conv.receive({"type": "session.input_transcript.delta", "delta": text,
                           "start_ms": self.ms, "end_ms": self.ms + 800})
        self.now[0] += 1.1  # the router waits for one second of transcript silence
        self.delegation += 1
        with self.conv.lock:
            self.conv.routing += 1
        self.conv.route("item_" + str(self.delegation))

    def user_fragments(self, *texts, gap_ms=1500):
        """One turn the provider transcribed as separate fragments, then routed once."""
        self.ms += 5000
        for text in texts:
            self.conv.receive({"type": "session.input_transcript.delta", "delta": text,
                               "start_ms": self.ms, "end_ms": self.ms + 800})
            self.ms += 800 + gap_ms
            self.now[0] += (800 + gap_ms) / 1000
        self.delegation += 1
        with self.conv.lock:
            self.conv.routing += 1
        self.conv.route("item_" + str(self.delegation))

    def replay(self, events, until_seq=None):
        """Feed journal events at their recorded times; a delegation is routed
        synchronously, as in ``user``. Output transcript deltas come with
        audible audio, as GPT-Live streams them."""
        if not hasattr(self, "replay_base"):
            self.replay_base, self.replayed = self.now[0], 0
        for event in events:
            if event["seq"] <= self.replayed:
                continue
            if until_seq is not None and event["seq"] > until_seq:
                break
            self.replayed = event["seq"]
            target = self.replay_base + event["elapsed_ms"] / 1000
            while self.now[0] + STEP <= target:
                self.now[0] += STEP
                self.conv.tick()
            self.now[0] = max(self.now[0], target)
            kind = event["type"]
            if kind == "session.delegation.created":
                self.delegation += 1
                with self.conv.lock:
                    self.conv.routing += 1
                self.conv.route(event["delegation"]["id"])
                continue
            if kind == "session.output_transcript.delta":
                self.conv.receive({"type": "session.output_audio.delta", "delta": _audio(3000)})
            self.conv.receive({key: event[key] for key in ("type", "delta", "start_ms", "end_ms")})

    def appends(self, index=0):
        return [event for event in map(json.loads, self.upstreams[index].sent) if event["type"].endswith(".append")
                and event["type"] != "session.input_audio.append"]

    def parts(self, index=0):
        return [event["content"] for event in self.appends(index) if "\nText:\n" in event["content"]]

    def down_types(self):
        return [event["type"] for event in self.down]


def _body(part):
    return part.split("\nText:\n", 1)[1]


def _header(part):
    return part.split("\nText:\n", 1)[0]


@pytest.fixture
def lab(monkeypatch):
    labs = []

    def make(**kwargs):
        value = Lab(monkeypatch, **kwargs)
        labs.append(value)
        return value
    yield make
    for value in labs:
        value.close()


def _theo_row():
    return {"delegation_id": "rtc-3384c121", "session": "theo-97e5", "status": "completed",
            "request_text": "Handle the current user request. Current user message, verbatim:\n"
                            "Validate or disqualify recursive goals", "result_text": THEO_REPLY}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _router_answers(monkeypatch, name, arguments):
    """The operator router proposes one tool call."""
    body = {"output": [{"type": "function_call", "name": name, "call_id": "c1",
                        "arguments": json.dumps(arguments)}]}
    monkeypatch.setattr(mod, "urlopen", lambda *a, **k: _Response(json.dumps(body).encode()))


def test_cedb186d_full_reply_arrives_in_order_across_parts(lab):
    """The 2026-09-26 call: a 3.4 KB reply. Every word arrives in order
    across parts, each part fits one append, and only the last part says it
    is the end."""
    lab = lab(rows=[_theo_row()])
    lab.wait(STEP)
    parts = lab.parts()
    total = len(oracle_relay.Relay("k", "x", "y", THEO_REPLY).chunks)
    assert total >= 3
    assert len(parts) == 1 and f"part 1 of {total}" in parts[0]

    # Oracle speaks part 1, goes quiet: the Host releases part 2 on its own.
    lab.oracle_speaks(8)
    lab.wait(2)
    assert len(lab.parts()) == 2 and f"part 2 of {total}" in lab.parts()[1]
    for _ in range(total):
        lab.oracle_speaks(6)
        lab.wait(2)
    first_pass = lab.parts()
    assert len(first_pass) == total
    assert "".join(_body(p) for p in first_pass) == THEO_REPLY
    for index, part in enumerate(first_pass, 1):
        assert part.count("\nText:\n") == 1 and len(part) <= 1500
        if index < total:
            assert "end of" not in _header(part) and "More remains" in _header(part)
        else:
            assert f"part {total} of {total}, end of" in _header(part)
    assert lab.tools.dispatched == []


def test_the_user_taking_the_floor_pauses_the_relay(lab):
    lab = lab(rows=[_theo_row()])
    lab.wait(STEP)
    lab.oracle_speaks(3)
    lab.ms += 5000
    lab.conv.receive({"type": "session.input_transcript.delta", "delta": "wait a second",
                      "start_ms": lab.ms, "end_ms": lab.ms + 800})
    lab.wait(10)
    assert len(lab.parts()) == 1, "no part is pushed over a user who took the floor"


def test_result_waits_for_the_user_to_finish_a_monologue(lab):
    lab = lab()
    lab.user("so my idea is that we have recursive goals")
    lab.tools.dispatched.clear()
    spoke = lab.conv.last_transcript
    lab.tools.rows.append(_theo_row())
    lab.tools.delegations.add("rtc-3384c121")
    while lab.now[0] - spoke < mod.RESULT_RELEASE_SILENCE_SECONDS - STEP:
        lab.wait(STEP)
        assert lab.parts() == [], "a result was injected while the user was mid-thought"
    lab.wait(2 * STEP)
    assert len(lab.parts()) == 1


@pytest.mark.parametrize("words", ["Ask Theo to check the deploy", "you stopped mid-sentence",
                                   "read it again word for word", "Put me through to Mike",
                                   "Any updates?", "What did Theo say?"])
def test_direct_mode_hands_every_turn_to_the_primary_unread(lab, words):
    """The Host never reads the user's words: whatever they say, a direct turn
    goes to the primary, who decides (continue, replay, put through, ...)."""
    lab = lab(rows=[_theo_row()])
    lab.wait(STEP)
    lab.user(words)
    [work] = lab.tools.dispatched
    assert work["request"].endswith("verbatim:\n" + words)


def test_operator_router_can_continue_a_result_without_dispatch(lab, monkeypatch):
    lab = lab(rows=[_theo_row()], strategy="operator")
    lab.wait(STEP)
    body = {"output": [{"type": "function_call", "name": "read_result", "call_id": "c1",
                        "arguments": json.dumps({"operation_id": "rtc-3384c121", "from_part": 1,
                                                 "verbatim": True})}]}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(mod, "urlopen", lambda *a, **k: Response(json.dumps(body).encode()))
    lab.user("read me his exact words")
    assert "part 1 of" in lab.parts()[-1] and "word for word" in _header(lab.parts()[-1])
    assert lab.tools.dispatched == []


def test_operator_router_reads_a_transcript_without_prompting_the_agent(lab, monkeypatch):
    transcript = [{"role": "user", "timestamp": "2026-09-26T10:00:00Z", "text": "Can you check the vault notes?"},
                  {"role": "assistant", "timestamp": "2026-09-26T10:01:00Z", "text": "The vault has two relevant notes."}]
    lab = lab(transcript=transcript, strategy="operator")
    _router_answers(monkeypatch, "read_agent_transcript", {"agent": "Theo", "limit": 10})
    lab.user("what has Theo been saying")
    assert lab.tools.dispatched == []
    [part] = lab.parts()
    assert "Can you check the vault notes?" in part and "The vault has two relevant notes." in part


def test_every_part_fits_one_append_even_for_multibyte_text():
    text = "Ferdig – alt OK ✓ æøå 日本語テキスト. " * 200
    relay = oracle_relay.Relay("op", "Théo's reply", "operation op, status completed", text, request="r" * 200)
    parts = [relay.part(i, served=True, resumed=True, pause_after=True) for i in range(relay.total)]
    assert all(len(part) <= oracle_relay.APPEND_CHARS for part in parts)
    assert "".join(_body(part) for part in parts) == text



# ---- the turn is the whole utterance (call 7946a1a7) -------------------------

def test_the_turn_is_the_whole_utterance_since_oracle_last_spoke(lab):
    lab = lab()
    lab.conv.receive({"type": "session.output_transcript.delta", "delta": "Go on.", "start_ms": 0, "end_ms": 400})
    lab.user_fragments("Can you check", "the deploy on", "staging")
    [work] = lab.tools.dispatched
    assert work["request"].endswith("verbatim:\nCan you check the deploy on staging")


def test_speech_before_oracle_last_spoke_is_not_part_of_the_turn(lab):
    lab = lab()
    lab.user_fragments("so the plan is recursive goals")
    lab.tools.dispatched.clear()
    lab.ms += 2000
    lab.conv.receive({"type": "session.input_transcript.delta", "delta": "can you can you",
                      "start_ms": lab.ms, "end_ms": lab.ms + 800})
    lab.conv.receive({"type": "session.output_transcript.delta", "delta": "Go on.",
                      "start_ms": lab.ms + 900, "end_ms": lab.ms + 1300})
    lab.user_fragments("Check the deploy")
    [work] = lab.tools.dispatched
    assert work["request"].endswith("verbatim:\nCheck the deploy")


def _oracle_says(lab, text, seconds=1.5):
    lab.ms += 1000
    lab.conv.receive({"type": "session.output_transcript.delta", "delta": text,
                      "start_ms": lab.ms, "end_ms": lab.ms + 400})
    lab.oracle_speaks(seconds)


# ---- earcons ----------------------------------------------------------------

def _cues(lab):
    return [e["name"] for e in lab.down if e["type"] == "oracle_v2.cue"]


def _cue_audio_follows_each_cue(lab):
    for i, event in enumerate(lab.down):
        if event["type"] == "oracle_v2.cue":
            audio = lab.down[i + 1]
            assert audio["type"] == "session.output_audio.delta"
            assert base64.b64decode(audio["delta"]) == mod.oracle_earcons.pcm(
                event["name"], event.get("agent"))


def test_handed_off_work_ticks_and_a_result_rings_before_its_read_out(lab):
    lab = lab()
    lab.user("Check the deploy")
    assert _cues(lab) == ["handed_off"]
    lab.tools.rows.append({"delegation_id": "new-1", "session": "theo-97e5", "status": "completed",
                           "request_text": "Check the deploy", "result_text": "The deploy is green."})
    lab.wait(6)
    assert _cues(lab) == ["handed_off", "result"]
    [part] = lab.parts()
    assert "The deploy is green." in part
    _cue_audio_follows_each_cue(lab)


def test_cues_wait_for_oracle_to_stop_speaking(lab):
    lab = lab()
    lab.oracle_speaks(1)
    lab.conv.cue("result")
    assert _cues(lab) == [], "never inside Oracle's speech"
    lab.oracle_speaks(1)
    lab.wait(2)
    types = lab.down_types()
    cue = types.index("oracle_v2.cue")
    model_audio = [i for i, e in enumerate(lab.down) if e["type"] == "session.output_audio.delta" and i != cue + 1]
    assert max(model_audio) < cue
    assert "oracle_v2.quiet" in types[cue:], "the phone returns to listening after the cue"


def test_earcons_off_by_preference_sends_nothing(lab):
    lab = lab()
    lab.conv.input({"type": "oracle_v2.preferences", "earcons": False})
    receipt = [e for e in lab.down if e["type"] == "oracle_v2.preferences"][-1]
    assert receipt["earcons"] is False
    lab.user("Check the deploy")
    assert _cues(lab) == []


def test_earcons_off_by_config_sends_nothing(lab):
    lab = lab()
    lab.conv.earcons = False
    lab.user("Check the deploy")
    assert _cues(lab) == []


@pytest.mark.parametrize("value,accepted", [(True, True), (False, True), ("on", False), (1, False)])
def test_the_earcons_preference_must_be_a_boolean(value, accepted):
    event = mod.client_event(json.dumps({"type": "oracle_v2.preferences", "earcons": value}))
    assert (event == {"type": "oracle_v2.preferences", "earcons": value}) is accepted


# ---- a new call opens quietly (calls bfd47723 and cae1c237) -----------------
#
# bfd47723 opened 6.7 minutes after call 6bd3aeea, whose "... Ups" had been
# handed to Theo; Theo answered after that call ended. At 0.3 s, before the
# user said anything, the Host cued a result and relayed the old reply, and
# Oracle read it over the user.

NEW_CALL = json.loads((FIXTURES / "bfd47723_new_call_after_unfinished_delegation.json").read_text())
STALE_CALL = json.loads((FIXTURES / "cae1c237_hours_old_completion_at_open.json").read_text())
PRIOR_REPLY = NEW_CALL["prior_operation"]["result_text"]


def _completion(fixture, stale):
    notice = dict(fixture["unread_completion"])
    notice["reference_ts"] = 1_790_435_949_105 - notice.pop("age_ms_at_open")
    notice["stale"] = stale
    return notice


def _new_call(lab, **kwargs):
    return lab(prior_rows=[dict(NEW_CALL["prior_operation"])],
               completions=[_completion(NEW_CALL, stale=False)], **kwargs)


def _answer_the_first_turn(lab):
    lab.replay(NEW_CALL["events"])
    lab.ms = 10_000
    _oracle_says(lab, " Ja, jeg hører deg.", seconds=2)
    lab.wait(8)


def test_bfd47723_nothing_from_the_last_call_before_the_user_speaks(lab):
    lab = _new_call(lab)
    lab.wait(10)
    assert lab.appends() == [], "the call opens quietly"
    assert _cues(lab) == []
    lab.replay(NEW_CALL["events"])
    lab.wait(1)
    assert lab.appends() == [] and _cues(lab) == [], "nor while the user's first turn is unanswered"


def test_bfd47723_the_last_calls_result_is_background_never_relayed(lab):
    lab = _new_call(lab)
    _answer_the_first_turn(lab)
    lab.wait(60)
    assert lab.parts() == [] and _cues(lab) == []
    assert not any(e["type"] == "session.commentary.append" for e in lab.appends())
    notes = [e["content"] for e in lab.appends()]
    assert len(notes) == 1 and "Background from before this call" in notes[0]
    assert "Do not bring it up" in notes[0]
    assert lab.tools.dispatched == []


def test_bfd47723_the_router_can_serve_the_earlier_reply_when_asked(lab, monkeypatch):
    lab = _new_call(lab, strategy="operator")
    _answer_the_first_turn(lab)
    [record] = [r for r in lab.conv.tools.rows]
    assert lab.conv.task_record(record)["from_earlier_call"] is True
    _router_answers(monkeypatch, "read_result", {"operation_id": NEW_CALL["prior_operation"]["delegation_id"]})
    lab.user("Hva sa Theo?")
    [part] = lab.parts()
    assert _body(part) == PRIOR_REPLY
    assert NEW_CALL["prior_operation"]["delegation_id"] in _header(part)
    assert lab.tools.dispatched == []


def test_bfd47723_results_of_this_calls_work_still_relay_with_the_cue(lab):
    lab = _new_call(lab)
    _answer_the_first_turn(lab)
    lab.user("Check the deploy")
    assert _cues(lab) == ["handed_off"]
    lab.tools.rows.append({"delegation_id": "new-1", "session": "theo-97e5", "status": "completed",
                           "request_text": "Check the deploy", "result_text": "The deploy is green."})
    lab.wait(6)
    assert _cues(lab) == ["handed_off", "result"]
    [part] = lab.parts()
    assert "The deploy is green." in part and PRIOR_REPLY not in part


def test_bfd47723_a_prior_call_result_finishing_during_this_call_is_not_relayed(lab):
    row = dict(NEW_CALL["prior_operation"], status="running")
    lab = lab(prior_rows=[row])
    _answer_the_first_turn(lab)
    row["status"] = "completed"
    lab.wait(30)
    assert lab.parts() == [] and _cues(lab) == []


def test_cae1c237_an_hours_old_completion_is_never_volunteered(lab):
    lab = lab(completions=[_completion(STALE_CALL, stale=True)])
    lab.wait(5)
    lab.replay(STALE_CALL["events"])
    lab.wait(30)
    assert lab.appends() == []


def test_pending_decisions_wait_for_the_first_turn_too(lab, monkeypatch):
    lab = lab()
    monkeypatch.setattr(mod.oracle_attention, "pending_decisions", lambda: [{
        "decision_id": "d-1", "updated_at": 1, "title": "Pick one"}])
    monkeypatch.setattr(mod.oracle_attention, "context_text", lambda decision: "Pending decision d-1")
    lab.wait(10)
    assert lab.appends() == []
    lab.user("hello")
    lab.oracle_speaks(1)
    lab.wait(8)
    assert [e["content"] for e in lab.appends()][-1] == "Pending decision d-1"


class FakeMemory:
    """The durable thread: a checkpoint and the thread's linked work."""

    def __init__(self):
        self.state, self.rows = {}, []

    def reconcile(self):
        pass

    def load(self):
        return {"revision": 0, **self.state}

    def save(self, state):
        self.state = json.loads(json.dumps(state))

    def work(self):
        return list(self.rows)

    def observe(self, event, source_key):
        return True


def _reconnect(monkeypatch, memory, tools, after_ms):
    monkeypatch.setattr(mod, "wall_ms", lambda: 1_000_000 + after_ms)
    up = FakeUpstream()
    conv = mod.Conversation(up, lambda event: None, tools, "key", clock=lambda: 500.0,
                            delegation_strategy="direct_contact", memory=memory)
    return conv, up


@pytest.mark.parametrize("after_ms,relayed", [(5_000, True), (mod.CALL_RESUME_GRACE_SECONDS * 1000 + 1, False)])
def test_a_reconnect_within_the_grace_continues_the_calls_relay(monkeypatch, after_ms, relayed):
    monkeypatch.setattr(mod.oracle_attention, "pending_decisions", lambda: [])
    monkeypatch.setattr(mod.oracle_attention, "pending_completion_notifications", lambda: [])
    memory = FakeMemory()
    old = FakeTools([dict(NEW_CALL["prior_operation"])])
    monkeypatch.setattr(mod, "wall_ms", lambda: 1_000_000)
    first = mod.Conversation(FakeUpstream(), lambda event: None, old, "key", clock=lambda: 100.0,
                             delegation_strategy="direct_contact", memory=memory)
    old.delegations.add("new-1")          # delegated in this call
    first.checkpoint()
    first.stop.set(); first.pool.shutdown(wait=True)
    assert memory.state["call_work"] == ["new-1"]
    memory.rows = [dict(NEW_CALL["prior_operation"]),
                   {"delegation_id": "new-1", "session": "theo-97e5", "status": "completed",
                    "request_text": "Check the deploy", "result_text": "The deploy is green."}]
    tools = FakeTools()
    tools.rows = list(memory.rows)
    conv, up = _reconnect(monkeypatch, memory, tools, after_ms)
    try:
        conv.tick()
        bodies = [json.loads(raw)["content"] for raw in up.sent]
        assert any("The deploy is green." in body for body in bodies) is relayed
        assert not any(PRIOR_REPLY in body for body in bodies)
    finally:
        conv.stop.set(); conv.pool.shutdown(wait=True)

