"""Opt-in tools in an Agent Live call (agent_live_tools): the agent acts while it talks.

A profile without ``live_tools`` keeps its exact snapshot and instructions.
With it, the Host routes the call's transcript to a text router and runs only
allowlisted actions, as the profile's own agent, behind the call's lifecycle
fence; delegations and call backs are durable operations that are never
repeated and never resent blindly when their outcome is unknown.
"""
from __future__ import annotations

import json
import time

import pytest

from lib import agent_live_tools as tools
from lib import agent_ring_live as live
from lib import agent_ring_live_socket as sock
from tests.unit.test_agent_ring_live import _live_ring, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_rings import PHONE, env  # noqa: F401 - env is a fixture

TEAM = [{"session": "drammen-jonas", "name": "Jonas", "role": "backend"},
        {"session": "drammen-noel", "name": "Noel", "role": "frontend"}]
DIRECTOR = "http://127.0.0.1:7790/regi/sandra/deleger"
RING = "http://127.0.0.1:7790/regi/sandra/tilbakering"
ASK = ("delegate_to_agent", {"agent": "drammen-jonas", "request": "Ta sak 182"})
CALL_BACK = ("schedule_callback", {"minutes": 5, "reason": "status"})


def _block(**changes):
    block = {"enabled": True, "from": "rk-sander-0001", "agents": TEAM}
    block.update(changes)
    return block


def _config(**changes):
    return tools.frozen_config({"profile_id": "p", "agent_session": "rk-sander-0001", "live_tools": _block(**changes)},
                               "user_call")


def _snapshot(**changes):
    cfg = _config(**changes)
    assert cfg is not None
    return {"profile": {"display_name": "Sandra"}, "agent": {"session": "rk-sander-0001"}, "host_id": "host-1",
            "live_tools": cfg}


class Inline:
    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class FakeEffects:
    def __init__(self, calls=()):
        self.calls, self.bodies = list(calls), []
        self.prompts, self.posts = [], []
        self.post_error = self.prompt_error = None
        self.url_errors: dict[str, Exception] = {}
        self.inbox: list[tuple[str, str]] = []      # what really reached members (for reconcile)
        self.recipients = {"device_phone", "device_other"}
        self.on_route = None

    def route(self, body):
        self.bodies.append(body)
        if self.on_route:
            self.on_route()
        return {"output": [{"type": "function_call", "name": name, "arguments": json.dumps(args)}
                           for name, args in self.calls]}

    def prompt(self, to, sender, text):
        self.prompts.append((to, sender, text))
        if self.prompt_error is not None:
            error, arrived = self.prompt_error
            if arrived:
                self.inbox.append((to, text))
            raise error
        self.inbox.append((to, text))

    def delivered(self, to, marker):
        return any(t == to and marker in text for t, text in self.inbox)

    def post(self, url, body, timeout=30):
        self.posts.append((url, body))
        error = self.url_errors.get(url, self.post_error)
        if error is not None:
            raise error
        return {"ok": True, "result": "accepted"}

    def to(self, url):
        return [body for u, body in self.posts if u == url]

    def recipient_ok(self, principal):
        return principal in self.recipients

    def status(self, session):
        return {"agent": session, "state": "working"}

    def transcript(self, session, limit):
        return {"agent": session, "messages": []}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def ledger(tmp_path):
    return tools.Ledger(tmp_path / "tool-ledger.json")


@pytest.fixture
def make(ledger):
    def build(effects, call_id="call-1", principal="device_phone", current=lambda: True, **changes):
        said, clock = [], Clock()
        call = tools.CallTools(_snapshot(**changes), call_id, lambda kind, text: said.append((kind, text)),
                               effects=effects, clock=clock, executor=Inline(), principal=principal,
                               current=current, ledger=ledger)
        return call, said, clock
    return build


def _user_says(call, clock, text):
    call.observe({"type": "session.input_transcript.delta", "delta": text})
    clock.now += tools.SETTLE_S + 0.1
    call.tick()


def _receipts(said):
    note = said[-1][1]
    return json.loads(note[note.index("["):note.rindex("]") + 1])


def _rows(ledger):
    return json.loads(ledger.path.read_text())


# ---- the frozen block ------------------------------------------------------------------

