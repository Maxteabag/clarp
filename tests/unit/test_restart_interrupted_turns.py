"""A server restart kills the in-flight turn; the user must be able to see it.

Issue #11: a message sent seconds before a restart sat in the transcript with
no reply and no marker. The restart heartbeat told the agent, but nothing told
the user. Boot recovery now records an INTERRUPTED state (so the banner shows)
and writes a visible marker against the orphaned message, for every backend.
"""
from __future__ import annotations

import itertools

import pytest

from lib import agents as agents_db
from lib import backends, db, heartbeat, interrupted_turns, message_store
from lib import user_notifications
from lib.protocol import AgentState

_IDS = itertools.count()
BACKENDS = backends.ids()


def _agent(backend: str) -> tuple[str, str, str]:
    session = f"{backend}-{next(_IDS)}"
    aid = agents_db.create_agent(
        persona=session.capitalize(), voice_id="v", cwd="/tmp",
        session=session, backend=backend)
    agents_db.start_runtime(aid, session)
    backend_session_id = f"bs-{aid}"
    agents_db.bind_backend_session(aid, backend_session_id)
    return aid, session, backend_session_id


def _dispatch(aid: str, backend_session_id: str, backend: str, *,
              origin: str = "user") -> tuple[str, str]:
    """Persist exactly what dispatch leaves behind once a turn is spawned."""
    trace = f"trace-{next(_IDS)}"
    row = message_store.record_user_message(
        agent_id=aid, backend_session_id=backend_session_id,
        client_msg_id=f"c-{trace}", text="yo", origin=origin)
    agents_db.record_state(aid, AgentState.THINKING, {
        "source": "pwa", "dispatch": backend, "origin": origin,
        "trace_id": trace, "backend_session_id": backend_session_id,
    })
    return row["id"], trace


def _visible(aid: str, backend_session_id: str) -> list[dict]:
    return message_store.list_messages(
        agent_id=aid, backend_session_id=backend_session_id,
        include_automated=False)


def _everything(aid: str, backend_session_id: str) -> list[dict]:
    return message_store.list_messages(
        agent_id=aid, backend_session_id=backend_session_id,
        include_automated=True)


def _state_kinds(aid: str) -> list[str]:
    rows = db.conn().execute(
        "SELECT kind FROM state_log WHERE agent_id = ? ORDER BY ts, state_id",
        (aid,)).fetchall()
    return [r["kind"] for r in rows]


def _classify(aid: str, session: str, backend_session_id: str) -> dict:
    return user_notifications.classify_completed_turn(
        agent_id=aid, session=session, persona=session.capitalize(),
        done_ts=db.now_ms() + 5, backend_session_id=backend_session_id,
        settle_timeout_s=0)


@pytest.mark.parametrize("backend", BACKENDS)
def test_in_flight_user_turn_is_marked_interrupted(backend):
    aid, session, bs = _agent(backend)
    user_id, trace = _dispatch(aid, bs, backend)

    recovered = interrupted_turns.recover_after_restart()

    assert [r["agent_id"] for r in recovered] == [aid]
    latest = agents_db.latest_state(aid)
    assert latest["kind"] == AgentState.INTERRUPTED
    assert latest["detail"]["source"] == "server_restart"
    assert latest["detail"]["trace_id"] == trace
    assert not agents_db.is_busy(aid)

    rows = _visible(aid, bs)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["id"] == user_id
    marker = rows[1]
    assert marker["origin"] == interrupted_turns.MARKER_ORIGIN
    assert marker["text"] == interrupted_turns.RESTART_MARKER_TEXT
    assert marker["automated"] is False
    assert marker["timestamp"] >= rows[0]["timestamp"]

    result = _classify(aid, session, bs)
    assert result["reason"] == "turn-interrupted"
    assert result["notify"] is False


