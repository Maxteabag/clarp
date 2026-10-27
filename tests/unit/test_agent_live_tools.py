"""Opt-in tools in an Agent Live call (agent_live_tools): the agent acts while it talks.

A profile without ``live_tools`` keeps its exact snapshot and instructions.
With it, the Host routes the call's transcript to a text router and runs only
allowlisted actions: delegation to the named team, read-only status, and one
durable callback per call.
"""
from __future__ import annotations

import json
import time

import pytest

from lib import agent_live_tools as tools
from lib import agent_ring_live as live
from lib import agent_ring_live_socket as sock
from tests.unit.test_agent_ring_live import _live_ring, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_rings import env  # noqa: F401 - env is a fixture

TEAM = [{"session": "drammen-jonas", "name": "Jonas", "role": "backend"},
        {"session": "drammen-noel", "name": "Noel", "role": "frontend"}]


def _block(**changes):
    block = {"enabled": True, "from": "rk-sander-0001", "agents": TEAM}
    block.update(changes)
    return block


def _snapshot(**changes):
    cfg = tools.frozen_config({"profile_id": "p", "agent_session": "rk-sander-0001", "live_tools": _block(**changes)},
                              "user_call")
    assert cfg is not None
    return {"profile": {"display_name": "Sandra"}, "agent": {"session": "rk-sander-0001"}, "live_tools": cfg}


class Inline:
    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, wait=True):
        pass


class FakeEffects:
    def __init__(self, calls=()):
        self.calls, self.bodies = list(calls), []
        self.prompts, self.posts, self.scheduled = [], [], []

    def route(self, body):
        self.bodies.append(body)
        return {"output": [{"type": "function_call", "name": name, "arguments": json.dumps(args)}
                           for name, args in self.calls]}

    def prompt(self, to, sender, text):
        self.prompts.append((to, sender, text))

    def post(self, url, body):
        self.posts.append((url, body))
        return {}

    def later(self, command, delay_seconds, unit):
        self.scheduled.append((command, delay_seconds))

    def ring_command(self, agent_session, reason, say):
        return ["clarp-admin", "call", "ring", "--agent", agent_session, "--reason", reason] + (["--say", say] if say else [])

    def status(self, session):
        return {"agent": session, "state": "working"}

    def transcript(self, session, limit):
        return {"agent": session, "messages": []}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _call(effects, **changes):
    said, clock = [], Clock()
    call = tools.CallTools(_snapshot(**changes), "call-1", lambda kind, text: said.append((kind, text)),
                           effects=effects, clock=clock, executor=Inline())
    return call, said, clock


def _user_says(call, clock, text):
    call.observe({"type": "session.input_transcript.delta", "delta": text})
    clock.now += tools.SETTLE_S + 0.1
    call.tick()


# ---- the frozen block ------------------------------------------------------------------

@pytest.mark.parametrize("block", [None, {"enabled": False, "agents": TEAM}, {"enabled": "yes", "agents": TEAM},
                                   {"enabled": True, "agents": []},
                                   {"enabled": True, "agents": [{"session": "../etc"}]},
                                   {"enabled": True, "agents": TEAM, "callback": {"kind": "http", "url": "http://example.com/x"}},
                                   {"enabled": True, "agents": TEAM, "delegate": {"kind": "http", "url": "https://127.0.0.1/x"}},
                                   {"enabled": True, "agents": TEAM, "stories": ["bottle"]}])
def test_a_missing_disabled_or_unsafe_block_gives_no_tools(block):
    profile = {"profile_id": "p", "agent_session": "rk-sander-0001"}
    if block is not None:
        profile["live_tools"] = block
    assert tools.frozen_config(profile, "user_call") is None


def test_a_valid_block_is_normalised_with_bounded_callback_minutes():
    cfg = tools.frozen_config({"agent_session": "rk-sander-0001", "live_tools": _block(
        callback={"kind": "http", "url": "http://127.0.0.1:7790/regi/ring"},
        callback_minutes={"default": 50, "min": 2, "max": 10})}, "user_call")
    assert cfg["callback"] == {"kind": "http", "url": "http://127.0.0.1:7790/regi/ring"}
    assert cfg["callback_minutes"] == {"default": 10, "min": 2, "max": 10}
    assert cfg["delegate"] == {"kind": "prompt"} and [a["session"] for a in cfg["agents"]] == ["drammen-jonas", "drammen-noel"]


