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

    def resolve(self, name):
        if str(name or "").strip().casefold() in ("theo", "theo-97e5"):
            return {"session": "theo-97e5", "persona": "Theo", "agent_id": "a-theo"}
        raise ValueError("Unknown or ambiguous agent; use a session from list_agents")

    def execute(self, name, arguments, call_id):
        self.calls.append((name, dict(arguments)))
        if name == "list_agents":
            return {"agents": [{"name": "Theo", "session": "theo-97e5"}], "oracle_contact": "theo-97e5"}
        if name in ("investigate_with_oracle", "delegate_to_agent"):
            self.dispatched.append(dict(arguments))
            return {"status": "accepted", "operation_id": "new-" + str(len(self.dispatched)),
                    "session": "theo-97e5"}
        if name == "read_agent_transcript":
            self.resolve(arguments.get("agent"))
            return {"agent": "Theo", "messages": self.transcript, "newest_message_age": "2 minutes ago",
                    "truncated": False}
        raise AssertionError("unexpected tool " + name)


class Lab:
    def __init__(self, monkeypatch, *, rows=(), transcript=(), strategy="direct_contact"):
        monkeypatch.setattr(mod.oracle_attention, "pending_decisions", lambda: [])
        monkeypatch.setattr(mod.oracle_attention, "pending_completion_notifications", lambda: [])
        self.now = [100.0]
        self.sent, self.down = [], []
        self.tools = FakeTools(rows, transcript)
        self.conv = mod.Conversation(type("Up", (), {"send": self.sent.append})(), self.down.append,
                                     self.tools, "key", clock=lambda: self.now[0],
                                     delegation_strategy=strategy)
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

    def appends(self):
        return [event for event in map(json.loads, self.sent) if event["type"].endswith(".append")
                and event["type"] != "session.input_audio.append"]

    def parts(self):
        return [event["content"] for event in self.appends() if "\nText:\n" in event["content"]]


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


def test_cedb186d_full_reply_meta_turns_and_no_redelegation(lab):
    """The 2026-09-26 call: a 3.4 KB reply, then "you stopped", "read the
    transcript", "are you sure". Every word arrives in order across parts,
    no meta turn becomes a new delegation, and completion is only claimed by
    the last part."""
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

    # Oracle stops mid-part-2 and the user says so before the quiet gate.
    lab.oracle_speaks(4)
    lab.user("I think you stopped mid-sentence")
    assert f"part 3 of {total}" in lab.parts()[-1]
    assert "finish it first" in _header(lab.parts()[-1])

    # The rest arrives part by part as Oracle finishes each one.
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

    # "read the transcript" replays the stored reply word for word from part 1.
    lab.user("No, just read the transcript to me")
    replay = lab.parts()[total:]
    assert len(replay) == 1 and "part 1 of" in replay[0] and "word for word" in _header(replay[0])
    for _ in range(total):
        lab.oracle_speaks(6)
        lab.wait(2)
    replay = lab.parts()[total:]
    assert "".join(_body(p) for p in replay) == THEO_REPLY
    assert all("word for word" in _header(p) for p in replay)

    # "are you sure" is answered from the cursor, not by asking Theo again.
    lab.user("Are you sure")
    status = lab.appends()[-1]["content"]
    assert f"All {total} parts" in status and "was the end" in status

    assert lab.tools.dispatched == []
    assert not any(name == "investigate_with_oracle" for name, _ in lab.tools.calls)


def test_are_you_sure_with_parts_pending_never_claims_completion(lab):
    lab = lab(rows=[_theo_row()])
    lab.wait(STEP)
    lab.oracle_speaks(3)
    lab.user("Are you sure?")          # user took the floor during part 1
    notes = [e["content"] for e in lab.appends()]
    assert any("not complete" in note and "more remains" in note.casefold() for note in notes)
    assert "part 2 of" in lab.parts()[-1]
    assert lab.tools.dispatched == []


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


def test_read_agent_transcript_reads_without_prompting_the_agent(lab):
    transcript = [{"role": "user", "timestamp": "2026-09-26T10:00:00Z", "text": "Can you check the vault notes?"},
                  {"role": "assistant", "timestamp": "2026-09-26T10:01:00Z", "text": "The vault has two relevant notes."}]
    lab = lab(transcript=transcript)
    lab.user("Read Theo's recent conversation")
    assert ("read_agent_transcript", {"agent": "Theo", "limit": oracle_relay.TRANSCRIPT_LIMIT}) in lab.tools.calls
    assert lab.tools.dispatched == []
    [part] = lab.parts()
    assert "Can you check the vault notes?" in part and "The vault has two relevant notes." in part
    assert "end of" in _header(part) or "complete" in _header(part)