@pytest.mark.parametrize("block", [
    None, {"enabled": False}, {"enabled": "yes"}, {"agents": []}, {"agents": [{"session": "../etc"}]},
    {"stories": ["bottle"]},
    {"from": "someone-else"}, {"agents": [{"session": "rk-sander-0001"}]}, {"agents": TEAM + TEAM[:1]},
    {"callback": {"kind": "ring"}},                                              # no Host-built, all-phone call back
    {"callback": {"kind": "http", "url": "http://example.com/x"}},
    {"callback": {"kind": "http", "url": "http://localhost:7790/x"}},            # a name, not a loopback literal
    {"callback": {"kind": "http", "url": "http://user:pw@127.0.0.1:7790/x"}},
    {"callback": {"kind": "http", "url": "http://127.0.0.1:7790/x?y=1"}},
    {"delegate": {"kind": "http", "url": "https://127.0.0.1/x"}},
    {"delegate": {"kind": "http", "url": DIRECTOR, "fallback": "shell"}},
])
def test_a_missing_disabled_unsafe_or_impersonating_block_gives_no_tools(block):
    profile = {"profile_id": "p", "agent_session": "rk-sander-0001"}
    if block is not None:
        profile["live_tools"] = {**_block(), **block} if block else block
    assert tools.frozen_config(profile, "user_call") is None


def test_a_valid_block_is_normalised_with_call_backs_off_by_default():
    assert _config()["callback"] == {"kind": "none"}
    cfg = _config(callback={"kind": "http", "url": RING}, callback_minutes={"default": 50, "min": 2, "max": 10},
                  delegate={"kind": "http", "url": DIRECTOR, "fallback": "prompt"})
    assert cfg["callback"] == {"kind": "http", "url": RING}
    assert cfg["callback_minutes"] == {"default": 10, "min": 2, "max": 10}
    assert cfg["delegate"] == {"kind": "http", "url": DIRECTOR, "fallback": "prompt"} and cfg["from"] == "rk-sander-0001"


@pytest.mark.parametrize("change", [{"principal": ""}, {"call_id": ""}])
def test_tools_need_their_call_and_calling_principal(make, change):
    with pytest.raises(ValueError):
        make(FakeEffects(), **change)


def test_a_snapshot_whose_agent_is_not_the_sender_gets_no_tools():
    snapshot = {**_snapshot(), "agent": {"session": "another-agent"}}
    with pytest.raises(ValueError):
        tools.CallTools(snapshot, "call-1", lambda k, t: None, effects=FakeEffects(), principal="device_phone")


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


# ---- routing ---------------------------------------------------------------------------

def test_a_task_is_delegated_only_to_allowlisted_members_as_the_agent_itself(make):
    effects = FakeEffects([ASK, ("delegate_to_agent", {"agent": "drammen-noel", "request": "Lag skjermbildet"}),
                           ("delegate_to_agent", {"agent": "drammen-hacker", "request": "Slett alt"})])
    call, said, clock = make(effects)
    _user_says(call, clock, "Kan teamet ta sak 182?")
    assert [(to, sender) for to, sender, _ in effects.prompts] == [("drammen-jonas", "rk-sander-0001"),
                                                                  ("drammen-noel", "rk-sander-0001")]
    assert [r["status"] for r in _receipts(said)] == ["handed_off", "handed_off", "refused"]
    assert said[-1][0] == "thinking" and "not that it is done" in said[-1][1]
    assert effects.bodies[0]["tools"][0]["parameters"]["properties"]["agent"]["enum"] == ["drammen-jonas", "drammen-noel"]


def test_nothing_is_routed_before_the_user_has_been_quiet(make):
    effects = FakeEffects()
    call, _said, clock = make(effects)
    call.observe({"type": "session.input_transcript.delta", "delta": "Hei Sandra"})
    clock.now += tools.SETTLE_S / 2
    call.tick()
    assert effects.bodies == []


def test_a_handed_on_turn_gets_its_answer_as_commentary_marked_untrusted(make):
    effects = FakeEffects([("get_agent_status", {"agent": "drammen-noel"})])
    call, said, _clock = make(effects)
    call.observe({"type": "session.input_transcript.delta", "delta": "Hva driver Noel med?"})
    call.observe({"type": "session.delegation.created", "delegation": {"id": "d1"}})
    assert said[-1][0] == "commentary" and _receipts(said)[0]["untrusted_data"]["state"] == "working"


