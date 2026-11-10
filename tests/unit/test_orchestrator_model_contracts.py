"""Provider request/response contracts for the unsplit hands-free router."""
from io import BytesIO
import json
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from lib import orchestrator


@pytest.fixture
def openai_transport(monkeypatch):
    calls = []
    reply = {"content": '{"kind":"ignored","confidence":1}'}
    monkeypatch.setattr(orchestrator.config, "load",
                        lambda: SimpleNamespace(openai_key=lambda: "synthetic-test-key"))

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        if "error" in reply:
            raise reply["error"]
        payload = reply.get("payload", {"choices": [{"message": {"content": reply["content"]}}]})
        return BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls, reply


@pytest.mark.parametrize("content", [
    '{"kind":"ignored","confidence":1}',
    '```json\n{"kind":"ignored","confidence":1}\n```',
    'Decision follows: {"kind":"ignored","confidence":1} end.',
])
def test_openai_router_sends_json_policy_and_normalizes_object_reply(openai_transport, content):
    calls, reply = openai_transport
    reply["content"] = content
    packet = {"utterance": "synthetic noise", "agents": [], "pending": [],
              "fallback_request": True}
    result = orchestrator.call_model(
        packet, orchestrator.OrchestratorSettings(provider="openai", model="", effort="",
                                                  timeout_ms=1))
    assert result == {"kind": "ignored", "confidence": 1}
    request, timeout = calls[0]
    assert request.full_url == "https://api.openai.com/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer synthetic-test-key"
    body = json.loads(request.data)
    assert body["model"] == "gpt-5.4-nano"
    assert body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "user"
    prompt = body["messages"][0]["content"]
    assert "Only agents included in INPUT are eligible" in prompt
    assert json.loads(prompt.split("INPUT:\n", 1)[1]) == packet
    assert timeout == 0.25


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"message": {}}]}])
def test_openai_router_rejects_a_response_without_message_content(openai_transport, payload):
    _, reply = openai_transport
    reply["payload"] = payload
    with pytest.raises(RuntimeError, match="unexpected openai response"):
        orchestrator.call_model({}, orchestrator.OrchestratorSettings())


def test_openai_router_preserves_http_status_and_provider_error(openai_transport):
    _, reply = openai_transport
    reply["error"] = urllib.error.HTTPError(
        "https://api.openai.com/v1/chat/completions", 429, "Rate limited", {},
        BytesIO(b"synthetic provider overload"))
    with pytest.raises(RuntimeError, match="openai http 429: synthetic provider overload"):
        orchestrator.call_model({}, orchestrator.OrchestratorSettings())


@pytest.mark.parametrize("content, message", [
    ("", "empty model response"),
    ("[]", "orchestrator response must be a JSON object"),
    ("no decision here", "Expecting value"),
])
def test_openai_router_rejects_unusable_decision_text(openai_transport, content, message):
    _, reply = openai_transport
    reply["content"] = content
    with pytest.raises(ValueError, match=message):
        orchestrator.call_model({}, orchestrator.OrchestratorSettings())


def test_openai_router_without_key_never_reaches_transport(openai_transport, monkeypatch):
    calls, _ = openai_transport
    monkeypatch.setattr(orchestrator.config, "load",
                        lambda: SimpleNamespace(openai_key=lambda: ""))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY not configured"):
        orchestrator.call_model({}, orchestrator.OrchestratorSettings())
    assert calls == []


def test_malformed_decision_cannot_gain_a_confident_routing_target():
    result = orchestrator.parse_decision({
        "kind": "unknown", "confidence": "very sure", "mentioned_sessions": {"mike": True},
        "name_corrections": "wrong shape", "candidate_scores": {"mike": 1},
        "control_payload": ["stop"],
    })
    assert result.kind == "error" and result.confidence == 0
    assert result.mentioned_sessions == result.name_corrections == result.candidate_scores == ()
    assert result.control_payload is None
