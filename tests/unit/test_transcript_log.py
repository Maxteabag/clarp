"""Tests for transcript_log — turn parsing and tool summarisation."""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.transcript_log import (  # noqa: E402
    truncate,
    summarise_tool,
    parse_turns,
    find_latest_jsonl,
    context_tokens_from_jsonl,
)


def test_truncate_short_passes_through():
    assert truncate("hi") == "hi"


def test_truncate_long_appends_ellipsis():
    assert truncate("a" * 700) == "a" * 600 + "…"


def test_truncate_non_string_returns_empty():
    assert truncate(None) == ""
    assert truncate(123) == ""


def test_summarise_edit_includes_paths_and_truncates_bodies():
    out = summarise_tool("Edit", {
        "file_path": "/a.py",
        "old_string": "x" * 800,
        "new_string": "y",
        "replace_all": True,
    })
    assert out["file_path"] == "/a.py"
    assert out["old"].endswith("…")
    assert out["new"] == "y"
    assert out["replace_all"] is True


def test_summarise_bash_truncates_command():
    out = summarise_tool("Bash", {"command": "a" * 500, "description": "d"})
    assert out["command"].endswith("…")
    assert out["description"] == "d"
    assert out["summary"] == "d"
    assert out["status"] == "recorded"


def test_summarise_unknown_tool_falls_back_to_scalars():
    out = summarise_tool("Mystery", {"k": "v", "ignored_list": [1, 2]})
    assert out["input"] == {"k": "v"}


def test_summarise_read_coerces_numeric_offset_limit():
    """Read offset/limit must be a real int or None — the client decodes them
    as Int?, so a bogus model value (e.g. offset='[160, 175]') would otherwise
    fail the WHOLE transcript decode and blank the conversation."""
    out = summarise_tool("Read", {"file_path": "/x", "offset": 10, "limit": 50})
    assert out["offset"] == 10 and out["limit"] == 50
    # Garbage values are dropped to None rather than passed through as strings.
    bad = summarise_tool("Read", {"file_path": "/x", "offset": "[160, 175]",
                                  "limit": "abc"})
    assert bad["offset"] is None and bad["limit"] is None
    # Numeric strings still coerce.
    coerced = summarise_tool("Read", {"file_path": "/x", "offset": "12"})
    assert coerced["offset"] == 12


def test_parse_turns_filters_and_keeps_text_and_tools(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "hello"}, "timestamp": "T0"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi back"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]}, "timestamp": "T1"}),
        json.dumps({"type": "system", "message": {"content": "skipped"}}),
        "not-json should be skipped",
        json.dumps({"type": "assistant", "message": {"content": ""}}),  # empty drop
    ]) + "\n")
    turns = parse_turns(p)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "hello"
    assert turns[1]["text"] == "hi back"
    assert turns[1]["tools"][0]["name"] == "Bash"
    assert turns[1]["tools"][0]["action"] == "running command"


def test_parse_turns_drops_injected_user_envelopes(tmp_path):
    """Background-task notifications / system reminders are recorded as user
    turns by claude-code but the user never typed them — they must not render
    as user messages in the PWA."""
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "real question"}, "timestamp": "T0"}),
        json.dumps({"type": "user", "message": {"content":
            "<task-notification>\n<task-id>b2h840af3</task-id>\nstopped\n</task-notification>"},
            "timestamp": "T1"}),
        json.dumps({"type": "user", "message": {"content":
            "<system-reminder>be brief</system-reminder>"}, "timestamp": "T2"}),
        json.dumps({"type": "assistant", "message": {"content": "answer"}, "timestamp": "T3"}),
    ]) + "\n")
    turns = parse_turns(p)
    assert [t["text"] for t in turns] == ["real question", "answer"]
    assert not any("task-notification" in t["text"] for t in turns)


def test_parse_turns_attaches_tool_result_status(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "false"}},
        ]}, "timestamp": "T1"}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "content": "exit 1"},
        ]}, "timestamp": "T2"}),
    ]) + "\n")
    turns = parse_turns(p)
    tool = turns[0]["tools"][0]
    assert tool["id"] == "toolu_1"
    assert tool["status"] == "error"
    assert tool["result"] == "exit 1"


def test_parse_turns_keeps_genuine_bash_calls(tmp_path):
    """Filter must NOT swallow real Bash commands that happen to start with `:`."""
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "running:"},
        {"type": "tool_use", "name": "Bash",
         "input": {"command": ": this is a real comment in user code"}},
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "ls -la"}},
    ]}}) + "\n")
    turns = parse_turns(p)
    assert len(turns[0]["tools"]) == 2
    assert turns[0]["tools"][0]["command"].startswith(": this is a real")
    assert turns[0]["tools"][1]["command"] == "ls -la"


def test_parse_turns_logs_json_decode_errors(tmp_path, capsys):
    p = tmp_path / "t.jsonl"
    p.write_text("garbage\n")
    parse_turns(p)
    assert "transcriptJsonLineSkip" in capsys.readouterr().err


def test_find_latest_jsonl_looks_up_by_exact_uuid(tmp_path):
    projects = tmp_path / "projects"
    proj = projects / "-home-user-proj-a"
    proj.mkdir(parents=True)
    target = proj / "session-abc.jsonl"
    target.write_text("")
    found = find_latest_jsonl("session-abc", projects_root=projects)
    assert found == target


def test_find_latest_jsonl_returns_none_for_empty_uuid(tmp_path):
    """The cwd-fallback that used to leak other agents' transcripts is
    gone — an empty UUID means "no JSONL", full stop. Even with files
    sitting in the cwd-encoded dir, none of them get picked."""
    projects = tmp_path / "projects"
    proj = projects / "-home-user-proj-a"
    proj.mkdir(parents=True)
    (proj / "some-other-agent.jsonl").write_text("")
    assert find_latest_jsonl("", projects_root=projects) is None


def test_context_tokens_uses_last_assistant_usage_not_cumulative(tmp_path):
    """Context size = the LAST assistant message's usage (input + cache_read +
    cache_creation), i.e. current window occupancy — NOT a sum across messages."""
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 5, "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 10, "output_tokens": 50}}}),
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        # The last assistant message is the one that counts: 2 + 168000 + 500.
        json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 2, "cache_read_input_tokens": 168000,
            "cache_creation_input_tokens": 500, "output_tokens": 9}}}),
    ]) + "\n")
    assert context_tokens_from_jsonl(p) == 168502


def test_context_tokens_none_when_no_usage(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    assert context_tokens_from_jsonl(p) is None


def test_context_tokens_missing_file_returns_none(tmp_path):
    assert context_tokens_from_jsonl(tmp_path / "nope.jsonl") is None


def test_find_latest_jsonl_returns_none_when_uuid_missing(tmp_path):
    """UUID isn't on disk anywhere — return None, don't substitute a sibling."""
    projects = tmp_path / "projects"
    (projects / "-home-user").mkdir(parents=True)
    (projects / "-home-user" / "different.jsonl").write_text("")
    assert find_latest_jsonl("not-on-disk", projects_root=projects) is None
