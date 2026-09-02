"""Tests for `lib.codex_transcript` — parsing a Codex rollout JSONL into
chat turns plus native Codex display cells.
"""
from __future__ import annotations

import json
import sqlite3
import pathlib
import sys

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import codex_transcript          # noqa: E402


def _write_rollout(path: pathlib.Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_parse_turns_user_assistant_and_tools(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "2026-05-31T10:00:00Z", "type": "session_meta",
         "payload": {"id": "s1", "cwd": "/x"}},
        {"timestamp": "2026-05-31T10:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "› count the files"}},
        {"timestamp": "2026-05-31T10:00:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "On it."}},
        {"timestamp": "2026-05-31T10:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "c1",
                     "arguments": json.dumps({"cmd": ["ls", "-1"]})}},
        {"timestamp": "2026-05-31T10:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1",
                     "output": "a\nb\nc"}},
        {"timestamp": "2026-05-31T10:00:05Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "There are 3 files."}},
    ])
    turns = codex_transcript.parse_turns(f)

    roles = [t["role"] for t in turns]
    assert roles == ["user", "assistant", "assistant"]
    # User marker '›' stripped.
    assert turns[0]["text"] == "count the files"
    # The exec_command tool hung on the first assistant turn, mapped to Bash.
    tool = turns[1]["tools"][0]
    assert tool["name"] == "Bash"
    assert "ls -1" in tool["command"]
    # Output got matched back by call_id and marked ok.
    assert tool["status"] == "ok"
    assert "a" in tool["result"]
    cell = turns[1]["display_cells"][0]
    assert cell["kind"] == "exploration"
    assert cell["title"] == "Explored"
    assert cell["lines"][0]["label"] == "List"
    assert turns[2]["text"] == "There are 3 files."


def test_parse_turns_preserves_assistant_message_phase(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Working on it.",
                     "phase": "commentary"}},
        {"timestamp": "t1", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Done.",
                     "phase": "final_answer"}},
    ])

    turns = codex_transcript.parse_turns(f)

    assert [turn["kind"] for turn in turns] == ["commentary", "final_answer"]


def test_parse_turns_response_item_messages_from_resumed_app_server(tmp_path):
    """App-server rollouts persist visible chat as response_item/message.

    Resumed sessions may contain no event_msg user_message/agent_message rows,
    so ignoring this shape collapses the whole transcript into one empty tool
    holder and makes subsequent replies invisible in the PWA.
    """
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "response_item", "payload": {
            "type": "message", "role": "developer",
            "content": [{"type": "input_text", "text": "hidden"}],
        }},
        {"timestamp": "t0.5", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{
                "type": "input_text",
                "text": "<environment_context>\n"
                        "<current_date>2026-08-24</current_date>\n"
                        "</environment_context>",
            }],
        }},
        {"timestamp": "t1", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Hey. Status?"}],
        }},
        {"timestamp": "t2", "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "I am here."}],
        }},
    ])

    turns = codex_transcript.parse_turns(f)

    assert [(turn["role"], turn["text"]) for turn in turns] == [
        ("user", "Hey. Status?"),
        ("assistant", "I am here."),
    ]
    assert turns[1]["kind"] == "final_answer"


def test_parse_turns_keeps_user_question_that_quotes_environment_context(tmp_path):
    f = tmp_path / "rollout.jsonl"
    question = "Why does <environment_context> appear in my chat?"
    _write_rollout(f, [{
        "timestamp": "t1", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": question}],
        },
    }])

    turns = codex_transcript.parse_turns(f)

    assert [(turn["role"], turn["text"]) for turn in turns] == [
        ("user", question),
    ]


def test_parse_turns_strips_voice_preamble_from_user_message(tmp_path):
    """The <speak> instruction we prepend for PWA/native Codex turns must be
    stripped from the user message in the history pane."""
    from lib import codex_runner
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "event_msg",
         "payload": {"type": "user_message",
                     "message": "› " + codex_runner.apply_voice_preamble("hello there")}},
        {"timestamp": "t1", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "<speak>Hi.</speak>"}},
    ])
    turns = codex_transcript.parse_turns(f)
    assert turns[0]["role"] == "user"
    assert turns[0]["text"] == "hello there", f"preamble not stripped: {turns[0]['text']!r}"


