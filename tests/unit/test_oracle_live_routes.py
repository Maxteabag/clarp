"""Test every provider-delegation exit without real worker effects or audio."""
import json
import io
import threading
from types import SimpleNamespace

from lib import oracle_live, oracle_live_stable, oracle_router


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


def _stable_function_call(call_id, agent, request):
    return {"type": "function_call", "name": "delegate_to_agent", "call_id": call_id,
            "arguments": json.dumps({"agent": agent, "request": request})}


def test_stable_route_admits_all_parallel_actions_and_dedupes_exact_proposals(monkeypatch):
    calls, bodies = [], []
    output = {"output": [
        _stable_function_call("yuki-1", "yuki", "Investigate Jev replacing Luna Delegator."),
        _stable_function_call("mike-1", "mike", "Check Knut Thomas Lien's other emails."),
        _stable_function_call("yuki-duplicate", "yuki", "Investigate Jev replacing Luna Delegator."),
    ]}

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def provider(request, **_kwargs):
        bodies.append(json.loads(request.data))
        return Response(json.dumps(output).encode())

    def execute(name, arguments, call_id):
        calls.append((name, arguments, call_id))
        return {"agents": [{"name": "Mike", "session": "mike"},
                            {"name": "Yuki", "session": "yuki"}]} if name == "list_agents" else {
            "status": "accepted", "operation_id": "op-" + call_id}

    monkeypatch.setattr(oracle_live_stable, "urlopen", provider)
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), execute=execute,
                            fallback="", results=lambda: [])
    c = oracle_live_stable.Conversation(SimpleNamespace(send=lambda _: None), lambda _: None,
        tools, "fixture", clock=lambda: 100)
    c.fragments = [
        {"role": "user", "text": "Mike, check Knut Thomas Lien's other emails."},
        {"role": "user", "text": "Yuki, investigate Jev replacing Luna Delegator."},
    ]
    c.revision = 2
    try:
        c.routing = 1
        c.route("combined-request")
        assert [arguments["agent"] for name, arguments, _ in calls
                if name == "delegate_to_agent"] == ["yuki", "mike"]
        assert len(bodies) == 1
        assert bodies[0]["parallel_tool_calls"] is True
        assert bodies[0]["max_output_tokens"] == 2400
    finally:
        c.stop.set(); c.pool.shutdown()


def test_stable_route_preserves_current_boundary_without_semantic_coverage_claim(monkeypatch):
    calls, bodies, sent = [], [], []
    response = {"output": [_stable_function_call(
        "yuki-1", "yuki", "Investigate Jev replacing Luna Delegator.")]}

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def provider(request, **_kwargs):
        bodies.append(json.loads(request.data))
        return Response(json.dumps(response).encode())

    def execute(name, arguments, call_id):
        calls.append((name, arguments, call_id))
        return {"agents": [{"name": "Mike", "session": "mike"},
                            {"name": "Yuki", "session": "yuki"}]} if name == "list_agents" else {
            "status": "accepted", "operation_id": "op-" + call_id}

    monkeypatch.setattr(oracle_live_stable, "urlopen", provider)
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), execute=execute,
                            fallback="", results=lambda: [])
    c = oracle_live_stable.Conversation(SimpleNamespace(send=sent.append), lambda _: None,
        tools, "fixture", clock=lambda: 100)
    c.fragments = [
        {"role": "user", "text": "Earlier, ask Mira about the old board item.", "revision": 1},
        {"role": "user", "text": "Did Mike reply about the board?", "revision": 6},
        {"role": "user", "text": "Mike, check other emails from him.", "revision": 7},
        {"role": "user", "text": "Yuki, investigate Jev replacing Luna Delegator.", "revision": 8},
    ]
    c.revision = 8
    c.routed_revision = 6
    try:
        c.routing = 1
        c.route("combined-request")
        assert [arguments["agent"] for name, arguments, _ in calls
                if name == "delegate_to_agent"] == ["yuki"]
        assert len(bodies) == 1
        payload = json.loads(bodies[0]["input"])
        current = [row["text"] for row in payload["current_user_requests"]]
        assert current == ["Mike, check other emails from him.",
                           "Yuki, investigate Jev replacing Luna Delegator."]
        assert "Earlier, ask Mira" not in current
        assert "Did Mike reply" not in current
        assert not any("could not confirm every current independent request" in raw
                        for raw in sent)
        before = len(bodies)
        c.routing = 1
        c.route("paraphrased-duplicate-trigger")
        assert len(bodies) == before, "A routed revision cannot replay a paraphrased action"
    finally:
        c.stop.set(); c.pool.shutdown()


