"""H1: ring-opening playback receipts on POST /clips/ack (docs/agent-rings.md "Opening receipts").

The phone acknowledges an answered ring's opening line as
``clip_id: "ring-opening-rng_<32 hex>"`` (or the explicit ``opening_id``).
Before H1 the Host int()-parsed every clip_id and answered 400, so no opening
receipt ever existed. These receipts are classes of what the phone reported,
never proof that anything was audible.
"""
from __future__ import annotations

import json
import threading

import pytest

from lib import agent_rings as rings
from lib import db, voice_events
from tests.unit.test_agent_rings import PHONE, _ring, _server, env  # noqa: F401


class _Ack:
    """Just enough of a request handler to run the real /clips/ack route."""

    def __init__(self, body, *, principal=PHONE, scope="full"):
        self.body, self.sent = body, []
        self._request_auth_validated, self._request_device_scope = True, scope
        self._request_principal = principal
        self._trace_id = None
        self.ctx = type("Ctx", (), {"default_session": None})()
        self.headers = {}

    def __getattr__(self, name):
        if name in {"_handle_clip_ack", "_record_voice", "_calls_principal",
                    "_handle_opening_ack", "_is_opening_ack"}:
            return getattr(_server().Handler, name).__get__(self)
        raise AttributeError(name)

    def _focused_session(self):
        return ""

    def _read_json(self):
        return self.body

    def _json(self, status, value):
        self.sent.append((status, value))

    def _json_ok(self, value):
        self.sent.append((200, value))

    def _json_error(self, status, message):
        self.sent.append((status, {"error": message}))


def _ack(body, **kw):
    handler = _Ack(body, **kw)
    _server().Handler._handle_clip_ack(handler)
    [(status, value)] = handler.sent
    return status, value


def _answered(env):
    ring = _ring()["ring"]
    rings.answer(PHONE, ring["ring_id"])
    return rings.get(ring["ring_id"])


def _rows(ring_id):
    return [dict(r) for r in db.conn().execute(
        "SELECT event, session, trace_id, detail FROM voice_events WHERE trace_id = ? ORDER BY event_id",
        (ring_id,)).fetchall()]


# ---- integer clips keep today's behaviour -----------------------------------------

def test_an_integer_clip_ack_is_unchanged(env):
    status, body = _ack({"clip_id": 424242, "status": "play-start", "url": "/audio/x.mp3"})
    assert status == 200 and body == {"ok": True, "updated": False}
    assert "opening" not in body


def test_an_arbitrary_string_clip_id_is_still_a_bad_clip_id(env):
    ring = _answered(env)
    for bad in ("hello", "ring-opening-", "ring-opening-rng_xyz", "ring-opening-" + ring["ring_id"].upper(),
                "ring-opening-" + ring["ring_id"] + "x", " ring-opening-" + ring["ring_id"]):
        status, body = _ack({"clip_id": bad, "status": "play-start"})
        assert (status, body) == (400, {"error": "bad clip_id"}), bad
    assert _rows(ring["ring_id"]) == []


def test_play_progress_is_still_a_bad_status_for_integer_clips(env):
    status, body = _ack({"clip_id": 1, "status": "play-progress"})
    assert (status, body) == (400, {"error": "bad status"})


# ---- the opening path ----------------------------------------------------------------

def test_an_answered_rings_opening_receipt_is_recorded(env):
    ring = _answered(env)
    oid = "ring-opening-" + ring["ring_id"]
    status, body = _ack({"clip_id": oid, "status": "play-start"})
    assert status == 200 and body["ok"] is True and body["updated"] is True
    assert body["opening"] == {"ring_id": ring["ring_id"], "call_id": ring["call_id"], "attempt": 1,
                               "class": "started", "historical": False}
    [row] = _rows(ring["ring_id"])
    assert row["event"] == "opening_started" and row["session"] == "theo-97e5"
    detail = json.loads(row["detail"])
    assert detail["opening_id"] == oid and detail["attempt"] == 1 and detail["status"] == "play-start"
    assert detail["historical"] is False and detail["call_id"] == ring["call_id"]


def test_the_explicit_opening_id_selects_the_opening_path_without_a_clip_id(env):
    ring = _answered(env)
    oid = "ring-opening-" + ring["ring_id"]
    status, body = _ack({"opening_id": oid, "status": "play-ok", "opening_attempt": 2})
    assert status == 200 and body["opening"]["class"] == "terminal_ok" and body["opening"]["attempt"] == 2
    # The same id in both fields is fine.
    status, body = _ack({"opening_id": oid, "clip_id": oid, "status": "queued", "opening_attempt": 2})
    assert status == 200 and body["opening"]["class"] == "attempted"


