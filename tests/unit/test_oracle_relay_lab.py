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
    """One GPT-Live session socket: what the Host sent it, and whether it was closed."""

    def __init__(self, session=None):
        self.session = session
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
        self.open_error = None
        self.tools = FakeTools(prior_rows, transcript)
        self.conv = mod.Conversation(self.upstreams[0], self.down.append,
                                     self.tools, "key", clock=lambda: self.now[0],
                                     delegation_strategy=strategy)
        for row in rows:
            self.tools.rows.append(row)
            self.tools.delegations.add(row["delegation_id"])
        self.conv.open_upstream = self.open_upstream
        self.ms = 0
        self.delegation = 0

    def open_upstream(self, session):
        """The fake GPT-Live: a new session per swap, or the scripted failure."""
        if self.open_error is not None:
            raise self.open_error
        upstream = FakeUpstream(session)
        self.upstreams.append(upstream)
        return upstream, "live_" + str(len(self.upstreams))

    def opened(self):
        return [up.session for up in self.upstreams[1:]]

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



# ---- talking to an agent directly (per-agent voices) ------------------------

def _session_voice(session):
    return session["audio"]["output"]["voice"]


def _history_text(session):
    return json.dumps(session.get("input", []))


def _switch_to_theo(lab):
    lab.user("Put me through to Theo")
    lab.oracle_speaks(1.5)   # "Putting you through to Theo."
    lab.wait(2)


def test_put_me_through_opens_theo_in_his_own_voice_on_the_same_phone_line(lab):
    lab = lab()
    lab.user("so the plan is recursive goals")
    lab.tools.dispatched.clear()
    lab.user("Put me through to Theo")
    assert lab.tools.dispatched == [], "a switch is not work for the primary"
    assert any("Connecting you to Theo" in e["content"] for e in lab.appends(0))
    assert lab.opened() == [], "the swap waits for Oracle to say it is putting the user through"
    old_session = lab.conv.provider_session
    lab.oracle_speaks(1.5)
    lab.wait(2)

    [session] = lab.opened()
    assert _session_voice(session) == "meridian"
    assert "You are the voice of Theo" in session["instructions"]
    assert "recursive goals" in _history_text(session)
    assert lab.conv.upstream is lab.upstreams[1]
    assert lab.upstreams[0].closed
    assert lab.conv.provider_session not in (None, old_session)
    contact = [e for e in lab.down if e["type"] == "oracle_v2.contact"]
    assert contact == [{"type": "oracle_v2.contact", "agent": "Theo", "session": "theo-97e5",
                        "voice": "meridian"}]
    types = lab.down_types()
    last_audio = max(i for i, kind in enumerate(types) if kind == "session.output_audio.delta")
    assert "oracle_v2.quiet" in types[last_audio:], "the phone must not be left in a speaking state"

    # The retired session's close never reaches the phone or ends the call.
    before = len(lab.down)
    lab.conv.receive({"type": "session.closed", "reason": "close_requested"}, source=lab.upstreams[0])
    lab.conv.receive({"type": "session.output_audio.delta", "delta": _audio(3000)}, source=lab.upstreams[0])
    assert lab.down[before:] == []
    assert "session.closed" not in lab.down_types()
    assert not lab.conv.closed.is_set() and not lab.conv.stop.is_set()

    # From now on substantive turns go to Theo, and the receipt goes to his session.
    admissions_before = sum("Work admission" in e["content"] for e in lab.appends(0))
    lab.user("What is the status of the deploy?")
    [work] = lab.tools.dispatched
    assert work["agent"] == "theo-97e5" and "What is the status of the deploy?" in work["request"]
    assert any("Work admission" in e["content"] for e in lab.appends(1))
    assert sum("Work admission" in e["content"] for e in lab.appends(0)) == admissions_before


def test_back_to_oracle_restores_marin_and_oracles_instructions(lab):
    lab = lab()
    _switch_to_theo(lab)
    lab.user("Back to Oracle")
    assert lab.tools.dispatched == []
    lab.oracle_speaks(1)
    lab.wait(2)
    assert [_session_voice(s) for s in lab.opened()] == ["meridian", "marin"]
    back = lab.opened()[1]
    assert "You are the voice of" not in back["instructions"]
    assert "Put me through to Theo" in _history_text(back)
    assert lab.conv.upstream is lab.upstreams[2] and lab.upstreams[1].closed
    assert [e for e in lab.down if e["type"] == "oracle_v2.contact"][-1] == {
        "type": "oracle_v2.contact", "agent": None, "session": None, "voice": "marin"}
    lab.user("Check the deploy")
    assert lab.tools.dispatched and "agent" not in lab.tools.dispatched[-1]


