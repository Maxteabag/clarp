"""Opt-in tools in an Agent Live call (agent_live_tools): the agent acts while it talks.

A profile without ``live_tools`` keeps its exact snapshot and instructions.
With it, the Host routes the call's transcript to a text router and runs only
allowlisted actions, as the profile's own agent, behind the call's lifecycle
fence; delegations and call backs are durable operations that are never
repeated and never resent blindly when their outcome is unknown.
"""
from __future__ import annotations

import json
import re
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
        self.prompts, self.posts, self.scheduled, self.stopped = [], [], [], []
        self.later_error = None
        self.timers: dict[str, str] = {}
        self.post_error = self.prompt_error = None
        self.asks, self.rows, self.acked, self.replies, self.ask_error = [], {}, [], {}, None
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
        return {"ok": True, "result": "forwarded", "sent_now": True}

    def to(self, url):
        return [body for u, body in self.posts if u == url]

    def later(self, command, delay_seconds, unit):
        self.scheduled.append((command, delay_seconds, unit))
        if self.later_error is not None:
            error, state = self.later_error
            self.timers[unit] = state
            raise error
        self.timers[unit] = "active"

    def timer_state(self, unit):
        return self.timers.get(unit, "absent")

    def stop(self, unit):
        self.stopped.append(unit)
        self.timers[unit] = "absent"

    def ask(self, delegation_id, session, request, principal):
        self.asks.append((delegation_id, session, request, principal))
        if self.ask_error is not None:
            raise self.ask_error
        return {"delegation_id": delegation_id, "status": "running"}

    def delegation(self, delegation_id):
        return self.rows.get(delegation_id)

    def acknowledge(self, delegation_id, principal):
        self.acked.append(delegation_id)

    def member_reply(self, session, since):
        return self.replies.get(session)

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


REAL_HOST_NOTE = tools.host_note


@pytest.fixture(autouse=True)
def full_receipts(monkeypatch):
    """Behaviour tests read every receipt field back from the note; the size tests use REAL_HOST_NOTE."""
    monkeypatch.setattr(tools, "host_note", lambda receipts: "Host note, receipts (not that it is done): "
                        + json.dumps(receipts, ensure_ascii=False) + " end")


@pytest.fixture(autouse=True)
def ledger(tmp_path):
    return tools.Ledger(tmp_path / "tool-ledger.json")


@pytest.fixture
def make(ledger):
    def build(effects, call_id="call-1", principal="device_phone", current=lambda: True, **changes):
        said, clock = [], Clock()
        call = tools.CallTools(_snapshot(**changes), call_id, lambda kind, text: said.append((kind, text)) or True,
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
                  delegate={"kind": "http", "url": DIRECTOR})
    assert cfg["callback"] == {"kind": "http", "url": RING}
    assert cfg["callback_minutes"] == {"default": 10, "min": 2, "max": 10}
    assert cfg["delegate"] == {"kind": "http", "url": DIRECTOR} and cfg["from"] == "rk-sander-0001"


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
    assert [r["status"] for r in _receipts(said)] == ["forwarded", "forwarded", "refused"]
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

def _age(ledger, key, seconds):
    rows = _rows(ledger)
    rows[key]["at"] -= seconds
    ledger.path.write_text(json.dumps(rows))



# ---- delegate_to_agent against regi (schema v3.1): exact receipts, regi owns dedupe -------

INTENT = "ab" * 32


def _v1(result, sent=False, intent=INTENT, intent_status="venter", reason=None, **extra):
    """One regi v3.1 success-path answer (HTTP 200, always every field)."""
    return {"sesjon": 1, "agent": "jonas", "intent": intent, "result": result, "intent_status": intent_status,
            "base_task": "already_started", "additions_used": 1, "additions_max": 3, "reason": reason,
            "sent": sent, "ok": sent, **extra}


class Regi(FakeEffects):
    """An isolated fake regi: answers from a script, records every POST and GET."""

    def __init__(self, calls=(), answers=(), gets=()):
        super().__init__(calls)
        self.answers, self.gets, self.get_answers = list(answers), [], list(gets)

    def post(self, url, body, timeout=30):
        self.posts.append((url, body))
        answer = self.answers.pop(0) if self.answers else _v1("already_present", True, intent_status="sendt")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, url, timeout=30):
        self.gets.append(url)
        answer = self.get_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


REGI = {"delegate": {"kind": "http", "url": DIRECTOR, "fallback": "none",
                     "reconcile_url": "http://127.0.0.1:7790/regi/sandra/intent"}}


@pytest.mark.parametrize("answer, status", [
    (_v1("forwarded", True, intent_status="sendt", base_task="started_now"), "forwarded"),
    (_v1("forwarded", True, intent_status="sendt"), "forwarded"),
    (_v1("forwarded", False, intent_status="venter"), "unknown_delivery"),       # forwarded only when sent:true
    (_v1("queued", intent_status="venter"), "queued_at_regi"),
    (_v1("queued", intent_status="sender"), "queued_at_regi"),
    (_v1("already_present", True, intent_status="sendt"), "already_present"),
    (_v1("not_forwarded", intent_status=None, reason="cap_reached"), "not_forwarded"),
    (_v1("not_forwarded", intent_status="feil", reason="base_task_failed", base_task="failed"), "not_forwarded"),
    (_v1("not_forwarded", intent_status="feil", reason="send_failed"), "not_forwarded"),
    (_v1("not_forwarded", intent_status="ukjent", reason="send_failed"), "unknown_delivery"),   # never retried
    ({"ok": True, "outcome": "forwarded", "sent": True}, "unknown_delivery"),     # missing schema fields
])
def test_every_regi_answer_maps_to_its_exact_receipt(make, answer, status):
    effects = Regi([ASK], [answer])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Ta sak 182.")
    receipt = _receipts(said)[0]
    assert receipt["status"] == status
    assert "handed_off" not in json.dumps(receipt)
    assert status != "forwarded" or receipt["sent"] is True
    if "intent" in answer:
        assert receipt["intent"] == answer["intent"]                              # regi's hash, never our own
    if status == "not_forwarded":
        assert receipt["reason"] == answer["reason"] and receipt["note"]
    if answer.get("base_task") == "started_now":
        assert "base task was started" in receipt["note"]


@pytest.mark.parametrize("code, body, reason", [
    (400, {"ok": False, "result": "refused", "reason": "empty_request"}, "empty_request"),
    (400, {"ok": False, "result": "refused", "reason": "invalid_json"}, "invalid_json"),
    (400, {"ok": False, "result": "refused", "reason": "unknown_agent"}, "unknown_agent"),
    (400, {"ok": False, "result": "refused", "reason": "missing_principal"}, "missing_principal"),
    (409, {"ok": False, "result": "refused", "reason": "reset_in_progress"}, "reset_in_progress"),
    (413, {"ok": False, "result": "refused", "reason": "request_too_large", "max": 4000, "len": 4001}, "request_too_large"),
    (413, {"ok": False, "result": "refused", "reason": "body_too_large", "max_bytes": 65536}, "body_too_large"),
])
def test_a_regi_refusal_is_refused_with_its_reason_and_nothing_was_sent(make, code, body, reason):
    effects = Regi([ASK], [tools.Refused(f"HTTP {code}", code, body)])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Ta sak 182.")
    receipt = _receipts(said)[0]
    assert (receipt["status"], receipt["reason"]) == ("refused", reason) and "nothing was sent" in receipt["note"]


@pytest.mark.parametrize("error", [tools.Unconfirmed("timeout"), tools.Unconfirmed("answer is not a JSON object"),
                                   tools.Unreachable("connection refused"),
                                   tools.Refused("HTTP 502", 502, {}), RuntimeError("boom")])
