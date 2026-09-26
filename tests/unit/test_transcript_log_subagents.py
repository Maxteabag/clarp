"""Claude's built-in sub-agents (Agent/Task tool) as `subagents` display cells."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.transcript_log import find_latest_jsonl, parse_turns  # noqa: E402

PARENT = "11111111-2222-3333-4444-555555555555"


def _write(path: pathlib.Path, records: list[dict]) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _agent_call(tool_id: str, **inp) -> dict:
    inp.setdefault("description", "Map the auth flow")
    inp.setdefault("prompt", "Read   the server\nand report the auth flow.")
    return {"type": "assistant", "timestamp": "t1", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "Agent", "input": inp}]}}


def _result(tool_id: str, text: str, meta: dict | None = None, *, is_error=False) -> dict:
    record = {"type": "user", "timestamp": "t2", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "is_error": is_error,
         "content": [{"type": "text", "text": text}]}]}}
    if meta is not None:
        record["toolUseResult"] = meta
    return record


def _launched(tool_id: str, agent_id: str) -> dict:
    return _result(tool_id, f"Async agent launched successfully.\nagentId: {agent_id}", {
        "isAsync": True, "status": "async_launched", "agentId": agent_id,
        "description": "Map the auth flow"})


def _notification(tool_id: str, agent_id: str, status: str, result: str = "") -> str:
    return (f"<task-notification>\n<task-id>{agent_id}</task-id>\n"
            f"<tool-use-id>{tool_id}</tool-use-id>\n"
            f"<output-file>/tmp/x/{agent_id}.output</output-file>\n"
            f"<status>{status}</status>\n"
            f"<summary>Agent \"Map the auth flow\" {status}</summary>\n"
            + (f"<result>{result}</result>\n" if result else "")
            + "</task-notification>")


def _cells(turns: list[dict]) -> list[dict]:
    return [cell for turn in turns for cell in turn.get("display_cells", [])]


def _lines(cell: dict) -> dict[str, str]:
    return {line["label"]: line["text"] for line in cell["lines"]}


def test_foreground_agent_call_becomes_a_finished_subagent_cell(tmp_path):
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        {"type": "user", "timestamp": "t0", "message": {"content": "look into auth"}},
        _agent_call("toolu_fg", subagent_type="Explore"),
        _result("toolu_fg", "Auth uses bearer tokens.", {
            "status": "completed", "agentId": "a0fg", "agentType": "Explore",
            "totalToolUseCount": 12, "totalDurationMs": 184_000}),
        {"type": "assistant", "timestamp": "t3", "message": {"content": [
            {"type": "text", "text": "Done."}]}},
    ])
    turns = parse_turns(path)

    assert [t["role"] for t in turns] == ["user", "assistant", "assistant"]
    agent_turn = turns[1]
    # The call is a cell, not a generic tool card.
    assert agent_turn["tools"] == []
    [cell] = agent_turn["display_cells"]
    assert cell["id"] == "toolu_fg"
    assert cell["kind"] == "subagents"
    assert cell["title"] == "Agent finished"
    assert cell["summary"] == "Map the auth flow"
    assert cell["status"] == "ok"
    assert cell["ephemeral"] is True
    assert cell["background"] is False
    assert cell["subagent_type"] == "Explore"
    assert cell["agent_id"] == "a0fg"
    assert cell["transcript_path"] == ""
    lines = _lines(cell)
    assert lines["Task"] == "Read the server and report the auth flow."
    assert lines["Type"] == "Explore"
    assert lines["Mode"] == "Foreground"
    assert lines["Result"] == "Auth uses bearer tokens."
    assert lines["Usage"] == "12 tool calls · 3m 4s"
    assert "Transcript" not in lines
    # Every line has the Codex display-line shape.
    assert all(set(line) == {"label", "text", "kind"} for line in cell["lines"])
    assert not any(key.startswith("_") for key in cell)


def test_background_agent_without_notification_is_still_running(tmp_path):
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        _agent_call("toolu_bg", run_in_background=True, subagent_type="general-purpose"),
        _launched("toolu_bg", "a0bg"),
    ])
    [cell] = _cells(parse_turns(path))

    assert cell["status"] == "running"
    assert cell["title"] == "Running agent"
    assert cell["background"] is True
    assert cell["agent_id"] == "a0bg"
    assert _lines(cell)["Mode"] == "Background"
    # The launch receipt is not a result.
    assert "Result" not in _lines(cell)


def test_async_launch_marks_background_even_without_the_flag(tmp_path):
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        _agent_call("toolu_auto"),
        _launched("toolu_auto", "a0auto"),
    ])
    [cell] = _cells(parse_turns(path))

    assert cell["background"] is True
    assert cell["status"] == "running"


def test_background_agent_finishes_on_its_task_notification(tmp_path):
    note = _notification("toolu_bg", "a0bg", "completed", "Found  three\nendpoints.")
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        _agent_call("toolu_bg", run_in_background=True),
        _launched("toolu_bg", "a0bg"),
        {"type": "assistant", "timestamp": "t3", "message": {"content": [
            {"type": "text", "text": "It is running."}]}},
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "t4",
         "content": note},
        {"type": "user", "timestamp": "t5", "message": {"content": note}},
    ])
    turns = parse_turns(path)

    # The notification envelope is still not shown as a user message.
    assert [t["role"] for t in turns] == ["assistant", "assistant"]
    [cell] = _cells(turns)
    assert cell["status"] == "ok"
    assert cell["title"] == "Agent finished"
    assert _lines(cell)["Result"] == "Found three endpoints."


def test_queue_operation_alone_settles_a_background_agent(tmp_path):
    # The parent's process can exit before the notification is delivered as
    # a user turn; the enqueue record is then the only marker.
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        _agent_call("toolu_bg", run_in_background=True),
        _launched("toolu_bg", "a0bg"),
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "t4",
         "content": _notification("toolu_bg", "a0bg", "completed")},
    ])
    [cell] = _cells(parse_turns(path))

    assert cell["status"] == "ok"
    assert _lines(cell)["Result"] == 'Agent "Map the auth flow" completed'


def test_failed_background_agent(tmp_path):
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        _agent_call("toolu_bg", run_in_background=True),
        _launched("toolu_bg", "a0bg"),
        {"type": "user", "timestamp": "t5", "message": {"content":
            _notification("toolu_bg", "a0bg", "failed", "API Error: overloaded")}},
    ])
    [cell] = _cells(parse_turns(path))

    assert cell["status"] == "error"
    assert cell["title"] == "Agent failed"
    assert _lines(cell)["Status"] == "API Error: overloaded"


def test_stopped_background_agent(tmp_path):
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        _agent_call("toolu_bg", run_in_background=True),
        _launched("toolu_bg", "a0bg"),
        {"type": "user", "timestamp": "t5", "message": {"content":
            _notification("toolu_bg", "a0bg", "stopped")}},
    ])
    [cell] = _cells(parse_turns(path))

    assert cell["status"] == "error"
    assert cell["title"] == "Agent stopped"


def test_failed_foreground_agent(tmp_path):
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        _agent_call("toolu_fg"),
        _result("toolu_fg", "[Request interrupted by user for tool use]", is_error=True),
    ])
    [cell] = _cells(parse_turns(path))

    assert cell["status"] == "error"
    assert cell["title"] == "Agent failed"
    assert cell["background"] is False
    assert _lines(cell)["Status"] == "[Request interrupted by user for tool use]"


def test_notifications_for_other_tasks_leave_the_cell_alone(tmp_path):
    # Background Bash tasks use the same envelope with their own ids.
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        _agent_call("toolu_bg", run_in_background=True),
        _launched("toolu_bg", "a0bg"),
        {"type": "user", "timestamp": "t5", "message": {"content":
            _notification("toolu_bash", "b0bash", "completed")}},
    ])
    [cell] = _cells(parse_turns(path))

    assert cell["status"] == "running"


def test_legacy_task_tool_name_is_a_subagent_cell(tmp_path):
    record = _agent_call("toolu_task")
    record["message"]["content"][0]["name"] = "Task"
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [record])
    [cell] = _cells(parse_turns(path))

    assert cell["kind"] == "subagents"
    assert cell["status"] == "running"


def test_links_the_subagent_transcript_by_tool_use_id(tmp_path):
    project = tmp_path / "projects" / "-home-user-proj"
    path = _write(project / f"{PARENT}.jsonl", [
        _agent_call("toolu_bg", run_in_background=True),
        # No agentId in the launch receipt: only the meta file links them.
        _result("toolu_bg", "Async agent launched successfully.", {
            "isAsync": True, "status": "async_launched"}),
    ])
    subagents = project / PARENT / "subagents"
    transcript = _write(subagents / "agent-a1b2c3.jsonl", [
        {"type": "user", "isSidechain": True, "agentId": "a1b2c3",
         "message": {"role": "user", "content": "Read the server"}}])
    (subagents / "agent-a1b2c3.meta.json").write_text(json.dumps({
        "agentType": "Explore", "description": "Map the auth flow",
        "toolUseId": "toolu_bg", "spawnDepth": 1, "requestShape": "background"}))
    # A nested sub-agent's meta names a tool_use in the sub-agent's own
    # transcript; it must not attach to anything in the parent.
    _write(subagents / "agent-dddd.jsonl", [])
    (subagents / "agent-dddd.meta.json").write_text(json.dumps({
        "toolUseId": "toolu_nested", "parentAgentId": "a1b2c3", "spawnDepth": 2}))

    [cell] = _cells(parse_turns(path))

    assert cell["transcript_path"] == str(transcript)
    assert cell["agent_id"] == "a1b2c3"
    assert _lines(cell)["Transcript"] == str(transcript)
    # The sub-agent transcript is not a conversation of its own.
    projects = tmp_path / "projects"
    assert find_latest_jsonl("agent-a1b2c3", projects_root=projects) is None
    assert find_latest_jsonl(PARENT, projects_root=projects) == path


def test_links_the_subagent_transcript_by_agent_id(tmp_path):
    project = tmp_path / "proj"
    path = _write(project / f"{PARENT}.jsonl", [
        _agent_call("toolu_bg", run_in_background=True),
        _launched("toolu_bg", "a9f8"),
    ])
    transcript = _write(project / PARENT / "subagents" / "agent-a9f8.jsonl", [])

    [cell] = _cells(parse_turns(path))

    assert cell["transcript_path"] == str(transcript)


def test_turns_without_agent_calls_carry_no_display_cells(tmp_path):
    path = _write(tmp_path / "proj" / f"{PARENT}.jsonl", [
        {"type": "assistant", "timestamp": "t1", "message": {"content": [
            {"type": "tool_use", "id": "toolu_b", "name": "Bash",
             "input": {"command": "ls"}}]}},
    ])
    [turn] = parse_turns(path)

    assert "display_cells" not in turn
    assert turn["tools"][0]["name"] == "Bash"
