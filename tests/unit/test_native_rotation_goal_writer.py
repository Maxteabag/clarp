"""Native callbacks cannot overwrite a new binding after an old-owner lookup."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from lib import agent_goals, agents, codex_app_server, db
from tests.unit.test_agent_goals import _agent, _client


@pytest.mark.parametrize("old_clear", [False, True])
def test_mirror_lookup_and_write_serialize_against_binding_cutover(tmp_path, monkeypatch, old_clear):
    agent = _agent(tmp_path)
    old = agents.live_backend_session(agent)
    initial = {"objective": "old work", "status": "paused", "tokens_used": 5,
               "time_used_seconds": 2, "token_budget": 50, "created_at": 1000, "updated_at": 2000}
    agent_goals.upsert(agent, session="theo", backend="codex", goal=initial)
    looked_up, release_reader = threading.Event(), threading.Event()
    errors = []
    real_lookup = agents.get_by_backend_session

    def held_lookup(thread_id):
        row = real_lookup(thread_id)
        if threading.current_thread().name == "rotation-old-goal" and thread_id == old:
            looked_up.set()
            if not release_reader.wait(5):
                raise RuntimeError("controlled mirror barrier did not release")
        return row

    monkeypatch.setattr(agents, "get_by_backend_session", held_lookup)
    client = _client()
    raw = {"threadId": old, "objective": "old work", "status": "active", "tokenBudget": 50,
           "tokensUsed": 9, "timeUsedSeconds": 3, "createdAt": 1, "updatedAt": 3}

    def old_callback():
        try:
            client._mirror_goal(old, None if old_clear else raw)
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close_local()

    worker = threading.Thread(target=old_callback, name="rotation-old-goal")
    worker.start()
    connection = db.conn()
    timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    replacement = {**initial, "objective": "carried new work", "tokens_used": 17,
                   "time_used_seconds": 8, "created_at": 1000, "updated_at": 4000}

    def cutover():
        connection.execute("BEGIN IMMEDIATE")
        try:
            agents.bind_backend_session(agent, "new-thread")
            agent_goals.upsert(agent, session="theo", backend="codex", goal=replacement)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    try:
        assert looked_up.wait(5), "real native mirror did not reach its owner lookup"
        connection.execute("PRAGMA busy_timeout=1")
        deferred = False
        try:
            cutover()
        except sqlite3.OperationalError as exc:
            assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY
            deferred = True
        finally:
            release_reader.set()
        worker.join(5)
        assert not worker.is_alive() and errors == []
        if deferred:
            # The old writer really completed under its admitted binding;
            # an exception swallowed by the reader is not a passing race.
            old_row = agent_goals.get(agent)
            if old_clear:
                assert old_row is None
            else:
                assert old_row["objective"] == "old work" and old_row["tokens_used"] == 9
            cutover()
        row = agent_goals.get(agent)
        assert agents.live_backend_session(agent) == "new-thread"
        assert row["objective"] == "carried new work"
        assert (row["tokens_used"], row["time_used_seconds"], row["created_at"]) == (17, 8, 1000)
        # A late old-thread clear after the new writer commits is ignored too.
        client._mirror_goal(old, None)
        assert agent_goals.get(agent) == row
    finally:
        release_reader.set()
        worker.join(5)
        connection.execute(f"PRAGMA busy_timeout={int(timeout)}")
