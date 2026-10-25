"""Muse Code session logs (``sessions/YYYY/MM/DD/<id>/session.jsonl``) as Clarp turns.

Fixtures in tests/fixtures/muse are compacted real logs from muse 1.4.13
(docs/architecture/muse-backend.md).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from lib import muse_transcript  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "muse"
TWO_TURNS_ID = "b2951c5f-f58b-4c62-a910-d3f52502b0b8"
TOOL_CALL_ID = "06a488a5-eb75-4aff-9c34-7ef0b3c89246"


def _rows(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line]


def _write(path: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _store(home: pathlib.Path, session_id: str, fixture: str, day: str = "2026/10/25") -> pathlib.Path:
    target = home / "sessions" / day / session_id / "session.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / fixture, target)
    return target


def _with_terminal(rows: list[dict], terminal: str, reason: str | None) -> list[dict]:
    out = []
    for row in rows:
        event = (row.get("payload") or {}).get("event") or {}
        if event.get("kind") == "assistant_message_committed":
            continue
        if event.get("kind") == "terminal":
            row = json.loads(json.dumps(row))
            row["payload"]["event"].update(terminal=terminal, reason=reason)
        out.append(row)
    return out


def test_two_echo_turns_alternate_user_and_assistant():
    turns = muse_transcript.parse_turns(FIXTURES / "session-two-turns.jsonl")
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "first turn"), ("assistant", "echo: first turn"),
        ("user", "second turn"), ("assistant", "echo: second turn"),
    ]
    assert all(t["timestamp"].startswith("2026-10-") for t in turns)
    assert turns[-1]["kind"] == "final_answer"


def test_a_tool_call_attaches_to_the_answer_with_its_result():
    turns = muse_transcript.parse_turns(FIXTURES / "session-tool-call.jsonl")
    assert [t["role"] for t in turns] == ["user", "assistant"]
    user, answer = turns
    assert user["text"].startswith("Read notes.txt with a tool")
    assert answer["text"] == "2"
    assert answer["kind"] == "final_answer"
    (tool,) = answer["tools"]
    assert tool["name"] == "Read"
    assert tool["file_path"] == "notes.txt"
    assert tool["status"] == "ok"
    assert "alpha" in tool.get("result", "")


def test_encrypted_reasoning_never_reaches_the_chat():
    turns = muse_transcript.parse_turns(FIXTURES / "session-tool-call.jsonl")
    assert not any("redacted-encrypted" in t["text"] for t in turns)


def test_a_failed_run_without_an_answer_is_one_failed_row(tmp_path):
    rows = _with_terminal(_rows("session-two-turns.jsonl")[:3] + _rows("session-two-turns.jsonl")[4:5],
                          "failed", "provider stream closed")
    path = _write(tmp_path / "s.jsonl", rows)
    turns = muse_transcript.parse_turns(path)
    assert turns[0]["role"] == "user"
    failed = turns[-1]
    assert failed["role"] == "assistant" and failed.get("failed") is True
    assert "provider stream closed" in failed["text"]
    assert "kind" not in failed


@pytest.mark.parametrize("terminal", ["cancelled", "interrupted"])
def test_a_stopped_run_leaves_no_error_row(tmp_path, terminal):
    rows = _with_terminal(_rows("session-two-turns.jsonl"), terminal, None)
    turns = muse_transcript.parse_turns(_write(tmp_path / "s.jsonl", rows))
    assert all(not t.get("failed") for t in turns)
    assert [t["role"] for t in turns] == ["user", "user"]


def test_the_voice_preamble_is_stripped_from_user_prompts(tmp_path):
    from lib.voice_preamble import apply_voice_preamble
    rows = _rows("session-two-turns.jsonl")
    voiced = apply_voice_preamble("first turn", voice=True, persona="Nadia", session="s")
    for row in rows:
        event = (row.get("payload") or {}).get("event") or {}
        if event.get("kind") == "started" and event.get("prompt") == "first turn":
            event["prompt"] = voiced
    turns = muse_transcript.parse_turns(_write(tmp_path / "s.jsonl", rows))
    assert turns[0]["text"] == "first turn"


def test_unreadable_or_garbage_lines_are_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("not json\n[1,2]\n" + (FIXTURES / "session-two-turns.jsonl").read_text())
    assert len(muse_transcript.parse_turns(path)) == 4
    assert muse_transcript.parse_turns(tmp_path / "missing.jsonl") == []


def test_find_latest_jsonl_walks_the_dated_session_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PWA_MUSE_HOME", str(tmp_path))
    stored = _store(tmp_path, TWO_TURNS_ID, "session-two-turns.jsonl")
    assert muse_transcript.find_latest_jsonl(TWO_TURNS_ID) == stored
    assert muse_transcript.find_latest_jsonl("00000000-0000-0000-0000-000000000000") is None
    assert muse_transcript.find_latest_jsonl("") is None
    assert muse_transcript.find_latest_jsonl("../../etc") is None


def test_list_sessions_filters_by_workspace_and_previews_the_first_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PWA_MUSE_HOME", str(tmp_path))
    older = _store(tmp_path, TWO_TURNS_ID, "session-two-turns.jsonl", day="2026/10/24")
    newer = _store(tmp_path, TOOL_CALL_ID, "session-tool-call.jsonl")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))
    ws = "/var/tmp/nadia/muse-probe/ws"
    rows = muse_transcript.list_sessions(ws, limit=10)
    assert [r["id"] for r in rows] == [TOOL_CALL_ID, TWO_TURNS_ID]
    assert rows[0]["preview"].startswith("Read notes.txt")
    assert rows[0]["cwd"] == ws and rows[0]["mtime"] == 2_000
    assert muse_transcript.list_sessions("/somewhere/else", limit=10) == []
    every = muse_transcript.list_sessions("", limit=1, all_projects=True)
    assert [r["id"] for r in every] == [TOOL_CALL_ID]
