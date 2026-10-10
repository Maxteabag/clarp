"""Background writers must not hold SQLite's single write lock across bulk work.

On 2026-10-10 the HTTP process's transcript import and Janitor admission held
BEGIN IMMEDIATE for 5-50 s on an IO-starved host (16 GiB in swap). Every other
writer in both processes hit its 5 s busy timeout with "database is locked",
and queued handoffs wedged (Pebble 07:26, Nadia 08:27). An import re-reads the
whole transcript on every inotify tick; measured on the live database, 11,385
of 11,396 message upserts across twelve imports rewrote an unchanged row.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

import pytest

from lib import agents as agents_db, db, message_store


class _Recording:
    """The import's connection, timing the work done in each write transaction.

    ``read_delay`` makes every SELECT slow, as page faults did on the
    swapping host, so work done under the lock shows up as hold time.
    """

    def __init__(self, inner, read_delay: float = 0.0):
        self._inner = inner
        self._read_delay = read_delay
        self._began = None
        self.holds: list[float] = []
        self.begins = 0

    def execute(self, sql, *args):
        verb = str(sql).split(None, 1)[0].upper()
        if verb == "SELECT" and self._read_delay:
            time.sleep(self._read_delay)
        if verb in {"COMMIT", "ROLLBACK"} and self._began is not None:
            # Up to the commit: writing its WAL frames is the disk's time.
            self.holds.append(time.monotonic() - self._began)
            self._began = None
        result = self._inner.execute(sql, *args)
        if verb == "BEGIN":
            self.begins += 1
            self._began = time.monotonic()
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _record(monkeypatch, read_delay: float = 0.0) -> _Recording:
    recording = _Recording(db.conn(), read_delay)
    monkeypatch.setattr(message_store, "conn", lambda: recording)
    return recording


def _agent(tmp_path) -> str:
    return agents_db.create_agent(persona="Cipher", voice_id="V", cwd=str(tmp_path),
                                  session="cipher", backend="codex")


def _turns(count: int, *, traced: bool = False) -> list[dict]:
    turns = []
    for i in range(count):
        stamp = f"2026-10-10T07:{(i // 60) % 60:02d}:{i % 60:02d}Z"
        turns.append({"role": "user", "text": f"question {i}", "timestamp": stamp,
                      **({"trace_id": f"trace-{i}"} if traced else {})})
        turns.append({"role": "assistant", "text": f"answer {i}", "timestamp": stamp})
    return turns


def _rows(agent_id: str) -> list[tuple]:
    return [tuple(row) for row in db.conn().execute(
        """SELECT message_id, seq, role, text, revision, updated_at, trace_id, phase,
                  origin, source_file FROM messages WHERE agent_id=? ORDER BY seq""",
        (agent_id,))]


def _head(agent_id: str) -> tuple:
    row = db.conn().execute(
        "SELECT revision, replace_revision FROM conversation_heads WHERE agent_id=?",
        (agent_id,)).fetchone()
    return tuple(row) if row else ()


def _store(agent_id: str, turns: list[dict]) -> None:
    message_store.store_transcript_turns(agent_id=agent_id, backend_session_id="bs",
                                         source_file="rollout.jsonl", turns=turns)


def test_unchanged_reimport_takes_no_write_lock_and_keeps_rows(tmp_path, monkeypatch):
    agent_id = _agent(tmp_path)
    turns = _turns(40, traced=True)
    _store(agent_id, turns)
    rows, head = _rows(agent_id), _head(agent_id)
    assert len(rows) == 80 and head and head[0] == max(row[4] for row in rows)

    recording = _record(monkeypatch)
    _store(agent_id, turns)

    assert recording.begins == 0, "an unchanged transcript must not take the write lock"
    assert _rows(agent_id) == rows
    assert _head(agent_id) == head


def test_changed_import_rewrites_only_changed_rows(tmp_path, monkeypatch):
    agent_id = _agent(tmp_path)
    turns = _turns(20)
    _store(agent_id, turns)
    before = {row[1]: row for row in _rows(agent_id)}
    old_high = max(row[4] for row in before.values())

    changed = [dict(turn) for turn in turns]
    changed[7]["text"] = "answer 3, revised"
    changed += _turns(22)[40:]
    # One batch: only the row count may decide, not a loaded runner's clock.
    monkeypatch.setattr(message_store, "IMPORT_WRITE_BUDGET_SECONDS", 60)
    recording = _record(monkeypatch)
    _store(agent_id, changed)

    after = {row[1]: row for row in _rows(agent_id)}
    assert [after[seq][3] for seq in sorted(after)] == [turn["text"] for turn in changed]
    rewritten = {seq for seq in after if after[seq][4] > old_high}
    assert rewritten == {7, 40, 41, 42, 43}
    assert all(after[seq] == before[seq] for seq in before if seq != 7)
    assert _head(agent_id)[0] == max(row[4] for row in after.values())
    assert recording.begins == 1


@pytest.fixture
def memory_backed_db():
    """Measure the lock, not the runner's disk.

    On a host under IO pressure a commit's WAL write alone took 1-2 s, which
    no change to the import can shorten. The database is a few MB.
    """
    if not os.path.isdir("/dev/shm"):
        yield
        return
    original = db.DB_PATH
    folder = tempfile.mkdtemp(prefix="clarp-writer-lock-", dir="/dev/shm")
    db.reset_for_tests(pathlib.Path(folder) / "state.sqlite")
    try:
        yield
    finally:
        db.reset_for_tests(original)
        shutil.rmtree(folder)


def test_slow_import_never_blocks_a_short_writer(tmp_path, monkeypatch, memory_backed_db):
    agent_id = _agent(tmp_path)
    # A WAL autocheckpoint runs in whichever COMMIT crosses the threshold,
    # after the lock is released; it is not lock time either.
    db.conn().execute("PRAGMA wal_autocheckpoint = 0")
    recording = _record(monkeypatch, read_delay=0.006)
    stop = threading.Event()
    waits: list[float] = []
    failures: list[BaseException] = []

    def short_writer():
        try:
            other = sqlite3.connect(str(db.DB_PATH), timeout=30, isolation_level=None)
            other.execute("PRAGMA wal_autocheckpoint = 0")
            # As db.py's connections: no fsync per commit in WAL mode.
            other.execute("PRAGMA synchronous = NORMAL")
            while not stop.is_set():
                started = time.monotonic()
                other.execute("UPDATE agents SET persona=? WHERE agent_id=?",
                              (f"tick {len(waits)}", agent_id))
                waits.append(time.monotonic() - started)
                time.sleep(0.005)
            other.close()
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    thread = threading.Thread(target=short_writer)
    thread.start()
    try:
        _store(agent_id, _turns(200, traced=True))
    finally:
        stop.set()
        thread.join(10)

    assert not failures
    assert len(_rows(agent_id)) == 400
    assert waits, "the short writer never ran"
    # Batches are budgeted at 25 ms. Before the fix the final batch alone
    # read under the lock for ~2.5 s; the waiter's bound also covers commits'
    # disk writes on a loaded runner.
    assert max(recording.holds) < 0.3, f"longest import hold {max(recording.holds):.3f}s"
    assert max(waits) < 2.0, f"short writer waited {max(waits):.3f}s"


def test_janitor_demand_admission_resolves_configuration_outside_the_write_lock(monkeypatch):
    import importlib
    from lib import backends
    builtins = importlib.import_module("lib.janitor_builtins")
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])
    config = builtins.ensure_builtins(cwd="/tmp", initial={"message-delegator": {"enabled": True}})["message-delegator"]
    resolve, held = builtins.resolve, []

    def watched(*args, **kwargs):
        held.append(db.conn().in_transaction)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(builtins, "resolve", watched)
    run = builtins.begin_run("message-delegator", "request-outside-lock")
    assert run is not None and run["agent_id"] == config["agent_id"]
    assert held and not any(held), "configuration was resolved under BEGIN IMMEDIATE"


def test_janitor_demand_admission_rereads_a_configuration_changed_after_resolving(monkeypatch):
    import importlib
    from lib import backends, janitors
    builtins = importlib.import_module("lib.janitor_builtins")
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])
    config = builtins.ensure_builtins(cwd="/tmp", initial={"message-delegator": {"enabled": True}})["message-delegator"]
    resolve, calls = builtins.resolve, []

    def racing(*args, **kwargs):
        resolved = resolve(*args, **kwargs)
        if not calls:
            # Another request reconfigures between the read and the write lock.
            changed = janitors.configure(config["session"], config["revision"], effort="medium")
            janitors.set_enabled(config["session"], changed["revision"], True)
        calls.append(resolved)
        return resolved

    monkeypatch.setattr(builtins, "resolve", racing)
    run = builtins.begin_run("message-delegator", "request-raced")
    assert len(calls) == 2
    assert run["configuration"]["effort"] == "medium"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify")
def test_inotify_burst_imports_each_agent_once(tmp_path, monkeypatch):
    from lib import transcript_watcher

    class Counting:
        def __init__(self):
            self.ticks = 0
            self.ticked = threading.Event()

        def tick(self):
            self.ticks += 1
            self.ticked.set()
            return 1

    watchers = {"a": Counting(), "b": Counting()}

    class Pool:
        def tick_all(self):
            return {}

        def watcher_for(self, agent_id):
            return watchers.get(agent_id)

    monkeypatch.setattr(transcript_watcher, "INOTIFY_COALESCE_SEC", 0.3)
    dispatcher = transcript_watcher.InotifyDispatcher(Pool())
    paths = {agent_id: tmp_path / f"{agent_id}.jsonl" for agent_id in watchers}
    for agent_id, path in paths.items():
        dispatcher.watch(agent_id, path)
    dispatcher.start()
    try:
        # A backend streams a reply as many small appends in quick succession.
        for i in range(12):
            for path in paths.values():
                with path.open("a") as handle:
                    handle.write(f'{{"n": {i}}}\n')
            time.sleep(0.01)
        assert all(w.ticked.wait(3) for w in watchers.values())
        time.sleep(0.4)
    finally:
        dispatcher.stop()
    assert {agent_id: w.ticks for agent_id, w in watchers.items()} == {"a": 1, "b": 1}


def test_critical_turn_writes_wait_out_a_long_writer(tmp_path):
    """Opening a turn outlasts the interactive 5 s budget instead of wedging."""
    from lib import timing, turn_lifecycle
    agent_id = _agent(tmp_path)
    assert timing.SQLITE_CRITICAL_BUSY_TIMEOUT_MS > timing.SQLITE_BUSY_TIMEOUT_MS
    holder = sqlite3.connect(str(db.DB_PATH), timeout=5, isolation_level=None,
                             check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    released = threading.Timer(0.6, lambda: holder.execute("COMMIT"))
    db.conn().execute("PRAGMA busy_timeout = 200")
    try:
        released.start()
        turn_id = turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="t-critical")
    finally:
        released.join()
        holder.close()
    assert turn_id
    assert db.conn().execute("PRAGMA busy_timeout").fetchone()[0] == 200, \
        "the caller's own busy timeout is restored"