def test_parse_turns_apply_patch_maps_to_edit(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "patching"}},
        {"timestamp": "t1", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch",
                     "call_id": "p1",
                     "input": "*** Begin Patch\n*** Add File: x.py\n+print(1)\n"}},
    ])
    turns = codex_transcript.parse_turns(f)
    tool = turns[0]["tools"][0]
    assert tool["name"] == "Edit"
    assert tool["file_path"] == "x.py"
    assert tool["new"] == "print(1)"
    assert turns[0]["display_cells"] == []


def test_patch_apply_end_changes_map_emits_structured_diff_cell(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Updating it."}},
        {"timestamp": "t1", "type": "event_msg",
         "payload": {"type": "patch_apply_end", "call_id": "p1",
                     "success": True, "stdout": "Success",
                     "changes": {
                         "/repo/app.py": {
                             "type": "update",
                             "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                         }
                     }}},
    ])

    cell = codex_transcript.parse_turns(f)[0]["display_cells"][0]
    assert cell["kind"] == "patch"
    assert cell["status"] == "ok"
    assert cell["lines"] == [
        {"label": "Edit", "text": "/repo/app.py", "kind": "detail"},
        {"label": "", "text": "@@ -1 +1 @@", "kind": "diff_header"},
        {"label": "", "text": "-old", "kind": "diff_old"},
        {"label": "", "text": "+new", "kind": "diff_new"},
    ]


def test_real_exec_smoke_output_stays_compact(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "checking"}},
        {"timestamp": "t1", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "read",
                     "arguments": json.dumps(
                         {"cmd": "/usr/bin/bash -lc 'sed -n \"1,20p\" README.md'"}
                     )}},
        {"timestamp": "t2", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "read",
                     "output": "Chunk ID: abc\nWall time: 0.0\nOutput:\n# Readme\n"}},
        {"timestamp": "t3", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch",
                     "call_id": "patch",
                     "input": "*** Begin Patch\n*** Update File: app.py\n@@\n"
                              " def status():\n-    return \"before\"\n"
                              "+    return \"after\"\n*** End Patch\n"}},
        {"timestamp": "t4", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "patch",
                     "output": "Exit code: 0\nWall time: 0.1\nOutput:\nSuccess\n"}},
        {"timestamp": "t5", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "compile",
                     "arguments": json.dumps({"cmd": "python -m py_compile app.py"})}},
        {"timestamp": "t6", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "compile",
                     "output": "Chunk ID: def\nWall time: 0.0\n"
                               "Process exited with code 3\nOutput:\n"}},
    ])

    cells = codex_transcript.parse_turns(f)[0]["display_cells"]

    assert cells[0]["kind"] == "exploration"
    assert cells[0]["lines"][0]["text"] == "README.md"
    tool = next(t for t in codex_transcript.parse_turns(f)[0]["tools"]
                if t["name"] == "Edit")
    assert tool["old"] == "def status():\n    return \"before\""
    assert tool["new"] == "def status():\n    return \"after\""
    assert cells[1]["kind"] == "command"
    assert cells[1]["status"] == "error"
    assert cells[1]["lines"][0]["text"] == "(no output)"


def test_exploration_classifies_shell_wrapped_rg_files(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t1", "type": "item.completed",
         "item": {"id": "cmd1", "type": "command_execution",
                  "command": "/usr/bin/bash -lc 'pwd && rg --files -uu'",
                  "aggregated_output": "/tmp/x\nREADME.md\napp.py\n",
                  "exit_code": 0}},
    ])

    cells = codex_transcript.parse_turns(f)[0]["display_cells"]

    assert cells[0]["kind"] == "exploration"
    assert cells[0]["lines"][0] == {
        "label": "List", "text": "rg --files", "kind": "detail",
    }


def test_modern_web_search_ignores_config_warning_items(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"type": "item.completed",
         "item": {"id": "warn", "type": "error", "message": "config warning"}},
        {"type": "item.completed",
         "item": {"id": "web1", "type": "web_search",
                  "query": "official OpenAI Codex GitHub repository"}},
        {"type": "item.completed",
         "item": {"id": "msg1", "type": "agent_message",
                  "text": "The repo is openai/codex."}},
    ])

    turns = codex_transcript.parse_turns(f)

    assert len(turns) == 2
    assert turns[0]["display_cells"][0]["kind"] == "web_search"
    assert turns[0]["display_cells"][0]["title"] == "Searched"
    assert turns[1]["text"] == "The repo is openai/codex."