def test_stable_route_preserves_distinct_current_requests_for_same_agent(monkeypatch):
    calls, sent = [], []
    output = {"output": [
        _stable_function_call("mike-1", "mike", "Check other emails from Knut."),
        _stable_function_call("mike-2", "mike", "Check the pending board item."),
    ]}

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): return False

    monkeypatch.setattr(oracle_live_stable, "urlopen",
                        lambda request, **_: Response(json.dumps(output).encode()))

    def execute(name, arguments, call_id):
        calls.append((name, arguments, call_id))
        return {"agents": [{"name": "Mike", "session": "mike"}]} if name == "list_agents" else {
            "status": "accepted", "operation_id": "op-" + call_id}

    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), execute=execute,
                            fallback="", results=lambda: [])
    c = oracle_live_stable.Conversation(SimpleNamespace(send=sent.append), lambda _: None,
        tools, "fixture", clock=lambda: 100)
    c.fragments = [
        {"role": "user", "text": "Mike, check other emails from Knut.", "revision": 1},
        {"role": "user", "text": "Mike, check the pending board item.", "revision": 2},
    ]
    c.revision = 2
    try:
        c.routing = 1
        c.route("same-agent-independent")
        assert [arguments["request"] for name, arguments, _ in calls
                if name == "delegate_to_agent"] == [
                    "Check other emails from Knut.", "Check the pending board item."]
        assert not any("could not confirm every" in raw for raw in sent)
    finally:
        c.stop.set(); c.pool.shutdown()


def test_stable_route_does_not_escalate_a_mention_only_question(monkeypatch):
    sent, calls = [], []
    response = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "I do not see a new reply."}]}]}

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): return False

    monkeypatch.setattr(oracle_live_stable, "urlopen",
                        lambda request, **_: Response(json.dumps(response).encode()))

    def execute(name, arguments, call_id):
        calls.append(name)
        return {"agents": [{"name": "Mike", "session": "mike"}]}

    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), execute=execute,
                            fallback="", results=lambda: [])
    c = oracle_live_stable.Conversation(SimpleNamespace(send=sent.append), lambda _: None,
        tools, "fixture", clock=lambda: 100)
    c.fragments = [{"role": "user", "text": "Did Mike reply about the board?", "revision": 1}]
    c.revision = 1
    try:
        c.routing = 1
        c.route("mention-only")
        assert calls == ["list_agents"]
        assert not any("could not confirm every" in raw for raw in sent)
    finally:
        c.stop.set(); c.pool.shutdown()


def test_stable_route_splits_post_admission_continuation_before_1100ms(monkeypatch):
    calls, bodies = [], []
    responses = [
        {"output": [_stable_function_call("mike-1", "mike", "Check other emails from him.")]},
        {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "I will keep that clarification with the current task."}]}]},
    ]

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def provider(request, **_kwargs):
        bodies.append(json.loads(request.data))
        return Response(json.dumps(responses.pop(0)).encode())

    def execute(name, arguments, call_id):
        calls.append((name, arguments, call_id))
        return {"agents": [{"name": "Mike", "session": "mike"}]} if name == "list_agents" else {
            "status": "accepted", "operation_id": "op-" + call_id}

    monkeypatch.setattr(oracle_live_stable, "urlopen", provider)
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), execute=execute,
                            fallback="", results=lambda: [])
    c = oracle_live_stable.Conversation(SimpleNamespace(send=lambda _: None), lambda _: None,
        tools, "fixture", clock=lambda: 100)
    c.fragments = [{"role": "user", "text": "Mike, check other emails from him.",
                    "revision": 1, "end_ms": 100}]
    c.revision = 1
    try:
        c.routing = 1
        c.route("initial-request")
        c.receive({"type": "session.input_transcript.delta",
                   "delta": "Actually, keep the board item separate.",
                   "event_id": "continuation", "start_ms": 500, "end_ms": 600})
        c.last_transcript = 0
        assert len(c.fragments) == 2
        assert c.fragments[0]["text"] == "Mike, check other emails from him."
        assert c.fragments[1]["text"] == "Actually, keep the board item separate."
        c.routing = 1
        c.route("continuation-request")
        assert [arguments["request"] for name, arguments, _ in calls
                if name == "delegate_to_agent"] == ["Check other emails from him."]
        assert len(bodies) == 2
        current = json.loads(bodies[1]["input"])["current_user_requests"]
        assert [row["text"] for row in current] == [
            "Actually, keep the board item separate."]
    finally:
        c.stop.set(); c.pool.shutdown()
