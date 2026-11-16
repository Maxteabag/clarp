"""The agent list reads an agent's state clocks again only when its state changed.

``agents.dashboard_states`` walked every live agent's state rows at every
request: a tenth of the agent list's time on a Host with 1,500 agents, most
of them archived and unchanged for weeks (issue 371). An agent's clocks are
kept for its newest state row. State rows are only added, or deleted by the
retention sweep, which says so; either way the clocks are read again.
"""
from __future__ import annotations

import json
import random
import sqlite3

import pytest

from lib import agents, db, maintenance

# The statement as it was before anything was kept: the reference.
AS_IT_WAS = """
    SELECT a.agent_id, l.kind, l.ts, l.detail,
           (SELECT MIN(b.ts) FROM state_log b
             WHERE b.agent_id = a.agent_id
               AND b.kind IN ('thinking', 'tool', 'compacting')
               AND b.ts > COALESCE((SELECT MAX(e.ts) FROM state_log e
                                     WHERE e.agent_id = a.agent_id
                                       AND e.kind IN ('done', 'idle', 'stopped', 'interrupted')), 0)
           ) AS turn_started_at,
           (SELECT c.ts FROM state_log c
             WHERE c.agent_id = a.agent_id
               AND c.kind IN ('done', 'idle', 'stopped')
               AND (c.kind = 'done' OR (c.kind = 'idle' AND
                    (SELECT p.kind FROM state_log p
                      WHERE p.agent_id = c.agent_id
                        AND (p.ts < c.ts OR (p.ts = c.ts AND p.state_id < c.state_id))
                      ORDER BY p.ts DESC, p.state_id DESC LIMIT 1) IN ('thinking', 'tool')))
             ORDER BY c.ts DESC, c.state_id DESC LIMIT 1
           ) AS last_turn_end
      FROM agents a
      JOIN state_log l ON l.state_id = (
               SELECT x.state_id FROM state_log x
                WHERE x.agent_id = a.agent_id
                ORDER BY x.ts DESC, x.state_id DESC LIMIT 1)
     WHERE a.deleted_at IS NULL
"""
KINDS = ("idle", "thinking", "tool", "compacting", "done", "stopped", "interrupted", "background")
# An agent is created with a state row of its own at the wall clock; the rows
# written here are dated after it.
T = 1_900_000_000_000


def _as_it_was():
    result = {}
    for row in db.conn().execute(AS_IT_WAS):
        state = dict(row)
        state["detail"] = json.loads(state["detail"] or "{}")
        result[row["agent_id"]] = state
    return result


def _agent(number):
    return agents.create_agent(persona=f"State {number}", voice_id="", cwd="/tmp", session=f"state-{number}")


def _row(agent_id, kind, ts, detail=None):
    """A state row with a time of its own choosing (a marker is written back-dated)."""
    db.conn().execute("INSERT INTO state_log (agent_id, ts, kind, detail) VALUES (?,?,?,?)",
                      (agent_id, ts, kind, json.dumps(detail or {})))


def _statements(run):
    seen = []
    db.conn().set_trace_callback(seen.append)
    try:
        run()
    finally:
        db.conn().set_trace_callback(None)
    return [sql for sql in seen if "state_log" in sql]


@pytest.mark.parametrize("seed", range(6))
def test_the_clocks_are_what_the_walk_of_every_agent_gives_after_every_write(seed):
    """Rows added in time order and back-dated, by kind at random, the sweep
    in between, an agent deleted and brought back: after each, what is
    answered from what is kept is what the statement over every agent reads."""
    chance = random.Random(seed)
    ids = [_agent(number) for number in range(6)]
    now = 1_800_000_000_000
    for step in range(90):
        aid = chance.choice(ids)
        roll = chance.random()
        if roll < 0.70:
            now += chance.choice((0, 1, 1000, 60_000))               # equal times happen
            _row(aid, chance.choice(KINDS), now, {"step": step})
        elif roll < 0.82:
            _row(aid, chance.choice(KINDS), now - chance.randrange(1, 5_000_000), {"back": step})
        elif roll < 0.90:
            maintenance._prune_state_log(db.conn(), now - chance.randrange(1_000, 3_000_000))
        elif roll < 0.95:
            db.conn().execute("UPDATE agents SET deleted_at = CASE WHEN deleted_at IS NULL THEN 1 END WHERE agent_id=?",
                              (aid,))
        else:
            agents.record_state(aid, chance.choice(KINDS))            # the fixture writer, at the wall clock
        assert agents.dashboard_states() == _as_it_was(), (seed, step)