def test_parse_turns_tool_before_any_assistant_creates_holder(tmp_path):
    """A function_call arriving before any agent_message still needs a
    turn to live on — the parser synthesises an empty assistant turn."""
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "c9", "arguments": "{\"cmd\":\"pwd\"}"}},
    ])
    turns = codex_transcript.parse_turns(f)
    assert len(turns) == 1
    assert turns[0]["role"] == "assistant"
    assert turns[0]["tools"][0]["name"] == "Bash"


def test_parse_turns_modern_item_events_emit_codex_display_cells(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "thread.started", "thread_id": "s1"},
        {"timestamp": "t1", "type": "item.started",
         "item": {"id": "cmd1", "type": "command_execution",
                  "command": "bash -lc 'rg Change Approved server tests'"}},
        {"timestamp": "t2", "type": "item.completed",
         "item": {"id": "cmd1", "type": "command_execution",
                  "command": "bash -lc 'rg Change Approved server tests'",
                  "aggregated_output": "one\ntwo\nthree\nfour\nfive\nsix\nseven",
                  "exit_code": 0}},
        {"timestamp": "t3", "type": "item.completed",
         "item": {"id": "mcp1", "type": "mcp_tool_call",
                  "server": "search", "tool": "find_docs",
                  "arguments": {"query": "ratatui styling"},
                  "result": "Found styling guidance"}},
        {"timestamp": "t4", "type": "item.completed",
         "item": {"id": "msg1", "type": "agent_message",
                  "text": "Done."}},
    ])

    turns = codex_transcript.parse_turns(f)

    first = turns[0]["display_cells"]
    assert first[0]["kind"] == "exploration"
    assert first[0]["title"] == "Explored"
    assert first[0]["lines"][0]["label"] == "Search"
    assert first[1]["kind"] == "mcp"
    assert first[1]["title"] == "Called"
    assert first[1]["summary"].startswith("search.find_docs(")
    assert first[1]["lines"][0]["text"] == "Found styling guidance"
    assert turns[-1]["text"] == "Done."


def test_parse_turns_official_rollout_thread_items_keep_semantic_cards(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "UserMessage", "id": "u1",
                "content": [{"type": "text", "text": "Fix it"}],
            }}},
        {"timestamp": "t1", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "CommandExecution", "id": "cmd1",
                "command": ["/bin/bash", "-lc", "npm test"],
                "cwd": "file:///repo", "status": "completed",
                "stdout": "12 passed", "exit_code": 0,
            }}},
        {"timestamp": "t2", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "FileChange", "id": "edit1", "status": "completed",
                "changes": {"src/app.py": {
                    "type": "update", "unified_diff": "@@ -1 +1 @@\n-old\n+new",
                }},
            }}},
        {"timestamp": "t3", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "McpToolCall", "id": "mcp1", "server": "github",
                "tool": "issues.read", "status": "completed",
                "arguments": {"number": 42}, "result": "Issue 42",
            }}},
        {"timestamp": "t4", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "DynamicToolCall", "id": "dyn1", "tool": "lookup",
                "status": "completed", "success": True,
                "contentItems": [{"type": "inputText", "text": "found"}],
            }}},
        {"timestamp": "t5", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "WebSearch", "id": "web1", "query": "Codex docs",
            }}},
        {"timestamp": "t6", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "ImageView", "id": "img1", "path": "/tmp/result.png",
            }}},
        {"timestamp": "t7", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "Plan", "id": "plan1", "text": "1. Inspect\n2. Fix",
            }}},
        {"timestamp": "t8", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "Reasoning", "id": "why1",
                "summary_text": ["Compared both implementations"],
            }}},
        {"timestamp": "t9", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "AgentMessage", "id": "a1", "phase": "final_answer",
                "content": [{"type": "Text", "text": "Fixed."}],
            }}},
    ])

    turns = codex_transcript.parse_turns(f)
    assert turns[0]["role"] == "user"
    tool_turn = next(turn for turn in turns if turn.get("display_cells"))
    cells = tool_turn["display_cells"]
    assert [cell["kind"] for cell in cells] == [
        "command", "patch", "mcp", "tool", "web_search", "image", "plan", "reasoning",
    ]
    assert cells[0]["title"] == "Ran"
    assert cells[0]["summary"] == "npm test"
    assert cells[1]["title"] == "Edited"
    assert cells[1]["lines"][0]["text"] == "src/app.py"
    assert cells[2]["summary"].startswith("github.issues.read(")
    assert turns[-1]["text"] == "Fixed."
    assert turns[-1]["kind"] == "final_answer"