def test_an_unclear_regi_answer_is_unknown_delivery_and_never_retried(make, ledger, error):
    effects = Regi([ASK], [error])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Ta sak 182.")
    assert _receipts(said)[0]["status"] == "unknown_delivery"
    call.tick()
    clock.now += 60
    call.tick()
    assert len(effects.posts) == 1 and effects.gets == []                         # no retry, no intent to read
    row = next(iter(_rows(ledger).values()))
    assert (row["status"], row["status_text"]) == (tools.UNCONFIRMED, "unknown_delivery")


def test_an_empty_or_oversized_request_is_refused_host_side_and_never_sent(make):
    effects = Regi([("delegate_to_agent", {"agent": "drammen-jonas", "request": "   "}),
                    ("delegate_to_agent", {"agent": "drammen-noel", "request": "x" * (tools.REQUEST_CHARS + 1)})])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Gjør dette.")
    assert effects.posts == []
    assert [(r["status"], r["reason"]) for r in _receipts(said)] == [("refused", "empty_request"),
                                                                      ("refused", "request_too_large")]
    assert effects.bodies[0]["tools"][0]["parameters"]["properties"]["request"]["maxLength"] == 4000


def test_the_host_never_suppresses_a_follow_up_and_sends_exactly_the_schema_body(make):
    """No Host text window: the same words again, a new call and another caller each reach regi, which dedupes."""
    effects = Regi([ASK], [_v1("forwarded", True, intent_status="sendt"),
                           _v1("already_present", True, intent_status="sendt"),
                           _v1("already_present", True, intent_status="sendt")])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Ta sak 182.")
    call, said2, clock = make(effects, call_id="call-2", **REGI)
    _user_says(call, clock, "Ta sak 182.")
    call, said3, clock = make(effects, call_id="call-3", principal="device_other", **REGI)
    _user_says(call, clock, "Ta sak 182.")
    assert len(effects.posts) == 3
    assert [_receipts(s)[0]["status"] for s in (said, said2, said3)] == ["forwarded", "already_present", "already_present"]
    assert set(effects.posts[0][1]) == {"call_id", "principal", "from", "agent", "request"}
    assert effects.posts[0][1]["principal"] == "device_phone" and effects.posts[2][1]["principal"] == "device_other"


def test_the_ledger_is_keyed_by_regi_intent_and_the_caller(make, ledger):
    effects = Regi([ASK], [_v1("forwarded", True, intent="fe" * 32, intent_status="sendt")])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Ta sak 182.")
    rows = _rows(ledger)
    request_row = next(r for k, r in rows.items() if k.startswith("rq-"))
    intent_row = next(r for k, r in rows.items() if k.startswith("dl-"))
    assert request_row["status_text"] == intent_row["status_text"] == "forwarded"
    assert intent_row["intent"] == "fe" * 32 and intent_row["principal"] == "device_phone"
    expected = "dl-" + tools._digest("host-1", "device_phone", "rk-sander-0001", "drammen-jonas", "fe" * 32)
    assert expected in rows


@pytest.mark.parametrize("first, later, after", [
    (_v1("queued"), {"result": "forwarded", "intent_status": "sendt", "sent": True}, "forwarded"),
    (_v1("queued"), {"result": "queued", "intent_status": "venter", "sent": False}, "queued_at_regi"),
    (_v1("queued", intent_status="sender"), {"result": "not_forwarded", "intent_status": "feil", "sent": False},
     "not_forwarded"),
    (_v1("queued", intent_status="sender"), {"result": "not_forwarded", "intent_status": "ukjent", "sent": False},
     "unknown_delivery"),
])
def test_a_queued_intent_is_reconciled_with_at_most_one_read_only_get(make, ledger, first, later, after):
    effects = Regi([ASK], [first], [{"sesjon": 1, "intent": first["intent"], "agent": "jonas", **later}])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Ta sak 182.")
    clock.now += tools.RECONCILE_AFTER_S + 1
    call.tick()
    clock.now += 600
    call.tick()
    assert effects.gets == ["http://127.0.0.1:7790/regi/sandra/intent?sesjon=1&principal=device_phone&intent=" + INTENT]
    assert len(effects.posts) == 1                                                # never a resend
    row = next(r for k, r in _rows(ledger).items() if k.startswith("dl-"))
    assert row["status_text"] == after and row["reconciled"] is True
    assert ("reconciled" in said[-1][1]) is (after == "forwarded")



def test_the_principal_comes_only_from_the_call_binding_and_the_post_waits_30_s(make):
    timeouts = []

    class Timed(Regi):
        def post(self, url, body, timeout=30):
            timeouts.append(timeout)
            return super().post(url, body, timeout)
    effects = Timed([("delegate_to_agent", {"agent": "drammen-jonas", "request": "Ta sak 182",
                                            "principal": "attacker", "from": "someone-else"})],
                    [_v1("forwarded", True, intent_status="sendt")])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Ta sak 182.")
    body = effects.posts[0][1]
    assert (body["principal"], body["from"]) == ("device_phone", "rk-sander-0001") and timeouts == [30]

def test_without_a_reconcile_url_nothing_is_read(make):
    effects = Regi([ASK], [_v1("queued")])
    call, said, clock = make(effects, delegate={"kind": "http", "url": DIRECTOR})
    _user_says(call, clock, "Ta sak 182.")
    clock.now += 600
    call.tick()
    assert effects.gets == []


def test_there_is_no_prompt_fallback_regi_is_the_one_dispatch_owner(make):
    assert _config(delegate={"kind": "http", "url": DIRECTOR, "fallback": "prompt"}) is None
    effects = Regi([ASK], [tools.Unreachable("connection refused")])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Ta sak 182.")
    assert effects.prompts == [] and _receipts(said)[0]["status"] == "unknown_delivery"


def test_a_refused_or_unclear_prompt_is_never_called_forwarded(make):
    effects = FakeEffects([ASK])
    effects.prompt_error = (tools.Refused("refused"), False)
    call, said, clock = make(effects)
    _user_says(call, clock, "Ta sak 182.")
    assert _receipts(said)[0]["status"] == "refused"
    effects.prompt_error = (tools.Unconfirmed("clarp-admin timeout"), True)
    call, said, clock = make(effects, call_id="call-2")
    _user_says(call, clock, "Ta sak 182.")
    assert _receipts(said)[0]["status"] == "unknown_delivery"


# ---- call backs: regi is the sole due-owner (A1.4); the Host keeps no timer --------------

CB = {"callback": {"kind": "http", "url": RING}}


class Tilbakering(FakeEffects):
    """An isolated fake of regi's /regi/sandra/tilbakering (A1.4): one pending per (principal, agent)."""

    def __init__(self, calls=(), script=(), explicit_support=True):
        super().__init__(calls)
        self.script, self.pending, self.ops = list(script), {}, {}
        self.explicit_support, self.cancel_errors = explicit_support, []

    def post(self, url, body, timeout=30):
        self.posts.append((url, dict(body)))
        if self.script:
            answer = self.script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        key = (body["recipient_principal"], body["agent_session"])
        current = self.pending.get(key)
        seen = self.ops.setdefault(body["operation_id"], body["action"])
        if seen != body["action"]:                                       # A1.4: an op id is bound to one action
            raise tools.Refused("HTTP 409", 409, {"ok": False, "result": "refused", "error": "operation_id_mismatch"})
        if body["action"] == "cancel":
            if self.cancel_errors:
                raise self.cancel_errors.pop(0)
            if current:
                del self.pending[key]
                return {"ok": True, "result": "cancelled", "operation_id": current["op"], "status": "avlyst"}
            return {"ok": True, "result": "nothing_pending"}
        explicit = body.get("explicit") is True and self.explicit_support
        if current and current["call_id"] == body["call_id"] and not explicit:
            return {"ok": True, "result": "already_scheduled", "operation_id": current["op"], "due": current["due"],
                    "status": "venter", "fired_at": None}
        if current and body.get("supersedes") != current["op"]:
            raise tools.Refused("HTTP 409", 409, {"ok": False, "result": "refused", "error": "supersedes_mismatch",
                                                  "pending": current["op"], "due": current["due"]})
        due = 1_800_000_000 + body["minutes"] * 60
        self.pending[key] = {"op": body["operation_id"], "call_id": body["call_id"], "due": due}
        return {"ok": True, "result": "scheduled", "operation_id": body["operation_id"], "due": due, "status": "venter",
                "fired_at": None, **({"superseded": current["op"]} if current else {})}


