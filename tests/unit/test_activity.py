import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))

from lib.activity import (  # noqa: E402
    state_activity_event,
    summarize_tool_activity,
    tool_input_from_hook_payload,
    tool_status_from_hook_payload,
)
from lib.protocol import ActivityStatus, AgentState, SSEType  # noqa: E402


def test_summarize_bash_prefers_description():
    out = summarize_tool_activity("Bash", {
        "command": "pytest tests/unit",
        "description": "run focused unit tests",
    })
    assert out == {
        "action": "running command",
        "summary": "run focused unit tests",
        "file_path": "",
    }


def test_summarize_edit_uses_short_path():
    out = summarize_tool_activity("Edit", {"file_path": "/home/example/GIT/app/static/app.js"})
    assert out["action"] == "editing file"
    assert out["summary"] == "static/app.js"
    assert out["file_path"].endswith("static/app.js")


def test_hook_payload_helpers_accept_current_claude_shapes():
    payload = {
        "tool": {"name": "Read", "input": {"file_path": "/tmp/a.py"}},
        "tool_response": {"is_error": True},
    }
    assert tool_input_from_hook_payload(payload) == {"file_path": "/tmp/a.py"}
    assert tool_status_from_hook_payload(payload) == ActivityStatus.ERROR


def test_state_activity_event_makes_waiting_readable():
    ev = state_activity_event(
        agent_id="a1",
        session="claude",
        persona="Mike",
        kind=AgentState.WAITING,
        ts=123,
        detail={"message": "Approve Bash?"},
    )
    assert ev["type"] == SSEType.AGENT_ACTIVITY
    assert ev["phase"] == "waiting"
    assert ev["status"] == ActivityStatus.ERROR
    assert ev["summary"] == "Approve Bash?"


def test_thinking_stays_thinking_without_an_account_park():
    ev = state_activity_event(
        agent_id="a1",
        session="claude",
        persona="Mike",
        kind=AgentState.THINKING,
        ts=123,
        detail={"dispatch": "claude", "trace_id": "t1"},
    )
    assert ev["phase"] == "thinking"
    assert ev["action"] == "thinking"
    assert ev["summary"] == "Thinking"
    assert ev["status"] == ActivityStatus.RUNNING


def test_account_parked_turn_is_not_presented_as_thinking():
    """A turn queued behind a provider limit must say so.

    It records THINKING to hold its place in dispatch, but it makes no
    progress, and "Thinking" made that indistinguishable from real work.
    """
    ev = state_activity_event(
        agent_id="a1",
        session="rachel",
        persona="Rachel",
        kind=AgentState.THINKING,
        ts=123,
        detail={
            "dispatch": "claude",
            "trace_id": "t1",
            "account_recovery": "waiting",
            "message": "Waiting for a Claude account with available usage",
        },
    )
    assert ev["phase"] == "account_recovery"
    assert ev["action"] == "waiting for account"
    assert ev["summary"] == "Waiting for a Claude account with available usage"
    assert ev["status"] == ActivityStatus.RUNNING


def test_account_parked_turn_without_a_message_still_explains_itself():
    ev = state_activity_event(
        agent_id="a1",
        session="rachel",
        persona="Rachel",
        kind=AgentState.THINKING,
        ts=123,
        detail={"account_recovery": "waiting"},
    )
    assert ev["phase"] == "account_recovery"
    assert ev["summary"] == "Waiting for an account with available usage"


def test_stop_and_repair_provenance_reach_the_activity_projection():
    # Contract 61: the snapshot row's activity and the agent-activity SSE
    # carry who stopped or repaired the agent, not only agent-state's detail.
    from lib import events
    stopped = state_activity_event(
        agent_id="a1", session="pebble", persona="Pebble",
        kind=AgentState.INTERRUPTED, ts=1,
        detail={"source": "user_stop", "message": "Turn stopped",
                "stop_actor": "agent:theo-97e5", "stop_actor_verified": False,
                "stop_reason": "busy with no process"})
    assert (stopped["stop_actor"], stopped["stop_actor_verified"],
            stopped["stop_reason"]) == ("agent:theo-97e5", False, "busy with no process")
    repaired = state_activity_event(
        agent_id="a1", session="pebble", persona="Pebble", kind=AgentState.DONE,
        ts=2, detail={"source": "slot_repair", "repair_actor": "user",
                      "repair_actor_verified": True, "repair_reason": "stuck",
                      "released_trace": "t1"})
    assert (repaired["repair_actor"], repaired["repair_actor_verified"],
            repaired["repair_reason"], repaired["released_trace"]) == (
        "user", True, "stuck", "t1")
    for event in (stopped, repaired):
        assert events.as_event(event)["type"] == SSEType.AGENT_ACTIVITY


def test_activity_without_provenance_omits_the_fields():
    # An older Stop (source user_stop alone) or any other state: absent, so a
    # client never reads an actor that was not recorded.
    for detail in ({"source": "user_stop", "message": "Turn stopped"}, {}):
        event = state_activity_event(agent_id="a1", session="s", persona="P",
                                     kind=AgentState.INTERRUPTED, ts=1, detail=detail)
        assert not {"stop_actor", "stop_actor_verified", "stop_reason",
                    "repair_actor"} & set(event)
