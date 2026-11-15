"""The user's focus moves only when a person addresses an agent.

Focus is the agent the user currently has open; a hands-free message without
a name goes to the agent that has it. Every dispatch to a named session used
to count as "addressed by name" and took the focus: the answer to a request
when it was delivered, a goal taken up again, a schedule, a peer agent's
message. With several agents working toward goals the focus changed under the
user, and an un-named spoken message could go to the wrong agent.

Found by the conformance run of the Host in Rust (run 43713907614, scenario
``approval``: no person ever sent the agent a message, and after the answer
was delivered it had the focus). server-rs/docs/reference-defects.md, R14.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import origins, team_store
from lib.turn_dispatch import TurnDispatchService
from tests.unit.test_turn_dispatch import _Backends, _Stream, _local_dispatch_runtime  # noqa: F401

BY_A_PERSON = ["user", "oracle"]
BY_THE_HOST = ["agent", "schedule", "automation", "watcher", "heartbeat", "leader_tick", "dreaming"]


@pytest.fixture
def two_agents(tmp_path):
    """Rachel has the focus; Mike is the one a message is sent to."""
    mike = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                  session="mike", backend="claude")
    rachel = agents_db.create_agent(persona="Rachel", voice_id="V", cwd=str(tmp_path),
                                    session="rachel", backend="claude")
    agents_db.start_runtime(mike, "mike")
    agents_db.start_runtime(rachel, "rachel")
    agents_db.set_focus(rachel)
    ctx = SimpleNamespace(default_session="rachel", agents_path=tmp_path / "unused.json",
                          stream=_Stream())
    backends = _Backends()
    service = TurnDispatchService(ctx, backend_registry=backends, home=tmp_path,
                                  uuid_factory=lambda: "backend-session-1", now=lambda: 12.5)
    return SimpleNamespace(service=service, backends=backends, mike=mike, rachel=rachel)


def _send(fleet, origin, **extra):
    return fleet.service.dispatch(
        text="continue", requested_session="mike", forced_session="mike",
        trace_id=f"trace-{origin}", synthesize_audio=False, origin=origin, **extra)


def fleet_send_again(fleet):
    """A second message from the user, under a trace of its own."""
    return fleet.service.dispatch(
        text="and this", requested_session="mike", forced_session="mike",
        trace_id="trace-user-2", synthesize_audio=False, origin="user", queue_if_busy=True)


def test_the_two_lists_name_every_origin_a_client_may_set():
    """A new origin has to be put on one side or the other, on purpose."""
    assert set(BY_A_PERSON) == set(origins.FOCUS_ORIGINS)
    assert set(BY_A_PERSON) | set(BY_THE_HOST) >= origins.CLIENT_SETTABLE_ORIGINS
    assert not set(BY_A_PERSON) & set(BY_THE_HOST)


@pytest.mark.parametrize("origin", BY_A_PERSON)
def test_a_person_addressing_an_agent_takes_the_focus_there(two_agents, origin):
    result = _send(two_agents, origin)

    assert result.session == "mike" and len(two_agents.backends.spawned) == 1
    assert agents_db.get_focus() == two_agents.mike


@pytest.mark.parametrize("origin", BY_THE_HOST)
def test_what_the_host_sends_by_itself_leaves_the_focus_where_it_was(two_agents, origin):
    extra = {"sender_agent_id": two_agents.rachel} if origin == "agent" else {}
    if origin == "leader_tick":                    # a leader tick needs a team that nudges
        team = team_store.create_team("Ops")
        team_store.add_member(team["team_id"], two_agents.mike)
        team_store.set_leader(team["team_id"], two_agents.mike)
        team_store.set_nudge_enabled(team["team_id"], True)

    result = _send(two_agents, origin, **extra)

    # The message is delivered all the same.
    assert result.session == "mike" and len(two_agents.backends.spawned) == 1
    assert agents_db.get_focus() == two_agents.rachel


def test_a_message_that_has_to_wait_follows_the_same_rule(two_agents):
    _send(two_agents, "user")                      # Mike works, and has the focus
    first = two_agents.backends.spawned[-1][1]
    assert first["on_session_init"](first["backend_session_id"] or "native-mike")
    agents_db.set_focus(two_agents.rachel)         # the user opens Rachel again

    queued = _send(two_agents, "automation", queue_if_busy=True)
    assert queued.queued and agents_db.get_focus() == two_agents.rachel

    queued = fleet_send_again(two_agents)
    assert queued.queued and agents_db.get_focus() == two_agents.mike


def test_no_focus_is_set_where_there_was_none(two_agents):
    agents_db.set_focus(None)

    _send(two_agents, "schedule")

    assert agents_db.get_focus() is None
