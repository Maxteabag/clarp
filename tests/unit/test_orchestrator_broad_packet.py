"""The broad routing packet shows the last words of at most 20 agents besides the requested and
the focused one, as the Rust Host does (issue 360, R35 part A); everyone stays listed by name."""
from __future__ import annotations

from lib import agents, db, orchestrator
from lib.orchestrator_context import build_context_packet
from lib.orchestrator_types import BROAD_DETAIL_AGENTS, OrchestratorSettings


def _fleet(count):
    later = db.now_ms() + 10_000_000
    for n in range(count):
        session = f"agent{n:03d}"
        agents.create_agent(persona=f"Persona{n:03d}", voice_id="", cwd="/srv", session=session)
        orchestrator.record_routing_message(session=session, role="user", text=f"told {session}")
        # agent029 is the most recently active, agent000 the least.
        db.conn().execute("UPDATE agents SET created_at = ? WHERE session = ?", (later + n * 1000, session))


def _packet(scope, requested="agent000"):
    return build_context_packet(utterance="hello", requested_session=requested, trace_id="t",
                                hands_free=True, settings=OrchestratorSettings(), context_scope=scope)


def _with_words(packet):
    return {agent["session"] for agent in packet["agents"] if agent["recent_user_messages"]}


def test_the_broad_packet_shows_the_words_of_the_twenty_most_recently_active_and_lists_everyone():
    _fleet(30)
    packet = _packet("all")
    listed = {agent["session"] for agent in packet["agents"]}
    assert {f"agent{n:03d}" for n in range(30)} <= listed
    assert BROAD_DETAIL_AGENTS == 20          # the Rust Host's WHOLE_MOST
    recent = {f"agent{n:03d}" for n in range(10, 30)}
    assert _with_words(packet) == recent | {"agent000"}


def test_the_requested_and_the_focused_agent_always_keep_their_words():
    _fleet(30)
    agents.set_focus(agents.get_by_session("agent001")["agent_id"])
    packet = _packet("all", requested="agent000")
    assert {"agent000", "agent001"} <= _with_words(packet)
    assert len(_with_words(packet) - {"agent000", "agent001"}) == 20


def test_a_small_fleet_is_shown_whole():
    _fleet(5)
    assert _with_words(_packet("all")) == {f"agent{n:03d}" for n in range(5)}


def test_the_focused_packet_is_unchanged():
    _fleet(30)
    assert _with_words(_packet("focused")) == {"agent000"}