def test_conflicting_ids_are_refused_without_writing(env):
    ring = _answered(env)
    oid = "ring-opening-" + ring["ring_id"]
    for body in ({"opening_id": oid, "clip_id": 7, "status": "play-start"},
                 {"opening_id": oid, "clip_id": "7", "status": "play-start"},
                 {"opening_id": oid, "clip_id": "ring-opening-rng_" + "0" * 32, "status": "play-start"}):
        status, out = _ack(body)
        assert (status, out) == (400, {"error": "conflicting clip ids"}), body
    status, out = _ack({"opening_id": "not-an-opening", "status": "play-start"})
    assert (status, out) == (400, {"error": "bad opening_id"})
    assert _rows(ring["ring_id"]) == []


def test_every_status_maps_to_a_receipt_class(env):
    ring = _answered(env)
    oid = "ring-opening-" + ring["ring_id"]
    expected = {"queued": "attempted", "play-start": "started", "play-progress": "progress",
                "play-ok": "terminal_ok", "play-fail": "terminal_error", "held": "held"}
    for status_name, cls in expected.items():
        status, body = _ack({"clip_id": oid, "status": status_name, "error": "x" if status_name == "play-fail" else ""})
        assert status == 200 and body["opening"]["class"] == cls, status_name
    events = [r["event"] for r in _rows(ring["ring_id"])]
    assert events == ["opening_" + cls for cls in expected.values()]
    status, body = _ack({"clip_id": oid, "status": "broadcast"})
    assert (status, body) == (400, {"error": "bad status"})


def test_classes_are_accepted_in_any_order_and_coalesced(env):
    """The phone's ACK queue may coalesce: a terminal without a start is valid."""
    ring = _answered(env)
    oid = "ring-opening-" + ring["ring_id"]
    status, body = _ack({"clip_id": oid, "status": "play-ok"})
    assert status == 200 and body["opening"]["class"] == "terminal_ok"
    status, body = _ack({"clip_id": oid, "status": "play-start"})
    assert status == 200 and body["updated"] is True


def test_a_duplicate_receipt_is_idempotent(env):
    ring = _answered(env)
    oid = "ring-opening-" + ring["ring_id"]
    assert _ack({"clip_id": oid, "status": "play-start"})[1]["updated"] is True
    status, body = _ack({"clip_id": oid, "status": "play-start"})
    assert status == 200 and body["updated"] is False
    assert len(_rows(ring["ring_id"])) == 1
    # Another attempt of the same class is a new receipt.
    assert _ack({"clip_id": oid, "status": "play-start", "opening_attempt": 2})[1]["updated"] is True
    assert len(_rows(ring["ring_id"])) == 2


def test_concurrent_duplicates_write_one_receipt(env):
    ring = _answered(env)
    oid = "ring-opening-" + ring["ring_id"]
    results, barrier = [], threading.Barrier(4)

    def go():
        barrier.wait()
        results.append(_ack({"clip_id": oid, "status": "play-ok"}))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(b["updated"] for _, b in results) == [False, False, False, True]
    assert len(_rows(ring["ring_id"])) == 1


def test_bad_attempt_values_are_refused(env):
    ring = _answered(env)
    oid = "ring-opening-" + ring["ring_id"]
    for attempt in (0, -1, "2", True, 1.5):
        status, body = _ack({"clip_id": oid, "status": "play-start", "opening_attempt": attempt})
        assert (status, body) == (400, {"error": "bad opening_attempt"}), attempt
    assert _rows(ring["ring_id"]) == []


# ---- binding -------------------------------------------------------------------------

def test_another_devices_ring_is_refused(env):
    ring = _answered(env)
    status, body = _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "play-start"},
                        principal="device_other")
    assert (status, body) == (403, {"error": "not your ring"})
    assert _rows(ring["ring_id"]) == []


def test_an_unknown_ring_is_refused(env):
    status, body = _ack({"clip_id": "ring-opening-rng_" + "a" * 32, "status": "play-start"})
    assert (status, body) == (404, {"error": "unknown opening"})