def test_structured_command_supersedes_outer_exec_wrapper_in_same_turn(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "outer-1",
            "input": 'const r = await tools.exec_command({"cmd":"pytest -q"});',
            "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
        }},
        {"timestamp": "t1", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "CommandExecution", "id": "cmd-1",
                "command": ["bash", "-lc", "pytest -q"],
                "status": "completed", "stdout": "12 passed", "exit_code": 0,
            }}},
    ])

    cells = codex_transcript.parse_turns(f)[0]["display_cells"]
    assert [(cell["kind"], cell["title"], cell["summary"]) for cell in cells] == [
        ("command", "Ran", "pytest -q")
    ]


def test_structured_dedupe_keeps_unmatched_legacy_command_in_same_turn(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "outer-1",
            "input": 'const r = await tools.exec_command({"cmd":"pytest -q"});',
            "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
        }},
        {"timestamp": "t1", "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "CommandExecution", "id": "cmd-1",
                "command": ["bash", "-lc", "pytest -q"],
                "status": "completed", "stdout": "12 passed", "exit_code": 0,
            }}},
        {"timestamp": "t2", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "outer-2",
            "input": 'const r = await tools.exec_command({"cmd":"ruff check"});',
            "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
        }},
    ])

    cells = codex_transcript.parse_turns(f)[0]["display_cells"]
    assert [(cell["title"], cell["summary"]) for cell in cells] == [
        ("Ran", "pytest -q"), ("Running", "ruff check"),
    ]


def test_outer_exec_fallback_recognizes_nested_operation_without_thread_item(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [{
        "timestamp": "t0", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "outer-1",
            "input": 'const r = await tools.exec_command({"cmd":"pytest -q"});',
        },
    }])

    cell = codex_transcript.parse_turns(f)[0]["display_cells"][0]
    assert cell["kind"] == "command"
    assert cell["title"] == "Running"
    assert cell["summary"] == "pytest -q"


def test_outer_exec_understands_javascript_keys_and_content_block_output(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "outer-1",
            "input": 'const r = await tools.exec_command({cmd:"pytest -q"});',
        }},
        {"timestamp": "t1", "type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "outer-1",
            "output": [
                {"type": "input_text", "text": "Script completed\nWall time 1s\nOutput:\n"},
                {"type": "input_text", "text": "24 passed\n"},
            ],
        }},
    ])

    cell = codex_transcript.parse_turns(f)[0]["display_cells"][0]
    assert (cell["kind"], cell["title"], cell["summary"]) == (
        "command", "Ran", "pytest -q",
    )
    assert cell["lines"][0]["text"] == "24 passed"


def test_nested_write_stdin_is_continuation_not_generic_tool(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [{
        "timestamp": "t0", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "outer-1",
            "input": "const r = await tools.write_stdin({session_id:57146});",
        },
    }])

    cell = codex_transcript.parse_turns(f)[0]["display_cells"][0]
    assert (cell["kind"], cell["title"]) == ("status", "Continuing command")


def test_patch_event_supersedes_outer_apply_patch_wrapper(tmp_path):
    f = tmp_path / "rollout.jsonl"
    turn_id = "turn-1"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "outer-1",
            "input": "const patch = '*** Begin Patch'; await tools.apply_patch(patch);",
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        }},
        {"timestamp": "t1", "type": "event_msg", "payload": {
            "type": "patch_apply_end", "turn_id": turn_id, "call_id": "patch-1",
            "success": True, "changes": {
                "app.py": {"type": "update", "unified_diff": "+fixed"},
            },
        }},
    ])

    cells = codex_transcript.parse_turns(f)[0]["display_cells"]
    assert [(cell["kind"], cell["title"]) for cell in cells] == [("patch", "Edited")]