def test_a_failed_router_is_told_once_and_not_retried_in_a_loop(make):
    class Broken(FakeEffects):
        def route(self, body):
            self.bodies.append(body)
            raise OSError("down")
    effects = Broken()
    call, said, clock = make(effects)
    _user_says(call, clock, "Ta sak 182.")
    call.tick()
    assert len(effects.bodies) == 1 and "nothing was handed off" in said[-1][1]


def test_a_route_runs_at_most_its_bounded_number_of_actions(make):
    effects = FakeEffects([("get_agent_status", {"agent": "drammen-jonas"})] * (tools.MAX_ACTIONS_PER_ROUTE + 5))
    call, said, clock = make(effects)
    _user_says(call, clock, "Hvordan går det?")
    assert len(_receipts(said)) == tools.MAX_ACTIONS_PER_ROUTE



def test_router_spend_is_capped_per_call(make):
    effects = FakeEffects()
    call, _said, clock = make(effects)
    for n in range(tools.MAX_ROUTES_PER_CALL + 10):
        _user_says(call, clock, f"Setning {n}.")
    assert len(effects.bodies) == tools.MAX_ROUTES_PER_CALL
    assert all(b["max_output_tokens"] == 2000 for b in effects.bodies)


def test_delegations_are_capped_per_call(make):
    asks = [("delegate_to_agent", {"agent": "drammen-jonas", "request": f"Oppgave {n}"}) for n in range(20)]
    effects = FakeEffects(asks[:tools.MAX_ACTIONS_PER_ROUTE])
    call, said, clock = make(effects)
    sent = []
    for start in range(0, 20, tools.MAX_ACTIONS_PER_ROUTE):
        effects.calls = asks[start:start + tools.MAX_ACTIONS_PER_ROUTE]
        _user_says(call, clock, f"Flere oppgaver {start}.")
        sent += [r["status"] for r in _receipts(said)]
    assert len(effects.prompts) == tools.MAX_DELEGATIONS_PER_CALL
    assert sent.count("refused") == 20 - tools.MAX_DELEGATIONS_PER_CALL


# ---- the lifecycle fence ---------------------------------------------------------------

def test_a_routing_result_that_lands_after_hang_up_does_nothing(make, ledger):
    effects = FakeEffects([ASK, CALL_BACK])
    holder = {}
    effects.on_route = lambda: holder["call"].close()      # the user hangs up while the router thinks
    call, said, clock = make(effects, callback={"kind": "http", "url": RING})
    holder["call"] = call
    _user_says(call, clock, "Ta sak 182 og ring meg om fem. Ha det!")
    assert effects.prompts == [] and effects.posts == [] and said == []
    assert all(row["status"] == tools.NOT_DONE for row in _rows(ledger).values())


def test_nothing_starts_once_the_socket_no_longer_owns_the_call(make):
    owner = {"current": True}
    effects = FakeEffects([ASK])
    call, said, clock = make(effects, current=lambda: owner["current"])
    owner["current"] = False                                  # retired by a close or fallback elsewhere
    _user_says(call, clock, "Ta sak 182.")
    assert effects.bodies == [] and effects.prompts == []


def test_a_closed_call_routes_and_says_nothing_more(make):
    effects = FakeEffects([ASK])
    call, said, clock = make(effects)
    call.observe({"type": "session.input_transcript.delta", "delta": "Ta sak 182, ha det!"})
    call.close()
    clock.now += 5
    call.tick()
    assert effects.bodies == [] and said == []


# ---- delegation: one durable operation per intent ---------------------------------------

