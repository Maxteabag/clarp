"""Agent-live rings: the owner's HTTP controls of a Live call (WIRE-v2 SCHEMA).

``POST /calls/ring/live/close`` retires the call's authority first and only
then stops its session; it is idempotent. ``POST /calls/ring/live/fallback``
is the one explicit switch to the classic call, allowed only before the Live
opening was admitted and before any user audio went upstream; it bumps the
epoch and starts the ordinary group call with its own opening, once.
"""
from __future__ import annotations

import pytest

from lib import agent_ring_live as live
from lib import agent_rings as rings
from lib import group_calls as calls
from tests.unit.test_agent_ring_live import OTHER_PHONE, PHONE, _live_ring, sander  # noqa: F401 - fixture
from tests.unit.test_agent_rings import _Handler, _server, env  # noqa: F401 - env is a fixture


@pytest.fixture
def grant(sander):
    live.reset_for_tests()
    ring = _live_ring()["ring"]
    return rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1", client_nonce="c1")["live"]


def _body(grant, **kw):
    body = {"call_id": grant["call_id"], "attempt_id": grant["attempt_id"], "owner_nonce": grant["owner_nonce"],
            "request_id": "q1"}
    body.update(kw)
    return body


def _close(grant, who=PHONE, **kw):
    return live.close(who, _body(grant, **{"call_epoch": grant["call_epoch"], "reason": "user_hangup", **kw}))


def _fallback(grant, who=PHONE, **kw):
    return live.fallback(who, _body(grant, **{"expected_epoch": grant["call_epoch"], "audio_stopped": True, **kw}))


def _ring_of(grant):
    return next(r for r in rings._load() if r.get("_live_call_id") == grant["call_id"])


def _refused(status, code, fn, *args, **kw):
    with pytest.raises(calls.CallError) as exc:
        fn(*args, **kw)
    assert (exc.value.status, exc.value.body()) == (status, {"ok": False, "error": code})


class Session:
    def __init__(self, grant, order):
        self.order, self.call_id = order, grant["call_id"]
        live._SESSIONS[grant["call_id"]] = self

    def retire(self, reason):
        # Authority is already gone on the record when the session hears about it. Like the real
        # socket, a stopped session leaves the registry (serve() unregisters it on its way out).
        self.order.append(("retire", reason, _ring_of({"call_id": self.call_id})["_live_phase"]))
        live._SESSIONS.pop(self.call_id, None)


# ---- close --------------------------------------------------------------------------

def test_close_retires_authority_first_then_the_session(grant):
    order = []
    Session(grant, order)
    out = _close(grant)
    assert out == {"ok": True, "call_id": grant["call_id"], "call_epoch": 1, "state": "retired"}
    assert order == [("retire", "user_hangup", "retired")]
    assert _ring_of(grant)["_live_retired_reason"] == "user_hangup"
    assert grant["owner_nonce"] not in str(out)


def test_close_is_idempotent_and_never_touches_the_session_twice(grant):
    order = []
    Session(grant, order)
    first = _close(grant)
    assert _close(grant) == first                                   # the original request replays
    _refused(409, "live_call_stale", _close, grant, request_id="q2", reason="transport_lost")
    assert len(order) == 1


@pytest.mark.parametrize("change", [{"owner_nonce": "f" * 64}, {"attempt_id": "att_other"}, {"call_id": "alv_nope"}])
def test_close_by_a_wrong_grant_is_an_owner_conflict(grant, change):
    _refused(409, "live_owner_conflict", _close, grant, **change)
    assert _ring_of(grant)["_live_phase"] == "answered_waiting_join"


def test_close_by_another_phone_is_an_owner_conflict(grant):
    _refused(409, "live_owner_conflict", _close, grant, who=OTHER_PHONE)


def test_close_with_a_stale_epoch_is_stale(grant):
    _refused(409, "live_call_stale", _close, grant, call_epoch=2)


