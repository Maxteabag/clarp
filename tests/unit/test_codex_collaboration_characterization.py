"""Recorded collaboration variants must retain their native display cells.

Synthetic rollout records exercise the public parser before the cell split.
The existing corpus covers spawning; these cases own interaction, waiting,
closing/resuming, activity notices and the legacy tool-result variants.
"""
import json

import pytest

from lib import codex_transcript


def _parse_cells(tmp_path, events):
    path = tmp_path / "synthetic-rollout.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    turns = codex_transcript.parse_turns(path)
    return [cell for turn in turns for cell in turn.get("display_cells", [])]


@pytest.mark.parametrize("event,payload,title,status,summary,lines", [
    ("collab_agent_interaction_begin",
     {"receiver_thread_id": "child-1", "receiver_agent_role": "reviewer", "prompt": "  Check\n ownership  "},
     "Sending input to agent", "running", "[reviewer]", [("Input", "Check ownership", "detail")]),
    ("collab_agent_interaction_end",
     {"receiver_thread_id": "child-1", "receiver_agent_nickname": "Ada", "status": {"errored": "refused"}},
     "Sent input to agent", "error", "Ada", []),
    ("collab_waiting_begin",
     {"receiver_thread_ids": ["child-a", "child-b"],
      "receiver_agents": [{"agentNickname": "Ada", "agentRole": "reviewer"}]},
     "Waiting for agents", "running", "2 agents",
     [("Ada [reviewer]", "child-a", "muted"), ("child-b", "child-b", "muted")]),
    ("collab_waiting_begin", {"receiver_thread_ids": "child-z"},
     "Waiting for agents", "running", "1 agent", [("child-z", "child-z", "muted")]),
    ("collab_waiting_end",
     {"statuses": {"child-b": {"errored": "  retry\n later "}, "child-a": "completed"}},
     "Finished waiting", "error", "2 agents",
     [("child-a", "Completed", "status"), ("child-b", "Errored - retry later", "status")]),
    ("collab_waiting_end",
     {"agentStatuses": [None,
         {"threadId": "child-a", "agentRole": "reviewer", "status": {"completed": " fixed\n result "}},
         {"thread_id": "abcdefghijk", "status": None}]},
     "Finished waiting", "ok", "2 agents",
     [("[reviewer]", "Completed - fixed result", "status"), ("abcdefgh", "Recorded", "status")]),
    ("collab_close_begin", {"receiver_thread_id": "child-1", "receiver_agent_nickname": "Ada"},
     "Closing agent", "running", "Ada", [("Thread", "child-1", "muted")]),
    ("collab_close_end", {"receiver_thread_id": "child-1", "status": {"shutdown": None}},
     "Closed agent", "ok", "child-1", [("Thread", "child-1", "muted")]),
    ("collab_resume_begin", {"receiver_agent_role": "reviewer"},
     "Resuming agent", "running", "[reviewer]", []),
    ("collab_resume_end", {"receiver_thread_id": "child-1", "status": "pending_init"},
     "Resumed agent", "running", "child-1", [("Thread", "child-1", "muted")]),
    ("sub_agent_activity", {"kind": "started", "agent_path": "root/helper"},
     "Started subagent", "ok", "root/helper", []),
    ("sub_agent_activity", {"kind": "interacted", "agentPath": "root/helper"},
     "Interacted with subagent", "ok", "root/helper", []),
    ("sub_agent_activity", {"kind": "interrupted", "agent_path": "root/helper"},
     "Interrupted subagent", "error", "root/helper", []),
    ("sub_agent_activity", {"kind": "notice", "agent_path": "root/helper"},
     "Subagent activity", "ok", "root/helper", []),
])
def test_protocol_collaboration_retains_identity_state_and_lines(
        tmp_path, event, payload, title, status, summary, lines):
    cells = _parse_cells(tmp_path, [
        {"type": event, "call_id": "call-1", "timestamp": "t1", **payload},
    ])
    assert len(cells) == 1
    cell = cells[0]
    assert (cell["id"], cell["kind"], cell["title"], cell["status"], cell["summary"]) == (
        "call-1", "subagents", title, status, summary)
    assert [(line.get("label", ""), line["text"], line["kind"]) for line in cell["lines"]] == lines


@pytest.mark.parametrize("tool,arguments,output,title,status,summary,last_line", [
    ("wait_agent", {"target": "child-a"}, {"status": {"errored": "job failed"}},
     "Finished waiting", "error", "1 agent", ("Status", "Errored - job failed")),
    ("wait_agent", {"targets": ["child-a", "child-b"]}, {"status": "running"},
     "Finished waiting", "running", "2 agents", ("Status", "Running")),
    ("send_input", {"target": "target-12345", "message": " Check\n ownership "}, {"completed": "accepted"},
     "Sent input to agent", "ok", "target-1", ("Result", "accepted")),
    ("resume_agent", {"id": "target-12345"}, "  plain\n reply ",
     "Resumed agent", "ok", "target-1", ("Result", "plain reply")),
    ("close_agent", {"target": "target-12345"}, {"status": None},
     "Closed agent", "recorded", "target-1", ("Status", "Recorded")),
    ("close_agent", {}, {}, "Closed agent", "ok", "agent", ("Result", "{}")),
    ("close_agent", {}, "", "Closed agent", "ok", "agent", None),
])
def test_legacy_collaboration_tool_result_updates_its_existing_cell(
        tmp_path, tool, arguments, output, title, status, summary, last_line):
    cells = _parse_cells(tmp_path, [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": tool, "call_id": "call-1",
            "arguments": json.dumps(arguments)}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "call-1",
            "output": output if isinstance(output, str) else json.dumps(output)}},
    ])
    assert len(cells) == 1  # the output updates, rather than duplicates, the call
    cell = cells[0]
    assert (cell["id"], cell["title"], cell["status"], cell["summary"]) == (
        "call-1", title, status, summary)
    if last_line is None:
        assert cell["lines"] == []
    else:
        assert (cell["lines"][-1]["label"], cell["lines"][-1]["text"]) == last_line


@pytest.mark.parametrize("states", [["malformed state"], {}])
def test_modern_failed_collaboration_keeps_receiver_without_agent_states(tmp_path, states):
    cells = _parse_cells(tmp_path, [{
        "type": "item.completed", "item": {
            "id": "modern-1", "type": "collab_agent_tool_call", "tool": "close_agent",
            "status": "failed", "receiver_thread_ids": "thread-12345", "agents_states": states,
        },
    }])
    assert len(cells) == 1
    cell = cells[0]
    assert (cell["id"], cell["title"], cell["status"], cell["summary"]) == (
        "modern-1", "Closed agent", "error", "thread-1")
    assert cell["lines"] == [{"text": "thread-12345", "label": "Agent", "kind": "muted"}]