def test_an_unanswered_ring_is_refused(env):
    ring = _ring()["ring"]
    status, body = _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "play-start"})
    assert (status, body) == (409, {"error": "ring not answered"})
    rings.decline(PHONE, ring["ring_id"])
    status, body = _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "play-start"})
    assert (status, body) == (409, {"error": "ring not answered"})
    assert _rows(ring["ring_id"]) == []


def test_a_limited_device_cannot_ack_an_opening(env):
    ring = _answered(env)
    status, _ = _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "play-start"}, scope="limited")
    assert status == 401
    assert _rows(ring["ring_id"]) == []


# ---- late receipts are history, never a call change ------------------------------------

def test_a_receipt_after_the_call_ended_is_historical_audit_only(env):
    from lib import group_calls as calls
    ring = _answered(env)
    calls.end(PHONE, call_id=ring["call_id"])
    before_ring = rings.get(ring["ring_id"])
    before_call = calls.get_call(ring["call_id"])
    pushes, events = list(env.pushes), list(env.stream.events)
    status, body = _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "play-ok"})
    assert status == 200 and body["opening"]["historical"] is True and body["updated"] is True
    after_ring, after_call = rings.get(ring["ring_id"]), calls.get_call(ring["call_id"])
    for doc in (before_ring, after_ring, before_call, after_call):
        doc.pop("server_now", None)
    assert after_ring == before_ring and after_call == before_call
    assert env.pushes == pushes and env.stream.events == events
    [row] = _rows(ring["ring_id"])
    assert json.loads(row["detail"])["historical"] is True


def test_the_ring_record_and_answer_are_untouched_by_receipts(env):
    ring = _answered(env)
    raw_before = db.conn().execute("SELECT value FROM settings WHERE key = ?", (rings.KEY,)).fetchone()[0]
    _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "play-start"})
    raw_after = db.conn().execute("SELECT value FROM settings WHERE key = ?", (rings.KEY,)).fetchone()[0]
    assert raw_after == raw_before


def test_the_feature_is_advertised_at_contract_68():
    from lib import server_identity
    assert "opening_receipts" in server_identity.FEATURES
    assert server_identity.FEATURE_CONTRACTS["opening_receipts"] == 68
    assert server_identity.HOST_CONTRACT == 68


def test_voice_event_names_fit():
    assert all(len("opening_" + c) <= 64 for c in rings.OPENING_CLASSES.values())
    assert voice_events  # imported for the row checks above


# ---- review F1: a client voice event must not suppress or stand in for a receipt -----

def test_a_client_voice_event_cannot_suppress_the_server_receipt(env):
    ring = _answered(env)
    # POST /voice-events (allowed even for limited devices) can store any event name.
    voice_events.record_batch(source="ios", events=[{"event": "opening_started", "trace_id": ring["ring_id"],
                                                     "detail": {"attempt": 1}}])
    status, body = _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "play-start"})
    assert status == 200 and body["updated"] is True
    sources = [r["source"] for r in db.conn().execute(
        "SELECT source FROM voice_events WHERE trace_id = ? AND event = 'opening_started' ORDER BY event_id",
        (ring["ring_id"],)).fetchall()]
    assert sources.count("server") == 1
    # The server receipt is then deduplicated against itself only.
    assert _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "play-start"})[1]["updated"] is False


def test_a_client_batch_claiming_server_source_cannot_forge_a_receipt(env):
    """Review F1 (delta): POST /voice-events takes `source` from the body, so a
    client batch must never be stored as source 'server'."""
    ring = _answered(env)
    voice_events.record_batch(source="server", events=[{"event": "opening_started", "trace_id": ring["ring_id"],
                                                        "detail": {"attempt": 1}}])
    stored = db.conn().execute(
        "SELECT source FROM voice_events WHERE trace_id = ? AND event = 'opening_started'",
        (ring["ring_id"],)).fetchall()
    assert [r["source"] for r in stored] == ["other"]
    status, body = _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "play-start"})
    assert status == 200 and body["updated"] is True


def test_a_refusal_is_answered_after_group_calls_is_reloaded(env, monkeypatch):
    """CI 38868445627: test_group_calls reloads lib.group_calls, which rebinds
    group_calls.CallError while agent_rings keeps the class it imported. The
    route must catch the class record_opening_receipt actually raises."""
    from lib import group_calls as calls
    monkeypatch.setattr(calls, "CallError", type("CallError", (Exception,), {}))
    ring = _answered(env)
    status, body = _ack({"clip_id": "ring-opening-" + ring["ring_id"], "status": "nope"})
    assert (status, body) == (400, {"error": "bad status"})