def test_legacy_function_tools_render_semantic_cards_not_called_generic(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "response_item", "payload": {
            "type": "function_call", "name": "update_plan", "call_id": "plan-1",
            "arguments": json.dumps({"plan": [
                {"step": "Inspect parser", "status": "completed"},
                {"step": "Implement cards", "status": "in_progress"},
            ]}),
        }},
        {"timestamp": "t1", "type": "response_item", "payload": {
            "type": "function_call", "name": "wait", "call_id": "wait-1",
            "arguments": json.dumps({"cell_id": "81"}),
        }},
        {"timestamp": "t2", "type": "response_item", "payload": {
            "type": "function_call", "namespace": "web", "name": "run",
            "call_id": "web-1", "arguments": json.dumps({
                "search_query": [{"q": "official Codex app server"}],
            }),
        }},
        {"timestamp": "t3", "type": "response_item", "payload": {
            "type": "function_call", "name": "view_image", "call_id": "img-1",
            "arguments": json.dumps({"path": "/tmp/screenshot.png"}),
        }},
    ])

    cells = codex_transcript.parse_turns(f)[0]["display_cells"]
    assert [(cell["kind"], cell["title"]) for cell in cells] == [
        ("plan", "Updated plan"),
        ("status", "Waiting"),
        ("web_search", "Searching"),
        ("image", "Viewing image"),
    ]
    assert cells[0]["lines"][1]["label"] == "In Progress"
    assert cells[2]["summary"] == "official Codex app server"


def test_parse_turns_spawn_agent_tool_emits_subagent_cell(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "response_item",
         "payload": {"type": "function_call", "name": "spawn_agent",
                     "namespace": "multi_agent_v1",
                     "call_id": "spawn-1",
                     "arguments": json.dumps({
                         "agent_type": "explore",
                         "message": "Inspect parser support for subagents",
                     })}},
        {"timestamp": "t1", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "spawn-1",
                     "output": json.dumps({
                         "agent_id": "019ef6d2-d6a2-7d43-8b58-43f429676f96",
                         "nickname": "Hegel",
                     })}},
    ])

    cell = codex_transcript.parse_turns(f)[0]["display_cells"][0]

    assert cell["kind"] == "subagents"
    assert cell["title"] == "Spawned agent"
    assert cell["summary"] == "Hegel"
    assert cell["status"] == "ok"
    assert cell["lines"][0]["label"] == "Task"
    assert "Inspect parser" in cell["lines"][0]["text"]
    assert cell["lines"][1]["label"] == "Thread"


def test_parse_turns_modern_collab_tool_call_emits_subagent_statuses(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "item.started",
         "item": {"id": "wait-1",
                  "details": {
                      "type": "collabToolCall",
                      "tool": "wait",
                      "status": "inProgress",
                      "senderThreadId": "parent",
                      "receiverThreadIds": ["child-a", "child-b"],
                      "agentsStates": {},
                  }}},
        {"timestamp": "t1", "type": "item.completed",
         "item": {"id": "wait-1",
                  "details": {
                      "type": "collabToolCall",
                      "tool": "wait",
                      "status": "completed",
                      "senderThreadId": "parent",
                      "receiverThreadIds": ["child-a", "child-b"],
                      "agentsStates": {
                          "child-a": {"status": "completed",
                                      "message": "Parser done"},
                          "child-b": {"status": "errored",
                                      "message": "tool timeout"},
                      },
                  }}},
    ])

    cell = codex_transcript.parse_turns(f)[0]["display_cells"][0]

    assert cell["kind"] == "subagents"
    assert cell["title"] == "Finished waiting"
    assert cell["status"] == "ok"
    assert [line["label"] for line in cell["lines"]] == ["child-a", "child-b"]
    assert cell["lines"][0]["text"] == "Completed - Parser done"
    assert cell["lines"][1]["text"] == "Errored - tool timeout"


