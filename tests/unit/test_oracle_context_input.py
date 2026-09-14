import base64
import io
import json
import threading
from types import SimpleNamespace

import pytest

from lib import oracle_live, oracle_memory, oracle_router

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7i8AAAAASUVORK5CYII=")


def conversation(memory, now):
    sent, down = [], []
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: [])
    c = oracle_live.Conversation(SimpleNamespace(send=sent.append), down.append, tools, "fixture",
                                 clock=lambda: now[0], memory=memory, provider_session="provider-1")
    return c, sent, down


def test_long_spoken_request_survives_checkpoint_without_losing_early_constraints():
    store = oracle_memory.open_thread("owner", "main", connection_id="connection-1")
    c, _, _ = conversation(store, [100.0])
    text = "Do not deploy. " + "Inspect the evidence and preserve all constraints. " * 230 + "Use build-42, not build-24."
    try:
        for index in range(0, len(text), 200):
            c.receive({"type": "session.input_transcript.delta", "event_id": str(index),
                       "delta": text[index:index+200], "start_ms": index, "end_ms": index+200})
        assert c.fragments[0]["text"] == text
        resumed = oracle_memory.open_thread("owner", "main", connection_id="connection-2")
        assert resumed.load()["fragments"][0]["text"] == text
    finally:
        c.stop.set(); c.pool.shutdown()


def test_reference_text_is_quiet_exact_and_duplicate_submission_does_not_bump_revision():
    store = oracle_memory.open_thread("owner", "main", connection_id="connection-1")
    c, sent, down = conversation(store, [100.0])
    event = {"type": "oracle_v2.context.add", "context_id": "typed-1", "text": "path=reports/Årsoppgjør-42.csv; amount=550"}
    try:
        c.input(event); c.input(event)
        assert c.revision == 1
        assert store.contexts()[0]["text"] == event["text"]
        assert all(json.loads(value)["type"] == "session.thinking.append" for value in sent)
        assert down[-1]["context_id"] == "typed-1"
    finally:
        c.stop.set(); c.pool.shutdown()


def test_image_reference_preserves_capture_identity_and_retirement():
    store = oracle_memory.open_thread("owner", "main", connection_id="connection-1")
    c, _, down = conversation(store, [100.0])
    event = {"type": "oracle_v2.context.add", "context_id": "image-1", "text": "Previous staging screen",
             "mime_type": "image/png", "image_base64": base64.b64encode(PNG).decode(), "captured_at": 1234}
    try:
        c.input(oracle_live.client_event(json.dumps(event)))
        assert down[-1]["items"][0]["captured_at"] == 1234
        assert "image_base64" not in json.dumps(down[-1])
        assert store.contexts(include_images=True)[0]["image"] == PNG
        c.input({"type": "oracle_v2.context.remove", "context_id": "image-1"})
        assert store.contexts() == [] and c.revision == 2
    finally:
        c.stop.set(); c.pool.shutdown()


def test_api_router_receives_pixels_not_only_image_metadata(monkeypatch):
    requests = []
    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": "A white pixel."}]}]}).encode())
    monkeypatch.setattr(oracle_router, "urlopen", respond)
    body = {"model": oracle_router.MODEL, "instructions": "Inspect the supplied image.", "input": "Describe this image.", "tools": []}
    oracle_router.route(body, backend="api", api_key="fixture", stop=threading.Event(), images=[{"mime_type": "image/png", "data": PNG}])
    content = requests[0]["input"][0]["content"]
    assert content[1]["type"] == "input_image"
    assert base64.b64decode(content[1]["image_url"].split(",", 1)[1]) == PNG
    assert body["input"] == "Describe this image."


def test_worker_reference_is_frozen_even_after_the_active_context_changes(tmp_path):
    store = oracle_memory.open_thread("owner", "main", connection_id="connection-1")
    store.add_context("image-1", text="Old screenshot", image=PNG, mime_type="image/png", captured_at=1234)
    contexts = store.contexts(include_images=True)
    reference = store.materialize_reference("Explain the release blocker", contexts, tmp_path)
    files = list((tmp_path/"oracle-context"/store.thread_id).glob("*.json"))
    assert len(files) == 1 and str(files[0]) in reference
    before = files[0].read_bytes()
    store.remove_context("image-1")
    store.add_context("typed-2", text="New current screen is different")
    assert files[0].read_bytes() == before
    assert json.loads(before)["contexts"][0]["captured_at"] == 1234
