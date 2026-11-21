"""The owner's front desk relays the user's words; under 'hide' they are shown
(issue 481, U3 after issue 478's hidden coordination).

A message from the verified own turn of a session named in
[agents] front_desk_sessions carries the Host's user-word mark (lib.send_request,
lib.turn_queue). It stays another agent's message, with its sender, but its turn
is recorded 'mixed' (shown for good), and steered into a coordination turn it
shows that turn as the user's own word does. Another agent's message without the
mark is hidden as before.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import coordination
from lib import turn_dispatch as td

from test_drain_pause_parity import World


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield
    td.reset_for_tests()


def _desk(tmp_path):
    return agents_db.create_agent(persona="Desk", voice_id="V", cwd=str(tmp_path),
                                  session="desk", backend="claude")


def _write(world, cid, *, sender, user_word):
    session = agents_db.live_backend_session(world.agent) or "native"
    if not agents_db.live_backend_session(world.agent):
        agents_db.bind_backend_session(world.agent, session)
    return agents_db.record_user_message(
        agent_id=world.agent, backend_session_id=session, client_msg_id=cid, text=cid,
        origin="agent", sender_agent_id=sender, trace_id=f"t-{cid}", user_word=user_word)


def _audience(agent_id, trace_id):
    row = agents_db.conn().execute(
        "SELECT audience, sender_agent_id FROM coordination_traces WHERE agent_id = ? AND trace_id = ?",
        (agent_id, trace_id)).fetchone()
    return tuple(row) if row else None


def _visible(agent_id):
    return {row[0] for row in agents_db.conn().execute(
        f"SELECT trace_id FROM messages m WHERE m.agent_id = ? AND NOT {coordination.HIDDEN_ROW_SQL}",
        (agent_id,))}


def test_a_front_desk_relay_is_recorded_shown_and_another_agents_message_is_not(tmp_path):
    world = World(tmp_path)
    desk = _desk(tmp_path)
    _write(world, "relay", sender=desk, user_word=True)
    _write(world, "peer", sender=desk, user_word=False)
    # The relay is still another agent's message, with its sender, but shown.
    assert _audience(world.agent, "t-relay") == (coordination.MIXED, desk)
    plain = coordination.AGENTS if coordination.can_leave_note(world.agent) else coordination.MIXED
    assert _audience(world.agent, "t-peer") == (plain, desk)
    visible = _visible(world.agent)
    assert "t-relay" in visible
    if plain == coordination.AGENTS:
        assert "t-peer" not in visible
    row = agents_db.conn().execute(
        "SELECT origin, sender_agent_id FROM messages WHERE trace_id = 't-relay'").fetchone()
    assert tuple(row) == ("agent", desk)


def test_a_relay_written_under_a_trace_already_marked_shows_it(tmp_path):
    """A retry of the relay under a trace an earlier attempt marked 'agents'."""
    world = World(tmp_path)
    desk = _desk(tmp_path)
    database = agents_db.conn()
    database.execute(
        "INSERT INTO coordination_traces (agent_id, trace_id, sender_agent_id, audience, created_at, updated_at)"
        " VALUES (?, 't-again', ?, 'agents', 1, 1)", (world.agent, desk))
    _write(world, "again", sender=desk, user_word=True)
    assert _audience(world.agent, "t-again")[0] == coordination.MIXED


def test_a_relay_steered_into_a_coordination_turn_shows_that_turn(tmp_path):
    world = World(tmp_path)
    desk = _desk(tmp_path)
    _write(world, "peer", sender=desk, user_word=False)
    agents_db.conn().execute(
        "UPDATE coordination_traces SET audience = 'agents' WHERE trace_id = 't-peer'")
    spec = SimpleNamespace(agent_id=world.agent, session="mike", origin="agent",
                           sender_agent_id=desk, user_word=True, trace_id="t-steer")
    world.service._steered_into(spec, "t-peer")
    assert _audience(world.agent, "t-peer")[0] == coordination.MIXED


def test_another_agents_message_steered_in_leaves_the_turn_hidden(tmp_path):
    world = World(tmp_path)
    desk = _desk(tmp_path)
    _write(world, "peer", sender=desk, user_word=False)
    agents_db.conn().execute(
        "UPDATE coordination_traces SET audience = 'agents' WHERE trace_id = 't-peer'")
    spec = SimpleNamespace(agent_id=world.agent, session="mike", origin="agent",
                           sender_agent_id=desk, user_word=False, trace_id="t-steer")
    world.service._steered_into(spec, "t-peer")
    assert _audience(world.agent, "t-peer")[0] == coordination.AGENTS

