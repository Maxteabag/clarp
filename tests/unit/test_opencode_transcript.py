"""OpenCode history renders with the display cells and message kinds Codex has."""
from __future__ import annotations

import json

from lib import opencode_transcript


def _row(role, finish=None, **extra):
    data = {"role": role, **extra}
    if finish:
        data["finish"] = finish
    return json.dumps(data)


def _tool(name, status="completed", call="c", **state):
    return {"type": "tool", "tool": name, "callID": call,
            "state": {"status": status, **state}}


def _turns(messages):
    """``messages`` is a list of (role, finish, parts)."""
    rows, parts, ids = [], {}, []
    for index, (role, finish, message_parts) in enumerate(messages):
        message_id = f"msg_{index}"
        rows.append((str(1_789_544_308_000 + index), _row(role, finish)))
        parts[message_id] = message_parts
        ids.append(message_id)
    return opencode_transcript._turns_from_messages(rows, parts, ids)


def _text(value):
    return {"type": "text", "text": value}


def test_bash_is_a_command_cell_with_output_and_exit_status():
    turns = _turns([
        ("user", None, [_text("run the tests")]),
        ("assistant", "stop", [
            _tool("bash", call="b1", input={"command": "pytest -q"},
                  output="1 failed", metadata={"exit": 1, "output": "1 failed"}),
            _text("One test fails."),
        ]),
    ])
    cell, = turns[1]["display_cells"]
    assert (cell["kind"], cell["id"], cell["title"], cell["summary"], cell["status"]) == (
        "command", "b1", "Ran", "pytest -q", "error")
    assert [line["text"] for line in cell["lines"]] == ["1 failed"]


def test_reads_and_searches_fold_into_one_exploration_cell():
    turns = _turns([
        ("user", None, [_text("where is login")]),
        ("assistant", "stop", [
            _tool("grep", call="g", input={"pattern": "login", "path": "src"}),
            _tool("read", call="r", input={"filePath": "src/auth.py"}),
            _tool("bash", call="s", input={"command": "sed -n 1,40p src/app.py"},
                  metadata={"exit": 0}, output="x"),
            _tool("bash", call="t", input={"command": "make test"},
                  metadata={"exit": 0}, output="ok"),
            _text("In src/auth.py."),
        ]),
    ])
    explored, ran = turns[1]["display_cells"]
    assert explored["kind"] == "exploration" and explored["title"] == "Explored"
    assert [(line["label"], line["text"]) for line in explored["lines"]] == [
        ("Search", "login in src"), ("Read", "src/auth.py"), ("Read", "src/app.py")]
    assert (ran["kind"], ran["summary"], ran["status"]) == ("command", "make test", "ok")
    assert "_explore" not in json.dumps(turns)


def test_step_finish_reason_sets_commentary_and_final_answer():
    turns = _turns([
        ("user", None, [_text("fix it")]),
        ("assistant", "tool-calls", [_text("Checking the tests first."),
                                     _tool("bash", call="a", input={"command": "ls"},
                                           metadata={"exit": 0}, output="")]),
        ("assistant", "tool-calls", [_tool("edit", call="e", input={
            "filePath": "/a.py", "oldString": "x = 1", "newString": "x = 2"})]),
        ("assistant", "stop", [_text("Fixed.")]),
    ])
    assert [(t["role"], t.get("kind"), t["text"]) for t in turns] == [
        ("user", None, "fix it"),
        ("assistant", "commentary", "Checking the tests first."),
        ("assistant", "final_answer", "Fixed."),
    ]
    # The tool-only step belongs under the commentary that announced it.
    assert [c["kind"] for c in turns[1]["display_cells"]] == ["exploration", "patch"]
    patch = turns[1]["display_cells"][1]
    assert [(line["label"], line["text"], line["kind"]) for line in patch["lines"]] == [
        ("Edit", "/a.py", "detail"), ("", "-x = 1", "diff_old"), ("", "+x = 2", "diff_new")]
    assert [tool["name"] for tool in turns[1]["tools"]] == ["Bash", "Edit"]


def test_todowrite_is_a_plan_cell():
    turns = _turns([
        ("user", None, [_text("plan")]),
        ("assistant", "stop", [_tool("todowrite", call="p", input={"todos": [
            {"content": "Write tests", "status": "completed", "priority": "high"},
            {"content": "Ship", "status": "in_progress", "priority": "high"}]}),
            _text("Planned.")]),
    ])
    cell, = turns[1]["display_cells"]
    assert (cell["kind"], cell["title"]) == ("plan", "Updated plan")
    assert [(line["label"], line["text"]) for line in cell["lines"]] == [
        ("Completed", "Write tests"), ("In Progress", "Ship")]


