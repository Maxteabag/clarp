"""R43: an agent's first turn that ends before the Host records that it began is not left busy.

A very fast backend finishes the turn inside spawn_turn. On a new agent, the end came while the
agent was still SPAWNED, where an ordinary turn end was refused; the dispatcher then wrote its
late THINKING, and the agent showed busy with no process. An end that names the turn the agent
has a row for is taken from SPAWNED, and the late THINKING is then not written."""
import pytest

from lib import agents as agents_db
from lib import turn_lifecycle
from lib.protocol import AgentState
from lib.turn_lifecycle import IllegalTransition, TurnEvent
from tests.unit.test_turn_dispatch import _Backends, _make_service


class _FinishesAtOnce(_Backends):
    def __init__(self, event):
        super().__init__()
        self.event = event

    def spawn_turn(self, backend, **kwargs):
        super().spawn_turn(backend, **kwargs)
        turn_lifecycle.try_transition(kwargs["agent_id"], self.event, {"trace_id": kwargs["trace_id"]})


@pytest.mark.parametrize("event, end", [(TurnEvent.PROCESS_EXITED_OK, AgentState.DONE),
                                        (TurnEvent.HOOK_STOP, AgentState.DONE),
                                        (TurnEvent.PROCESS_EXITED_FAILED, AgentState.INTERRUPTED)])
def test_a_first_turn_that_ends_inside_its_spawn_leaves_the_agent_at_rest(tmp_path, event, end):
    service, backends, agent_id = _make_service(tmp_path)
    service.backends = fast = _FinishesAtOnce(event)
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.SPAWNED
    service.dispatch(text="synthetic first words", requested_session="mike",
                     trace_id="t-first", client_msg_id="u-first")
    assert len(fast.spawned) == 1
    latest = agents_db.latest_state(agent_id)
    assert latest["kind"] == end and latest["detail"]["trace_id"] == "t-first"


def test_an_end_naming_no_turn_of_the_agent_is_still_refused_from_spawned(tmp_path):
    service, _backends, agent_id = _make_service(tmp_path)
    for detail in (None, {"trace_id": "t-unknown"}):
        with pytest.raises(IllegalTransition):
            turn_lifecycle.transition(agent_id, TurnEvent.PROCESS_EXITED_OK, detail)
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.SPAWNED