def test_a_second_agent_gets_a_different_voice_than_the_first(lab):
    lab = lab()
    _switch_to_theo(lab)
    lab.user("Put me through to Nadia")
    lab.oracle_speaks(1)
    lab.wait(2)
    assert [_session_voice(s) for s in lab.opened()] == ["meridian", "willow"]
    assert "You are the voice of Nadia" in lab.opened()[1]["instructions"]


def test_asking_for_the_agent_already_on_the_line_does_not_reopen(lab):
    lab = lab()
    _switch_to_theo(lab)
    lab.user("Put me through to Theo")
    lab.wait(8)
    assert len(lab.opened()) == 1


def test_relay_parts_pending_across_a_switch_continue_in_theos_session(lab):
    lab = lab(rows=[_theo_row()])
    lab.wait(STEP)
    lab.oracle_speaks(8)            # part 1 spoken in full
    lab.wait(2)
    assert len(lab.parts(0)) == 2   # part 2 sent, not yet spoken
    _switch_to_theo(lab)
    old, new = lab.parts(0), lab.parts(1)
    assert [p.split(" of ")[0].rsplit("part ", 1)[1] for p in old] == ["1", "2"]
    assert new and "part 2 of" in new[0], "the unspoken part is resent to the new voice"
    total = len(oracle_relay.Relay("k", "x", "y", THEO_REPLY).chunks)
    for _ in range(total):
        lab.oracle_speaks(6)
        lab.wait(2)
    new = lab.parts(1)
    assert "".join(_body(p) for p in old[:1] + new) == THEO_REPLY
    assert len(new) == total - 1
    assert lab.tools.dispatched == []


def test_a_reply_spoken_before_the_switch_is_not_repeated_after_it(lab):
    lab = lab(rows=[_theo_row()])
    lab.wait(STEP)
    total = len(oracle_relay.Relay("k", "x", "y", THEO_REPLY).chunks)
    for _ in range(total):
        lab.oracle_speaks(6)
        lab.wait(2)
    assert len(lab.parts(0)) == total
    _switch_to_theo(lab)
    lab.wait(10)
    assert len(lab.opened()) == 1 and lab.parts(1) == []


def test_work_requests_that_mention_talking_to_someone_do_not_switch(lab):
    lab = lab()
    lab.user("Ask Theo to talk to Lena")
    lab.oracle_speaks(1)
    lab.wait(8)
    assert lab.opened() == []
    assert len(lab.tools.dispatched) == 1


def test_a_failed_swap_keeps_the_call_on_the_current_session_and_says_so(lab):
    lab = lab()
    lab.open_error = OSError("upstream refused")
    lab.user("Put me through to Theo")
    lab.oracle_speaks(1.5)
    lab.wait(2)
    assert lab.opened() == []
    assert lab.conv.upstream is lab.upstreams[0] and not lab.upstreams[0].closed
    assert not lab.conv.stop.is_set()
    assert any("could not put the user through to Theo" in e["content"] for e in lab.appends(0))
    assert lab.conv.contact is None
    lab.user("Check the deploy")            # the call still works as Oracle
    assert len(lab.tools.dispatched) == 1 and "agent" not in lab.tools.dispatched[0]


def test_narration_off_switches_without_an_announcement(lab):
    lab = lab()
    lab.conv.input({"type": "oracle_v2.preferences", "narration": "off"})
    lab.user("Let me talk to Theo directly")
    assert not any("Connecting you" in e["content"] for e in lab.appends(0))
    lab.wait(STEP)
    [session] = lab.opened()
    assert _session_voice(session) == "meridian"
    assert any(mod.NARRATION_OFF in e["content"] for e in lab.appends(1))


def test_config_override_picks_the_agent_voice(lab):
    lab = lab()
    lab.conv.voice_overrides = {"theo": "ash"}
    _switch_to_theo(lab)
    assert _session_voice(lab.opened()[0]) == "ash"