def test_a_delegation_is_one_operation_across_routes_reconnects_calls_and_restarts(make, ledger):
    first = FakeEffects([ASK, ASK])
    call, said, clock = make(first, call_id="call-1")
    _user_says(call, clock, "Ta sak 182.")
    assert [r["status"] for r in _receipts(said)] == ["handed_off", "already_handed_off"]
    operation = _receipts(said)[0]["operation_id"]
    assert f"[{operation}]" in first.prompts[0][2]
    tools.Ledger(ledger.path)                               # a Host restart reads the same file
    second = FakeEffects([ASK])
    call, said, clock = make(second, call_id="call-2")
    _user_says(call, clock, "Ta sak 182.")
    assert second.prompts == [] and _receipts(said)[0]["operation_id"] == operation
    row = _rows(ledger)[operation]
    assert (row["status"], row["call_id"], row["principal"], row["agent_session"], row["host_id"]) == (
        tools.DONE, "call-1", "device_phone", "rk-sander-0001", "host-1")


def test_a_claim_left_by_a_crash_mid_send_is_never_sent_again(make, ledger):
    call, _said, clock = make(FakeEffects([ASK]))
    _user_says(call, clock, "Ta sak 182.")
    operation = next(iter(_rows(ledger)))
    ledger.update(operation, status=tools.CLAIMED)           # as if the Host died between claim and outcome
    again = FakeEffects([ASK])
    call, said, clock = make(again, call_id="call-2")
    _user_says(call, clock, "Ta sak 182.")
    assert again.prompts == [] and _receipts(said)[0]["status"] == "in_progress"


@pytest.mark.parametrize("arrived, status", [(True, "already_handed_off"), (False, "unconfirmed")])
def test_an_unconfirmed_prompt_is_reconciled_by_its_marker_never_resent(make, ledger, arrived, status):
    effects = FakeEffects([ASK])
    effects.prompt_error = (tools.Unconfirmed("clarp-admin timeout"), arrived)
    call, said, clock = make(effects)
    _user_says(call, clock, "Ta sak 182.")
    assert _receipts(said)[0]["status"] == "unconfirmed"
    effects.prompt_error = None
    call2, said2, clock2 = make(effects, call_id="call-2")
    _user_says(call2, clock2, "Ta sak 182.")
    assert len(effects.prompts) == 1 and _receipts(said2)[0]["status"] == status


def test_a_refused_prompt_is_not_done_and_may_be_asked_again(make):
    effects = FakeEffects([ASK])
    effects.prompt_error = (tools.Refused("refused"), False)
    call, said, clock = make(effects)
    _user_says(call, clock, "Ta sak 182.")
    assert _receipts(said)[0]["status"] == "failed"
    effects.prompt_error = None
    call, said, clock = make(effects, call_id="call-2")
    _user_says(call, clock, "Ta sak 182.")
    assert len(effects.prompts) == 2 and _receipts(said)[0]["status"] == "handed_off"


@pytest.mark.parametrize("error, prompted, status", [
    (None, 0, "handed_off"),
    (tools.Unreachable("connection refused"), 1, "handed_off"),
    (tools.Unconfirmed("timeout"), 0, "unconfirmed"),
    (tools.Refused("HTTP 400"), 0, "failed"),
])
def test_a_director_delegation_falls_back_only_when_it_certainly_did_not_arrive(make, error, prompted, status):
    effects = FakeEffects([ASK])
    effects.post_error = error
    call, said, clock = make(effects, delegate={"kind": "http", "url": DIRECTOR, "fallback": "prompt"})
    _user_says(call, clock, "Ta sak 182.")
    body = effects.posts[0][1]
    assert body["operation_id"].startswith("dl-") and body["principal"] == "device_phone"
    assert body["from"] == "rk-sander-0001"
    assert len(effects.prompts) == prompted and _receipts(said)[0]["status"] == status


def test_an_unconfirmed_director_delegation_is_reconciled_with_the_same_operation_id_only(make):
    effects = FakeEffects([ASK])
    effects.post_error = tools.Unconfirmed("timeout")
    call, _said, clock = make(effects, delegate={"kind": "http", "url": DIRECTOR, "fallback": "prompt"})
    _user_says(call, clock, "Ta sak 182.")
    effects.post_error = tools.Unreachable("connection refused")      # the director is down now
    call, said, clock = make(effects, call_id="call-2", delegate={"kind": "http", "url": DIRECTOR, "fallback": "prompt"})
    _user_says(call, clock, "Ta sak 182.")
    ids = {body["operation_id"] for _url, body in effects.posts}
    assert len(effects.posts) == 2 and len(ids) == 1 and effects.prompts == []   # never the prompt route
    assert _receipts(said)[0]["status"] == "failed"


