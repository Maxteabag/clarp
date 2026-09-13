"""Regression tests driven by the long-request finding-loss reproduction."""
import json
from types import SimpleNamespace

import pytest

from lib import oracle_live


def conversation(rows=()):
    sent = []
    instance = oracle_live.Conversation(SimpleNamespace(send=sent.append), lambda _: None,
        SimpleNamespace(results=lambda: rows), "fixture", lambda: 100.0)
    return instance, sent


def close(instance):
    instance.stop.set()
    instance.pool.shutdown()


def test_valid_long_request_cannot_push_verified_finding_out_of_voice_context():
    row = {"delegation_id": "result-1", "session": "main-contact", "status": "completed",
           "request_text": "Respect this valid constraint and preserve the exact requested outcome. " * 40,
           "result_text": "The verified checksum is 7ac9. The remaining risk is an unsigned dependency."}
    c, sent = conversation([row])
    try:
        c.tick()
        contents = "".join(json.loads(raw)["content"] for raw in sent)
        assert row["result_text"] in contents
        assert "result-1" in contents
        assert "main-contact" in contents
    finally:
        close(c)


@pytest.mark.parametrize("text", [
    "First finding. " * 250 + "Do not deploy: the checksum is wrong.",
    "日本語の結果。" * 300 + "識別子: build-42",
    "Navnet er Bjørn. " * 150 + "Beløpet er 550, ikke 50.",
    "a" * 2400,
], ids=["long-caveat", "japanese", "norwegian", "unbroken-identifier"])
def test_context_chunking_preserves_entire_result_and_late_caveats(text):
    c, sent = conversation()
    try:
        c.append("commentary", text)
        chunks = [json.loads(raw)["content"] for raw in sent]
        assert "".join(chunks) == text
        assert all(len(chunk.encode("utf-8")) <= 480 for chunk in chunks)
        assert len({json.loads(raw)["event_id"] for raw in sent}) == len(sent)
    finally:
        close(c)


def test_task_scoped_append_preserves_original_provider_delegation_id():
    c, sent = conversation()
    try:
        c.append("thinking", "No booking has been made.", delegation_id="provider-delegation-1")
        assert json.loads(sent[0])["delegation_id"] == "provider-delegation-1"
    finally:
        close(c)


def test_work_panel_uses_only_owned_ids_and_emits_changes_without_repeating(monkeypatch):
    import threading
    down = []
    rows = {"owned": {"delegation_id": "owned", "session": "mira", "status": "accepted",
                       "request_text": "Inspect build-42", "created_at": 1},
            "unowned": {"delegation_id": "unowned", "session": "private", "status": "completed"}}
    monkeypatch.setattr(oracle_live.oracle_delegations, "get", rows.get)
    tools = SimpleNamespace(lock=threading.Lock(), delegations={"owned"}, results=lambda: [])
    c = oracle_live.Conversation(SimpleNamespace(send=lambda _: None), down.append, tools, "fixture")
    try:
        c.tick(); c.tick()
        assert len(down) == 1 and down[0]["items"][0]["id"] == "owned"
        assert "unowned" not in json.dumps(down)
        rows["owned"]["status"] = "completed"
        rows["owned"]["result_text"] = "Signature missing."
        c.tick()
        assert len(down) == 2
        assert down[-1]["items"][0]["result"] == "Signature missing."
    finally:
        close(c)