def test_operator_router_can_switch_contact(lab, monkeypatch):
    lab = lab(strategy="operator")
    body = {"output": [{"type": "function_call", "name": "switch_contact", "call_id": "c1",
                        "arguments": json.dumps({"agent": "Theo"})}]}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(mod, "urlopen", lambda *a, **k: Response(json.dumps(body).encode()))
    lab.user("could I have a word with Theo himself")
    lab.oracle_speaks(1.5)
    lab.wait(2)
    assert [_session_voice(s) for s in lab.opened()] == ["meridian"]
    assert lab.tools.dispatched == []
    assert any(t["name"] == "switch_contact" for t in mod.router_tools())


class _Socket:
    """A scripted upstream for pump(): frames, then close."""

    def __init__(self, frames, conversation=None, swap_to=None):
        self.frames, self.conversation, self.swap_to = list(frames), conversation, swap_to

    def recv(self):
        if self.swap_to is not None and self.conversation.upstream is self:
            self.conversation.upstream = self.swap_to
            raise ConnectionError("socket closed by swap")
        return self.frames.pop(0) if self.frames else ""


def test_pump_follows_the_swapped_upstream_instead_of_ending_the_call():
    events = []

    class Conv:
        stop, closed = threading.Event(), threading.Event()

        def receive(self, event, source=None):
            events.append((event["type"], source))
            if event["type"] == "session.closed":
                self.closed.set()
    conv = Conv()
    new = _Socket([json.dumps({"type": "session.output_transcript.delta", "delta": "Theo here"}),
                   json.dumps({"type": "session.closed"})])
    conv.upstream = _Socket([], conv, swap_to=new)
    mod.pump_upstream(conv, TimeoutError)
    assert events == [("session.output_transcript.delta", new), ("session.closed", new)]


# ---- call 7946a1a7: "can you put me through to, can you put me through to Marcus"

MARCUS_CALL = json.loads((FIXTURES / "7946a1a7_put_me_through_to_marcus.json").read_text())["events"]


def test_7946a1a7_a_restarted_switch_request_split_by_a_pause_puts_the_user_through(lab):
    lab = lab()
    lab.replay(MARCUS_CALL, until_seq=62)
    assert lab.tools.dispatched == [], "a switch request is never also sent to the primary as work"
    assert lab.conv.pending_swap is not None and lab.conv.pending_swap["contact"]["persona"] == "Marcus"
    lab.replay(MARCUS_CALL, until_seq=74)   # Oracle: "Sure, put you through to Marcus."
    lab.wait(2)
    [session] = lab.opened()
    assert _session_voice(session) == "cedar"
    assert "You are the voice of Marcus" in session["instructions"]
    assert lab.conv.contact["session"] == "marcus-5b1a"


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


def test_a_name_after_a_dangling_switch_phrase_and_oracles_go_on_switches(lab):
    lab = lab()
    lab.user_fragments("Can you put me through to")
    lab.tools.dispatched.clear()
    lab.conv.receive({"type": "session.output_transcript.delta", "delta": " Go on.",
                      "start_ms": lab.ms, "end_ms": lab.ms + 400})
    lab.user_fragments("Marcus")
    assert lab.tools.dispatched == []
    assert lab.conv.pending_swap["contact"]["persona"] == "Marcus"


def test_a_switch_to_an_unknown_agent_is_not_sent_to_the_primary(lab):
    lab = lab()
    lab.user("Can you put me through to Zorblax")
    assert lab.tools.dispatched == []
    assert lab.opened() == [] and lab.conv.pending_swap is None
    assert any("no agent called Zorblax" in e["content"] for e in lab.appends(0))


def _oracle_says(lab, text, seconds=1.5):
    lab.ms += 1000
    lab.conv.receive({"type": "session.output_transcript.delta", "delta": text,
                      "start_ms": lab.ms, "end_ms": lab.ms + 400})
    lab.oracle_speaks(seconds)


def _user_says(lab, text):
    lab.ms += 1000
    lab.conv.receive({"type": "session.input_transcript.delta", "delta": text,
                      "start_ms": lab.ms, "end_ms": lab.ms + 400})


def _host_notes(lab, needle, index=0):
    return [e for e in lab.appends(index) if needle in e["content"]]


def test_oracle_claiming_a_switch_the_host_never_started_is_corrected(lab):
    lab = lab()
    lab.user("Check the deploy")                     # routed as work, no switch
    _oracle_says(lab, " Sure—put you through to Marcus.", seconds=1)
    lab.wait(0.5)
    assert _host_notes(lab, "could not connect") == [], "the Host gives the switch a moment to start"
    lab.wait(2)
    [note] = _host_notes(lab, "could not connect")
    assert "still talking to Oracle" in note["content"]
    lab.wait(10)
    assert len(_host_notes(lab, "could not connect")) == 1


