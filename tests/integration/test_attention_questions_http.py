"""Real HTTP dispatch and JSON contracts against a temporary DB and fake agent."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import urllib.error
import urllib.request

import pytest

from lib import agents, artifacts


_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("attention_http_server", _ROOT / "server/server.py")
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

TOKEN = "isolated-attention-test"


@pytest.fixture
def host(tmp_path, monkeypatch):
    agents.create_agent(persona="Theo", voice_id="V", cwd=str(tmp_path), session="theo")
    state = SimpleNamespace(deliveries=[], fail_delivery=False, events=[])

    class Dispatch:
        def __init__(self, ctx):
            pass

        def dispatch(self, **kwargs):
            state.deliveries.append(kwargs)
            if state.fail_delivery:
                raise RuntimeError("isolated delivery temporarily unavailable")

    monkeypatch.setattr(server_module, "TurnDispatchService", Dispatch)
    ctx = SimpleNamespace(auth_token=TOKEN, stream=SimpleNamespace(broadcast=state.events.append))
    # Use the actual HTTP handler/auth/dispatch with no production startup,
    # scanner, delivery thread, model, subprocess or external service.
    srv = server_module.ContextHTTPServer(("127.0.0.1", 0), server_module.Handler, ctx)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{srv.server_port}"
    state.ctx = ctx
    try:
        yield state
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _request(host, path, body=None, *, authenticated=True):
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Authorization"] = "Bearer " + TOKEN
    request = urllib.request.Request(host.base + path, headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def _question(host, **fields):
    body = {"session": "theo", "title": "Layout", "question": "Which layout should I build?",
            "response_type": "single_choice", "allow_custom_text": True,
            "options": [{"id": "keep", "label": "Keep the current layout"},
                        {"id": "simplify", "label": "Simplify it"}], **fields}
    status, result = _request(host, "/decisions", body)
    assert status == 201, result
    return result["artifact"]


def test_create_attention_and_legacy_binary_compatibility(host):
    status, out = _request(host, "/decisions", {
        "session": "theo", "title": "Approve", "question": "Publish the reviewed draft?"})
    assert status == 201
    approval = out["artifact"]
    question = _question(host, blocks_progress=True, priority_reason="The layout blocks implementation.",
                         urgency="time_sensitive", response_effort="quick", recommended_option_id="keep")
    assert question["type"] == "question"
    assert question["decision"]["blocks_progress"] is True
    assert question["decision"]["recommended_option_id"] == "keep"
    status, legacy = _request(host, "/attention")
    assert status == 200 and legacy["decision_format"] == 2
    assert [item["artifact_id"] for item in legacy["items"]] == [approval["artifact_id"]]
    status, modern = _request(host, "/attention?decision_format=2")
    assert status == 200
    assert modern["items"][0]["artifact_id"] == question["artifact_id"]
    assert modern["count"] == 2
    decision_id = question["decision"]["decision_id"]
    status, _ = _request(host, f"/decisions/{decision_id}/resolve", {"choice": "accepted", "expected_revision": 1})
    assert status == 409
    assert artifacts.get(question["artifact_id"])["decision"]["status"] == "pending"
    decision_id = approval["decision"]["decision_id"]
    status, result = _request(host, f"/decisions/{decision_id}/resolve", {"choice": "accepted", "expected_revision": 1})
    assert status == 200 and result["artifact"]["decision"]["status"] == "accepted"


@pytest.mark.parametrize("answer,expected", [
    ({"option_id": "simplify"}, {"option_id": "simplify", "label": "Simplify it"}),
    ({"text": "  Keep tabs; simplify the Rows.  "}, {"text": "Keep tabs; simplify the Rows."}),
])
def test_question_answer_retry_conflict_and_delivery(host, answer, expected):
    question = _question(host)
    decision_id = question["decision"]["decision_id"]
    body = {"expected_revision": 1, "answer": answer}
    status, result = _request(host, f"/decisions/{decision_id}/resolve", body)
    assert status == 200 and result["changed"] is True
    assert result["artifact"]["decision"]["status"] == "answered"
    assert result["artifact"]["decision"]["answer"] == expected
    assert result["delivery_pending"] is False
    status, result = _request(host, f"/decisions/{decision_id}/resolve", body)
    assert status == 200 and result["changed"] is False
    status, _ = _request(host, f"/decisions/{decision_id}/resolve",
                         {"expected_revision": 1, "answer": {"text": "A conflicting answer"}})
    assert status == 409 and len(host.deliveries) == 1
    sent = host.deliveries[0]
    assert sent["forced_session"] == "theo" and sent["origin"] == "automation"
    assert sent["queue_if_busy"] is True and sent["synthesize_audio"] is False
    assert sent["client_msg_id"] == "decision-" + decision_id
    assert next(iter(expected.values())) in sent["text"]


def test_failed_foreground_delivery_retries_with_identical_background_prompt(host):
    question = _question(host)
    decision_id = question["decision"]["decision_id"]
    host.fail_delivery = True
    status, result = _request(host, f"/decisions/{decision_id}/resolve",
                              {"expected_revision": 1, "answer": {"text": "My actual answer"}})
    assert status == 200 and result["delivery_pending"] is True
    host.fail_delivery = False
    server_module._deliver_decision_rows(host.ctx)
    assert len(host.deliveries) == 2
    assert host.deliveries[0]["text"] == host.deliveries[1]["text"]
    assert host.deliveries[0]["client_msg_id"] == host.deliveries[1]["client_msg_id"]
    assert artifacts.delivery_pending(decision_id) is False


def test_archive_restore_and_discard_decision_preserve_different_semantics(host):
    question = _question(host)
    artifact_id = question["artifact_id"]
    status, result = _request(host, f"/artifacts/{artifact_id}/archive",
                              {"archived": True, "expected_updated_at": question["updated_at"]})
    assert status == 200 and result["artifact"]["archived_at"] is not None
    archived = result["artifact"]
    assert archived["decision"]["status"] == "pending" and host.deliveries == []
    assert _request(host, "/attention?decision_format=2")[1]["count"] == 0
    assert _request(host, "/attention?decision_format=2&include_archived=1")[1]["count"] == 1
    status, result = _request(host, f"/artifacts/{artifact_id}/archive",
                              {"archived": False, "expected_updated_at": archived["updated_at"]})
    assert status == 200 and result["artifact"]["archived_at"] is None
    restored = result["artifact"]
    status, _ = _request(host, f"/artifacts/{artifact_id}/discard",
                         {"expected_updated_at": restored["updated_at"]})
    assert status == 409
    decision_id = question["decision"]["decision_id"]
    status, result = _request(host, f"/decisions/{decision_id}/dismiss", {"expected_revision": 1})
    assert status == 200 and result["artifact"]["decision"]["status"] == "cancelled"
    assert result["delivery_pending"] is False
    status, result = _request(host, f"/decisions/{decision_id}/dismiss", {"expected_revision": 1})
    assert status == 200 and result["changed"] is False and len(host.deliveries) == 1
    assert _request(host, "/attention?decision_format=2&include_archived=1")[1]["count"] == 0


def test_artifact_discard_hides_record_and_never_deletes_source_file(host, tmp_path):
    source = tmp_path / "research.md"
    source.write_text("The original report")
    status, out = _request(host, "/artifacts", {"session": "theo", "type": "document",
        "title": "Research", "payload": {"content": "The original report", "source": str(source)}})
    assert status == 201
    artifact = out["artifact"]
    path = "/artifacts/" + artifact["artifact_id"] + "/discard"
    body = {"expected_updated_at": artifact["updated_at"]}
    status, result = _request(host, path, body)
    assert status == 200 and result["changed"] is True
    status, result = _request(host, path, body)
    assert status == 200 and result["changed"] is False
    assert source.read_text() == "The original report"
    assert _request(host, "/artifacts?session=theo")[1]["artifacts"] == []


@pytest.mark.parametrize("body", [
    {"answer": {"option_id": "keep"}},
    {"answer": {"option_id": "keep"}, "expected_revision": True},
    {"answer": {"option_id": "keep"}, "choice": "accepted", "expected_revision": 1},
    {"expected_revision": 1},
])
def test_invalid_resolution_body_is_rejected_before_mutation(host, body):
    question = _question(host)
    status, _ = _request(host, f"/decisions/{question['decision']['decision_id']}/resolve", body)
    assert status == 400
    assert artifacts.get(question["artifact_id"])["decision"]["status"] == "pending"
    assert host.deliveries == []


def test_revision_and_timestamp_fences_auth_and_reserved_type(host):
    question = _question(host)
    artifact_id = question["artifact_id"]
    decision_id = question["decision"]["decision_id"]
    for path, body in [
        (f"/decisions/{decision_id}/dismiss", {"expected_revision": 1}),
        (f"/artifacts/{artifact_id}/archive", {"archived": True, "expected_updated_at": question["updated_at"]}),
    ]:
        assert _request(host, path, body, authenticated=False)[0] == 401
    assert _request(host, f"/decisions/{decision_id}/dismiss", {"expected_revision": 999})[0] == 409
    assert _request(host, f"/artifacts/{artifact_id}/archive",
                     {"archived": True, "expected_updated_at": 1})[0] == 409
    assert _request(host, f"/artifacts/{artifact_id}/archive",
                     {"archived": "true", "expected_updated_at": question["updated_at"]})[0] == 400
    assert _request(host, f"/artifacts/{artifact_id}/discard", {"expected_updated_at": True})[0] == 400
    assert _request(host, "/artifacts", {"session": "theo", "type": "question", "title": "Bypass"})[0] == 409
    assert artifacts.get(artifact_id)["decision"]["status"] == "pending"
    assert host.deliveries == []


@pytest.mark.parametrize("response_type,choice,queued", [
    (None, "accepted", False), (None, "rejected", False),
    ("approval", "accepted", False), ("approval", "rejected", False),
    ("approval", "dismissed", True), ("approval", "expired", True),
    ("single_choice", "answered", True), ("single_choice", "dismissed", True),
    ("single_choice", "expired", True),
])
def test_delivery_admission_policy_matches_foreground_and_background(
        host, monkeypatch, response_type, choice, queued):
    pending = {"decision_id": "decision-policy", "artifact_id": "artifact-policy",
               "session": "theo", "question": "Choose?", "context": "",
               "reference_id": "", "payload_json": "{}", "choice": choice,
               "answer": {"text": "Use the compact layout"}}
    if response_type is not None:
        pending["response_type"] = response_type
    monkeypatch.setattr(artifacts, "pending_deliveries", lambda: [pending])
    delivered = []
    monkeypatch.setattr(artifacts, "mark_delivered", delivered.append)
    handler = object.__new__(server_module.Handler)
    handler.server = SimpleNamespace(ctx=host.ctx)
    handler._deliver_pending_decisions()
    server_module._deliver_decision_rows(host.ctx)
    assert delivered == ["decision-policy", "decision-policy"]
    assert len(host.deliveries) == 2
    for sent in host.deliveries:
        assert sent["queue_if_busy"] is queued
        assert sent["client_msg_id"] == "decision-decision-policy"
        assert sent["origin"] == "automation"


@pytest.mark.parametrize("notification", ["question", "dismissal", "expiry"])
@pytest.mark.parametrize("delivery_path", ["foreground", "background"])
def test_notifications_preserve_active_nonsteerable_turn_and_queue_durably(
        host, tmp_path, monkeypatch, notification, delivery_path):
    from lib import turn_dispatch, turn_queue

    class NonsteerableBackend:
        CLAUDE = "claude"

        def __init__(self):
            self.spawned = []
            self.interrupted = []

        def normalize(self, backend):
            return backend or "claude"

        def active_handles(self, backend, agent_id):
            return ["active-handle"]

        def spawn_turn(self, backend, **kwargs):
            self.spawned.append(kwargs)

        def interrupt(self, backend, agent_id):
            self.interrupted.append(agent_id)

    agent_id = agents.get_by_session("theo")["agent_id"]
    agents.start_runtime(agent_id, "theo")
    host.ctx.default_session = "theo"
    host.ctx.agents_path = tmp_path / "unused.json"
    backend = NonsteerableBackend()
    service = turn_dispatch.TurnDispatchService(
        host.ctx, backend_registry=backend, home=tmp_path,
        uuid_factory=lambda: "isolated-session")
    monkeypatch.setattr(server_module, "TurnDispatchService", lambda ctx: service)
    service.dispatch(text="Continue independent work", requested_session="theo",
                     trace_id="existing-turn", synthesize_audio=False)
    assert len(backend.spawned) == 1
    assert turn_dispatch._INFLIGHT[agent_id] == "existing-turn"

    if notification == "question":
        artifact = _question(host)
    else:
        artifact = artifacts.create_decision(session="theo", title="Approval",
            question="Publish the reviewed draft?", expires_at=1 if notification == "expiry" else None)
    decision_id = artifact["decision"]["decision_id"]
    if delivery_path == "foreground":
        if notification == "question":
            status, _ = _request(host, f"/decisions/{decision_id}/resolve",
                {"expected_revision": 1, "answer": {"text": "Use compact spacing"}})
        elif notification == "dismissal":
            status, _ = _request(host, f"/decisions/{decision_id}/dismiss", {"expected_revision": 1})
        else:
            status, _ = _request(host, "/attention")
        assert status == 200
    else:
        if notification == "question":
            artifacts.resolve(decision_id, expected_revision=1, answer={"text": "Use compact spacing"})
        elif notification == "dismissal":
            artifacts.dismiss(decision_id, expected_revision=1)
        else:
            artifacts.attention()
        server_module._deliver_decision_rows(host.ctx)

    assert backend.interrupted == []
    assert len(backend.spawned) == 1
    assert turn_dispatch._INFLIGHT[agent_id] == "existing-turn"
    queued = turn_queue.get("decision-" + decision_id)
    assert queued is not None and queued["status"] == "queued"
    assert queued["origin"] == "automation" and queued["session"] == "theo"
    assert artifacts.delivery_pending(decision_id) is False

    # Completing the original turn admits the queued notice exactly once.
    backend.spawned[0]["on_result"]({"duration_ms": 5})
    assert len(backend.spawned) == 2 and backend.interrupted == []
    assert decision_id in backend.spawned[1]["text"]
    assert turn_queue.status("decision-" + decision_id) == "started"
    backend.spawned[1]["on_result"]({"duration_ms": 3})
    assert agent_id not in turn_dispatch._INFLIGHT