@pytest.mark.parametrize("change", [{"reason": "bored"}, {"call_epoch": "1"}, {"call_epoch": 0}, {"request_id": 5}])
def test_close_with_a_bad_body_is_invalid(grant, change):
    body = _body(grant, call_epoch=1, reason="user_hangup")
    body.update(change)
    _refused(400, "live_request_invalid", live.close, PHONE, body)


# ---- fallback -----------------------------------------------------------------------

def test_fallback_before_the_opening_starts_the_classic_call_once(grant, sander):
    order = []
    Session(grant, order)
    out = _fallback(grant)
    assert out["conversation_mode"] == "classic" and out["changed"] is True
    assert out["call"]["call_id"] and not out["call"]["call_id"].startswith("alv_")
    assert out["opening"]["text"] == _ring_of(grant)["opening_line"] and out["opening"]["audio_url"]
    assert out["ring"]["ring_id"] == grant["ring_id"]
    stored = _ring_of(grant)
    assert stored["_live_phase"] == "classic" and stored["_live_call_epoch"] == 2
    assert order == [("retire", "fallback", "falling_back")]     # stopped before anything classic exists
    again = _fallback(grant)                                      # the same request: the same call, no second
    assert again["changed"] is False and again["call"]["call_id"] == out["call"]["call_id"]
    _refused(409, "live_fallback_not_allowed", _fallback, grant, request_id="q2")


def test_fallback_after_a_close_without_opening_is_allowed(grant):
    _close(grant, reason="audio_not_ready")
    assert _fallback(grant)["conversation_mode"] == "classic"


@pytest.mark.parametrize("boundary", ["_live_opening_consumed", "_live_audio_forwarded"])
def test_fallback_after_the_opening_or_user_audio_is_refused(grant, boundary):
    with rings._lock:
        records = rings._load()
        next(r for r in records if r.get("_live_call_id") == grant["call_id"])[boundary] = True
        rings._save(records)
    _refused(409, "live_fallback_not_allowed", _fallback, grant)
    assert _ring_of(grant)["_live_phase"] == "answered_waiting_join" and not _ring_of(grant)["call_id"]


def test_fallback_needs_the_phones_audio_stopped_word_and_a_current_epoch(grant):
    _refused(400, "live_request_invalid", _fallback, grant, audio_stopped=False)
    _refused(400, "live_request_invalid", _fallback, grant, audio_stopped="true")
    _refused(409, "live_call_stale", _fallback, grant, expected_epoch=2)
    _refused(409, "live_owner_conflict", _fallback, grant, who=OTHER_PHONE)


# ---- the HTTP routes ------------------------------------------------------------------

def test_the_live_control_routes_answer_with_the_fixed_strings(grant, sander):
    server = _server()
    bad = _Handler(_body(grant, call_epoch=1, reason="user_hangup", owner_nonce="0" * 64), sander)
    server.Handler._handle_calls_ring_live_close(bad)
    assert bad.sent == [(409, {"ok": False, "error": "live_owner_conflict"})]
    limited = _Handler(_body(grant, call_epoch=1, reason="user_hangup"), sander, scope="limited")
    server.Handler._handle_calls_ring_live_close(limited)
    assert limited.sent[0][0] == 401
    fb = _Handler(_body(grant, expected_epoch=1, audio_stopped=True), sander)
    server.Handler._handle_calls_ring_live_fallback(fb)
    assert fb.sent[0][0] == 200 and fb.sent[0][1]["conversation_mode"] == "classic"
    close = _Handler(_body(grant, call_epoch=1, reason="user_hangup"), sander)
    server.Handler._handle_calls_ring_live_close(close)                 # Live already ended by the fallback
    assert close.sent == [(409, {"ok": False, "error": "live_call_stale"})]