def test_parse_turns_protocol_collab_events_without_payload(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0",
         "type": "collab_agent_spawn_begin",
         "call_id": "spawn-2",
         "sender_thread_id": "parent",
         "prompt": "Review the Swift cell",
         "model": "gpt-5.4",
         "reasoning_effort": "high"},
        {"timestamp": "t1",
         "type": "collab_agent_spawn_end",
         "call_id": "spawn-2",
         "sender_thread_id": "parent",
         "new_thread_id": "child-c",
         "new_agent_nickname": "Socrates",
         "new_agent_role": "designer",
         "prompt": "Review the Swift cell",
         "model": "gpt-5.4",
         "reasoning_effort": "high",
         "status": {"completed": "Looks good"}},
    ])

    cell = codex_transcript.parse_turns(f)[0]["display_cells"][0]

    assert cell["kind"] == "subagents"
    assert cell["title"] == "Spawned agent"
    assert cell["summary"] == "Socrates [designer]"
    assert cell["status"] == "ok"
    assert cell["lines"][0]["label"] == "Task"
    assert cell["lines"][1]["text"] == "child-c"


def test_command_display_cell_truncates_long_non_exploration_output(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t1", "type": "item.completed",
         "item": {"id": "cmd1", "type": "command_execution",
                  "command": "npm test",
                  "aggregated_output": "\n".join(f"line {i}" for i in range(8)),
                  "exit_code": 1}},
    ])

    turns = codex_transcript.parse_turns(f)
    cell = turns[0]["display_cells"][0]

    assert cell["kind"] == "command"
    assert cell["title"] == "Ran"
    assert cell["status"] == "error"
    assert any(line["kind"] == "omitted" for line in cell["lines"])


def test_command_display_cell_caps_very_long_output_lines(tmp_path):
    long_ps_line = (
        "user 812699 145641 0 23:23 ? 00:00:00 "
        "node /home/example/.local/share/mise/installs/node/25.2.1/bin/"
        "codex exec --json --dangerously-bypass-approvals-and-sandbox "
        + "[voice-mode] " * 180
    )
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t1", "type": "item.completed",
         "item": {"id": "cmd1", "type": "command_execution",
                  "command": "ps -ef | rg claude-pwa",
                  "aggregated_output": long_ps_line,
                  "exit_code": 0}},
    ])

    turns = codex_transcript.parse_turns(f)
    line = turns[0]["display_cells"][0]["lines"][0]["text"]

    assert len(line) <= codex_transcript.TOOL_OUTPUT_CHAR_LIMIT
    assert line.endswith("…")
    assert "[voice-mode]" in line


def test_huge_diff_cell_is_capped_by_backstop(tmp_path):
    """A file_change with a giant diff must not ship an unbounded cell — the
    per-cell backstop caps line count + bytes and marks the truncation. The
    diff-card path builds one display line per raw line with no cap of its own,
    so this is the belt-and-braces net."""
    big_patch = (
        "*** Begin Patch\n*** Add File: gen.py\n"
        + "".join(f"+line {i} " + "x" * 50 + "\n" for i in range(5000))
        + "*** End Patch\n"
    )
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "generating"}},
        {"timestamp": "t1", "type": "item.completed",
         "item": {"id": "fc1", "type": "file_change", "patch": big_patch}},
    ])
    cell = codex_transcript.parse_turns(f)[0]["display_cells"][0]

    assert cell["kind"] == "patch"
    assert len(cell["lines"]) <= codex_transcript.CELL_LINE_COUNT_HARD_LIMIT + 1
    assert cell["lines"][-1]["kind"] == "omitted"
    assert len(json.dumps(cell)) <= codex_transcript.CELL_BYTES_HARD_LIMIT + 2048


def test_single_giant_line_is_hard_capped(tmp_path):
    """Even a path that bypasses _preview_output (a one-line minified diff)
    can't emit a multi-megabyte display line."""
    giant = "Z" * 3_000_000
    big_patch = (
        "*** Begin Patch\n*** Add File: bundle.js\n+" + giant + "\n*** End Patch\n"
    )
    f = tmp_path / "rollout.jsonl"
    _write_rollout(f, [
        {"timestamp": "t0", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "x"}},
        {"timestamp": "t1", "type": "item.completed",
         "item": {"id": "fc2", "type": "file_change", "patch": big_patch}},
    ])
    cell = codex_transcript.parse_turns(f)[0]["display_cells"][0]

    assert max(len(line["text"]) for line in cell["lines"]) <= \
        codex_transcript.CELL_LINE_CHAR_HARD_LIMIT
    assert len(json.dumps(cell)) <= codex_transcript.CELL_BYTES_HARD_LIMIT + 2048