@pytest.mark.parametrize("words", ["Ask Theo to check the deploy", "Tell Theo to continue the migration",
                                   "Read the config file and tell me what port it uses"])
def test_substantive_requests_still_go_to_the_primary(lab, words):
    lab = lab(rows=[_theo_row()])
    lab.wait(STEP)
    lab.user(words)
    assert len(lab.tools.dispatched) == 1


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


@pytest.mark.parametrize("words,expected", [
    ("I think you stopped mid-sentence", ("continue", None)),
    ("Okay, you stopped again", ("continue", None)),
    ("Oh yeah. Because in the text I'm reading, he has more", ("continue", None)),
    ("keep going", ("continue", None)),
    ("No, just read the transcript to me", ("replay", None)),
    ("Okay, so you kinda summarized his idea. Can we have him say that in his own words", ("replay", None)),
    ("No, you already have it. You already have his response. You don't need to prompt him", ("replay", None)),
    ("read it again", ("replay", None)),
    ("Are you sure", ("status", None)),
    ("Is that all?", ("status", None)),
    ("Read Theo's recent conversation", ("transcript", "theo")),
    ("just look at what Omar said", ("transcript", "omar")),
    ("Tell Theo to continue the migration", None),
    ("Read the config file and tell me what port it uses", None),
    ("What's going on with the deploy", None),
    ("go ahead", None),
    ("what did he say", None),
    ("No, just read the transcript to me", ("replay", None)),
    ("I think you stopped reading", ("continue", None)),
    # Substantive work that merely contains a meta phrase must reach the primary:
    # a missed meta turn only costs the old behaviour, a false one swallows work.
    ("Tell Theo to rewrite the doc in his own words", None),
    ("Ask Theo to summarise the report word for word", None),
    ("Ask him to rewrite it in his own words", None),
    ("Ask her to rewrite it word for word", None),
    ("Can we have her read that word for word", ("replay", None)),
    ("Ask Theo what else did he say in the meeting notes", None),
    ("Have Theo read the transcript of the standup and summarise it", None),
    ("Can Theo read me the full text of the README", None),
    ("tell Theo there is more work on the migration", None),
    ("Ask Omar to check what Theo said", None),
    ("Tell Theo you stopped the build", None),
    ("I think you stopped the server", None),
    ("check what Theo said about the budget and fix it", None),
    ("read the transcript of the standup", None),
])
def test_meta_turn_classifier(words, expected):
    assert oracle_relay.classify(words) == expected


def test_a_meta_turn_split_across_fragments_is_still_served(lab):
    """cedb186d: "No, just read the transcript to" and "me" arrived as two fragments."""
    lab = lab(rows=[_theo_row()])
    lab.wait(STEP)
    lab.oracle_speaks(8)
    lab.wait(2)
    before = len(lab.parts())
    lab.user_fragments("No, just read the transcript to", "me")
    assert lab.tools.dispatched == []
    assert "part 1 of" in lab.parts()[before] and "word for word" in _header(lab.parts()[before])


def test_a_meta_phrase_after_unrouted_work_does_not_swallow_the_work(lab):
    lab = lab(rows=[_theo_row()])
    lab.wait(STEP)
    lab.user_fragments("Ask Theo to draft the goals page", "and read it back to me")
    assert len(lab.tools.dispatched) == 1


def test_long_utterances_are_never_meta_turns():
    assert oracle_relay.classify("keep going " + "and then we talk about the roadmap " * 6) is None


def test_every_part_fits_one_append_even_for_multibyte_text():
    text = "Ferdig – alt OK ✓ æøå 日本語テキスト. " * 200
    relay = oracle_relay.Relay("op", "Théo's reply", "operation op, status completed", text, request="r" * 200)
    parts = [relay.part(i, served=True, resumed=True, pause_after=True) for i in range(relay.total)]
    assert all(len(part) <= oracle_relay.APPEND_CHARS for part in parts)
    assert "".join(_body(part) for part in parts) == text