@pytest.mark.parametrize("backend", BACKENDS)
def test_completed_turn_is_left_alone(backend):
    aid, _session, bs = _agent(backend)
    _dispatch(aid, bs, backend)
    agents_db.record_state(aid, AgentState.DONE, {"source": "hook"})

    assert interrupted_turns.recover_after_restart() == []
    assert agents_db.latest_state(aid)["kind"] == AgentState.DONE
    assert [r["role"] for r in _visible(aid, bs)] == ["user"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_user_stopped_turn_is_left_alone(backend):
    aid, _session, bs = _agent(backend)
    _dispatch(aid, bs, backend)
    agents_db.record_state(aid, AgentState.INTERRUPTED,
                           {"source": "user_stop", "message": "Turn stopped"})
    before = _state_kinds(aid)

    assert interrupted_turns.recover_after_restart() == []
    assert _state_kinds(aid) == before
    assert agents_db.latest_state(aid)["detail"]["source"] == "user_stop"
    assert [r["role"] for r in _visible(aid, bs)] == ["user"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("origin", ["heartbeat", "leader_tick", "dreaming", "watcher"])
def test_automation_turn_gets_state_but_no_visible_row(backend, origin):
    aid, _session, bs = _agent(backend)
    _dispatch(aid, bs, backend, origin=origin)

    recovered = interrupted_turns.recover_after_restart()

    assert [r["agent_id"] for r in recovered] == [aid]
    latest = agents_db.latest_state(aid)
    assert latest["kind"] == AgentState.INTERRUPTED
    assert latest["detail"]["source"] == "server_restart"
    assert [r["role"] for r in _everything(aid, bs)] == ["user"]
    assert _visible(aid, bs) == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_archived_agent_is_skipped(backend):
    aid, _session, bs = _agent(backend)
    _dispatch(aid, bs, backend)
    agents_db.set_archived(aid, True)

    assert interrupted_turns.recover_after_restart() == []
    assert agents_db.latest_state(aid)["kind"] == AgentState.THINKING
    assert [r["role"] for r in _visible(aid, bs)] == ["user"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_second_boot_does_not_duplicate_the_marker(backend):
    aid, _session, bs = _agent(backend)
    _dispatch(aid, bs, backend)

    assert len(interrupted_turns.recover_after_restart()) == 1
    assert interrupted_turns.recover_after_restart() == []

    assert [r["role"] for r in _visible(aid, bs)] == ["user", "assistant"]
    assert _state_kinds(aid).count(AgentState.INTERRUPTED) == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_only_the_stalled_agent_is_touched(backend):
    stalled, _s1, bs1 = _agent(backend)
    idle, _s2, bs2 = _agent(backend)
    _dispatch(stalled, bs1, backend)
    _dispatch(idle, bs2, backend)
    agents_db.record_state(idle, AgentState.DONE, {"source": "hook"})

    recovered = interrupted_turns.recover_after_restart()

    assert [r["agent_id"] for r in recovered] == [stalled]
    assert agents_db.latest_state(idle)["kind"] == AgentState.DONE
    assert [r["role"] for r in _visible(idle, bs2)] == ["user"]


def test_restart_heartbeat_still_follows_the_marker(monkeypatch):
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", "0")
    aid, session, bs = _agent(backends.CLAUDE)
    _dispatch(aid, bs, backends.CLAUDE)
    interrupted_turns.recover_after_restart()
    sent: list[tuple[str, str]] = []
    scheduler = heartbeat.HeartbeatScheduler(
        send_heartbeat=lambda s, text: sent.append((s, text)),
        now=lambda: 1_000.0)

    assert scheduler.run_restart_recovery_once() == 1
    assert sent[0][0] == session
    assert sent[0][1].startswith(heartbeat.RESTART_HEARTBEAT_PREFIX)
    # The restart prefix already says the turn may have been cut; do not
    # stack the usage-limit wording on top of it.
    assert heartbeat.INTERRUPTED_HEARTBEAT_PREFIX not in sent[0][1]


def test_marker_never_becomes_the_next_turns_reply():
    aid, session, bs = _agent(backends.CLAUDE)
    _dispatch(aid, bs, backends.CLAUDE)
    interrupted_turns.recover_after_restart()
    # The restart heartbeat lands right after the marker and completes with
    # nothing to say; the marker must not be mistaken for its reply.
    _dispatch(aid, bs, backends.CLAUDE, origin="heartbeat")
    agents_db.record_state(aid, AgentState.DONE, {"source": "hook"})

    result = _classify(aid, session, bs)

    assert result["notify"] is False
    assert result["preview"] == ""
    assert result["reason"] != "text-reply"