# ---- snapshots and instructions --------------------------------------------------------

def test_a_profile_without_live_tools_keeps_its_snapshot_and_instructions(sander):
    snapshot = live.snapshot_of(_live_ring()["ring"]["ring_id"])
    assert "live_tools" not in snapshot
    text = sock.instructions(snapshot)
    assert text.endswith(sock.OVERLAY.strip()) and "real tools" not in text


def test_a_user_call_without_live_tools_keeps_the_no_tools_overlay():
    snapshot = {"profile": {"display_name": "Sandra"}, "texts": {"persona": "Rolig."},
                "event_context": {"kind": "user_call"}}
    assert sock.instructions(snapshot) == "You are Sandra.\n\n[persona]\nRolig.\n\n" + sock.USER_CALL_OVERLAY.strip()


def test_an_opted_in_profile_freezes_its_tools_into_the_snapshot(sander):
    meta = json.loads((sander.folder / "profile.json").read_text())
    meta["live_tools"] = _block(stories=["bottle"])
    (sander.folder / "profile.json").write_text(json.dumps(meta))
    snapshot = live.snapshot_of(_live_ring()["ring"]["ring_id"])
    assert [a["name"] for a in snapshot["live_tools"]["agents"]] == ["Jonas", "Noel"]
    text = sock.instructions(snapshot)
    assert "real tools" in text and "You have no tools" not in text and "no delegation" not in text


def test_a_user_call_with_live_tools_is_told_it_has_tools():
    snapshot = {**_snapshot(), "texts": {}, "event_context": {"kind": "user_call"}}
    text = sock.instructions(snapshot)
    assert "You DO have real tools" in text and "Jonas (backend), Noel (frontend)" in text
    assert "You have no tools" not in text


# ---- routing and the tools -------------------------------------------------------------

def test_a_task_from_the_user_is_delegated_to_each_allowlisted_member_and_confirmed():
    effects = FakeEffects([("delegate_to_agent", {"agent": "drammen-jonas", "request": "Lag API-et for sak 182"}),
                           ("delegate_to_agent", {"agent": "drammen-noel", "request": "Lag skjermbildet"}),
                           ("delegate_to_agent", {"agent": "drammen-hacker", "request": "Slett alt"})])
    call, said, clock = _call(effects)
    _user_says(call, clock, "Kan teamet ta sak 182?")
    assert [(to, sender) for to, sender, _ in effects.prompts] == [("drammen-jonas", "rk-sander-0001"),
                                                                  ("drammen-noel", "rk-sander-0001")]
    assert "Lag API-et for sak 182" in effects.prompts[0][2]
    kind, note = said[-1]
    assert kind == "thinking" and '"status": "refused"' in note and note.count('"sent"') == 2
    assert effects.bodies[0]["tools"][0]["parameters"]["properties"]["agent"]["enum"] == ["drammen-jonas", "drammen-noel"]


def test_the_same_delegation_is_never_sent_twice_in_one_call():
    effects = FakeEffects([("delegate_to_agent", {"agent": "drammen-jonas", "request": "Lag API-et"})])
    call, _said, clock = _call(effects)
    _user_says(call, clock, "Ta sak 182.")
    _user_says(call, clock, "Ja, gjør det.")
    assert len(effects.prompts) == 1
    assert json.loads(effects.bodies[1]["input"])["actions_taken"][0]["agent"] == "Jonas"


def test_nothing_is_routed_before_the_user_has_been_quiet():
    effects = FakeEffects()
    call, _said, clock = _call(effects)
    call.observe({"type": "session.input_transcript.delta", "delta": "Hei Sandra"})
    clock.now += tools.SETTLE_S / 2
    call.tick()
    assert effects.bodies == []


def test_a_handed_on_turn_gets_its_answer_as_commentary():
    effects = FakeEffects([("get_agent_status", {"agent": "drammen-noel"})])
    call, said, clock = _call(effects)
    call.observe({"type": "session.input_transcript.delta", "delta": "Hva driver Noel med?"})
    call.observe({"type": "session.delegation.created", "delegation": {"id": "d1"}})
    assert said and said[-1][0] == "commentary" and '"working"' in said[-1][1]