def test_without_a_fallback_an_unreachable_director_is_a_failure(make):
    effects = FakeEffects([ASK])
    effects.post_error = tools.Unreachable("connection refused")
    call, said, clock = make(effects, delegate={"kind": "http", "url": DIRECTOR})
    _user_says(call, clock, "Ta sak 182.")
    assert effects.prompts == [] and _receipts(said)[0]["status"] == "failed"


# ---- call backs: handed to the director, which owns the timer and the one ring ---------

CB = {"callback": {"kind": "http", "url": RING}}


def test_call_backs_are_off_unless_a_director_places_them(make):
    effects = FakeEffects([CALL_BACK, ("cancel_callback", {})])
    call, said, clock = make(effects)
    _user_says(call, clock, "Ring meg om fem.")
    assert effects.posts == [] and [r["status"] for r in _receipts(said)] == ["refused", "refused"]
    assert "schedule_callback" not in [t["name"] for t in effects.bodies[0]["tools"]]


def test_a_call_back_is_handed_to_the_director_once_bound_to_the_calling_phone(make, ledger):
    effects = FakeEffects([("schedule_callback", {"minutes": 90, "reason": "status"})])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    [body] = effects.to(RING)
    assert body["action"] == "schedule" and body["minutes"] == 30 and "supersedes" not in body
    assert (body["recipient_principal"], body["agent_session"], body["call_id"], body["host_id"]) == (
        "device_phone", "rk-sander-0001", "call-1", "host-1")
    assert body["operation_id"] == _receipts(said)[0]["operation_id"] and _receipts(said)[0]["status"] == "scheduled"
    row = next(iter(_rows(ledger).values()))
    assert (row["status"], row["operation_id"], row["recipient_principal"]) == (tools.DONE, body["operation_id"], "device_phone")


def test_a_phone_that_cannot_take_a_live_call_back_gets_none(make):
    effects = FakeEffects([CALL_BACK])
    effects.recipients = set()
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    assert effects.posts == [] and _receipts(said)[0]["status"] == "refused"


def test_the_caller_principal_can_never_be_the_agent_itself(make):
    with pytest.raises(ValueError):
        make(FakeEffects(), principal="rk-sander-0001")


def test_asking_again_in_the_same_call_keeps_the_one_already_scheduled(make):
    effects = FakeEffects([CALL_BACK, CALL_BACK])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem. Ring meg om fem, ja.")
    _user_says(call, clock, "Husk å ringe meg.")
    assert len(effects.to(RING)) == 1
    assert [r["status"] for r in _receipts(said)] == ["already_scheduled", "already_scheduled"]