def test_list_sessions_filters_by_cwd(tmp_path):
    root = tmp_path / "sessions" / "2026" / "05" / "31"
    root.mkdir(parents=True)
    # Two sessions in the wanted cwd, one in another dir.
    def _session(name, sid, cwd, prompt):
        _write_rollout(root / name, [
            {"timestamp": "2026-05-31T10:00:00Z", "type": "session_meta",
             "payload": {"id": sid, "cwd": cwd}},
            {"timestamp": "2026-05-31T10:00:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": f"› {prompt}"}},
        ])
    _session("rollout-a-uuid-aaa.jsonl", "uuid-aaa", "/home/example/proj", "first task")
    _session("rollout-b-uuid-bbb.jsonl", "uuid-bbb", "/home/example/proj", "second task")
    _session("rollout-c-uuid-ccc.jsonl", "uuid-ccc", "/home/example/other", "elsewhere")

    got = codex_transcript.list_sessions(
        "/home/example/proj", sessions_root=tmp_path / "sessions")
    ids = {s["id"] for s in got}
    assert ids == {"uuid-aaa", "uuid-bbb"}, f"cwd filter wrong: {ids}"
    # Preview is the cleaned first user message.
    previews = {s["preview"] for s in got}
    assert "first task" in previews and "second task" in previews
    # Non-matching cwd → empty.
    assert codex_transcript.list_sessions(
        "/nope", sessions_root=tmp_path / "sessions") == []


def test_list_sessions_reads_renamed_threads_across_projects(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    rollouts = []
    for sid in ("uuid-a", "uuid-b"):
        path = root / f"rollout-{sid}.jsonl"
        path.write_text("{}\n")
        rollouts.append(path)
    _write_rollout(root / "rollout-uuid-c.jsonl", [
        {"type": "session_meta", "payload": {"id": "uuid-c", "cwd": "/work/three"}},
        {"type": "event_msg", "payload": {
            "type": "user_message", "message": "Unindexed session"}},
    ])
    state = tmp_path / "state.sqlite"
    with sqlite3.connect(state) as conn:
        conn.execute("""CREATE TABLE threads(
            id TEXT,cwd TEXT,name TEXT,title TEXT,preview TEXT,
            first_user_message TEXT,updated_at INTEGER,updated_at_ms INTEGER,
            rollout_path TEXT,has_user_event INTEGER,recency_at_ms INTEGER)""")
        conn.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("uuid-a", "/work/one", "Renamed session", "Generated",
             "Preview one", "First one", 10, 10_000, str(rollouts[0]), 1, 10_000))
        conn.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("uuid-b", "/work/two", None, "Second title",
             "", "First two", 20, 20_000, str(rollouts[1]), 1, 20_000))

    scoped = codex_transcript.list_sessions(
        "/work/one", sessions_root=root, state_db=state)
    assert scoped == [{
        "id": "uuid-a", "mtime": 10, "preview": "Preview one",
        "title": "Renamed session", "cwd": "/work/one",
    }]
    all_sessions = codex_transcript.list_sessions(
        "/work/one", sessions_root=root, state_db=state, all_projects=True)
    assert {item["id"] for item in all_sessions} == {"uuid-a", "uuid-b", "uuid-c"}
    assert next(item for item in all_sessions if item["id"] == "uuid-b")["title"] \
        == "Second title"


def test_find_latest_jsonl_matches_uuid_suffix(tmp_path):
    root = tmp_path / "sessions" / "2026" / "05" / "31"
    root.mkdir(parents=True)
    target = root / "rollout-2026-05-31T10-00-00-abc-123-uuid.jsonl"
    target.write_text("{}\n")
    (root / "rollout-2026-05-31T09-00-00-other-uuid.jsonl").write_text("{}\n")

    hit = codex_transcript.find_latest_jsonl(
        "abc-123-uuid", sessions_root=tmp_path / "sessions")
    assert hit == target
    assert codex_transcript.find_latest_jsonl(
        "nonexistent", sessions_root=tmp_path / "sessions") is None
    assert codex_transcript.find_latest_jsonl("") is None
