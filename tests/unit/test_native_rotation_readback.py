"""A seed ACK cannot substitute for the actual persisted history-only item.

These temporary native-format files test readback only, not transport ACK,
provider quiescence, or a real rotation/activation.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from lib import native_rotation_context as context
from lib import native_rotation_readback as readback
from lib.native_rotation_seed import Seed


def _history(tmp_path):
    seed = Seed("handover", "old-thread", '{"handover":"preserve task"}', 100, (1, 2, 100, 3, 4))
    item = context.build_item(seed, agent_id="agent", new_thread_id="new-thread", operation_id="rotation-a")
    rows = [{"type": "session_meta", "payload": {"id": "new-thread"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                                     "content": [{"type": "input_text", "text": "Current persona"}]}},
            {"type": "response_item", "payload": item}]
    path = tmp_path / "owned-new-rollout.jsonl"
    return path, item, rows


def _store(path, rows):
    raw = b"".join(json.dumps(row).encode("utf-8") + b"\n" for row in rows)
    path.write_bytes(raw)
    return raw


def test_seed_readback_binds_one_persisted_item_and_the_actual_opened_file(tmp_path):
    path, item, rows = _history(tmp_path)
    raw = _store(path, rows)
    receipt = readback.verify_seed(path, item, native_thread_id="new-thread")
    stat = path.stat()
    assert receipt.native_thread_id == "new-thread" and receipt.item_id == item["id"]
    assert receipt.file_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.text_sha256 == hashlib.sha256(item["content"][0]["text"].encode()).hexdigest()
    assert receipt.witness == (stat.st_dev, stat.st_ino, len(raw), stat.st_mtime_ns, stat.st_ctime_ns)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "foreign_header", "duplicate_header",
                                    "wrong_role", "changed_data", "model_turn", "unknown", "malformed", "partial"])
def test_readback_does_not_certify_ambiguous_or_executing_native_history(tmp_path, damage):
    path, item, rows = _history(tmp_path)
    if damage == "missing":
        rows.pop()
    elif damage == "duplicate":
        rows.append(rows[-1])
    elif damage == "foreign_header":
        rows[0]["payload"]["id"] = "foreign-thread"
    elif damage == "duplicate_header":
        rows.append(rows[0])
    elif damage == "wrong_role":
        rows[-1] = {"type": "response_item", "payload": {**item, "role": "assistant"}}
    elif damage == "changed_data":
        rows[-1] = {"type": "response_item", "payload": {**item, "content": [
            {"type": "input_text", "text": item["content"][0]["text"] + " changed"}]}}
    elif damage == "model_turn":
        rows.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "unexpected"}})
    elif damage == "unknown":
        rows.append({"type": "future_native_record", "payload": {}})
    _store(path, rows)
    if damage == "malformed":
        with path.open("ab") as stream:
            stream.write(b"{broken-json}\n")
    elif damage == "partial":
        with path.open("ab") as stream:
            stream.write(b'{"type":')
    with pytest.raises(readback.ReadbackUnavailable):
        readback.verify_seed(path, item, native_thread_id="new-thread")


def test_readback_refuses_a_file_replaced_during_its_owned_read(tmp_path, monkeypatch):
    path, item, rows = _history(tmp_path)
    _store(path, rows)
    replacement = tmp_path / "replacement.jsonl"
    _store(replacement, rows)
    actual_read = readback.os.read
    replaced = False

    def replace_after_read(fd, size):
        nonlocal replaced
        chunk = actual_read(fd, size)
        if not replaced:
            replaced = True
            replacement.replace(path)
        return chunk

    monkeypatch.setattr(readback.os, "read", replace_after_read)
    with pytest.raises(readback.ReadbackUnavailable):
        readback.verify_seed(path, item, native_thread_id="new-thread")
    assert replaced


def test_readback_rejects_oversize_before_consuming_the_file(tmp_path, monkeypatch):
    path, item, rows = _history(tmp_path)
    _store(path, rows)
    reads = []
    actual_read = readback.os.read

    def counted(fd, size):
        reads.append(size)
        return actual_read(fd, size)

    monkeypatch.setattr(readback.os, "read", counted)
    with pytest.raises(readback.ReadbackUnavailable):
        readback.verify_seed(path, item, native_thread_id="new-thread", max_bytes=32)
    assert reads == []


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_readback_refuses_special_or_redirected_source_without_reading(tmp_path, monkeypatch, kind):
    path, item, rows = _history(tmp_path)
    target = tmp_path / "different-file.jsonl"
    _store(target, rows)
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.symlink_to(target)
    reads = []
    actual_read = readback.os.read

    def counted(fd, size):
        reads.append(size)
        return actual_read(fd, size)

    monkeypatch.setattr(readback.os, "read", counted)
    with pytest.raises(readback.ReadbackUnavailable):
        readback.verify_seed(path, item, native_thread_id="new-thread")
    assert reads == []
