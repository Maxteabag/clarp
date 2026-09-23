"""Regression tests for provider-rejected Oracle context injection.

Recorded evidence, 2026-09-23: every `session.thinking.append` on the
`live-webrtc` transport was answered with `context_injection_incomplete`
(18/18 and 148/148 in two sessions), while `clarp-live-v2` sessions with
comparable append counts saw none. `append()` only observes that the chunk
reached the socket, so the finding was still recorded as delivered, persisted
by `checkpoint()`, and never re-sent after a reconnect. The work was lost
rather than delayed. These tests pin the bookkeeping, not the transport.
"""
import json
import threading
from types import SimpleNamespace

from lib import oracle_live


ROW = {"delegation_id": "result-1", "session": "main-contact", "status": "completed",
       "request_text": "Check the signing chain.",
       "result_text": "The signature is missing on build-42.",
       "agent_id": "agent-1", "backend_session_id": "backend-1",
       "result_message_id": "message-1"}


def conversation(rows=()):
    sent = []
    instance = oracle_live.Conversation(
        SimpleNamespace(send=sent.append), lambda _: None,
        SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: rows),
        "fixture", lambda: 100.0)
    return instance, sent


def close(instance):
    instance.stop.set()
    instance.pool.shutdown()


def append_event_ids(sent):
    return [json.loads(raw)["event_id"] for raw in sent if "event_id" in json.loads(raw)]


def rejection(event_id):
    return {"type": "error", "error": {"type": "server_error",
                                       "code": "context_injection_incomplete",
                                       "message": "The session closed before the estimated "
                                                  "context injection completed.",
                                       "client_event_id": event_id}}


def test_rejected_injection_makes_the_finding_deliverable_again():
    c, sent = conversation([ROW])
    try:
        c.tick()
        assert "result-1" in c.results_sent, "precondition: the append was recorded as delivered"
        assert "result-1" in c.forwarded_findings.values()
        for event_id in append_event_ids(sent):
            c.receive(rejection(event_id))
        assert "result-1" not in c.results_sent
        assert "result-1" not in c.forwarded_findings.values()
    finally:
        close(c)


def test_every_chunk_of_one_finding_is_rejected_without_double_counting():
    long_row = dict(ROW, result_text="Finding sentence. " * 200)
    c, sent = conversation([long_row])
    try:
        c.tick()
        ids = append_event_ids(sent)
        assert len(ids) > 1, "the reproduction needs a multi-chunk finding"
        for event_id in ids:
            c.receive(rejection(event_id))
        assert c.injection_failures == len(ids)
        assert "result-1" not in c.results_sent
    finally:
        close(c)


def test_unrelated_error_codes_do_not_undo_delivery():
    c, sent = conversation([ROW])
    try:
        c.tick()
        for event_id in append_event_ids(sent):
            c.receive({"type": "error", "error": {"type": "server_error", "code": "rate_limited",
                                                  "client_event_id": event_id}})
        assert "result-1" in c.results_sent
        assert c.injection_failures == 0
    finally:
        close(c)


def test_rejection_for_an_unknown_event_is_counted_and_harmless():
    c, _ = conversation()
    try:
        c.receive(rejection("never-sent"))
        c.receive({"type": "error", "error": {"code": "context_injection_incomplete"}})
        c.receive({"type": "error"})
        assert c.injection_failures == 2
        assert c.results_sent == set()
    finally:
        close(c)


def test_rejected_finding_is_not_persisted_as_delivered():
    saved = {}
    c, sent = conversation([ROW])
    c.memory = SimpleNamespace(save=saved.update)
    try:
        c.tick()
        for event_id in append_event_ids(sent):
            c.receive(rejection(event_id))
        assert "result-1" not in saved.get("results_sent", []), (
            "a reconnected session must be able to re-deliver the finding")
    finally:
        close(c)


def test_rejection_arriving_mid_send_still_finds_its_owner():
    """The reader thread answers while `append` is still writing later chunks.

    Registering ownership after the loop left a window where the rejection
    resolved to nothing and the finding was recorded as delivered anyway.
    """
    long_row = dict(ROW, result_text="Finding sentence. " * 200)
    rejected_first = {}
    c = None

    def send_then_reject(raw):
        event = json.loads(raw)
        event_id = event.get("event_id")
        if event_id and not rejected_first:
            rejected_first[event_id] = True
            c.receive(rejection(event_id))

    c = oracle_live.Conversation(
        SimpleNamespace(send=send_then_reject), lambda _: None,
        SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: [long_row]),
        "fixture", lambda: 100.0)
    try:
        c.tick()
        assert rejected_first, "the reproduction needs the rejection to land during the send"
        assert "result-1" not in c.results_sent
        assert "result-1" not in c.forwarded_findings.values()
    finally:
        close(c)


def test_rejection_arriving_after_send_but_before_marking_is_reconciled():
    """Every chunk reaches the socket, then the rejection lands before the mark."""
    holder = {}

    def reject_all_at_end(raw):
        holder.setdefault("ids", []).append(json.loads(raw)["event_id"])

    c = oracle_live.Conversation(
        SimpleNamespace(send=reject_all_at_end), lambda _: None,
        SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: []),
        "fixture", lambda: 100.0)
    try:
        sent = c.append("commentary", "A short finding.", owner="result-1")
        for event_id in sent:
            c.receive(rejection(event_id))
        assert all(event_id in c.rejected_appends for event_id in sent)
        assert "result-1" not in c.results_sent
    finally:
        close(c)


def test_append_ledgers_stay_bounded():
    c, _ = conversation()
    try:
        for index in range(oracle_live.APPEND_LEDGER_LIMIT + 500):
            c.append("commentary", f"entry {index}", owner=f"delegation-{index}")
            c.receive(rejection(f"absent-{index}"))
        assert len(c.result_append_ids) <= oracle_live.APPEND_LEDGER_LIMIT
        assert len(c.rejected_appends) <= oracle_live.APPEND_LEDGER_LIMIT
    finally:
        close(c)


def test_failed_socket_write_does_not_leave_a_dangling_owner():
    c = oracle_live.Conversation(
        SimpleNamespace(send=lambda raw: (_ for _ in ()).throw(OSError("socket gone"))),
        lambda _: None,
        SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: []),
        "fixture", lambda: 100.0)
    try:
        c.close_sent = True          # send() refuses and returns False
        assert c.append("commentary", "Unsent finding.", owner="result-1") == []
        assert c.result_append_ids == {}
    finally:
        close(c)