def test_task_is_a_subagent_cell_and_question_shows_its_error():
    turns = _turns([
        ("user", None, [_text("research")]),
        ("assistant", "stop", [
            _tool("task", call="k", input={"description": "Map auth", "prompt": "Find it",
                                           "subagent_type": "general"},
                  output="Found three files", metadata={"sessionId": "ses_child"}),
            _tool("question", status="error", call="q", input={"questions": [
                {"question": "Which one?", "options": [{"label": "A"}, {"label": "B"}]}]},
                  error="Error: The user dismissed this question"),
            _text("Done."),
        ]),
    ])
    task, question = turns[1]["display_cells"]
    assert (task["kind"], task["title"], task["summary"]) == ("subagents", "Agent finished", "Map auth")
    assert ("Session", "ses_child") in [(line["label"], line["text"]) for line in task["lines"]]
    assert (question["summary"], question["status"]) == ("Which one?", "error")
    assert question["lines"][-1] == {"label": "", "text": "The user dismissed this question",
                                     "kind": "error"}


def test_interrupted_calls_stop_spinning_once_a_newer_reply_exists():
    turns = _turns([
        ("user", None, [_text("go")]),
        ("assistant", None, [_tool("bash", status="running", call="x",
                                   input={"command": "make build"})]),
        ("user", None, [_text("again")]),
        ("assistant", None, [_tool("bash", status="running", call="y",
                                   input={"command": "make build"})]),
    ])
    stale, live = turns[1]["display_cells"][0], turns[3]["display_cells"][0]
    assert (stale["status"], stale["title"]) == ("recorded", "Ran")
    assert turns[1]["tools"][0]["status"] == "recorded"
    assert (live["status"], live["title"]) == ("running", "Running")


def test_failed_turn_has_no_kind_and_does_not_collect_later_tools():
    rows = [("1", _row("user")), ("2", _row("assistant", error={
        "name": "APIError", "data": {"message": "Account is suspended"}})),
        ("3", _row("assistant")), ("4", _row("assistant", "stop"))]
    parts = {"u": [_text("hi")], "e": [],
             "t": [_tool("glob", call="g", input={"pattern": "*.py"})],
             "s": [_text("Found them.")]}
    turns = opencode_transcript._turns_from_messages(rows, parts, ["u", "e", "t", "s"])
    assert [(t["text"], t.get("kind"), len(t.get("display_cells", []))) for t in turns] == [
        ("hi", None, 0), ("OpenCode APIError: Account is suspended", None, 0),
        ("Found them.", "final_answer", 1)]


def test_session_picker_hides_sub_agent_child_sessions(tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "opencode.db")
    con.executescript(
        """CREATE TABLE session (id text PRIMARY KEY, parent_id text, directory text,
             title text, time_updated integer, time_archived integer);
           CREATE TABLE message (id text, session_id text, time_created integer, data text);
           CREATE TABLE part (id text, message_id text, session_id text, data text);
           INSERT INTO session VALUES ('ses_parent', NULL, '/p', 'Main', 2, NULL);
           INSERT INTO session VALUES ('ses_child', 'ses_parent', '/p', 'Sub', 3, NULL);""")
    con.commit()
    con.close()
    listed = opencode_transcript.list_sessions("/p", home=tmp_path)
    assert [row["id"] for row in listed] == ["ses_parent"]


def test_a_stopped_turn_leaves_no_error_row_but_keeps_what_it_did():
    aborted = {"name": "MessageAbortedError", "data": {"message": "The operation was aborted."}}
    rows = [("1", _row("user")), ("2", _row("assistant", error=aborted)),
            ("3", _row("user")), ("4", _row("assistant", error=aborted))]
    parts = {"u1": [_text("go")], "a1": [],
             "u2": [_text("go on")],
             "a2": [_text("Half an answer"), _tool("bash", status="error", call="b",
                                                   input={"command": "sleep 60"},
                                                   error="Tool execution aborted")]}
    turns = opencode_transcript._turns_from_messages(rows, parts, ["u1", "a1", "u2", "a2"])
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "go"), ("user", "go on"), ("assistant", "Half an answer")]
    assert "MessageAbortedError" not in json.dumps(turns)