def test_call_backs_are_off_unless_configured(make):
    effects = Tilbakering([CALL_BACK, ("cancel_callback", {})])
    call, said, clock = make(effects)
    _user_says(call, clock, "Ring meg om fem.")
    assert effects.posts == [] and [r["status"] for r in _receipts(said)] == ["refused", "refused"]
    assert "schedule_callback" not in [t["name"] for t in effects.bodies[0]["tools"]]


def test_a_call_back_is_handed_to_regi_at_once_bound_to_the_calling_phone_and_the_host_keeps_no_timer(make, ledger):
    effects = Tilbakering([("schedule_callback", {"minutes": 90, "reason": "status"})])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    [(url, body)] = effects.posts
    assert url == RING and body["action"] == "schedule" and body["minutes"] == 30 and "supersedes" not in body
    assert (body["recipient_principal"], body["agent_session"], body["call_id"], body["host_id"]) == (
        "device_phone", "rk-sander-0001", "call-1", "host-1")
    receipt = _receipts(said)[0]
    assert receipt["status"] == "scheduled" and receipt["operation_id"] == body["operation_id"]
    assert "never 'rang'" in receipt["note"] and not hasattr(tools.Effects, "later")
    row = next(iter(_rows(ledger).values()))
    assert (row["operation_id"], row["status"], row["call_id"]) == (body["operation_id"], "pending", "call-1")


def test_a_phone_that_cannot_take_a_live_call_back_gets_none(make):
    effects = Tilbakering([CALL_BACK])
    effects.recipients = set()
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    assert effects.posts == [] and _receipts(said)[0]["status"] == "refused"


def test_the_caller_principal_can_never_be_the_agent_itself(make):
    with pytest.raises(ValueError):
        make(FakeEffects(), principal="rk-sander-0001")


def test_asking_again_in_the_same_call_with_the_same_minutes_is_already_scheduled(make):
    effects = Tilbakering([CALL_BACK, ("schedule_callback", {"minutes": 5, "reason": "igjen"})])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem. Ja, om fem.")
    assert [r["status"] for r in _receipts(said)] == ["scheduled", "already_scheduled"]
    assert [b["action"] for _u, b in effects.posts] == ["schedule", "schedule"]
    assert all("supersedes" not in body for _url, body in effects.posts)


def test_a_new_call_supersedes_with_regis_pending_id_and_other_phones_have_their_own(make):
    effects = Tilbakering([CALL_BACK])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    first = _receipts(said)[0]["operation_id"]
    effects.calls = [("schedule_callback", {"minutes": 2, "reason": "status"})]
    call, said, clock = make(effects, call_id="call-2", **CB)
    _user_says(call, clock, "Ring meg heller om to minutter.")
    receipt = _receipts(said)[0]
    assert effects.posts[-1][1]["supersedes"] == first and receipt["superseded"] == first
    assert receipt["status"] == "scheduled" and receipt["operation_id"] != first
    call, said, clock = make(effects, call_id="call-3", principal="device_other", **CB)
    effects.calls = [CALL_BACK]
    _user_says(call, clock, "Ring meg om fem.")
    assert "supersedes" not in effects.posts[-1][1] and _receipts(said)[0]["status"] == "scheduled"


def test_a_stale_supersedes_is_retried_once_with_the_pending_id_regi_names(make, ledger):
    effects = Tilbakering([CALL_BACK])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    pending = _receipts(said)[0]["operation_id"]
    slot = next(iter(_rows(ledger)))
    ledger.update(slot, operation_id="cb-stale")              # the Host's view went stale
    call, said, clock = make(effects, call_id="call-2", **CB)
    _user_says(call, clock, "Ring meg om fem.")
    sent = [body.get("supersedes") for _url, body in effects.posts[1:]]
    assert sent == ["cb-stale", pending] and _receipts(said)[0]["superseded"] == pending


def test_ikke_ring_likevel_cancels_only_this_callers_pending_call_back(make):
    effects = Tilbakering([CALL_BACK])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    effects.calls = [("cancel_callback", {})]
    call, said, clock = make(effects, call_id="call-2", principal="device_other", **CB)
    _user_says(call, clock, "Ikke ring likevel.")
    assert _receipts(said)[0]["status"] == "nothing_pending"          # another caller cannot cancel it
    call, said, clock = make(effects, call_id="call-3", **CB)
    _user_says(call, clock, "Ikke ring likevel.")
    assert _receipts(said)[0]["status"] == "cancelled" and effects.pending == {}



@pytest.mark.parametrize("regi_status, extra, words, never", [
    ("venter", {}, "already pending", "gone out"),
    ("fyrt", {}, "already gone out", "pending"),
    ("fyrer", {}, "being placed now (not confirmed)", "gone out"),
    ("fyrt_usikker", {}, "never rung again", "pending"),
    ("avlyst", {}, "cancelled earlier", "pending"),
    ("ikke_plassert", {}, "not placed", "gone out"),
    ("feil", {"feil": "ring_feil: no device"}, "failed (ring_feil: no device); it is not tried again", "pending"),
    ("feil", {}, "failed (no reason given)", "gone out"),
    (None, {}, "status is unknown", "already pending"),
    ("something_new", {}, "status is unknown", "gone out"),
])
def test_an_already_scheduled_answer_is_told_by_regis_current_status(make, regi_status, extra, words, never):
    effects = Tilbakering([CALL_BACK], [{"ok": True, "result": "already_scheduled", "operation_id": "cb-x",
                                         "due": 1_800_000_300, "status": regi_status, "fired_at": None, **extra}])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    receipt = _receipts(said)[0]
    assert receipt["regi_status"] == regi_status and words in receipt["note"] and never not in receipt["note"]

@pytest.mark.parametrize("script, posts, status", [
    ([tools.Unconfirmed("timeout"), None], 2, "scheduled"),         # one identical re-POST reconciles it
    ([tools.Unconfirmed("timeout"), tools.Unconfirmed("timeout")], 2, "unconfirmed"),
    ([RuntimeError("unreadable"), tools.Unconfirmed("timeout")], 2, "unconfirmed"),
    ([tools.Refused("HTTP 500", 500, {"ok": False, "error": "internal_error"}), None], 2, "scheduled"),
])
def test_an_unclear_schedule_gets_exactly_one_identical_re_post(make, script, posts, status):
    effects = Tilbakering([CALL_BACK], [s for s in script if s is not None])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    assert len(effects.posts) == posts and effects.posts[0][1] == effects.posts[1][1]   # identical body, same op
    assert _receipts(said)[0]["status"] == status


def test_an_unconfirmed_cancel_never_claims_cancelled(make):
    effects = Tilbakering([CALL_BACK])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    effects.calls = [("cancel_callback", {})]
    effects.script = [tools.Unconfirmed("timeout"), tools.Unconfirmed("timeout")]
    call, said, clock = make(effects, call_id="call-2", **CB)
    _user_says(call, clock, "Ikke ring likevel.")
    receipt = _receipts(said)[0]
    assert receipt["status"] == "unconfirmed" and "may still come" in receipt["note"]


@pytest.mark.parametrize("code, error", [(400, "missing_recipient_principal"), (400, "due_out_of_range"),
                                         (409, "operation_id_mismatch")])
def test_a_regi_refusal_is_refused_and_never_claimed_scheduled(make, code, error):
    effects = Tilbakering([CALL_BACK], [tools.Refused(f"HTTP {code}", code,
                                                      {"ok": False, "result": "refused", "error": error})])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    receipt = _receipts(said)[0]
    assert (receipt["status"], receipt["error"]) == ("refused", error) and len(effects.posts) == 1


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



