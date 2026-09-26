"""identity.resolve(): every input shape lands on the same AgentRef."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import agents as agents_db  # noqa: E402
from lib import identity  # noqa: E402
from lib.identity import AgentRef, TurnRef  # noqa: E402


def _agent(session="mike", persona="Mike"):
    return agents_db.create_agent(persona=persona, voice_id="V", cwd="/tmp",
                                  session=session)


def test_resolve_accepts_every_identity_shape():
    agent_id = _agent()
    expected = AgentRef(agent_id=agent_id, session="mike")
    row = agents_db.get_by_session("mike")
    assert identity.resolve("mike") == expected
    assert identity.resolve(agent_id) == expected
    assert identity.resolve(row) == expected
    assert identity.resolve(expected) is expected
    assert identity.resolve({"agent_id": agent_id}) == expected  # refreshed by id


def test_resolve_normalises_whitespace_and_rejects_empty():
    agent_id = _agent()
    assert identity.resolve("  mike \n") == AgentRef(agent_id, "mike")
    assert identity.resolve(f" {agent_id} ") == AgentRef(agent_id, "mike")
    for empty in ("", "   ", None, {}, {"agent_id": ""}, {"session": "mike"}):
        assert identity.resolve(empty) is None
    assert identity.resolve(42) is None


def test_unknown_and_deleted_agents_resolve_to_none():
    agent_id = _agent()
    assert identity.resolve("nobody") is None
    assert identity.resolve("0" * 16) is None
    agents_db.soft_delete(agent_id)
    assert identity.resolve("mike") is None
    assert identity.resolve(agent_id) is None
    assert identity.resolve(AgentRef(agent_id, "mike")) == AgentRef(agent_id, "mike")
    assert identity.lookup(AgentRef(agent_id, "mike")) is None


def test_lookup_returns_the_row_and_trusts_a_complete_row():
    agent_id = _agent()
    row = identity.lookup("mike")
    assert row["agent_id"] == agent_id and row["persona"] == "Mike"
    assert identity.lookup(agent_id)["session"] == "mike"
    stale = {"agent_id": agent_id, "session": "mike", "persona": "Renamed"}
    assert identity.lookup(stale) is stale


def test_agent_id_shaped_session_names_still_resolve():
    hex_session = "abcdef0123456789"
    agent_id = _agent(session=hex_session)
    assert identity.resolve(hex_session) == AgentRef(agent_id, hex_session)
    assert identity.looks_like_agent_id(hex_session)
    assert not identity.looks_like_agent_id("mike")


def test_backend_session_and_hook_resolution():
    agent_id = _agent()
    assert identity.backend_session("mike") == ""
    assert identity.resolve_backend_session("uuid-1") is None
    agents_db.start_runtime(agent_id, "mike")
    agents_db.bind_backend_session(agent_id, "uuid-1")
    ref = AgentRef(agent_id, "mike")
    assert identity.backend_session("mike") == "uuid-1"
    assert identity.backend_session(ref) == "uuid-1"
    assert identity.backend_session("nobody") == ""
    assert identity.resolve_backend_session(" uuid-1 ") == ref
    assert identity.resolve_hook(backend_session_id="uuid-1", session=None) == ref
    assert identity.resolve_hook(backend_session_id="", session="mike") == ref
    assert identity.resolve_hook(backend_session_id="other", session="mike") == ref
    assert identity.resolve_hook(backend_session_id=None, session=None) is None


def test_turn_ref_binds_trace_to_the_current_runtime():
    agent_id = _agent()
    assert identity.turn_ref("mike", "not a trace") is None
    assert identity.turn_ref("nobody", "0123456789abcdef") is None
    turn = identity.turn_ref("mike", "0123456789ABCDEF")
    assert turn == TurnRef(agent=AgentRef(agent_id, "mike"),
                           trace_id="0123456789abcdef", turn_id=None,
                           runtime_id=None, backend_session_id="")
    runtime_id = agents_db.start_runtime(agent_id, "mike")
    agents_db.bind_backend_session(agent_id, "uuid-2")
    turn = identity.turn_ref(AgentRef(agent_id, "mike"), "0123456789abcdef", turn_id=5)
    assert (turn.runtime_id, turn.backend_session_id, turn.turn_id) == (runtime_id, "uuid-2", 5)
    assert (turn.agent_id, turn.session) == (agent_id, "mike")


def test_refs_are_frozen_and_hashable():
    ref = AgentRef("a", "s")
    assert {ref: 1}[AgentRef("a", "s")] == 1
    try:
        ref.session = "other"
    except AttributeError:
        pass
    else:
        raise AssertionError("AgentRef must be frozen")
