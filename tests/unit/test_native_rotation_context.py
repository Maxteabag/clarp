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


@pytest.mark.parametrize("metadata", ["missing", "foreign", "after_turn"])
def test_parser_requires_positive_native_metadata_and_pre_turn_context_position(tmp_path, metadata):
    item = _item()
    path = tmp_path / "source.jsonl"
    rows = []
    if metadata != "missing":
        rows.append({"type": "session_meta", "payload": {"id": "foreign" if metadata == "foreign" else "new-thread"}})
    if metadata == "after_turn":
        rows.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "real-user-turn"}})
    rows.append({"type": "response_item", "payload": item})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert any(turn["text"] == item["content"][0]["text"] for turn in codex_transcript.parse_turns(path))


def test_real_turn_boundary_tail_without_metadata_matches_full_visible_history(tmp_path):
    from lib import transcript_incremental
    item = _item()
    prefix = [{"type": "session_meta", "payload": {"id": "new-thread"}},
              {"type": "response_item", "payload": item}]
    tail = [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "real-turn"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Actual user request"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Actual completion"}}]
    prefix_bytes = "".join(json.dumps(row) + "\n" for row in prefix).encode()
    tail_bytes = "".join(json.dumps(row) + "\n" for row in tail).encode()
    path = tmp_path / "source.jsonl"
    path.write_bytes(prefix_bytes + tail_bytes)
    full = codex_transcript.parse_turns(path)
    actual_tail = transcript_incremental.parse_range("codex", path, len(prefix_bytes), path.stat().st_size)
    assert [(row["role"], row["text"]) for row in full] == [
        ("user", "Actual user request"), ("assistant", "Actual completion")]
    assert actual_tail == full


def test_late_conflicting_metadata_restores_the_earlier_context_candidate(tmp_path):
    item = _item()
    path = tmp_path / "conflicted.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "new-thread"}},
            {"type": "response_item", "payload": item},
            {"type": "session_meta", "payload": {"id": "foreign-thread"}}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert [row["text"] for row in codex_transcript.parse_turns(path)] == [item["content"][0]["text"]]


def test_a_genuine_user_event_closes_bootstrap_even_without_a_start_record(tmp_path):
    item = _item()
    path = tmp_path / "missing-start.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "new-thread"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Real input"}},
            {"type": "response_item", "payload": item}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert [row["text"] for row in codex_transcript.parse_turns(path)] == [
        "Real input", item["content"][0]["text"]]


def test_a_real_user_copy_of_context_text_is_not_lost_to_dual_format_deduplication(tmp_path):
    item = _item()
    path = tmp_path / "user-copy.jsonl"
    ordinary = {**item, "id": "real-provider-user-id"}
    rows = [{"type": "session_meta", "payload": {"id": "new-thread"}},
            {"type": "response_item", "payload": item},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "real-turn"}},
            {"type": "response_item", "payload": ordinary}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert [row["text"] for row in codex_transcript.parse_turns(path)] == [ordinary["content"][0]["text"]]


@pytest.mark.parametrize("record", ["{broken json", "[]", "null"])
@pytest.mark.parametrize("before_seed", [False, True])
def test_invalid_outer_records_close_admission_without_revoking_a_prior_seed(tmp_path, record, before_seed):
    item = _item()
    header = json.dumps({"type": "session_meta", "payload": {"id": "new-thread"}}) + "\n"
    seed = json.dumps({"type": "response_item", "payload": item}) + "\n"
    path = tmp_path / "invalid-record.jsonl"
    path.write_text(header + (record + "\n" + seed if before_seed else seed + record + "\n"))
    # Invalid outer records retain the ordinary parser's skip policy. Before
    # the candidate they prevent new suppression; after it they do not change
    # already-attributed prefix visibility (unlike parsed conflicting metadata).
    assert [row["text"] for row in codex_transcript.parse_turns(path)] == (
        [item["content"][0]["text"]] if before_seed else [])


@pytest.mark.parametrize("witness", [(), (1, 2), (1, 2, 100),
                                    (True, 2, 100, 3, 4), (1, 2, 100, -1, 4)])
def test_context_builder_refuses_incomplete_or_malformed_source_witness(witness):
    from lib.native_rotation_seed import SeedUnavailable
    seed = Seed("handover", "old-thread", '{"handover":"saved"}', 100, witness)
    with pytest.raises(SeedUnavailable):
        context.build_item(seed, agent_id="agent", new_thread_id="new-thread", operation_id="rotation-a")