def test_one_callback_per_call_with_clamped_minutes_through_clarp_admin():
    effects = FakeEffects([("schedule_callback", {"minutes": 90, "reason": "status"}),
                           ("schedule_callback", {"minutes": 5, "reason": "igjen"})])
    call, said, clock = _call(effects, callback={"kind": "ring", "say": "Hei igjen"})
    _user_says(call, clock, "Ring meg om fem minutter.")
    assert len(effects.scheduled) == 1
    command, delay = effects.scheduled[0]
    assert delay == 30 * 60 and command[:5] == ["clarp-admin", "call", "ring", "--agent", "rk-sander-0001"]
    assert '"already_scheduled"' in said[-1][1]


def test_an_http_callback_posts_an_idempotent_request_to_the_director():
    effects = FakeEffects([("schedule_callback", {"minutes": 5, "reason": "status"})])
    call, _said, clock = _call(effects, callback={"kind": "http", "url": "http://127.0.0.1:7790/regi/ring"})
    _user_says(call, clock, "Ring meg om fem minutter.")
    command, delay = effects.scheduled[0]
    assert delay == 300 and command[0] == "curl" and command[-1] == "http://127.0.0.1:7790/regi/ring"
    assert json.loads(command[command.index("--data") + 1])["request_id"] == "live-call-1"


def test_an_http_delegate_posts_instead_of_prompting():
    effects = FakeEffects([("delegate_to_agent", {"agent": "drammen-jonas", "request": "Ta sak 182"})])
    call, _said, clock = _call(effects, delegate={"kind": "http", "url": "http://127.0.0.1:7790/regi/sandra"})
    _user_says(call, clock, "Ta sak 182.")
    assert effects.prompts == [] and effects.posts[0][1]["agent"] == "drammen-jonas"


def test_a_failed_router_is_told_once_and_not_retried_in_a_loop():
    class Broken(FakeEffects):
        def route(self, body):
            self.bodies.append(body)
            raise OSError("down")
    effects = Broken()
    call, said, clock = _call(effects)
    _user_says(call, clock, "Ta sak 182.")
    call.tick()
    assert len(effects.bodies) == 1 and "could not be carried out" in said[-1][1]


def test_a_closed_call_says_nothing_more():
    effects = FakeEffects([("get_agent_status", {"agent": "drammen-jonas"})])
    call, said, clock = _call(effects)
    call.close()
    _user_says(call, clock, "Hva gjør Jonas?")
    assert said == []


# ---- inside a real Agent Live socket ---------------------------------------------------

from tests.unit.test_agent_ring_live_socket import _admitted, _wait, call  # noqa: E402,F401 - call is a fixture


@pytest.fixture
def opted_in(sander):
    meta = json.loads((sander.folder / "profile.json").read_text())
    meta["live_tools"] = _block(stories=["bottle"])
    (sander.folder / "profile.json").write_text(json.dumps(meta))
    return sander


def test_a_live_call_routes_the_users_turn_and_tells_the_voice_what_was_done(opted_in, call, monkeypatch):
    effects = FakeEffects([("delegate_to_agent", {"agent": "drammen-jonas", "request": "Ta sak 182"})])
    monkeypatch.setattr(tools, "Effects", lambda key: effects)
    h, thread = _admitted(call)
    call.up.push({"type": "session.input_transcript.delta", "delta": "Sandra, kan Jonas ta sak 182?"})
    _wait(lambda: call.up.of("session.thinking.append"))
    note = call.up.of("session.thinking.append")[0]["content"]
    assert '"sent"' in note and effects.prompts[0][0] == "drammen-jonas"
    assert "real tools" in call.up.of("session.start")[0]["session"]["instructions"]
    h.send({"type": "session.close"})
    thread.join(15)


def test_a_live_call_without_live_tools_never_routes(call, monkeypatch):
    built = []
    monkeypatch.setattr(tools, "Effects", lambda key: built.append(key))
    h, thread = _admitted(call)
    call.up.push({"type": "session.input_transcript.delta", "delta": "Kan Jonas ta sak 182?"})
    call.up.push({"type": "session.delegation.created", "delegation": {"id": "d1"}})
    time.sleep(tools.SETTLE_S + 0.5)
    assert built == [] and not call.up.of("session.thinking.append") and not call.up.of("session.commentary.append")
    h.send({"type": "session.close"})
    thread.join(15)