def test_a_new_call_supersedes_the_pending_call_back_and_other_phones_have_their_own(make, ledger):
    first = FakeEffects([CALL_BACK])
    call, said, clock = make(first, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    old = _receipts(said)[0]["operation_id"]
    second = FakeEffects([("schedule_callback", {"minutes": 2, "reason": "status"})])
    call, said, clock = make(second, call_id="call-2", **CB)
    _user_says(call, clock, "Ring meg heller om to minutter.")
    [body] = second.to(RING)
    receipt = _receipts(said)[0]
    assert body["supersedes"] == old and receipt["superseded"] == old and receipt["operation_id"] != old
    other = FakeEffects([CALL_BACK])
    call, said, clock = make(other, call_id="call-3", principal="device_other", **CB)
    _user_says(call, clock, "Ring meg om fem.")
    assert "supersedes" not in other.to(RING)[0]
    assert len(_rows(ledger)) == 2                            # one slot per (Host, principal, agent)


def test_a_supersede_the_director_certainly_did_not_take_keeps_the_earlier_call_back(make, ledger):
    first = FakeEffects([CALL_BACK])
    call, said, clock = make(first, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    old = _receipts(said)[0]["operation_id"]
    second = FakeEffects([CALL_BACK])
    second.url_errors[RING] = tools.Unreachable("connection refused")
    call, said, clock = make(second, call_id="call-2", **CB)
    _user_says(call, clock, "Ring meg om fem i stedet.")
    assert _receipts(said)[0]["status"] == "failed"
    row = next(iter(_rows(ledger).values()))
    assert (row["operation_id"], row["status"]) == (old, tools.DONE)


def test_ikke_ring_likevel_cancels_the_pending_call_back_at_the_director(make, ledger):
    first = FakeEffects([CALL_BACK])
    call, said, clock = make(first, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    op = _receipts(said)[0]["operation_id"]
    cancel = FakeEffects([("cancel_callback", {})])
    call, said, clock = make(cancel, call_id="call-2", **CB)
    _user_says(call, clock, "Ikke ring likevel.")
    assert cancel.to(RING) == [{"action": "cancel", "operation_id": op, "recipient_principal": "device_phone",
                                "agent_session": "rk-sander-0001", "call_id": "call-2", "host_id": "host-1"}]
    assert _receipts(said)[0]["status"] == "cancelled"
    again = FakeEffects([("cancel_callback", {})])
    call, said, clock = make(again, call_id="call-3", **CB)
    _user_says(call, clock, "Ikke ring.")
    assert again.posts == [] and _receipts(said)[0]["status"] == "nothing_pending"
    later = FakeEffects([CALL_BACK])
    call, said, clock = make(later, call_id="call-4", **CB)
    _user_says(call, clock, "Ring meg likevel om fem.")
    assert "supersedes" not in later.to(RING)[0] and _receipts(said)[0]["status"] == "scheduled"


@pytest.mark.parametrize("error, status", [(tools.Unconfirmed("timeout"), "unconfirmed"),
                                           (tools.Refused("HTTP 409"), "failed")])
def test_a_cancel_the_director_did_not_confirm_never_claims_cancelled(make, error, status):
    first = FakeEffects([CALL_BACK])
    call, _said, clock = make(first, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    cancel = FakeEffects([("cancel_callback", {})])
    cancel.url_errors[RING] = error
    call, said, clock = make(cancel, call_id="call-2", **CB)
    _user_says(call, clock, "Ikke ring likevel.")
    assert _receipts(said)[0]["status"] == status


@pytest.mark.parametrize("second_error, status", [(None, "already_scheduled"), (tools.Unconfirmed("timeout"), "unconfirmed")])
def test_an_unconfirmed_schedule_is_reconciled_with_the_same_operation_id_only(make, ledger, second_error, status):
    first = FakeEffects([CALL_BACK])
    first.url_errors[RING] = tools.Unconfirmed("timeout")
    call, said, clock = make(first, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    assert _receipts(said)[0]["status"] == "unconfirmed"
    op = first.to(RING)[0]["operation_id"]
    second = FakeEffects([("schedule_callback", {"minutes": 2, "reason": "x"})])
    if second_error:
        second.url_errors[RING] = second_error
    call, said, clock = make(second, call_id="call-2", **CB)
    _user_says(call, clock, "Ring meg om to.")
    [body] = second.to(RING)
    assert body == first.to(RING)[0] and body["operation_id"] == op       # the same intent, nothing new
    assert _receipts(said)[0]["status"] == status


def test_a_schedule_the_director_certainly_did_not_take_may_be_asked_again(make):
    first = FakeEffects([CALL_BACK])
    first.url_errors[RING] = tools.Unreachable("connection refused")
    call, said, clock = make(first, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    assert _receipts(said)[0]["status"] == "failed"
    second = FakeEffects([CALL_BACK])
    call, said, clock = make(second, call_id="call-2", **CB)
    _user_says(call, clock, "Ring meg om fem.")
    assert _receipts(said)[0]["status"] == "scheduled" and "supersedes" not in second.to(RING)[0]


# ---- one owner per effect: the director owns hand-out and call back -------------------

def test_a_director_owned_profile_never_prompts_members_and_never_schedules_a_ring(make):
    """The Drammen profile: hand-out only through the director (no prompt fallback), no call back from the Host.
    The director stays the single owner of distribution and of the one call back ring."""
    settings = {"delegate": {"kind": "http", "url": DIRECTOR, "fallback": "none"}, "callback": {"kind": "none"}}
    for n, error in enumerate((None, tools.Unreachable("HTTP 404"), tools.Unconfirmed("timeout"))):
        ask = ("delegate_to_agent", {"agent": "drammen-jonas", "request": f"Ta sak {182 + n}"})
        effects = FakeEffects([ask, CALL_BACK, ("cancel_callback", {})])
        effects.post_error = error
        call, said, clock = make(effects, call_id=f"call-{error!r}", **settings)
        _user_says(call, clock, "Ta sak 182 og ring meg om fem.")
        assert len(effects.posts) == 1 and effects.to(RING) == [] and effects.prompts == []
        assert {"schedule_callback", "cancel_callback"}.isdisjoint(t["name"] for t in effects.bodies[0]["tools"])


# ---- the real POST ---------------------------------------------------------------------

def test_the_real_post_is_loopback_only_never_proxied_or_redirected_and_reads_the_body(monkeypatch):
    import http.server
    import socket
    import threading as th

    class Director(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/slow":
                time.sleep(1.5)
            code, body = {"/ok": (200, b'{"ok": true}'), "/missing": (404, b"{}"), "/down": (503, b"{}"),
                          "/bad": (400, b"{}"), "/lies": (200, b'{"ok": false, "error": "busy"}'),
                          "/moved": (307, b""), "/slow": (200, b"{}")}[self.path]
            self.send_response(code)
            if code == 307:
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9/")    # a proxy in the environment is never used
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Director)
    th.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert tools._post(base + "/ok", {}) == {"ok": True}
        with pytest.raises(tools.Unreachable):
            tools._post(base + "/missing", {})
        for path in ("/down", "/bad", "/lies"):
            with pytest.raises(tools.Refused):
                tools._post(base + path, {})
        for path, timeout in (("/moved", 5), ("/slow", 0.3)):
            with pytest.raises(tools.Unconfirmed):
                tools._post(base + path, {}, timeout=timeout)
        with pytest.raises(tools.Refused):
            tools._post("http://localhost:" + base.rsplit(":", 1)[1] + "/ok", {})
    finally:
        server.shutdown()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    with pytest.raises(tools.Unreachable):
        tools._post(f"http://127.0.0.1:{port}/x", {})


# ---- inside a real Agent Live socket ---------------------------------------------------

from tests.unit.test_agent_ring_live_socket import _admitted, _wait, call  # noqa: E402,F401 - call is a fixture


@pytest.fixture
def opted_in(sander):
    meta = json.loads((sander.folder / "profile.json").read_text())
    meta["live_tools"] = _block(stories=["bottle"])
    (sander.folder / "profile.json").write_text(json.dumps(meta))
    return sander


def test_a_live_call_routes_the_users_turn_and_tells_the_voice_what_was_done(opted_in, call, monkeypatch):
    effects = FakeEffects([ASK])
    monkeypatch.setattr(tools, "Effects", lambda key: effects)
    h, thread = _admitted(call)
    call.up.push({"type": "session.input_transcript.delta", "delta": "Sandra, kan Jonas ta sak 182?"})
    _wait(lambda: call.up.of("session.thinking.append"))
    note = call.up.of("session.thinking.append")[0]["content"]
    assert '"handed_off"' in note and effects.prompts[0][:2] == ("drammen-jonas", "rk-sander-0001")
    assert "real tools" in call.up.of("session.start")[0]["session"]["instructions"]
    assert not any(f["type"].startswith("session.thinking") for f in h.wfile.frames())   # nothing new to the phone
    h.send({"type": "session.close"})
    thread.join(15)
    # The tools act for the device that answered and joined this call (the grant's owner), never the agent.
    row = next(iter(json.loads((live.ROOT / "tool-ledger.json").read_text()).values()))
    assert row["principal"] == PHONE != row["agent_session"] == "rk-sander-0001"
    assert row["call_id"] == call.grant["call_id"]


def test_a_live_call_after_hang_up_starts_no_effect(opted_in, call, monkeypatch):
    effects = FakeEffects([ASK])
    monkeypatch.setattr(tools, "Effects", lambda key: effects)
    h, thread = _admitted(call)
    call.up.push({"type": "session.input_transcript.delta", "delta": "Ta sak 182, ha det!"})
    h.send({"type": "session.close"})
    thread.join(15)
    time.sleep(tools.SETTLE_S + 0.5)
    assert effects.prompts == []


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