# ---- the 20:06 real-call findings: the note's size and the promised call back ----------------


def _status_of(note_text, receipt):
    return receipt["status"] in note_text


WORST8 = ([{"tool": "delegate_to_agent", "agent": a, "status": s, "reason": r, "regi_status": None}
           for a, s, r in (("Jonas", "forwarded", None), ("Noël-Østensjø 🚀🚀🚀", "not_forwarded",
                           "maks 3 tillegg per agent per sesjon er nådd; ikke lagt inn 🙈" * 4),
                           ("Eira", "unknown_delivery", "Tidsavbrudd etter 30 s – usikkert om det kom frem æøå" * 3),
                           ("Mira", "refused", "request_too_large"))]
          + [{"tool": "schedule_callback", "status": "scheduled", "minutes": 7, "at": "20:41", "replaced": {"minutes": 5}},
             {"tool": "cancel_callback", "status": "unconfirmed", "note": "the call back may still come"},
             {"tool": "schedule_callback", "status": "already_scheduled", "regi_status": "feil",
              "reason": "ring() feilet: ingen enhet svarte 😬 " * 10},
             {"tool": "get_agent_status", "agent": "Jonas", "status": "ok",
              "untrusted_data": {"state": "working", "current_step": "Skriver tester for prisvisning 🧪" * 20,
                                 "last_said": "Jeg holder på med API-et og regner med å være ferdig snart 👍" * 30}}])


def test_every_note_is_at_most_the_byte_budget_including_control_text_for_the_worst_case():
    notes = REAL_HOST_NOTE(WORST8)
    assert notes and all(tools.note_tokens(n) <= tools.NOTE_BYTES < 500 for n in notes)
    assert all(n.startswith("Host note") for n in notes)
    joined = " ".join(notes)
    for r in WORST8:                                   # every receipt, with its status, is in exactly one note
        assert sum(1 for n in notes if r["status"] in n) >= 1
    assert "not_forwarded" in joined and "unknown_delivery" in joined and "refused" in joined
    assert "regi feil" in joined and "reason ring() feilet" in joined and "replaced 5 min" in joined
    assert "7 min" in joined and "unconfirmed" in joined


def test_receipts_are_never_split_across_notes():
    notes = REAL_HOST_NOTE(WORST8 * 3)
    for n in notes:
        body = n[len("Host note"):]
        assert all(part.count(": ") >= 1 for part in body.split(" | "))
    assert sum(n.count(" | ") + 1 for n in notes) == 24


def test_the_20_06_receipts_now_fit_in_bounded_notes():
    """The real call's four receipts (the provider refused them as one 500+ token append) are bounded now."""
    text = ("Gå gjennom backend-arbeidet og identifiser de viktigste konkrete oppgavene på to-do-listen akkurat nå. "
            "Prioriter dem og meld kort tilbake hva som bør tas først.")
    receipts = [{"tool": "delegate_to_agent", "agent": a, "status": "forwarded", "request": text, "sesjon": 1,
                 "intent": "ab" * 32, "intent_status": "sendt", "base_task": b, "additions_used": 0, "additions_max": 3,
                 "sent": True, "note": "the team's base task was started and the instruction was sent"}
                for a, b in (("Jonas", "started_now"), ("Noel", "already_started"), ("Eira", "already_started"),
                             ("Mira", "already_started"))]
    notes = REAL_HOST_NOTE(receipts)
    assert all(tools.note_tokens(n) <= tools.NOTE_BYTES for n in notes) and " ".join(notes).count(": forwarded") == 4


def test_a_live_call_sends_bounded_notes(make, monkeypatch):
    monkeypatch.setattr(tools, "host_note", REAL_HOST_NOTE)
    effects = Regi([ASK, ("delegate_to_agent", {"agent": "drammen-noel", "request": "x"})],
                   [_v1("forwarded", True, intent_status="sendt", base_task="started_now"),
                    _v1("forwarded", True, intent_status="sendt")])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Kan teamet ta sak 182?")
    assert said and all(tools.note_tokens(text) <= tools.NOTE_BYTES for _k, text in said)

AUTO = {"callback": {"kind": "http", "url": RING}, "callback_on_delegation": {"minutes": 5}}


def test_callback_on_delegation_books_the_promised_call_back_once_after_work_was_handed_on(make):
    effects = Tilbakering([ASK, ("delegate_to_agent", {"agent": "drammen-noel", "request": "Lag skjermbildet"})])
    call, said, clock = make(effects, **AUTO)
    _user_says(call, clock, "Kan teamet ta sak 182?")             # nobody said "ring meg"
    callbacks = effects.to(RING)
    assert len(callbacks) == 1 and callbacks[0]["action"] == "schedule" and callbacks[0]["minutes"] == 5
    assert [r["status"] for r in _receipts(said)][-1] == "scheduled"
    effects.calls = [("delegate_to_agent", {"agent": "drammen-jonas", "request": "Og eksport"})]
    _user_says(call, clock, "Og eksport.")
    assert len(effects.to(RING)) == 1                              # once per call


def test_callback_on_delegation_does_nothing_without_handed_on_work_or_when_already_asked(make):
    effects = Tilbakering([("get_agent_status", {"agent": "drammen-jonas"})])
    call, said, clock = make(effects, **AUTO)
    _user_says(call, clock, "Hva gjør Jonas?")
    assert effects.to(RING) == []
    both = Tilbakering([ASK, ("schedule_callback", {"minutes": 7, "reason": "status"})])
    call, said, clock = make(both, call_id="call-2", **AUTO)
    _user_says(call, clock, "Ta sak 182 og ring meg om sju.")
    assert [b["minutes"] for b in both.to(RING)] == [7]            # the user's own ask wins; no second booking


def test_callback_on_delegation_needs_a_regi_call_back():
    assert _config(callback_on_delegation={"minutes": 5}) is None
    cfg = _config(callback={"kind": "http", "url": RING}, callback_on_delegation=True)
    assert cfg["callback_on_delegation"] == {"minutes": 5}


AUTO7 = {"callback": {"kind": "http", "url": RING}, "callback_minutes": {"default": 7, "min": 1, "max": 15},
         "callback_on_delegation": {"minutes": 7}}


@pytest.mark.parametrize("explicit_support, actions", [
    (True, ["schedule", "schedule"]),                          # regi with explicit: replaced in one POST
    (False, ["schedule", "schedule", "cancel", "schedule"]),   # an older regi: kept => cancel + book anew
])
def test_the_automatic_booking_is_replaced_when_peter_names_other_minutes_in_the_same_call(make, ledger,
                                                                                         explicit_support, actions):
    effects = Tilbakering([ASK], explicit_support=explicit_support)
    call, said, clock = make(effects, **AUTO7)
    _user_says(call, clock, "Kan teamet ta sak 182?")
    first = _receipts(said)[-1]
    assert (first["status"], first["minutes"]) == ("scheduled", 7)
    assert "explicit" not in effects.to(RING)[0]                               # the automatic one never is
    effects.calls = [("schedule_callback", {"minutes": 5, "reason": "status"})]
    _user_says(call, clock, "Ring meg om fem minutter.")
    receipt = _receipts(said)[-1]
    assert (receipt["status"], receipt["minutes"], receipt["replaced"]["minutes"]) == ("scheduled", 5, 7)
    assert [b["action"] for b in effects.to(RING)] == actions
    explicit_post = effects.to(RING)[1]
    assert explicit_post["explicit"] is True and explicit_post["supersedes"] == first["operation_id"]
    assert len(effects.pending) == 1 and next(iter(effects.pending.values()))["op"] == receipt["operation_id"]
    row = next(v for k, v in _rows(ledger).items() if k.startswith("cb-"))
    assert (row["minutes"], row["status"]) == (5, "pending")


