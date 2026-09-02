"""Tests for the canonical session-id binding API.

bind_backend_session is the only function allowed to write
runtimes.backend_session_id. It enforces "one agent ↔ one live UUID":
attempts to bind a UUID that another live runtime already owns
raise SessionAlreadyBound rather than silently overwriting.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import agents as agents_db  # noqa: E402


def _agent(persona: str, session: str, cwd: str = "/home/example") -> str:
    agent_id = agents_db.create_agent(
        persona=persona, voice_id="v", cwd=cwd, session=session,
    )
    agents_db.start_runtime(agent_id, session)
    return agent_id


def test_binds_a_fresh_agent():
    agent_id = _agent("Mike", "claude")
    agents_db.bind_backend_session(agent_id, "uuid-mike")
    assert agents_db.live_backend_session(agent_id) == "uuid-mike"


def test_idempotent_on_same_uuid():
    """Setting the same UUID twice is a no-op (no exception, same value)."""
    agent_id = _agent("Mike", "claude")
    agents_db.bind_backend_session(agent_id, "uuid-mike")
    agents_db.bind_backend_session(agent_id, "uuid-mike")
    assert agents_db.live_backend_session(agent_id) == "uuid-mike"


def test_rebinding_same_agent_to_new_uuid_works():
    """Agent moves to a new conversation — bind_backend_session updates the UUID."""
    agent_id = _agent("Mike", "claude")
    agents_db.bind_backend_session(agent_id, "uuid-one")
    agents_db.bind_backend_session(agent_id, "uuid-two")
    assert agents_db.live_backend_session(agent_id) == "uuid-two"


def test_cross_binding_two_agents_to_same_uuid_raises():
    """The regression test for the cross-bind bleed: two agents
    cannot share a live backend_session_id UUID. The partial unique
    index turns this into SessionAlreadyBound."""
    mike = _agent("Mike", "claude")
    arnold = _agent("Arnold", "arnold")
    agents_db.bind_backend_session(mike, "shared-uuid")
    with pytest.raises(agents_db.SessionAlreadyBound) as excinfo:
        agents_db.bind_backend_session(arnold, "shared-uuid")
    assert excinfo.value.owner_agent_id == mike
    assert excinfo.value.agent_id == arnold
    # Arnold's runtime must NOT have been mutated.
    assert agents_db.live_backend_session(arnold) == ""


def test_after_runtime_ends_other_agent_can_claim_uuid():
    """When agent A's runtime ends (relaunch / stop), the partial
    unique index excludes it. Agent B can then bind the same UUID
    without conflict."""
    mike = _agent("Mike", "claude")
    arnold = _agent("Arnold", "arnold")
    agents_db.bind_backend_session(mike, "shared-uuid")
    # End Mike's live runtime (simulates a relaunch / stop).
    agents_db.end_current_runtime(mike)
    # Now Arnold can claim that UUID — Mike's no longer holds it live.
    agents_db.bind_backend_session(arnold, "shared-uuid")
    assert agents_db.live_backend_session(arnold) == "shared-uuid"


def test_empty_uuid_raises_value_error():
    agent_id = _agent("Mike", "claude")
    with pytest.raises(ValueError):
        agents_db.bind_backend_session(agent_id, "")


def test_empty_agent_id_raises_value_error():
    with pytest.raises(ValueError):
        agents_db.bind_backend_session("", "some-uuid")


def test_unknown_agent_id_raises_value_error():
    with pytest.raises(ValueError):
        agents_db.bind_backend_session("ghost-agent-id", "some-uuid")
