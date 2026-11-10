"""History-only rotation data must not become a second visible user message."""
from __future__ import annotations

import copy
import json

import pytest

from lib import codex_transcript, native_rotation_context as context
from lib.native_rotation_seed import Seed


def _item():
    data = {"handover": "Preserve task g1; the patch is saved.",
            "source_turn_id": "finished-native-turn", "source_cutoff": 100}
    seed = Seed("handover", "old-thread", json.dumps(data), 100, (1, 2, 100, 3, 4))
    return context.build_item(seed, agent_id="agent", new_thread_id="new-thread", operation_id="rotation-a")


def _parse(tmp_path, item):
    path = tmp_path / "rollout.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "new-thread"}},
            {"type": "response_item", "payload": item},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fresh user request"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Actual reply"}}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return codex_transcript.parse_turns(path)


def test_exact_injected_data_is_persistent_history_but_not_a_visible_user_push_cause(tmp_path):
    item = _item()
    assert item["type"] == "message" and item["role"] == "user"
    assert context.is_context_item(item, "new-thread")
    assert not context.is_context_item(item, "old-thread")
    assert "history data" in item["content"][0]["text"]
    assert [(turn["role"], turn["text"]) for turn in _parse(tmp_path, item)] == [
        ("user", "Fresh user request"), ("assistant", "Actual reply")]


@pytest.mark.parametrize("damage", ["role", "id", "missing_id", "quoted", "digest", "malformed", "extra"])
def test_lookalike_or_unattributed_user_content_is_never_hidden(tmp_path, damage):
    item = copy.deepcopy(_item())
    if damage == "role":
        item["role"] = "assistant"
    elif damage == "id":
        item["id"] = "provider-generated-user-item"
    elif damage == "missing_id":
        del item["id"]
    elif damage == "quoted":
        item["content"][0]["text"] = "Quoted example: " + item["content"][0]["text"]
    elif damage == "digest":
        item["content"][0]["text"] = item["content"][0]["text"].replace("patch is saved", "patch is changed")
    elif damage == "malformed":
        item["content"][0]["text"] += " trailing user instructions"
    else:
        item["unexpected"] = True
    assert not context.is_context_item(item, "new-thread")
    turns = _parse(tmp_path, item)
    assert any(turn["text"] == item["content"][0]["text"] for turn in turns)


def test_native_user_item_projection_requires_the_same_owned_id_and_data():
    item = _item()
    projection = {"type": "userMessage", "id": item["id"],
                  "content": [{"type": "text", "text": item["content"][0]["text"]}]}
    assert context.is_context_item(projection, "new-thread")
    del projection["id"]
    assert not context.is_context_item(projection, "new-thread")
    # EventMsg/UserMessage has no attributable raw ResponseItem ID. A body
    # copied into a genuine user-message event is not a hidden context item.
    assert not context.is_context_item({"type": "user_message", "message": item["content"][0]["text"]},
                                       "new-thread")