def test_the_ring_and_answer_routes_carry_the_live_fields(sander):
    from tests.unit.test_agent_ring_live import OPENING, _context
    server = _server()
    made = _Handler({"reason": "r", "opening_line": OPENING, "conversation_mode": "agent_live_v1",
                     "event_context": _context()}, sander, turn_agent="a-sander")
    server.Handler._handle_calls_ring(made)
    assert made.sent[0][0] == 200 and made.sent[0][1]["ring"]["conversation_mode"] == "agent_live_v1"
    rid = made.sent[0][1]["ring"]["ring_id"]
    old = _Handler({"ring_id": rid}, sander)
    server.Handler._handle_calls_ring_answer(old)
    assert old.sent == [(409, {"ok": False, "error": "live_client_required"})]
    answer = _Handler({"ring_id": rid, "conversation_mode": "agent_live_v1", "request_id": "r1",
                       "client_nonce": "c1"}, sander)
    server.Handler._handle_calls_ring_answer(answer)
    assert answer.sent[0][0] == 200 and answer.sent[0][1]["live"]["call_id"].startswith("alv_")
    reg = _Handler({"token": "ef" * 32, "supported_conversation_modes": ["agent_live_v1"]}, sander,
                   principal=OTHER_PHONE)
    server.Handler._handle_calls_ring_voip_token(reg)
    assert reg.sent[0][0] == 200
    assert {r["principal"]: r for r in rings.voip_tokens()}[OTHER_PHONE]["supported_conversation_modes"] == \
        ["agent_live_v1"]


# ---- the producer CLI (CONTEXT-v1) -----------------------------------------------------