def test_a_request_walks_only_the_agents_whose_state_changed():
    ids = [_agent(number) for number in range(5)]
    for number, aid in enumerate(ids):
        _row(aid, "thinking", T + 1000 + number)
        _row(aid, "done", T + 2000 + number)
    first = _statements(agents.dashboard_states)
    assert len(first) == 2                                  # the newest row of each, then all five walked

    again = _statements(agents.dashboard_states)
    assert len(again) == 1 and "ORDER BY" not in again[0]   # the newest row of each, nothing walked

    _row(ids[3], "thinking", T + 5000)
    changed = _statements(agents.dashboard_states)
    assert len(changed) == 2
    assert ids[3] in changed[1] and not any(aid in changed[1] for aid in ids if aid != ids[3])
    assert agents.dashboard_states()[ids[3]]["turn_started_at"] == T + 5000


def test_rows_the_sweep_deletes_are_no_longer_in_the_clocks():
    """An agent's last turn ended long ago; a back-dated marker is its newest
    row by number. The sweep keeps that row and deletes the turn's end: the
    newest row is the same one, and the last turn end is gone."""
    aid = _agent(0)
    db.conn().execute("DELETE FROM state_log WHERE agent_id=?", (aid,))
    _row(aid, "thinking", 100)
    _row(aid, "done", 200)
    _row(aid, "background", 50)                             # written last, dated first
    assert agents.dashboard_states()[aid]["last_turn_end"] == 200
    assert maintenance._prune_state_log(db.conn(), 1000) == 2
    assert agents.dashboard_states() == _as_it_was()
    assert agents.dashboard_states()[aid]["last_turn_end"] is None
    assert len(_statements(agents.dashboard_states)) == 1    # kept again, under the sweep's new number
    assert maintenance.STATES_PRUNED_KEY == agents.STATES_PRUNED_KEY


def test_a_sweep_that_deletes_nothing_leaves_what_is_kept():
    aid = _agent(0)
    _row(aid, "done", T + 5000)
    agents.dashboard_states()
    assert maintenance._prune_state_log(db.conn(), 1000) == 0
    assert len(_statements(agents.dashboard_states)) == 1


def test_nothing_read_inside_a_transaction_is_kept():
    aid = _agent(0)
    _row(aid, "done", T + 1000)
    db.conn().commit()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        _row(aid, "thinking", T + 2000)
        assert agents.dashboard_states()[aid]["kind"] == "thinking"     # its own uncommitted row
    finally:
        c.execute("ROLLBACK")
    # The row that was rolled back gave its number back: the next row takes
    # the same one, and must not be answered with what that one said.
    _row(aid, "stopped", T + 3000)
    db.conn().commit()
    assert agents.dashboard_states()[aid]["kind"] == "stopped"
    assert agents.dashboard_states() == _as_it_was()


def test_an_agent_without_a_state_row_and_a_deleted_one_are_not_answered():
    with_state, without, deleted = (_agent(number) for number in range(3))
    db.conn().execute("DELETE FROM state_log WHERE agent_id=?", (without,))
    assert set(agents.dashboard_states()) == {with_state, deleted}
    assert len(_statements(agents.dashboard_states)) == 1    # nothing to walk for the one without
    db.conn().execute("UPDATE agents SET deleted_at = 1 WHERE agent_id=?", (deleted,))
    assert set(agents.dashboard_states()) == {with_state}
    assert without not in agents.dashboard_states()


def test_what_a_caller_does_to_its_answer_stays_its_own():
    aid = _agent(0)
    _row(aid, "tool", T + 1000, {"tool": {"name": "Bash"}})
    first = agents.dashboard_states()
    first[aid]["kind"] = "changed"
    first[aid]["detail"]["tool"]["name"] = "changed"
    first.pop(aid)
    assert agents.dashboard_states()[aid] == _as_it_was()[aid]


# --- from the review of #379 ----------------------------------------------------

def _pruned():
    row = db.conn().execute("SELECT value FROM settings WHERE key=?", (agents.STATES_PRUNED_KEY,)).fetchone()
    return int(row[0]) if row else 0


def _an_old_turn(aid):
    """An agent whose last turn ended long ago, and a newer row that the sweep keeps."""
    db.conn().execute("DELETE FROM state_log WHERE agent_id=?", (aid,))
    _row(aid, "thinking", 100)
    _row(aid, "done", 200)
    _row(aid, "background", T)


def test_a_sweep_that_fails_part_way_still_says_rows_went(monkeypatch):
    """One batch is deleted, the next gives up on the lock. Rows are gone and
    the sweep never reached its end: the count has moved all the same, and
    the clocks are read again."""
    aid = _agent(0)
    _an_old_turn(aid)
    assert agents.dashboard_states()[aid]["last_turn_end"] == 200
    monkeypatch.setattr(maintenance, "STATE_LOG_DELETE_BATCH", 1)
    plain = db._TrackedConnection.execute
    deletes = []

    def execute(self, sql, parameters=(), /):
        if str(sql).lstrip().startswith("DELETE FROM state_log"):
            deletes.append(1)
            if len(deletes) == 2:
                raise sqlite3.OperationalError("database is locked")
        return plain(self, sql, parameters)
    monkeypatch.setattr(db._TrackedConnection, "execute", execute)
    with pytest.raises(sqlite3.OperationalError):
        maintenance._prune_state_log(db.conn(), 1000)
    monkeypatch.setattr(db._TrackedConnection, "execute", plain)

    assert db.conn().execute("SELECT COUNT(*) FROM state_log WHERE agent_id=?", (aid,)).fetchone()[0] == 2
    assert _pruned() >= 1
    assert agents.dashboard_states() == _as_it_was()


