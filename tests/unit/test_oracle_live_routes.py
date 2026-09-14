"""Test every provider-delegation exit without real worker effects or audio."""
import json
import threading
from types import SimpleNamespace

from lib import oracle_live, oracle_router


def controller(output=None, response=None):
    sent, downstream = [], []
    def execute(name, arguments, call_id):
        if name == "list_agents": return {"agents": []}
        return output
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), execute=execute, results=lambda: [])
    c = oracle_live.Conversation(SimpleNamespace(send=sent.append), downstream.append, tools, "fixture",
        clock=lambda: 100.0, route_request=response or (lambda *args, **kwargs: {"output": [
            {"type": "function_call", "name": "cancel_agent", "call_id": "tool-1", "arguments": "{}"}]}))
    return c, sent, downstream


def test_cancel_result_answers_its_original_voice_delegation():
    c, sent, _ = controller({"cancelled": True})
    try:
        c.routing = 1; c.route("provider-1")
        events = [json.loads(raw) for raw in sent]
        assert len(events) == 1
        assert events[0]["delegation_id"] == "provider-1"
        assert '"cancelled": true' in events[0]["content"]
        assert c.routing == 0
    finally:
        c.stop.set(); c.pool.shutdown()


def test_accepted_work_has_silent_receipt_and_retains_provider_correlation():
    c, sent, _ = controller({"status": "accepted", "operation_id": "worker-1"})
    try:
        c.routing = 1; c.route("provider-1")
        receipt = json.loads(sent[0])
        assert receipt["type"] == "session.thinking.append"
        assert receipt["delegation_id"] == "provider-1"
        assert c.provider_delegations == {"worker-1": "provider-1"}
    finally:
        c.stop.set(); c.pool.shutdown()


def test_router_failure_is_scoped_and_never_leaks_provider_details():
    def fail(*args, **kwargs): raise oracle_router.RouterError("codex_router_failed")
    c, sent, downstream = controller(response=fail)
    try:
        c.routing = 1; c.route("provider-failed")
        assert all(json.loads(raw)["delegation_id"] == "provider-failed" for raw in sent)
        assert "failed" in "".join(json.loads(raw)["content"] for raw in sent)
        assert downstream[-1]["type"] == "oracle_v2.notice"
        assert c.routing == 0
    finally:
        c.stop.set(); c.pool.shutdown()


def test_three_stale_proposals_admit_nothing_and_release_voice_wait():
    c, sent, _ = controller()
    calls = []
    def stale(*args, **kwargs):
        calls.append(True); c.revision += 1
        return {"output": []}
    c.route_request = stale
    try:
        c.routing = 1; c.route("provider-stale")
        assert len(calls) == 3
        assert "No new work" in "".join(json.loads(raw)["content"] for raw in sent)
        assert all(json.loads(raw)["delegation_id"] == "provider-stale" for raw in sent)
    finally:
        c.stop.set(); c.pool.shutdown()


def test_steered_receipts_for_one_native_result_are_not_spoken_twice():
    c, sent, down = controller()
    now = [100.0]; c.clock = lambda: now[0]
    base = {"session": "rowan", "agent_id": "rowan-id", "backend_session_id": "native-conversation",
            "status": "completed", "result_message_id": "one-native-answer", "result_text": "Cancellation is not checked.",
            "request_text": "Inspect retries"}
    c.tools.results = lambda: [{**base, "delegation_id": "original"}, {**base, "delegation_id": "correction"}]
    c.provider_delegations = {"original": "voice-1", "correction": "voice-2"}
    try:
        c.tick(); now[0] += 5; c.tick()
        events = [json.loads(row) for row in sent]
        findings = [row for row in events if row["type"] == "session.commentary.append"]
        assert len(findings) == 1, "One actual native result must not become two spoken announcements"
        assert c.results_sent == {"original", "correction"}
        assert {row["delegation_id"] for row in events} == {"voice-1", "voice-2"}
        aliases = [row for row in down if row.get("shared_finding_of")]
        assert len(aliases) == 1
    finally:
        c.stop.set(); c.pool.shutdown()


def test_equal_words_from_independent_workers_are_still_separate_findings():
    c, sent, _ = controller()
    now = [100.0]; c.clock = lambda: now[0]
    base = {"status": "completed", "result_text": "The checksum matches.", "request_text": "Verify the checksum"}
    c.tools.results = lambda: [{**base, "delegation_id": "mira-1", "session": "mira", "agent_id": "mira",
                                "backend_session_id": "native-mira", "result_message_id": "answer-mira"},
                               {**base, "delegation_id": "rowan-1", "session": "rowan", "agent_id": "rowan",
                                "backend_session_id": "native-rowan", "result_message_id": "answer-rowan"}]
    try:
        c.tick(); now[0] += 5; c.tick()
        assert sum(json.loads(row)["type"] == "session.commentary.append" for row in sent) == 2
    finally:
        c.stop.set(); c.pool.shutdown()
