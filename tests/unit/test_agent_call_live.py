"""Issue #216: the USER starts a Live call with an agent whose approved profile opts it in (POST /calls/live).

Only a paired full-scope phone that runs Live; only an agent opted in by its
operator-approved profile (``user_call_default: "live"`` + ``stories.user_call``
+ ``user_call_opening``). The Host builds a ``user_call`` context (no event, no
camera observation, no client text), makes a private Live ring for that phone
alone and answers it on the existing Live path. Every refusal keeps its fixed
Live code and never becomes a Normal call; POST /calls is untouched.
CONTEXT-v1 rings are unchanged and still refuse a user-call context.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from lib import agent_ring_live as live
from lib import agent_ring_live_socket as sock
from lib import agent_rings as rings
from lib import group_calls as calls
from tests.unit.test_agent_ring_live import FILES, PROFILE, _context, _live_ring, _refused, _write, sander  # noqa: F401
from tests.unit.test_agent_rings import PHONE, _agent, env  # noqa: F401 - env / sander are fixtures

USER_OPENING = "Hei, det er Sandra. Hva kan jeg hjelpe deg med?"
USER_FILES = {"user_call_brief": ("USER-CALL.md", "Brukeren ringer selv; svar kort og rolig.")}


def _opt_in(folder, **changes):
    data = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
    for key, (name, text) in USER_FILES.items():
        _write(folder / name, text)
        data["files"][key] = {"file": name, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    data["stories"]["user_call"] = ["agents", "persona", *USER_FILES]
    data.update({"user_call_default": "live", "user_call_opening": USER_OPENING})
    data.update(changes)
    _write(folder / "profile.json", json.dumps(data))
    return data


@pytest.fixture
def opted(sander):
    _opt_in(sander.folder)
    return sander


def _call(principal=PHONE, agent="a-sander", request_id="call-req-1", nonce="n-1"):
    return rings.user_call_live(principal, agent, request_id, nonce)


def test_an_opted_in_agent_starts_a_live_user_call_answered_by_this_phone(opted):
    out = _call()
    assert set(out) == {"ok", "ring", "changed", "conversation_mode", "live"} and out["changed"] is True
    assert out["ring"]["origin"] == "user_call" and "origin" not in out["live"]
    ring, grant = out["ring"], out["live"]
    assert out["conversation_mode"] == "agent_live_v1" and ring["state"] == "answered"
    assert ring["answered_by"] == PHONE and ring["agent_id"] == "a-sander" and ring["opening_line"] == USER_OPENING
    assert (grant["provider"], grant["model"], grant["voice"]) == ("openai", "gpt-live-1", "marin")
    assert grant["ring_id"] == ring["ring_id"] and grant["agent_id"] == "a-sander"
    snap = live.snapshot_of(ring["ring_id"])
    assert grant["context_digest"] == hashlib.sha256(live.canonical(snap)).hexdigest()
    ctx = snap["event_context"]
    assert ctx["kind"] == "user_call" and ctx["source"] == "user-initiated" and ctx["story"] == "user_call"
    assert set(ctx) == {"version", "kind", "profile_id", "story", "source", "request_id", "requested_at",
                        "opening_text"}
    assert "event" not in ctx and ctx["request_id"] == "call-req-1"
    assert list(snap["texts"]) == ["agents", "persona", "user_call_brief"] and "bottle_rds" not in snap["texts"]
    assert calls.snapshot(PHONE)["call"] is None                 # no Normal call was started


def test_a_user_call_is_private_to_its_phone(opted, monkeypatch):
    pushed = []
    monkeypatch.setattr(rings, "_push_voip", lambda ring: pushed.append(ring) or {})
    ring = _call()["ring"]
    assert pushed == [] and not [e for e in opted.stream.of("agent-ring") if e.get("ring_id") == ring["ring_id"]]
    assert rings.current("device_phone2") is None and rings.current(PHONE) is None


def test_the_live_instructions_of_a_user_call_describe_no_event(opted):
    text = sock.instructions(live.snapshot_of(_call()["ring"]["ring_id"]))
    assert "The user called you from the Clarp app." in text
    assert "event you are calling about" not in text and "camera observation and the event log below" not in text
    assert FILES["persona"][1] in text and USER_FILES["user_call_brief"][1] in text
    assert FILES["bottle_rds"][1] not in text


def test_a_retry_with_the_same_request_id_gets_the_same_call(opted):
    first = _call()
    again = _call()
    assert again["changed"] is False and again["live"] == first["live"]
    assert len([r for r in rings._load() if r.get("conversation_mode") == "agent_live_v1"]) == 1


@pytest.mark.parametrize("principal", ["", "admin", "device_unpaired"])
def test_only_a_paired_phone_may_start_a_live_user_call(opted, principal):
    _refused(403, "live_auth", _call, principal=principal)
    assert not any(r.get("conversation_mode") for r in rings._load())


def test_an_agent_that_is_not_opted_in_is_refused_never_called_normally(sander):
    _refused(409, "live_context_unavailable", _call)                 # approved profile without the opt-in
    _agent("a-theo2", "Theo", "theo-0002")
    _refused(409, "live_context_unavailable", _call, agent="a-theo2")
    assert calls.snapshot(PHONE)["call"] is None


@pytest.mark.parametrize("damage", [{"stories": None}, {"user_call_opening": ""}, {"user_call_opening": "x" * 601}])
def test_an_opted_in_agent_with_an_unusable_profile_is_refused_never_downgraded(sander, damage):
    data = _opt_in(sander.folder)
    if "stories" in damage:
        data["stories"].pop("user_call")
        _write(sander.folder / "profile.json", json.dumps(data))
    else:
        _opt_in(sander.folder, **damage)
    _refused(409, "live_context_unavailable", _call)
    assert not any(r.get("conversation_mode") for r in rings._load())
    assert calls.snapshot(PHONE)["call"] is None


def test_a_changed_user_call_file_pin_is_refused(opted):
    (opted.folder / USER_FILES["user_call_brief"][0]).write_text("endret", encoding="utf-8")
    _refused(409, "live_context_unavailable", _call)


def test_the_provider_being_unusable_is_refused_not_downgraded(opted, monkeypatch):
    monkeypatch.setattr(live, "provider_problem", lambda: "no key")
    _refused(503, "live_unavailable", _call)
    assert calls.snapshot(PHONE)["call"] is None


def test_the_calling_phone_must_run_live(opted):
    rings.register_voip_token(PHONE, "ab" * 32, supported_conversation_modes=["classic"])
    _refused(409, "live_client_required", _call)


def test_a_bad_request_id_or_nonce_is_refused_and_blocks_nothing(opted):
    _refused(400, "live_request_invalid", _call, request_id="")
    _refused(400, "live_request_invalid", _call, request_id="x" * 129)
    _refused(400, "live_request_invalid", _call, request_id="ok-1", nonce="")
    assert not any(r["state"] == "ringing" for r in rings._load())   # a failed answer cancels its private ring
    assert _call(request_id="ok-2")["ring"]["state"] == "answered"


def test_two_opted_in_profiles_for_one_agent_are_refused(opted):
    other = opted.folder.parent / "rk-sander-2"
    data = json.loads((opted.folder / "profile.json").read_text(encoding="utf-8"))
    data["profile_id"] = "rk-sander-2"
    for name in {pin["file"] for pin in data["files"].values()}:
        _write(other / name, (opted.folder / name).read_bytes())
    _write(other / "profile.json", json.dumps(data))
    _refused(409, "live_context_unavailable", _call)


def test_context_v1_rings_are_unchanged_and_refuse_a_user_call_context(opted):
    assert _live_ring()["ring"]["conversation_mode"] == "agent_live_v1"     # leak-watch bottle ring still works
    for change in ({"source": "user-initiated"}, {"story": "user_call"}):
        _refused(400, "live_context_invalid", _live_ring, _context(**change))


def test_a_user_call_ignores_the_agent_ring_interval_but_not_a_ringing_ring(opted):
    grant = _call(request_id="r-1")["live"]
    live.close(PHONE, {"call_id": grant["call_id"], "attempt_id": grant["attempt_id"],
                       "owner_nonce": grant["owner_nonce"], "request_id": "close-1",
                       "call_epoch": grant["call_epoch"], "reason": "user_hangup"})
    second = _call(request_id="r-2")
    assert second["ring"]["state"] == "answered"                        # no "wait before calling again"
    grant = second["live"]
    live.close(PHONE, {"call_id": grant["call_id"], "attempt_id": grant["attempt_id"],
                       "owner_nonce": grant["owner_nonce"], "request_id": "close-2",
                       "call_epoch": grant["call_epoch"], "reason": "user_hangup"})
    _live_ring()                                                        # an agent ring is ringing now
    _refused(409, "the user is already being called", _call, request_id="r-3")


def test_a_replay_with_the_same_request_but_another_nonce_is_a_conflict(opted):
    _call(request_id="r-x", nonce="n-x")
    _refused(400, "live_request_invalid", _call, request_id="r-x", nonce="n-other")
    assert len([r for r in rings._load() if r.get("conversation_mode") == "agent_live_v1"]) == 1


def test_a_user_call_sends_no_alert_or_voip_and_starts_no_capture(opted, monkeypatch):
    sent = []
    monkeypatch.setattr(rings, "_push_voip", lambda ring: sent.append(("voip", ring["ring_id"])) or {})
    monkeypatch.setattr(rings, "_push", lambda ring, kind: sent.append((kind, ring["ring_id"])) or {})
    grant = _call()["live"]
    live.close(PHONE, {"call_id": grant["call_id"], "attempt_id": grant["attempt_id"],
                       "owner_nonce": grant["owner_nonce"], "request_id": "close-1",
                       "call_epoch": grant["call_epoch"], "reason": "user_hangup"})
    assert sent == [] and calls.snapshot(PHONE)["call"] is None


def test_a_user_call_closes_on_the_existing_owner_control(opted):
    live.reset_for_tests()
    out = _call()
    grant = out["live"]
    body = {"call_id": grant["call_id"], "attempt_id": grant["attempt_id"], "owner_nonce": grant["owner_nonce"],
            "request_id": "close-1", "call_epoch": grant["call_epoch"], "reason": "user_hangup"}
    first = live.close(PHONE, body)
    again = live.close(PHONE, body)
    assert first["ok"] is True and again["ok"] is True
    assert live.retired(grant["call_id"])
    _refused(409, "live_owner_conflict", live.close, "device_phone2", dict(body, request_id="close-2"))


def test_a_new_request_while_this_phone_has_a_live_call_is_refused(opted):
    live.reset_for_tests()
    first = _call(request_id="r-a", nonce="n-a")
    _refused(409, "live_call_active", _call, request_id="r-b", nonce="n-b")
    assert _call(request_id="r-a", nonce="n-a")["live"] == first["live"]      # the replay still works
    grant = first["live"]
    live.close(PHONE, {"call_id": grant["call_id"], "attempt_id": grant["attempt_id"],
                       "owner_nonce": grant["owner_nonce"], "request_id": "close-a",
                       "call_epoch": grant["call_epoch"], "reason": "user_hangup"})
    assert _call(request_id="r-c", nonce="n-c")["changed"] is True              # after the hangup a new call works


def test_an_incoming_live_answer_carries_no_origin(opted):
    ring = _live_ring()["ring"]
    out = rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="in-1",
                       client_nonce="n-in-1")
    assert "origin" not in out and "origin" not in out["live"]


def _join_of(grant, **changes):
    return {"type": "agent_live.join", **{k: grant[k] for k in sock.JOIN_KEYS}, **changes}


def test_lost_response_replay_never_extends_persisted_deadline_and_new_intent_recovers(opted):
    grant = _call()["live"]
    assert grant["expires_at"] == opted.clock.now + 60000
    opted.clock.advance(59)
    assert _call()["live"] == grant
    opted.clock.advance(1)
    _refused(409, "live_join_expired", _call)
    with pytest.raises(sock.Refused) as error:
        sock._join(PHONE, _join_of(grant))
    assert error.value.code == "live_join_expired"
    saved = rings._load()[0]
    assert saved["_live_phase"] == "retired" and saved["_live_join_expires_at"] == grant["expires_at"]
    assert _call(request_id="new-intent")["changed"] is True


def test_join_before_deadline_survives_unjoined_expiry_sweeper(opted):
    grant = _call()["live"]
    opted.clock.advance(59.999)
    joined = sock._join(PHONE, _join_of(grant))
    assert joined["_live_join_consumed"] is True
    opted.clock.advance(300)
    rings.expire_due()
    assert _call()["live"]["join_consumed"] is True
    assert rings._load()[0]["_live_phase"] == "connecting"


def test_join_at_exact_deadline_is_refused_even_without_sweeper(opted):
    grant = _call()["live"]
    opted.clock.advance(60)
    with pytest.raises(sock.Refused) as error:
        sock._join(PHONE, _join_of(grant))
    assert error.value.code == "live_join_expired"
    assert not rings._load()[0].get("_live_join_consumed")


def test_reusing_request_for_another_agent_refuses_before_second_call(opted):
    _call()
    _refused(400, "live_request_invalid", _call, agent="a-theo")
    assert len(rings._load()) == 1


@pytest.mark.parametrize("damage", ["overflow", "malformed"])
def test_incomplete_registry_is_refused_without_silent_normal(opted, damage):
    if damage == "overflow":
        for i in range(64):
            folder = opted.folder.parent / f"other-{i}"
            _write(folder / "profile.json", json.dumps({"profile_id": folder.name, "agent_session": f"other-{i}"}))
    else:
        _write(opted.folder.parent / "broken" / "profile.json", "{")
    _refused(409, "live_context_unavailable", _call)
    from lib.snapshot import build_agent_snapshot
    rows = build_agent_snapshot(None)["agents"]
    assert next(a for a in rows if a["agent_id"] == "a-sander")["call_mode"] == "unavailable"
    assert not rings._load()


def test_known_live_discovery_stays_live_when_pins_drift(opted):
    (opted.folder / USER_FILES["user_call_brief"][0]).write_text("drift")
    from lib.snapshot import build_agent_snapshot
    rows = build_agent_snapshot(None)["agents"]
    assert next(a for a in rows if a["agent_id"] == "a-sander")["call_mode"] == "agent_live_v1"
    _refused(409, "live_context_unavailable", _call)


def test_user_call_has_no_classic_fallback_and_is_visible_only_to_owner(opted):
    out = _call()
    assert rings.event_visible(out["ring"], PHONE)
    assert not rings.event_visible(out["ring"], "device_phone2")
    grant = out["live"]
    _refused(409, "live_fallback_not_allowed", live.fallback, PHONE,
             {"call_id": grant["call_id"], "attempt_id": grant["attempt_id"],
              "owner_nonce": grant["owner_nonce"], "request_id": "fallback",
              "expected_epoch": grant["call_epoch"], "audio_stopped": True})
    assert calls.snapshot(PHONE)["call"] is None


def test_provider_opens_only_after_valid_user_socket_join(opted, monkeypatch):
    from tests.unit.test_agent_ring_live_socket import Handler, Upstream, _serve, _wait
    from lib import config, oracle_realtime, oracle_live_stable
    from types import SimpleNamespace
    live.reset_for_tests()
    with oracle_realtime._ACTIVE_LOCK:
        oracle_realtime._ACTIVE_PRINCIPALS.clear()
    with oracle_live_stable._CLOSING_CONDITION:
        oracle_live_stable._CLOSING.clear()
        oracle_live_stable._STOP_HOOKS.clear()
    connects = []
    upstream = Upstream()
    monkeypatch.setattr(sock, "_connect", lambda key: connects.append(key) or upstream)
    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(openai_key=lambda: "fake-key"))
    grant = _call()["live"]
    assert connects == []
    invalid = Handler()
    invalid.send(_join_of(grant, owner_nonce="wrong"))
    thread = _serve(invalid)
    thread.join(5)
    assert not thread.is_alive() and connects == []
    handler = Handler()
    handler.send(_join_of(grant))
    thread = _serve(handler)
    try:
        _wait(lambda: len(connects) == 1)
        handler.send({"type": "session.close"})
    finally:
        handler.rfile.close()
        thread.join(5)
        live.reset_for_tests()
    assert not thread.is_alive() and len(connects) == 1
