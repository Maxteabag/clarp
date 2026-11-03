"""TP-N1b review (lead): a terminal row and its turn settlement are one write.
On a lock error it is retried, bounded, as a whole; a retry rechecks that the
turn is still the agent's, so it never overwrites a successor or a Stop."""
from __future__ import annotations

import sqlite3

from lib import db, turn_dispatch as dispatch, turn_lifecycle as lifecycle
from lib.protocol import AgentState
from lib.turn_lifecycle import TurnEvent

from test_drain_pause_parity import World


def _locked_once(monkeypatch, during_backoff=None):
    real = lifecycle._record_outcome
    failures = []

    def record(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_record_outcome", record)
    if during_backoff is not None:
        real_sleep = db.time.sleep
        monkeypatch.setattr(db.time, "sleep", lambda s: (during_backoff(), real_sleep(0))[1])
    return failures


def _turn_row(agent_id, trace_id):
    return db.conn().execute("SELECT settled_at, outcome FROM turns WHERE agent_id=? AND trace_id=?",
                             (agent_id, trace_id)).fetchone()


def _world(tmp_path, monkeypatch, cid):
    monkeypatch.setattr(dispatch, "_RUNTIME_CLIENT", None)
    dispatch.reset_for_tests()
    world = World(tmp_path)
    world.send(cid)
    world.provider.live.discard(world.agent)
    return world, world.provider.spawned[-1]


def test_a_clean_result_that_meets_one_lock_ends_completed(tmp_path, monkeypatch):
    world, call = _world(tmp_path, monkeypatch, "clean")
    failures = _locked_once(monkeypatch)
    try:
        call["on_result"]({"result": "done"})
        assert failures == [True]
        assert lifecycle.current_kind(world.agent) == AgentState.DONE
        assert _turn_row(world.agent, "t-clean")["outcome"] == "completed"
    finally:
        dispatch.reset_for_tests()


def test_a_failed_result_that_meets_one_lock_ends_failed(tmp_path, monkeypatch):
    world, call = _world(tmp_path, monkeypatch, "broken")
    failures = _locked_once(monkeypatch)
    try:
        call["on_error"]("synthetic unclassified failure")
        assert failures == [True]
        assert lifecycle.current_kind(world.agent) not in lifecycle.BUSY
        assert _turn_row(world.agent, "t-broken")["outcome"] == "failed"
    finally:
        dispatch.reset_for_tests()


def test_a_retried_terminal_write_never_overwrites_a_successor(tmp_path, monkeypatch):
    """The first attempt meets a lock; during the backoff the agent's next turn
    starts. The retry finds the turn no longer the agent's and writes nothing."""
    world, _call = _world(tmp_path, monkeypatch, "first")

    def successor_starts():
        lifecycle.open_turn(agent_id=world.agent, source="pwa", trace_id="t-next")
        lifecycle.transition(world.agent, TurnEvent.SPAWN_STARTED,
                             {"source": "pwa", "dispatch": "claude", "trace_id": "t-next"})

    _locked_once(monkeypatch, during_backoff=successor_starts)
    try:
        written = lifecycle.try_transition(world.agent, TurnEvent.PROCESS_EXITED_OK,
                                           {"trace_id": "t-first"})
        assert written is None
        latest = db.conn().execute(
            "SELECT kind, json_extract(detail, '$.trace_id') AS trace FROM state_log"
            " WHERE agent_id = ? ORDER BY state_id DESC LIMIT 1", (world.agent,)).fetchone()
        assert (latest["kind"], latest["trace"]) == (AgentState.THINKING, "t-next")
        assert _turn_row(world.agent, "t-next")["settled_at"] is None
    finally:
        dispatch.reset_for_tests()


import pytest


@pytest.mark.parametrize("meanwhile", ["untraced-stop", "successor-then-untraced-row"])
def test_a_retried_terminal_write_yields_to_any_row_not_its_own(tmp_path, monkeypatch, meanwhile):
    """TP-N1b review 4: a row written during the backoff that is not this
    turn's own, traced or not, wins over the retried terminal write."""
    world, _call = _world(tmp_path, monkeypatch, "first")
    before = db.conn().execute("SELECT MAX(state_id) FROM state_log WHERE agent_id = ?",
                               (world.agent,)).fetchone()[0]

    def intervene():
        if meanwhile == "untraced-stop":
            lifecycle.transition(world.agent, TurnEvent.STOP_REQUESTED, {"source": "user_stop"})
        else:
            lifecycle.open_turn(agent_id=world.agent, source="pwa", trace_id="t-next")
            lifecycle.transition(world.agent, TurnEvent.SPAWN_STARTED, {"trace_id": "t-next"})
            lifecycle.transition(world.agent, TurnEvent.TOOL_STARTED, {"source": "hook"})

    _locked_once(monkeypatch, during_backoff=intervene)
    try:
        assert lifecycle.try_transition(world.agent, TurnEvent.PROCESS_EXITED_OK,
                                        {"trace_id": "t-first"}) is None
        kinds = [row["kind"] for row in db.conn().execute(
            "SELECT kind FROM state_log WHERE agent_id = ? AND state_id > ? ORDER BY state_id",
            (world.agent, before))]
        assert AgentState.DONE not in kinds
    finally:
        dispatch.reset_for_tests()


def test_a_retried_terminal_write_still_lands_after_its_own_turns_row(tmp_path, monkeypatch):
    """Control: a row of the same turn written during the backoff does not
    block the retry, and the terminal row it then writes is the agent's
    newest (TP-N1b review 6: the retry takes a fresh stamp, not the first
    attempt's, so it never sorts behind a row written meanwhile)."""
    world, _call = _world(tmp_path, monkeypatch, "first")
    clock = [lifecycle.now_ms()]
    monkeypatch.setattr(lifecycle, "now_ms", lambda: clock[0])

    def same_turn_tool_row_later():
        clock[0] += 50
        lifecycle.transition(world.agent, TurnEvent.TOOL_STARTED,
                             {"source": "hook", "trace_id": "t-first"})

    _locked_once(monkeypatch, during_backoff=same_turn_tool_row_later)
    try:
        assert lifecycle.try_transition(world.agent, TurnEvent.PROCESS_EXITED_OK,
                                        {"trace_id": "t-first"}) is not None
        assert lifecycle.current_kind(world.agent) == AgentState.DONE
        assert _turn_row(world.agent, "t-first")["outcome"] == "completed"
    finally:
        dispatch.reset_for_tests()


def test_a_stop_right_after_the_failed_attempt_still_wins(tmp_path, monkeypatch):
    """TP-N1b review 5: a Stop that commits in the instant after the failed
    attempt rolled back and released its lock, before anything else, still
    fences the retry."""
    world, _call = _world(tmp_path, monkeypatch, "first")
    _locked_once(monkeypatch)
    real_write = lifecycle.TurnStateMachine._write_terminal
    stopped = []

    def write_then_stop_on_failure(self, *args, **kwargs):
        try:
            return real_write(self, *args, **kwargs)
        except sqlite3.OperationalError:
            if not stopped:
                assert not db.conn().in_transaction
                lifecycle.transition(world.agent, TurnEvent.STOP_REQUESTED, {"source": "user_stop"})
                stopped.append(True)
            raise

    monkeypatch.setattr(lifecycle.TurnStateMachine, "_write_terminal", write_then_stop_on_failure)
    try:
        assert lifecycle.try_transition(world.agent, TurnEvent.PROCESS_EXITED_OK,
                                        {"trace_id": "t-first"}) is None
        assert stopped == [True]
        assert lifecycle.current_kind(world.agent) == AgentState.INTERRUPTED
    finally:
        dispatch.reset_for_tests()
