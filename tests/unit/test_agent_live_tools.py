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
        self.prompts, self.posts, self.scheduled, self.stopped = [], [], [], []
        self.later_error = None
        self.timers: dict[str, str] = {}
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


def test_an_unreachable_regi_falls_back_to_a_prompt_only_when_configured(make):
    effects = Regi([ASK], [tools.Unreachable("connection refused")])
    call, said, clock = make(effects, delegate={"kind": "http", "url": DIRECTOR, "fallback": "prompt"})
    _user_says(call, clock, "Ta sak 182.")
    assert len(effects.prompts) == 1 and _receipts(said)[0]["status"] == "forwarded"


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

    def __init__(self, calls=(), script=()):
        super().__init__(calls)
        self.script, self.pending, self.ops = list(script), {}, {}

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
            if current:
                del self.pending[key]
                return {"ok": True, "result": "cancelled", "operation_id": current["op"], "status": "avlyst"}
            return {"ok": True, "result": "nothing_pending"}
        if current and current["call_id"] == body["call_id"]:
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


def test_asking_again_in_the_same_call_is_already_scheduled_and_never_supersedes(make):
    effects = Tilbakering([CALL_BACK, ("schedule_callback", {"minutes": 7, "reason": "igjen"})])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem. Nei, om sju.")
    assert [r["status"] for r in _receipts(said)] == ["scheduled", "already_scheduled"]
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



@pytest.mark.parametrize("regi_status, words", [("venter", "already pending"), ("fyrt", "already gone out"),
                                                ("fyrt_usikker", "never rung again"), ("avlyst", "cancelled earlier")])
def test_an_already_scheduled_answer_is_told_by_regis_current_status(make, regi_status, words):
    effects = Tilbakering([CALL_BACK], [{"ok": True, "result": "already_scheduled", "operation_id": "cb-x",
                                         "due": 1_800_000_300, "status": regi_status, "fired_at": None}])
    call, said, clock = make(effects, **CB)
    _user_says(call, clock, "Ring meg om fem.")
    receipt = _receipts(said)[0]
    assert receipt["regi_status"] == regi_status and words in receipt["note"]

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