def test_explicit_minutes_and_a_delegation_in_one_utterance_book_exactly_one_call_back(make):
    effects = Tilbakering([ASK, ("schedule_callback", {"minutes": 5, "reason": "status"})])
    call, said, clock = make(effects, **AUTO7)
    _user_says(call, clock, "Ta sak 182 og ring meg om fem minutter.")
    assert [b["minutes"] for b in effects.to(RING)] == [5] and len(effects.pending) == 1


def test_the_same_minutes_as_the_automatic_booking_keep_it(make):
    effects = Tilbakering([ASK])
    call, said, clock = make(effects, **AUTO7)
    _user_says(call, clock, "Kan teamet ta sak 182?")
    effects.calls = [("schedule_callback", {"minutes": 7, "reason": "status"})]
    _user_says(call, clock, "Ring meg om sju.")
    assert _receipts(said)[-1]["status"] == "already_scheduled"
    assert [b["action"] for _u, b in effects.posts if _u == RING] == ["schedule", "schedule"]


def test_a_replacement_whose_cancel_is_unclear_never_books_a_second_call_back(make):
    effects = Tilbakering([ASK], explicit_support=False)
    call, said, clock = make(effects, **AUTO7)
    _user_says(call, clock, "Kan teamet ta sak 182?")
    effects.calls = [("schedule_callback", {"minutes": 5, "reason": "status"})]
    effects.cancel_errors = [tools.Unconfirmed("timeout"), tools.Unconfirmed("timeout")]
    _user_says(call, clock, "Ring meg om fem.")
    receipt = _receipts(said)[-1]
    assert receipt["status"] == "unconfirmed" and "still stands" in receipt["note"]
    assert [b["action"] for b in effects.to(RING)] == ["schedule", "schedule", "cancel", "cancel"]
    assert len(effects.pending) == 1                                          # still exactly the old one


def test_an_unclear_explicit_replacement_is_unconfirmed_and_never_books_twice(make):
    effects = Tilbakering([ASK])
    call, said, clock = make(effects, **AUTO7)
    _user_says(call, clock, "Kan teamet ta sak 182?")
    effects.calls = [("schedule_callback", {"minutes": 5, "reason": "status"})]
    effects.script = [tools.Unconfirmed("timeout"), tools.Unconfirmed("timeout")]
    _user_says(call, clock, "Ring meg om fem.")
    receipt = _receipts(said)[-1]
    assert receipt["status"] == "unconfirmed" and len(effects.pending) == 1


def test_ikke_ring_cancels_the_automatic_booking(make):
    effects = Tilbakering([ASK])
    call, said, clock = make(effects, **AUTO7)
    _user_says(call, clock, "Kan teamet ta sak 182?")
    effects.calls = [("cancel_callback", {})]
    _user_says(call, clock, "Ikke ring meg likevel.")
    assert _receipts(said)[-1]["status"] == "cancelled" and effects.pending == {}



def test_delegation_and_ikke_ring_in_the_same_round_never_books_a_call_back(make):
    effects = Tilbakering([ASK, ("cancel_callback", {})])
    call, said, clock = make(effects, **AUTO7)
    _user_says(call, clock, "Ta sak 182, men ikke ring meg.")
    assert [b["action"] for b in effects.to(RING)] == ["cancel"] and effects.pending == {}
    effects.calls = [("delegate_to_agent", {"agent": "drammen-noel", "request": "Lag skjermbildet"})]
    _user_says(call, clock, "Og Noel tar skjermbildet.")                      # a later round: still no auto booking
    assert [b["action"] for b in effects.to(RING)] == ["cancel"] and effects.pending == {}


def test_ikke_ring_in_an_earlier_round_suppresses_the_auto_booking_later(make):
    effects = Tilbakering([("cancel_callback", {})])
    call, said, clock = make(effects, **AUTO7)
    _user_says(call, clock, "Du trenger ikke ringe meg i kveld.")
    effects.calls = [ASK]
    _user_says(call, clock, "Ta sak 182.")
    assert [b["action"] for b in effects.to(RING)] == ["cancel"]

def test_two_cancels_in_one_call_are_two_operations(make):
    effects = Tilbakering([ASK], explicit_support=False)               # the replacement itself cancels once
    call, said, clock = make(effects, **AUTO7)
    _user_says(call, clock, "Ta sak 182.")
    effects.calls = [("schedule_callback", {"minutes": 5, "reason": "x"})]
    _user_says(call, clock, "Ring meg om fem.")
    effects.calls = [("cancel_callback", {})]
    _user_says(call, clock, "Ikke ring.")
    cancels = [b["operation_id"] for _u, b in effects.posts if _u == RING and b["action"] == "cancel"]
    assert len(cancels) == 2 and len(set(cancels)) == 2 and _receipts(said)[-1]["status"] == "cancelled"

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
        for path in ("/down", "/bad"):
            with pytest.raises(tools.Refused) as refused:
                tools._post(base + path, {})
            assert refused.value.code in (503, 400)
        assert tools._post(base + "/lies", {}) == {"ok": False, "error": "busy"}   # 2xx: the caller reads it
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

from tests.unit.test_agent_ring_live_socket import _admitted, _wait, call  # noqa: F401 - call is a fixture


@pytest.fixture
def opted_in(sander):
    meta = json.loads((sander.folder / "profile.json").read_text())
    meta["live_tools"] = _block(stories=["bottle"])
    (sander.folder / "profile.json").write_text(json.dumps(meta))
    return sander


def test_a_live_call_routes_the_users_turn_and_tells_the_voice_what_was_done(opted_in, call, monkeypatch):
    effects = FakeEffects([ASK])
    monkeypatch.setattr(tools, "Effects", lambda key, ctx=None: effects)
    h, thread = _admitted(call)
    call.up.push({"type": "session.input_transcript.delta", "delta": "Sandra, kan Jonas ta sak 182?"})
    _wait(lambda: call.up.of("session.thinking.append"))
    note = call.up.of("session.thinking.append")[0]["content"]
    assert '"forwarded"' in note and effects.prompts[0][:2] == ("drammen-jonas", "rk-sander-0001")
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
    monkeypatch.setattr(tools, "Effects", lambda key, ctx=None: effects)
    h, thread = _admitted(call)
    call.up.push({"type": "session.input_transcript.delta", "delta": "Ta sak 182, ha det!"})
    h.send({"type": "session.close"})
    thread.join(15)
    time.sleep(tools.SETTLE_S + 0.5)
    assert effects.prompts == []


def test_a_live_call_without_live_tools_never_routes(call, monkeypatch):
    built = []
    monkeypatch.setattr(tools, "Effects", lambda key, ctx=None: built.append(key))
    h, thread = _admitted(call)
    call.up.push({"type": "session.input_transcript.delta", "delta": "Kan Jonas ta sak 182?"})
    call.up.push({"type": "session.delegation.created", "delegation": {"id": "d1"}})
    time.sleep(tools.SETTLE_S + 0.5)
    assert built == [] and not call.up.of("session.thinking.append") and not call.up.of("session.commentary.append")
    h.send({"type": "session.close"})
    thread.join(15)



# ---- Oracle parity: ask my own agent, answers relayed back in parts, read_result ----------------

BOARD_Q = ("ask_my_agent", {"question": "Hva er klart å gjøre på SolNO-boardet som er tildelt Peter?"})
LONG_ANSWER = ("Tre saker er klare for deg på SolNO-boardet. Først #491, rekkefølge på kalkulatorer i "
               "tjenestekatalogen, som står i Ready. ") + "Deretter flere detaljer om status og hvem som eier hva. " * 40


def _quiet(call, clock, seconds=6.0):
    clock.now += seconds
    call.tick()


