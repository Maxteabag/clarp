import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.protocol import AgentState, ClientAction, ClipProducerStatus, ClipStatus, SSEType


def test_protocol_strings_match_wire_contract():
    assert SSEType.AGENT_STATE == "agent-state"
    assert SSEType.AGENT_ROSTER == "agent-roster"
    assert SSEType.AGENT_FOCUS == "agent-focus"
    assert SSEType.REMOTE_ACTION == "remote-action"
    assert SSEType.SERVER_VERSION == "server-version"
    assert SSEType.USER_NOTIFICATION == "user-notification"

    assert AgentState.busy_states() == {"thinking", "tool", "compacting"}
    assert ClientAction.valid() == {"record", "record-toggle", "stop-agent"}
    assert ClipStatus.valid() == {
        "synthesized", "broadcast", "queued", "held",
        "play-start", "play-ok", "play-fail",
    }
    assert ClipProducerStatus.valid() == {"streaming", "complete", "failed"}


def test_agent_state_rejects_unknown_kind():
    assert AgentState.is_valid("thinking") is True
    assert AgentState.is_valid("surprised") is False


def test_interrupted_is_a_valid_non_busy_state():
    assert AgentState.INTERRUPTED == "interrupted"
    assert AgentState.is_valid("interrupted") is True
    assert "interrupted" not in AgentState.busy_states()


def test_latest_state_breaks_same_millisecond_ties_by_insert_order():
    from lib import agents as agents_db

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd="/tmp", session="claude")
    agents_db.record_state(agent_id, AgentState.THINKING)
    agents_db.record_state(agent_id, AgentState.IDLE)

    assert agents_db.latest_state(agent_id)["kind"] == AgentState.IDLE