def test_oracles_own_announcement_of_a_real_switch_is_not_corrected(lab):
    lab = lab()
    lab.user("Put me through to Theo")
    _oracle_says(lab, " Connecting you to Theo.")
    lab.wait(3)
    assert len(lab.opened()) == 1
    assert _host_notes(lab, "could not connect") == []


def test_a_failed_swap_tells_the_user_they_are_still_with_oracle(lab):
    lab = lab()
    lab.open_error = OSError("upstream refused")
    lab.user("Put me through to Theo")
    lab.oracle_speaks(1.5)
    lab.wait(2)
    [note] = _host_notes(lab, "could not put the user through to Theo")
    assert "still talking to Oracle" in note["content"]


def test_a_user_talking_into_silence_gets_a_nudge_once(lab):
    """7946a1a7: "Hey Marcus, how are you", "Marcus, are you there", "Hello"
    for 35 s with no delegation and no reply."""
    lab = lab()
    _oracle_says(lab, " Sure.")
    lab.wait(2)
    _user_says(lab, " Hey Marcus, how are you")
    lab.wait(5)
    assert _host_notes(lab, "no reply") == []
    lab.wait(4)
    [note] = _host_notes(lab, "no reply")
    _user_says(lab, " Marcus, are you there")
    lab.wait(12)
    assert len(_host_notes(lab, "no reply")) == 1, "one nudge until Oracle speaks again"
    _oracle_says(lab, " Sorry, I'm here.")
    _user_says(lab, " Hello")
    lab.wait(10)
    assert len(_host_notes(lab, "no reply")) == 2


def test_a_delegated_turn_is_not_nudged(lab):
    lab = lab()
    lab.user("Check the deploy")
    lab.wait(15)
    assert _host_notes(lab, "no reply") == []


def test_switch_announcement_says_connecting_not_done(lab):
    lab = lab()
    lab.user("Put me through to Theo")
    [note] = _host_notes(lab, "Connecting you to Theo")
    assert "Putting you through" not in note["content"]


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


def test_a_switch_plays_started_then_a_connected_chime_and_back_plays_falling(lab):
    lab = lab()
    _switch_to_theo(lab)
    assert _cues(lab) == ["switch_started", "connected"]
    connected = next(e for e in lab.down if e.get("name") == "connected")
    assert connected["agent"] == "Theo"
    lab.user("Back to Oracle")
    lab.oracle_speaks(1)
    lab.wait(2)
    assert _cues(lab) == ["switch_started", "connected", "switch_started", "back_to_oracle"]
    _cue_audio_follows_each_cue(lab)


def test_a_failed_switch_plays_the_low_double_tone(lab):
    lab = lab()
    lab.open_error = OSError("upstream refused")
    lab.user("Put me through to Theo")
    lab.oracle_speaks(1.5)
    lab.wait(2)
    assert _cues(lab) == ["switch_started", "switch_failed"]


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
    _switch_to_theo(lab)
    lab.user("Check the deploy")
    assert _cues(lab) == []
    assert len(lab.opened()) == 1


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


@pytest.mark.parametrize("words", ["What did Theo say?", "Any updates?", "Hva sa Theo?"])
def test_bfd47723_asking_about_it_serves_the_stored_reply(lab, words):
    lab = _new_call(lab)
    _answer_the_first_turn(lab)
    lab.user(words)
    [part] = lab.parts()
    assert _body(part) == PRIOR_REPLY
    assert NEW_CALL["prior_operation"]["delegation_id"] in _header(part)
    assert lab.tools.dispatched == []
    assert not any(name == "read_agent_transcript" for name, _ in lab.tools.calls)


def test_bfd47723_asking_before_anything_was_said_still_serves_it(lab):
    lab = _new_call(lab)
    lab.wait(2)
    lab.user("What did Theo say")
    assert _body(lab.parts()[0]) == PRIOR_REPLY


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


@pytest.mark.parametrize("words,expected", [
    ("Any updates?", True), ("Okay, anything new?", True), ("What's new", True),
    ("Has anyone gotten back to me?", True), ("Noe nytt?", True),
    ("Any updates on the deploy? Ask Theo to check it", False),
    ("Tell Theo there is news", False), ("Update the config", False),
    ("any new ideas for the roadmap", False),
])
def test_update_questions(words, expected):
    assert oracle_relay.asks_for_updates(words) is expected