def test_ask_my_agent_uses_oracles_delegation_to_the_contact_and_says_she_checks(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart på boardet mitt?")
    [(ident, session, request, principal)] = effects.asks
    assert session == "rk-sander-0001" and principal == "device_phone" and "SolNO-boardet" in request
    # exactly the shape oracle_delegations.undelivered() holds back for this call (and only that shape)
    assert re.fullmatch(r"sandra-[0-9a-f]{24}", ident) and re.match(r"Spørsmål fra .*Live-samtale", request)
    receipt = _receipts(said)[0]
    assert receipt["status"] == "asked" and receipt["operation_id"] == ident and "jeg sjekker" in receipt["note"]


def test_the_contact_is_configurable_and_never_a_team_member():
    cfg = _config(contact={"session": "rk-sander-0001", "name": "Sandras agent"})
    assert cfg["contact"] == {"session": "rk-sander-0001", "name": "Sandras agent"}
    assert _config(contact={"session": "drammen-jonas"}) is None


def test_the_answer_is_relayed_into_the_call_in_parts_oracle_style_and_acknowledged(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart på boardet mitt?")
    ident = effects.asks[0][0]
    n_before = len(said)
    _quiet(call, clock)
    assert len(said) == n_before                                    # nothing until the answer is there
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": LONG_ANSWER,
                           "request_text": "Hva er klart?", "session": "rk-sander-0001"}
    _quiet(call, clock)                                             # poll: queued
    _quiet(call, clock)                                             # quiet: part 1
    parts = [text for kind, text in said[n_before:] if re.search(r"\) 1/\d+, ", text)]
    assert parts and "Tre saker er klare" in parts[0] and effects.acked == [ident]
    total = int(re.search(r"\) 1/(\d+), ", parts[0]).group(1))
    assert total > 1
    call.note_output(); _quiet(call, clock)                         # spoken + quiet => next part
    assert any(f") 2/{total}," in text for _k, text in said)
    assert all(len(text.encode("utf-8")) <= tools.NOTE_BYTES for _k, text in said)


def test_no_part_is_sent_while_the_user_is_speaking_or_the_voice_has_not_spoken(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": LONG_ANSWER, "session": "x"}
    _quiet(call, clock); _quiet(call, clock)
    n = sum("part " in t for _k, t in said)
    _quiet(call, clock)                                             # the voice has not spoken part 1 yet
    assert sum("part " in t for _k, t in said) == n
    call.observe({"type": "session.input_transcript.delta", "delta": "vent litt"})
    call.note_output(); clock.now += 1.0; call.tick()               # the user just spoke: hold
    assert sum("part " in t for _k, t in said) == n


def test_read_result_replays_or_continues_from_stored_text_without_asking_again(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": LONG_ANSWER, "session": "x"}
    _quiet(call, clock); _quiet(call, clock)
    effects.calls = [("read_result", {"from_part": 1})]
    _user_says(call, clock, "Kan du lese det første igjen?")
    assert len(effects.asks) == 1                                   # never asked again
    assert any(re.search(r"\) 1/\d+, ", t) and "Stored text." in t for _k, t in said)


def test_a_failed_ask_is_said_honestly(make):
    effects = FakeEffects([BOARD_Q])
    effects.ask_error = RuntimeError("no context")
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    assert _receipts(said)[0]["status"] == "failed"


def test_an_unclear_ask_that_oracle_admitted_is_watched_not_resent(make):
    effects = FakeEffects([BOARD_Q])
    effects.ask_error = TimeoutError("slow")
    effects.delegation = lambda ident: {"delegation_id": ident, "status": "running"}
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    assert _receipts(said)[0]["status"] == "asked" and len(effects.asks) == 1


def test_an_ask_whose_admission_cannot_be_read_back_is_unknown(make):
    effects = FakeEffects([BOARD_Q])
    effects.ask_error = TimeoutError("slow")
    effects.delegation = lambda ident: (_ for _ in ()).throw(OSError("db"))
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    assert _receipts(said)[0]["status"] == "unknown" and len(effects.asks) == 1


def test_the_same_question_in_one_call_goes_out_once(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart på boardet mitt?")
    _user_says(call, clock, "Sjekk boardet mitt igjen.")
    assert len(effects.asks) == 1 and _receipts(said)[0]["status"] == "already_asked"


def test_an_answer_after_the_call_ended_is_never_relayed(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart på boardet mitt?")
    ident = effects.asks[0][0]
    call.close()
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.",
                           "request_text": "Hva er klart?", "session": "rk-sander-0001"}
    n = len(said)
    _quiet(call, clock); _quiet(call, clock)
    assert len(said) == n and ident.startswith(tools.ASK_PREFIX)


def test_a_members_transcript_is_never_relayed_automatically_after_a_delegation(make):
    effects = Regi([ASK], [_v1("forwarded", True, intent_status="sendt")])
    call, said, clock = make(effects, **REGI)
    _user_says(call, clock, "Ta sak 182.")
    n = len(said)
    _quiet(call, clock); _quiet(call, clock)
    assert len(said) == n and not hasattr(tools.Effects, "member_reply")   # not bound to the intent: never relayed


def test_a_refused_append_never_shrinks_the_relay_below_its_bound():
    effects = FakeEffects()
    call = tools.CallTools(_snapshot(), "call-1", lambda k, x: None, effects=effects, executor=Inline(),
                           principal="device_phone", ledger=tools.Ledger(__import__("pathlib").Path("/dev/null")))
    relay = tools._relay("k", "Æøå🙂" * 40, "src " + "🙂" * 40, LONG_ANSWER, "Spørsmål 🙂 " * 200, tools.RELAY_PART_BYTES)
    call.relays["k"] = call.active_relay = relay
    relay.next = 2
    for _ in range(5):
        call.append_rejected()
    assert call.relay_part_bytes == tools.RELAY_PART_BYTES and call.active_relay is relay and relay.next == 2


def test_the_ask_note_tells_sandra_to_say_she_is_checking():
    notes = REAL_HOST_NOTE([{"tool": "ask_my_agent", "status": "asked", "operation_id": "sandra-x"}])
    assert len(notes) == 1 and "jeg sjekker" in notes[0] and tools.note_tokens(notes[0]) <= tools.NOTE_BYTES



@pytest.mark.parametrize("text", ["ø" * 3000, "🙂" * 900, "Tre saker. " * 300, "x" * 5000])
def test_every_relay_append_is_at_most_450_utf8_bytes_and_the_text_is_lossless(text):
    worst_request = "Spørsmål 🙂 " * 200
    for label in ("Sandras agent's reply", "Æøå🙂" * 40):
        relay = tools._relay("sandra-" + "f" * 24, label, "operation " + "sandra-" + "f" * 24, text, worst_request,
                             tools.RELAY_PART_BYTES)
        assert "".join(relay.chunks) == text
        for verbatim in (False, True):
            relay.verbatim = verbatim
            for i in range(relay.total):
                for served in (False, True):
                    for pause in (False, True):
                        part = relay.part(i, served=served, resumed=True, pause_after=pause)
                        assert len(part.encode("utf-8")) <= tools.NOTE_BYTES
            for sent in (0, relay.total):
                relay.next = sent
                assert len(relay.status().encode("utf-8")) <= tools.NOTE_BYTES


def test_a_late_poll_after_hangup_neither_adopts_nor_acknowledges(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    call.close()
    call.poll_answers()                                             # a poll that was already queued runs late
    assert effects.acked == [] and call.pending_relays == []        # Oracle's row stays undelivered


def test_a_poll_for_a_call_that_is_no_longer_current_does_nothing(make):
    effects = FakeEffects([BOARD_Q])
    live = {"now": True}
    call, said, clock = make(effects, current=lambda: live["now"])
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    live["now"] = False
    call.poll_answers()
    assert effects.acked == [] and call.pending_relays == []


def test_the_answer_is_acknowledged_only_once_its_first_part_is_in_the_call(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    call.poll_answers()
    assert call.pending_relays and effects.acked == []              # adopted, not yet delivered
    _quiet(call, clock)
    assert effects.acked == [ident] and any("Fjorten saker." in t for _k, t in said)



def test_a_part_the_socket_did_not_send_is_neither_counted_nor_acknowledged(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    call.poll_answers()
    sent = {"ok": False}
    call.speak = lambda kind, text: sent["ok"] and (said.append((kind, text)) or True)
    _quiet(call, clock)                                             # dropped (not drained / no upstream)
    relay = call.active_relay
    assert relay.next == 0 and relay.sent == 0 and effects.acked == [] and relay.ack == ident
    sent["ok"] = True
    _quiet(call, clock)                                             # once quiet: the same part 1 again, now sent
    assert relay.next == 1 and effects.acked == [ident] and any("Fjorten saker." in t for _k, t in said)


def test_a_close_between_the_poll_and_the_append_leaves_the_answer_unacknowledged(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    call.poll_answers()
    relay = call.pending_relays[0]
    call.close()
    assert call._speak_part(relay) is False                         # even a direct append attempt after close
    assert effects.acked == [] and relay.next == 0 and not any("Fjorten" in t for _k, t in said)


def test_the_socket_reports_whether_a_tool_note_was_actually_sent():
    import threading as _threading
    from lib import agent_ring_live_socket as sock

    class Up:
        def __init__(self):
            self.frames = []

        def send(self, raw):
            self.frames.append(json.loads(raw))

    s = object.__new__(sock.LiveSocket)
    s.call_id, s.drained, s.closed, s.stop = "call-x", False, _threading.Event(), _threading.Event()
    s._fence, s._send_lock, s._state, s.instructions_appends_issued = _threading.Lock(), _threading.Lock(), _threading.Lock(), 0
    s.upstream = Up()
    assert s.tool_context("commentary", "del 1") is False             # not drained: dropped
    s.drained = True
    up, s.upstream = s.upstream, None
    assert s.tool_context("commentary", "del 1") is False             # no upstream: dropped
    s.upstream = up
    assert s.tool_context("commentary", "del 1") is True and len(up.frames) == 1
    s.stop.set()
    assert s.tool_context("commentary", "del 2") is False and len(up.frames) == 1   # stopped/retired: refused
    s.stop.clear()
    retired = {"now": True}
    original = sock.live.retired
    sock.live.retired = lambda call_id: retired["now"] or original(call_id)
    try:
        assert s.tool_context("commentary", "del 2") is False and len(up.frames) == 1   # retired: effect False
    finally:
        sock.live.retired = original

    class Broken(Up):
        def send(self, raw):
            raise OSError("socket gone")
    s.upstream = Broken()
    with pytest.raises(OSError):                                       # a send error is never reported as sent
        s.tool_context("commentary", "del 2")
    s.upstream = up
    s.closed.set()
    assert s.tool_context("commentary", "del 2") is False and len(up.frames) == 1


def test_a_send_error_keeps_the_part_unsent_and_the_answer_unacknowledged(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    call.poll_answers()

    def broken(kind, text):
        raise OSError("socket gone")
    call.speak = broken
    _quiet(call, clock)
    relay = call.active_relay
    assert relay.next == 0 and relay.sent == 0 and relay.uncertain == 0 and relay.held and effects.acked == []
    call.speak = lambda kind, text: said.append((kind, text)) or True
    n = len(said)
    for _ in range(4):
        _quiet(call, clock, seconds=tools.RELAY_STALL_S + 1)       # an uncertain part is never replayed by itself
    assert len(said) == n and effects.acked == [] and relay.next == 0
    assert "Fjorten saker." in relay.text                             # the text is kept
    effects.calls = [("read_result", {})]
    _user_says(call, clock, "Les svaret igjen.")                     # on request: sent, counted, acknowledged
    assert relay.next == 1 and relay.uncertain is None and effects.acked == [ident]
    assert any("Fjorten saker." in t for _k, t in said[n:])


def test_a_known_drop_is_retried_but_a_send_error_is_not(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    call.poll_answers()
    attempts = []
    call.speak = lambda kind, text: attempts.append("drop") and False
    _quiet(call, clock); _quiet(call, clock)
    assert len(attempts) == 2 and call.active_relay.unsent and call.active_relay.uncertain is None


def test_a_live_call_relays_the_answer_through_the_socket_and_acknowledges_after_the_append(opted_in, call,
                                                                                            monkeypatch):
    effects = FakeEffects([BOARD_Q])
    monkeypatch.setattr(tools, "Effects", lambda key, ctx=None: effects)
    h, thread = _admitted(call)
    call.up.push({"type": "session.input_transcript.delta", "delta": "Hva er klart på boardet mitt?"})
    _wait(lambda: effects.asks)
    ident = effects.asks[0][0]
    assert effects.acked == []
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker er klare.",
                           "request_text": "Hva er klart?", "session": "rk-sander-0001"}
    _wait(lambda: any("Fjorten saker er klare." in str(f) for f in call.up.of("session.commentary.append")), 20)
    _wait(lambda: effects.acked == [ident])
    assert all(len(json.dumps(f["content"], ensure_ascii=False).encode()) <= tools.NOTE_BYTES + 2
               for f in call.up.of("session.commentary.append"))
    h.send({"type": "session.close"})
    thread.join(15)


def test_before_the_opening_is_drained_a_current_call_neither_advances_nor_acknowledges(make):
    import threading as _threading
    from lib import agent_ring_live_socket as sock

    class Up:
        def __init__(self):
            self.frames = []

        def send(self, raw):
            self.frames.append(json.loads(raw))

    s = object.__new__(sock.LiveSocket)                                # the real socket's send path
    s.call_id, s.drained, s.closed, s.stop = "call-x", False, _threading.Event(), _threading.Event()
    s._fence, s._send_lock, s._state, s.instructions_appends_issued = _threading.Lock(), _threading.Lock(), _threading.Lock(), 0
    s.upstream = up = Up()
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)                                  # current() is True throughout
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    call.poll_answers()
    call.speak = s.tool_context
    _quiet(call, clock)                                                # pre-drain: dropped by the socket
    relay = call.active_relay
    assert relay.next == 0 and relay.sent == 0 and effects.acked == [] and up.frames == []
    s.drained = True
    _quiet(call, clock)                                                # drained: sent, then counted and acked
    assert relay.next == 1 and effects.acked == [ident] and len(up.frames) == 1
    assert "Fjorten saker." in json.dumps(up.frames[0], ensure_ascii=False)



def test_only_audible_output_counts_as_the_voice_speaking_so_a_relay_can_start():
    import array
    import base64 as _b64
    from lib import agent_ring_live_socket as sock

    class Tools:
        spoke = 0

        def note_output(self):
            Tools.spoke += 1

    s = object.__new__(sock.LiveSocket)
    s.tools, s._note_order, s._tools_observe, s._output = Tools(), (lambda k, e: None), (lambda e: None), (lambda d: None)
    silence = _b64.b64encode(array.array("h", [0] * 480).tobytes()).decode()
    speech = _b64.b64encode(array.array("h", [3000, -3000] * 240).tobytes()).decode()
    for _ in range(50):                                          # GPT-Live streams silent frames between turns
        s.receive({"type": "session.output_audio.delta", "delta": silence})
    assert Tools.spoke == 0
    s.receive({"type": "session.output_audio.delta", "delta": speech})
    s.receive({"type": "session.output_audio.delta", "delta": "not base64!"})
    assert Tools.spoke == 1


def test_a_finished_answer_is_relayed_while_the_provider_streams_silence(make):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    for _ in range(12):                                          # silence only: note_output is never called
        clock.now += 1.0; call.tick()
    assert any("Fjorten saker." in t for _k, t in said) and effects.acked == [ident]



def _answered(make, routing=False):
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects)
    _user_says(call, clock, "Hva er klart?")
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    clock.now += tools.APPEND_GAP_S
    call.poll_answers()
    call.routing = routing
    return call, said, clock, effects, ident


def test_a_finished_answer_starts_at_a_short_pause_even_while_a_route_runs(make):
    call, said, clock, effects, ident = _answered(make, routing=True)
    call.last_user, call.last_output = clock.now - 0.9, clock.now - 0.9     # the user only 0.9 s quiet
    call.tick()
    assert not any("Fjorten saker." in t for _k, t in said)
    call.last_user = clock.now - 1.1                                        # 1.0 s user / 0.8 s voice, a route running
    call.tick()
    assert any("Fjorten saker." in t for _k, t in said) and effects.acked == [ident]


def _mic(call, clock, seconds, audible, transcript_every=None):
    """The phone sends a microphone chunk every 0.1 s; the transcript (if any) arrives only every N s."""
    for step in range(int(round(seconds * 10))):
        clock.now += 0.1
        call.note_input(audible)
        if transcript_every and step % int(transcript_every * 10) == 0:
            call.observe({"type": "session.input_transcript.delta", "delta": "og så "})
        call.last_output = clock.now - 2.0                                  # the voice is quiet throughout
        call.tick()


def test_continuous_speech_with_a_sparse_late_transcript_never_starts_an_answer(make):
    call, said, clock, effects, ident = _answered(make)
    _mic(call, clock, 10.0, audible=True, transcript_every=3.0)             # 10 s of speech, transcript every 3 s
    assert not any("Fjorten saker." in t for _k, t in said) and effects.acked == []


def test_a_real_half_second_pause_after_six_seconds_starts_the_answer(make):
    call, said, clock, effects, ident = _answered(make)
    _mic(call, clock, 6.5, audible=True)
    assert not any("Fjorten saker." in t for _k, t in said)
    _mic(call, clock, 0.6, audible=False)                                   # a real pause: quiet chunks for 0.6 s
    assert any("Fjorten saker." in t for _k, t in said) and effects.acked == [ident]


def test_a_gap_in_the_microphone_audio_is_not_a_pause(make):
    call, said, clock, effects, ident = _answered(make)
    _mic(call, clock, 6.5, audible=True)
    for _ in range(30):                                                     # 3 s without any chunk (network lag)
        clock.now += 0.1
        call.last_output = clock.now - 2.0
        call.tick()
    assert not any("Fjorten saker." in t for _k, t in said) and effects.acked == []


def test_the_socket_reports_the_users_microphone_speech_to_the_tools():
    import array
    import base64 as _b64
    import threading as _threading
    from lib import agent_ring_live_socket as sock

    heard = []

    class Tools:
        def note_input(self, audible):
            heard.append(audible)

    s = object.__new__(sock.LiveSocket)
    s.closed, s.stop = _threading.Event(), _threading.Event()
    s.admitted, s.forwarding, s.tools = True, True, Tools()
    s.upstream_send = lambda event: True
    quiet = _b64.b64encode(array.array("h", [0] * 480).tobytes()).decode()
    speech = _b64.b64encode(array.array("h", [3000, -3000] * 240).tobytes()).decode()
    for audio in (quiet, speech, quiet):
        s.client({"type": "session.input_audio.append", "audio": audio})
    assert heard == [False, True, False]


def test_a_chatty_user_without_pauses_gets_no_start_and_a_short_pause_after_six_seconds_starts_it(make):
    call, said, clock, effects, ident = _answered(make, routing=True)
    for _ in range(9):                                                      # Peter talks without pauses for 9 s
        clock.now += 1.0
        call.last_user, call.last_output = clock.now - 0.1, clock.now - 1.6  # the voice is quiet, Peter is not
        call.tick()
    assert not any("Fjorten saker." in t for _k, t in said) and effects.acked == []
    clock.now += 0.6                                                        # a short pause (0.6 s) after 6 s waited
    call.tick()
    assert any("Fjorten saker." in t for _k, t in said) and effects.acked == [ident]


def test_a_waiting_answer_logs_why_every_three_seconds(make, monkeypatch):
    logged = []
    monkeypatch.setattr(tools, "log", lambda event, text: logged.append((event, text)))
    call, said, clock, effects, ident = _answered(make)
    for _ in range(4):                                                      # 4 s with the voice speaking
        clock.now += 1.0
        call.last_user, call.last_output = clock.now - 0.1, clock.now - 0.1
        call.tick()
    waits = [t for e, t in logged if e == "agentLiveRelayWaiting"]
    assert 1 <= len(waits) <= 2 and "reason=voice_speaking" in waits[0] and "waited_s=" in waits[0]


# ---- save_transcript ---------------------------------------------------------------------------

def test_save_transcript_is_on_only_when_the_profile_says_true():
    assert _config(save_transcript=True)["save_transcript"] is True
    assert "save_transcript" not in _config() and "save_transcript" not in _config(save_transcript="yes")


def test_without_the_flag_no_transcript_is_written(make, ledger):
    call, said, clock = make(FakeEffects([BOARD_Q]))
    _user_says(call, clock, "Hva er klart?")
    call.close()
    assert not (ledger.path.parent / "transcripts").exists()


def test_the_transcript_is_written_as_it_happens_and_saved_whole_when_the_call_closes(make, ledger):
    import stat
    effects = FakeEffects([BOARD_Q])
    call, said, clock = make(effects, save_transcript=True)
    folder = ledger.path.parent / "transcripts"
    _user_says(call, clock, "Hva er klart ")
    call.observe({"type": "session.input_transcript.delta", "delta": "på boardet?"})
    call.observe({"type": "session.output_transcript.delta", "delta": "Jeg sjekker."})
    partial = folder / "call-1.jsonl"
    lines = [json.loads(x) for x in partial.read_text().splitlines()]          # already on disk, before the close
    assert stat.S_IMODE(partial.stat().st_mode) == 0o600 and stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert [x["kind"] for x in lines].count("text") == 3 and any(x["kind"] == "receipt" for x in lines)
    ident = effects.asks[0][0]
    effects.rows[ident] = {"delegation_id": ident, "status": "completed", "result_text": "Fjorten saker.", "session": "x"}
    clock.now += tools.APPEND_GAP_S
    call.poll_answers(); clock.now += 2; call.tick()                       # answered + relayed: pacing entries
    call.close()
    doc = json.loads((folder / "call-1.json").read_text())
    assert not partial.exists() and stat.S_IMODE((folder / "call-1.json").stat().st_mode) == 0o600
    assert doc["call_id"] == "call-1" and doc["principal"] == "device_phone" and doc["agent_session"] == "rk-sander-0001"
    assert [(f["role"], f["text"]) for f in doc["fragments"]] == [("user", "Hva er klart på boardet?"),
                                                                    ("assistant", "Jeg sjekker.")]
    assert all(f["at"] <= f["end"] for f in doc["fragments"])
    assert doc["receipts"][0]["tool"] == "ask_my_agent" and doc["receipts"][0]["status"] == "asked"
    assert {p["event"] for p in doc["pacing"]} >= {"agentLiveAskAnswered", "agentLiveRelayStart"}
    assert "audio" not in json.dumps(doc).lower().replace("audio never", "")


def test_a_crash_keeps_everything_said_so_far(make, ledger):
    call, said, clock = make(FakeEffects(), save_transcript=True)
    for word in ("Hei ", "Sandra, ", "ring meg."):
        call.observe({"type": "session.input_transcript.delta", "delta": word})
    # no close(): the process died
    lines = [json.loads(x) for x in (ledger.path.parent / "transcripts" / "call-1.jsonl").read_text().splitlines()]
    assert "".join(x["text"] for x in lines if x["kind"] == "text") == "Hei Sandra, ring meg."


def test_transcripts_older_than_fourteen_days_are_deleted(tmp_path):
    import os as _os
    old, new, partial = tmp_path / "alv_old.json", tmp_path / "alv_new.json", tmp_path / "alv_crash.jsonl"
    for path in (old, new, partial):
        path.write_text("{}")
    past = time.time() - 15 * 24 * 3600
    _os.utime(old, (past, past)); _os.utime(partial, (past, past))
    assert tools.prune_transcripts(tmp_path) == 2 and new.exists() and not old.exists() and not partial.exists()
