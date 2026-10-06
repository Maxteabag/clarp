"""Tests for lib.agy_transcript — parsing antigravity brain transcripts
into the PWA turn list, plus locating/listing conversations.
"""
from __future__ import annotations

import json
import pathlib
import sys

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agy_transcript          # noqa: E402


def _write_transcript(brain_root: pathlib.Path, conv_id: str, rows: list[dict]) -> pathlib.Path:
    d = brain_root / conv_id / ".system_generated" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_parse_turns_user_tools_assistant(tmp_path):
    rows = [
        {"step_index": 0, "type": "USER_INPUT", "created_at": "t0",
         "content": "<USER_REQUEST>\nlist the files\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "t1",
         "tool_calls": [{"name": "run"}]},
        {"step_index": 2, "type": "RUN_COMMAND", "created_at": "t2",
         "content": "Created At: t2\nCompleted At: t2\nOutput:\na\nb"},
        {"step_index": 3, "type": "PLANNER_RESPONSE", "created_at": "t3",
         "content": "There are 2 files. <speak>Two files.</speak>"},
    ]
    p = _write_transcript(tmp_path / "brain", "conv-1", rows)
    turns = agy_transcript.parse_turns(p)

    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "list the files"          # <USER_REQUEST> unwrapped
    assert "Two files" in turns[1]["text"]
    # The RUN_COMMAND tool hung onto the assistant turn, mapped to Bash.
    tool = turns[1]["tools"][0]
    assert tool["name"] == "Bash"
    assert "a" in (tool.get("result") or "")


def test_parse_turns_strips_voice_preamble(tmp_path):
    from lib.voice_preamble import apply_voice_preamble
    rows = [
        {"step_index": 0, "type": "USER_INPUT", "created_at": "t0",
         "content": "<USER_REQUEST>\n" + apply_voice_preamble("hello") + "\n</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "t1",
         "content": "<speak>Hi.</speak>"},
    ]
    p = _write_transcript(tmp_path / "brain", "conv-2", rows)
    turns = agy_transcript.parse_turns(p)
    assert turns[0]["text"] == "hello", f"preamble not stripped: {turns[0]['text']!r}"


def test_find_latest_jsonl(tmp_path):
    brain = tmp_path / "brain"
    p = _write_transcript(brain, "conv-x", [{"step_index": 0, "type": "USER_INPUT",
                                             "content": "<USER_REQUEST>hi</USER_REQUEST>"}])
    assert agy_transcript.find_latest_jsonl("conv-x", brain_root=brain) == p
    assert agy_transcript.find_latest_jsonl("missing", brain_root=brain) is None
    assert agy_transcript.find_latest_jsonl("") is None


def test_list_sessions_uses_cwd_mapping(tmp_path):
    brain = tmp_path / "brain"
    _write_transcript(brain, "conv-home", [
        {"step_index": 0, "type": "USER_INPUT", "created_at": "t0",
         "content": "<USER_REQUEST>build the thing</USER_REQUEST>"}])
    cache = tmp_path / "last_conversations.json"
    cache.write_text(json.dumps({"/home/example/proj": "conv-home"}))

    got = agy_transcript.list_sessions("/home/example/proj",
                                       cache_file=cache, brain_root=brain)
    assert len(got) == 1
    assert got[0]["id"] == "conv-home"
    assert got[0]["preview"] == "build the thing"
    # cwd with no mapping → empty
    assert agy_transcript.list_sessions("/other", cache_file=cache, brain_root=brain) == []


def test_agy_1_3_calls_are_read_from_their_step_and_answered_in_order(tmp_path):
    p = _write_transcript(tmp_path / "brain", "conv-13", [
        {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>check</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Looking.",
         "thinking": "The user wants a check.\n\nRead the readme first.", "tool_calls": [
            {"name": "view_file", "args": {"AbsolutePath": '"/repo/README.md"', "toolSummary": "Read the readme"}},
            {"name": "run_command", "args": {"CommandLine": "false", "Cwd": "/repo"}}]},
        {"step_index": 2, "type": "GENERIC", "content": "Created At: x\n\nFile Path: /repo/README.md\n1: hi"},
        {"step_index": 3, "type": "GENERIC", "content": "Created At: x\n\nThe command exited with code 1.\nOutput:\nboom\n"},
        {"step_index": 4, "type": "PLANNER_RESPONSE", "content": "Done."},
        # An exchange with calls but no text made no turn before, and still makes none.
        {"step_index": 5, "type": "USER_INPUT", "content": "<USER_REQUEST>again</USER_REQUEST>"},
        {"step_index": 6, "type": "PLANNER_RESPONSE", "content": "",
         "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}]},
    ])
    turns = agy_transcript.parse_turns(p)
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "check"), ("assistant", "Looking."), ("assistant", "Done."), ("user", "again")]
    read, run = turns[1]["tools"]
    assert (read["name"], read["summary"], read["status"]) == ("Read", "Read the readme", "ok")
    assert read["input"]["file_path"] == "/repo/README.md"
    assert (run["name"], run["status"], run["result"]) == ("Bash", "error", "boom")
    cells = turns[1]["display_cells"]
    assert [c["kind"] for c in cells] == ["reasoning", "exploration", "command"]
    assert [line["text"] for line in cells[0]["lines"]] == [
        "The user wants a check.", "Read the readme first."]