def test_a_sweep_whose_last_word_fails_has_said_so_before_it_deleted(monkeypatch):
    """The count's own write after the deletes gives up. One was added before
    the first batch, so the clocks kept from before the sweep are not used."""
    aid = _agent(0)
    _an_old_turn(aid)
    assert agents.dashboard_states()[aid]["last_turn_end"] == 200
    plain = db._TrackedConnection.execute
    said = []

    def execute(self, sql, parameters=(), /):
        if "INSERT INTO settings" in str(sql) and agents.STATES_PRUNED_KEY in tuple(parameters):
            said.append(1)
            if len(said) == 2:
                raise sqlite3.OperationalError("database is locked")
        return plain(self, sql, parameters)
    monkeypatch.setattr(db._TrackedConnection, "execute", execute)
    with pytest.raises(sqlite3.OperationalError):
        maintenance._prune_state_log(db.conn(), 1000)
    monkeypatch.setattr(db._TrackedConnection, "execute", plain)

    assert len(said) == 2 and _pruned() == 1
    assert agents.dashboard_states()[aid]["last_turn_end"] is None
    assert agents.dashboard_states() == _as_it_was()


def test_a_sweep_with_nothing_to_delete_says_nothing():
    aid = _agent(0)
    _row(aid, "done", T + 5000)
    assert maintenance._prune_state_log(db.conn(), 1000) == 0
    assert _pruned() == 0


def test_a_row_that_arrives_between_the_two_statements_is_kept_under_its_own_id(monkeypatch):
    """A request reads every agent's newest id, then the clocks of those that
    changed. Another process writes a state row between the two. The clocks
    read hold that row, and they are kept under the id read with them, not
    under the one the request began with: the next request finds them."""
    import threading

    aid, other = _agent(0), _agent(1)
    _row(aid, "thinking", T + 1000)
    _row(other, "done", T + 1000)
    db.conn().commit()
    plain = db._TrackedConnection.execute
    here, arrived = threading.get_ident(), []

    def execute(self, sql, parameters=(), /):
        if "AS turn_started_at" in str(sql) and threading.get_ident() == here and not arrived:
            arrived.append(True)

            def write():
                _row(aid, "done", T + 2000)
                db.conn().commit()
                db.close_local()
            writer = threading.Thread(target=write)
            writer.start()
            writer.join(20)
        return plain(self, sql, parameters)
    monkeypatch.setattr(db._TrackedConnection, "execute", execute)
    first = agents.dashboard_states()
    monkeypatch.setattr(db._TrackedConnection, "execute", plain)

    assert arrived and first[aid]["kind"] == "done" and first[aid]["last_turn_end"] == T + 2000
    assert len(_statements(agents.dashboard_states)) == 1    # found under the id read with them
    assert agents.dashboard_states() == _as_it_was()


def test_state_rows_are_only_inserted_and_only_the_sweep_deletes_them():
    """What is kept rests on these two facts. Read from the sources: no
    statement updates a state row, and the only delete is the sweep's, which
    adds to the count. Whoever adds another must add to the count too."""
    import pathlib
    import re
    library = pathlib.Path(agents.__file__).resolve().parent
    updates, deletes = [], []
    for path in sorted(library.rglob("*.py")):
        source = path.read_text()
        updates += [path.name for _ in re.finditer(r"UPDATE\s+state_log\b", source)]
        deletes += [path.name for _ in re.finditer(r"DELETE\s+FROM\s+state_log\b", source)]
    assert updates == []
    assert deletes == ["maintenance.py"]


def test_clocks_read_while_the_sweep_runs_are_read_again_when_it_has_ended(monkeypatch):
    """A request comes after the sweep has said it begins and before its
    deletes: its clocks hold the rows about to go, kept under the sweep's
    first number. The second number, after the deletes, has them read again."""
    aid = _agent(0)
    _an_old_turn(aid)
    plain = maintenance.batched_delete.prune
    during = []

    def prune(*args, **kwargs):
        during.append(agents.dashboard_states()[aid]["last_turn_end"])
        return plain(*args, **kwargs)
    monkeypatch.setattr(maintenance.batched_delete, "prune", prune)
    assert maintenance._prune_state_log(db.conn(), 1000) == 2
    assert during == [200] and _pruned() == 2
    assert agents.dashboard_states()[aid]["last_turn_end"] is None
    assert agents.dashboard_states() == _as_it_was()