def test_the_cli_sends_a_live_ring_with_the_context_it_read_itself(monkeypatch, tmp_path, capsys):
    import json as _json

    from tests.unit.test_agent_ring_live import _context
    from tests.unit.test_agent_rings import _admin
    admin = _admin()
    sent = []
    monkeypatch.setattr(admin, "api_request", lambda method, path, body=None, **kw: sent.append((method, path, body))
                        or {"ok": True, "ring": {"ring_id": "rng_1", "state": "ringing"}})
    run = lambda argv: admin.parser().parse_args(argv).func(admin.parser().parse_args(argv))  # noqa: E731
    path = tmp_path / "event.json"
    path.write_text(_json.dumps(_context()), encoding="utf-8")
    assert run(["call", "ring", "--agent", "rk-sander", "--oracle", "--context-file", str(path),
                "--reason", "Vann ved vanntanken", "--say", "Hei."]) == 0
    assert sent == [("POST", "/calls/ring", {"agent": "rk-sander", "reason": "Vann ved vanntanken",
                                             "opening_line": "Hei.", "conversation_mode": "agent_live_v1",
                                             "event_context": _context()})]
    sent.clear()
    big = tmp_path / "big.json"
    big.write_text(_json.dumps({"x": "y" * 40000}), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    for argv in (["--oracle"], ["--context-file", str(path)], ["--oracle", "--context-file", str(big)],
                 ["--oracle", "--context-file", str(bad)], ["--oracle", "--context-file", str(tmp_path / "none")]):
        assert run(["call", "ring", "--reason", "r", "--say", "s", *argv]) == 2
    assert sent == []
    capsys.readouterr()


@pytest.mark.parametrize("end", ["close", "fallback"])
def test_a_retired_or_fallen_back_live_call_drops_its_snapshot(grant, end):
    assert "_live_snapshot" in _ring_of(grant)
    (_close if end == "close" else _fallback)(grant)
    assert "_live_snapshot" not in _ring_of(grant) and _ring_of(grant)["_live_context_digest"]


# ---- H3/H4: close always stops the session, and binds the exact epoch -----------------------------

def test_close_stops_the_session_even_when_the_audit_write_fails(grant, monkeypatch):
    order = []
    Session(grant, order)
    monkeypatch.setattr(rings, "_audit", lambda entry: (_ for _ in ()).throw(OSError("audit disk full")))
    out = _close(grant)
    assert out["state"] == "retired" and order == [("retire", "user_hangup", "retired")]
    assert _ring_of(grant)["_live_phase"] == "retired"


def test_close_stops_the_session_and_reports_the_error_when_the_record_cannot_be_saved(grant, monkeypatch):
    order = []
    Session(grant, order)
    monkeypatch.setattr(rings, "_save", lambda records: (_ for _ in ()).throw(OSError("db locked")))
    with pytest.raises(OSError, match="db locked"):
        _close(grant)
    assert [entry[:2] for entry in order] == [("retire", "user_hangup")]


def test_a_retired_close_answers_its_own_receipt_and_never_echoes_another_epoch(grant):
    first = _close(grant)
    assert first["call_epoch"] == 1
    assert _close(grant) == first
    _refused(409, "live_call_stale", _close, grant, request_id="q2")   # only the original request replays
    _refused(400, "live_request_invalid", _close, grant, call_epoch=2)  # the same request id, another body
    _refused(409, "live_call_stale", _close, grant, request_id="q3", call_epoch=7)


def test_close_after_the_fallback_is_stale(grant):
    _fallback(grant)
    _refused(409, "live_call_stale", _close, grant)
    _refused(409, "live_call_stale", _close, grant, call_epoch=2)


def test_every_live_http_route_refuses_in_the_ring_routes_ok_false_envelope(grant, sander):
    """Frozen per route: {"ok": false, "error": "<fixed code>"} (the existing _calls_ring_run wrapper)."""
    from tests.unit.test_agent_ring_live import OPENING
    server = _server()
    cases = [
        ("_handle_calls_ring", {"reason": "r", "opening_line": OPENING, "conversation_mode": "agent_live_v2"},
         400, "live_mode_invalid", "a-sander"),
        ("_handle_calls_ring_answer", {"ring_id": grant["ring_id"]}, 409, "live_client_required", ""),
        ("_handle_calls_ring_voip_token", {"token": "ab" * 32, "supported_conversation_modes": ["x"]},
         400, "live_mode_invalid", ""),
        ("_handle_calls_ring_live_close", _body(grant, call_epoch=9, reason="user_hangup"), 409, "live_call_stale", ""),
        ("_handle_calls_ring_live_fallback", _body(grant, expected_epoch=1, audio_stopped=False),
         400, "live_request_invalid", ""),
    ]
    for route, body, status, code, turn_agent in cases:
        handler = _Handler(body, sander, turn_agent=turn_agent)
        getattr(server.Handler, route)(handler)
        assert handler.sent == [(status, {"ok": False, "error": code})], route


# ---- drain-review re-review findings (H-2a, M-1) ------------------------------------------------

def test_a_retried_close_still_stops_a_session_that_outlived_the_first_close(grant):
    _close(grant)                                         # retired while no session was registered yet
    order = []
    Session(grant, order)                                 # … the socket registers late (join window)
    assert _close(grant)["state"] == "retired"
    assert [entry[:2] for entry in order] == [("retire", "user_hangup")]


def test_a_repeat_live_answer_after_fallback_returns_the_classic_state(grant):
    out = _fallback(grant)
    again = rings.answer(PHONE, grant["ring_id"], conversation_mode="agent_live_v1", request_id="r1", client_nonce="c1")
    assert again["conversation_mode"] == "classic" and again["changed"] is False
    assert again["call"]["call_id"] == out["call"]["call_id"] and "live" not in again


def test_a_repeat_live_answer_after_close_is_stale(grant):
    _close(grant)
    _refused(409, "live_call_stale", rings.answer, PHONE, grant["ring_id"], conversation_mode="agent_live_v1", request_id="r1", client_nonce="c1")


# ---- H-3: the ordered fallback transition (design H2-FALLBACK-TRANSITION-DESIGN.md) --------------

class StubbornSession(Session):
    """A session whose stop never completes (it stays registered)."""

    def retire(self, reason):
        self.order.append(("retire", reason, _ring_of({"call_id": self.call_id})["_live_phase"]))


def test_fallback_stops_live_and_revokes_its_epoch_before_the_classic_call_starts(grant, monkeypatch):
    order = []
    Session(grant, order)
    real_start = calls.start

    def start(*args, **kwargs):
        ring = _ring_of(grant)
        order.append(("start", ring["_live_phase"], ring["_live_call_epoch"], grant["call_id"] in live._SESSIONS))
        return real_start(*args, **kwargs)
    monkeypatch.setattr(calls, "start", start)
    out = _fallback(grant)
    assert order == [("retire", "fallback", "falling_back"), ("start", "falling_back", 2, False)]
    assert out["conversation_mode"] == "classic" and _ring_of(grant)["_live_phase"] == "classic"


def test_fallback_completes_and_stops_live_even_when_the_audit_write_fails(grant, monkeypatch):
    order = []
    Session(grant, order)
    monkeypatch.setattr(rings, "_audit", lambda entry: (_ for _ in ()).throw(OSError("audit disk full")))
    assert _fallback(grant)["conversation_mode"] == "classic"
    assert [entry[:2] for entry in order] == [("retire", "fallback")]


def test_a_failed_classic_start_leaves_live_revoked_and_the_same_request_resumes_once(grant, monkeypatch):
    real_start = calls.start
    monkeypatch.setattr(calls, "start", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tts router down")))
    _refused(503, "live_unavailable", _fallback, grant)
    ring = _ring_of(grant)
    assert ring["_live_phase"] == "falling_back" and ring["_live_call_epoch"] == 2 and not ring["call_id"]
    _refused(409, "live_call_stale", _close, grant)                              # Live authority is gone
    _refused(409, "live_fallback_not_allowed", _fallback, grant, request_id="other")
    monkeypatch.setattr(calls, "start", real_start)
    out = _fallback(grant)                                                        # same request_id resumes
    assert out["conversation_mode"] == "classic" and out["changed"] is True
    assert _fallback(grant)["changed"] is False and _fallback(grant)["call"]["call_id"] == out["call"]["call_id"]


def test_a_live_session_that_will_not_stop_blocks_the_classic_start(grant, monkeypatch):
    monkeypatch.setattr(live, "FALLBACK_STOP_WAIT_S", 0.2)
    order = []
    StubbornSession(grant, order)
    started = []
    monkeypatch.setattr(calls, "start", lambda *a, **k: started.append(1))
    _refused(503, "live_unavailable", _fallback, grant)
    assert started == [] and _ring_of(grant)["_live_phase"] == "falling_back"


# ---- ROOT dispositions: M1 request validation, M2 device-only controls, L1 receipt replay ----------

def test_a_close_while_the_fallback_is_pending_is_stale(grant, monkeypatch):
    monkeypatch.setattr(calls, "start", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    _refused(503, "live_unavailable", _fallback, grant)
    _refused(409, "live_call_stale", _close, grant, call_epoch=2)


@pytest.mark.parametrize("fn", ["close", "fallback"])
def test_live_controls_from_a_non_device_principal_are_refused_with_live_auth(grant, fn):
    _refused(403, "live_auth", _close if fn == "close" else _fallback, grant, who="administrator")


def test_a_retired_close_replays_only_for_its_original_request(grant):
    first = _close(grant, request_id="q1")
    assert _close(grant, request_id="q1") == first
    _refused(409, "live_call_stale", _close, grant, request_id="q2")


@pytest.mark.parametrize("change", [{"request_id": 7}, {"request_id": ""}, {"request_id": "x" * 129},
                                    {"client_nonce": None}, {"client_nonce": "y" * 129}])
def test_a_live_answer_validates_its_request_identity(sander, change):
    from tests.unit.test_agent_ring_live import _live_ring
    ring = _live_ring()["ring"]
    kw = {"request_id": "r1", "client_nonce": "c1", **change}
    _refused(400, "live_request_invalid", rings.answer, PHONE, ring["ring_id"], conversation_mode="agent_live_v1", **kw)
    assert rings.get(ring["ring_id"])["state"] == "ringing"


def test_a_repeat_answer_with_the_same_request_id_but_another_nonce_is_refused(sander):
    from tests.unit.test_agent_ring_live import _live_ring
    ring = _live_ring()["ring"]
    first = rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1", client_nonce="c1")
    _refused(400, "live_request_invalid", rings.answer, PHONE, ring["ring_id"], conversation_mode="agent_live_v1",
             request_id="r1", client_nonce="c2")
    again = rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1", client_nonce="c1")
    assert again["live"] == first["live"] and again["changed"] is False


def test_the_owner_cli_rings_live_for_a_named_agent_and_an_agent_turn_still_rings_only_as_itself(sander):
    """Producer auth (leak-watch): the owner's full-auth CLI, no agent turn, names --agent rk-sander;
    the profile binds to that resolved agent. A turn-proven agent cannot ring as another."""
    from tests.unit.test_agent_ring_live import OPENING, _context
    server = _server()
    owner = _Handler({"agent": "rk-sander-0001", "reason": "r", "opening_line": OPENING,
                      "conversation_mode": "agent_live_v1", "event_context": _context()},
                     sander, principal="administrator", turn_agent="")
    server.Handler._handle_calls_ring(owner)
    assert owner.sent[0][0] == 200 and owner.sent[0][1]["ring"]["agent_id"] == "a-sander"
    rings.decline(PHONE, owner.sent[0][1]["ring"]["ring_id"])
    sander.clock.advance(rings.AGENT_INTERVAL_MS / 1000)
    impostor = _Handler({"agent": "rk-sander-0001", "reason": "r", "opening_line": OPENING,
                         "conversation_mode": "agent_live_v1",
                         "event_context": _context(event={**_context()["event"], "id": "lw-x"})},
                        sander, turn_agent="a-theo")
    server.Handler._handle_calls_ring(impostor)
    assert impostor.sent == [(403, {"ok": False, "error": "an agent can only ring as itself"})]


def test_a_boolean_epoch_on_the_http_controls_is_invalid(grant):
    _refused(400, "live_request_invalid", _close, grant, call_epoch=True)
    _refused(400, "live_request_invalid", _fallback, grant, expected_epoch=True)


# ---- WIRE-v2-REVIEW-DISPOSITIONS fef6043f -------------------------------------------------------

def test_a_different_answer_request_against_an_answered_live_ring_is_stale(grant):
    _refused(409, "live_call_stale", rings.answer, PHONE, grant["ring_id"], conversation_mode="agent_live_v1",
             request_id="r2", client_nonce="c2")


def test_answer_identity_bounds_are_utf8_bytes(sander):
    from tests.unit.test_agent_ring_live import _live_ring
    ring = _live_ring()["ring"]
    _refused(400, "live_request_invalid", rings.answer, PHONE, ring["ring_id"], conversation_mode="agent_live_v1",
             request_id="ø" * 65, client_nonce="c1")                    # 65 chars = 130 UTF-8 bytes
    out = rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="ø" * 64,
                       client_nonce="c1")
    assert out["changed"] is True


def test_the_close_receipt_is_the_saved_one_and_a_reused_request_id_with_another_body_is_invalid(grant):
    first = _close(grant, request_id="q1", reason="user_hangup")
    assert first == {"ok": True, "call_id": grant["call_id"], "call_epoch": 1, "state": "retired"}
    assert _close(grant, request_id="q1", reason="user_hangup") == first
    _refused(400, "live_request_invalid", _close, grant, request_id="q1", reason="transport_lost")


def test_an_exact_pre_fallback_close_replays_its_receipt_after_the_fallback(grant):
    first = _close(grant, request_id="q1")
    _fallback(grant)
    assert _close(grant, request_id="q1") == first                    # its saved Live-retirement receipt only
    _refused(409, "live_call_stale", _close, grant, request_id="q9")
    _refused(409, "live_call_stale", _close, grant, request_id="q9", call_epoch=2)
