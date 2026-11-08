"""Real SQLite concurrency with synthetic per-row work, never the live DB."""
import sqlite3
import threading
import time
import pytest

from lib import agents, db, message_store
from tests.patching import patch_shared


@pytest.mark.parametrize("writer", ["sqlite", "oracle-admission"])
def test_transcript_import_yields_to_independent_writer(tmp_path, monkeypatch, writer):
    agent = agents.create_agent(persona='Writer', voice_id='v', cwd=str(tmp_path), session='writer')
    path = db.conn().execute('PRAGMA database_list').fetchone()[2]
    entered = threading.Event()
    errors = []
    original = db._TrackedConnection.execute

    def slow_execute(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if threading.current_thread().name == 'test-import' and str(sql).lstrip().startswith('INSERT INTO messages'):
            entered.set()
            time.sleep(.004)  # deterministic stand-in for expensive per-row work
        return result

    monkeypatch.setattr(db._TrackedConnection, 'execute', slow_execute)

    def importing():
        try:
            message_store.store_transcript_turns(agent_id=agent, backend_session_id='one', source_file='fixture',
                turns=[{'role':'user', 'text':f'original {i}', 'timestamp':'2026-01-01T00:00:00Z'} for i in range(240)])
        except Exception as exc:
            errors.append(exc)
        finally:
            db.close_local()

    worker = threading.Thread(target=importing, name='test-import')
    worker.start()
    assert entered.wait(2)
    # The writer waits as long as a production writer does. What proves the
    # import yields is that the writer gets in while the import is still under
    # way, not a short timeout: SQLite's busy handler polls rather than queues,
    # and a .25 s budget could miss every 10 ms yield on a loaded CI runner
    # (Docker, e2365ef6, 2026-10-10) though batches were as short as ever.
    imported_when_admitted = None
    try:
        if writer == 'sqlite':
            with sqlite3.connect(path, timeout=5) as other:
                other.execute("INSERT INTO settings(key,value,updated_at) VALUES('concurrent-send','accepted',0)")
                imported_when_admitted = other.execute(
                    "SELECT count(*) FROM messages WHERE agent_id=?", (agent,)).fetchone()[0]
        else:
            from lib import oracle_delegations
            db.conn().execute('PRAGMA busy_timeout=5000')
            identity = dict(delegation_id='concurrent-operation', trace_id='trace',
                client_msg_id='request', agent_id=agent, session='writer',
                request_text='Keep this exact request', owner_principal='phone')
            row, created = oracle_delegations.begin(**identity)
            assert created and row['status'] == 'accepted'
            same, created_again = oracle_delegations.begin(**identity)
            assert not created_again and same['delegation_id'] == row['delegation_id']
            imported_when_admitted = db.conn().execute(
                "SELECT count(*) FROM messages WHERE agent_id=?", (agent,)).fetchone()[0]
    finally:
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert db.conn().execute("SELECT count(*) FROM messages WHERE agent_id=?", (agent,)).fetchone()[0] == 240
    assert imported_when_admitted is not None and imported_when_admitted < 240, \
        f"the writer only got in after the import finished ({imported_when_admitted} rows)"


# ---- RM-N7: a writer waiting in SQLite's busy handler is starved by an import ----
# SQLite's busy handler sleeps 1, 2, 5, ... 50, 100 ms between attempts, so a waiter
# on its 100 ms steps keeps missing the import's 10 ms gaps. Two fixes, each pinned
# deterministically here: Clarp's own lock-acquiring statements poll every 2 ms
# (lib.db), and the import yields longer than the busy handler's longest step at
# least once per second of held lock (lib.message_writes).

import types


class _FakeTime:
    """A clock for lib.message_writes only: rows cost time, sleeps are recorded."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []          # (clock when the sleep started, seconds)

    def monotonic(self):
        return self.now

    def perf_counter(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append((self.now, seconds))
        self.now += seconds


def test_an_import_yields_longer_than_the_busy_handlers_longest_step_every_second(
        tmp_path, monkeypatch):
    from lib import message_writes
    agent = agents.create_agent(persona='Writer', voice_id='v', cwd=str(tmp_path), session='writer')
    fake = _FakeTime()
    patch_shared(monkeypatch, message_writes, "time", types.SimpleNamespace(
        monotonic=fake.monotonic, perf_counter=fake.perf_counter, time=fake.time,
        sleep=fake.sleep))
    original = db._TrackedConnection.execute

    def costly(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if str(sql).lstrip().startswith('INSERT INTO messages'):
            fake.now += 0.004          # per-row work, as in the test above
        return result
    monkeypatch.setattr(db._TrackedConnection, 'execute', costly)
    message_store.store_transcript_turns(
        agent_id=agent, backend_session_id='one', source_file='fixture',
        turns=[{'role': 'user', 'text': f'row {i}', 'timestamp': '2026-01-01T00:00:00Z'}
               for i in range(600)])          # about 2.4 s of held lock
    held, worst, long_yields, last = 0.0, 0.0, 0, 1000.0
    for started, seconds in fake.sleeps:
        held += started - last
        last = started + seconds
        if seconds >= 0.101:
            worst, held, long_yields = max(worst, held), 0.0, long_yields + 1
    worst = max(worst, held)
    assert long_yields >= 2, fake.sleeps[:5]
    assert worst <= 1.0 + 0.025 + 0.004 + 1e-9, worst


def _holder(path):
    holder = sqlite3.connect(path, timeout=0, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO settings(key,value,updated_at) VALUES('held','x',0)")
    return holder


def test_a_lock_acquiring_statement_polls_every_2_ms_and_gets_in_at_once(tmp_path, monkeypatch):
    connection = db.conn()
    path = connection.execute('PRAGMA database_list').fetchone()[2]
    holder = _holder(path)
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        if len(polls) == 3:
            holder.execute("COMMIT")          # the import's gap opens
    monkeypatch.setattr(db, "time", types.SimpleNamespace(
        monotonic=time.monotonic, perf_counter=time.perf_counter, time=time.time, sleep=sleep))
    connection.execute("PRAGMA busy_timeout = 300")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO settings(key,value,updated_at) VALUES('waiter','in',0)")
        connection.execute("COMMIT")
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()
    assert polls and all(s <= 0.002 for s in polls) and len(polls) == 3, polls
    assert int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) == 300


def test_an_autocommit_write_polls_too(tmp_path, monkeypatch):
    connection = db.conn()
    path = connection.execute('PRAGMA database_list').fetchone()[2]
    holder = _holder(path)
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        if len(polls) == 2:
            holder.execute("COMMIT")
    monkeypatch.setattr(db, "time", types.SimpleNamespace(
        monotonic=time.monotonic, perf_counter=time.perf_counter, time=time.time, sleep=sleep))
    connection.execute("PRAGMA busy_timeout = 300")
    try:
        connection.execute("INSERT INTO settings(key,value,updated_at) VALUES('auto','in',0)")
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()
    assert len(polls) == 2 and all(s <= 0.002 for s in polls), polls


def test_a_writer_that_never_gets_in_fails_as_before_after_its_own_deadline(tmp_path, monkeypatch):
    connection = db.conn()
    path = connection.execute('PRAGMA database_list').fetchone()[2]
    holder = _holder(path)
    reports = []
    monkeypatch.setattr(db, "_report_database_locked", lambda sql: reports.append(sql))
    connection.execute("PRAGMA busy_timeout = 120")
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            connection.execute("BEGIN IMMEDIATE")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    waited = time.monotonic() - started
    assert 0.110 <= waited < 1.5, waited
    assert len(reports) == 1
    assert int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) == 120
    assert not connection.in_transaction


@pytest.mark.parametrize("transaction", ["explicit", "implicit"])
def test_nothing_inside_a_transaction_is_retried(tmp_path, monkeypatch, transaction):
    path = db.conn().execute('PRAGMA database_list').fetchone()[2]
    polls = []
    monkeypatch.setattr(db, "time", types.SimpleNamespace(
        monotonic=time.monotonic, perf_counter=time.perf_counter, time=time.time,
        sleep=lambda s: polls.append(s)))
    if transaction == "explicit":
        connection = db.conn()
        connection.execute("BEGIN")                       # deferred: holds no lock yet
        connection.execute("SELECT count(*) FROM settings").fetchone()
    else:
        # Python's implicit BEGIN before DML (isolation_level set)
        connection = sqlite3.connect(path, timeout=0.05, isolation_level="DEFERRED",
                                     factory=db._TrackedConnection)
    holder = _holder(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO settings(key,value,updated_at) VALUES('in-txn','x',0)")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if transaction == "implicit":
            connection.close()
    assert polls == []


def test_a_write_that_waited_long_logs_one_line(tmp_path, monkeypatch):
    from lib import log as log_module
    connection = db.conn()
    path = connection.execute('PRAGMA database_list').fetchone()[2]
    holder = _holder(path)
    lines = []
    monkeypatch.setattr(log_module, "log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    timer = threading.Timer(0.3, lambda: holder.execute("COMMIT"))
    timer.start()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("COMMIT")
    finally:
        timer.join()
        holder.close()
    waits = [(e, d) for e, d in lines if e == "sqliteWriteWait"]
    assert len(waits) == 1, lines
    assert "waited_ms=" in waits[0][1] and "import_in_process=False" in waits[0][1]


def test_the_long_wait_log_names_the_program_but_never_its_arguments(tmp_path, monkeypatch):
    from lib import log as log_module
    connection = db.conn()
    path = connection.execute('PRAGMA database_list').fetchone()[2]
    holder = _holder(path)
    lines = []
    monkeypatch.setattr(log_module, "log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    monkeypatch.setattr("sys.argv", ["/opt/clarp/bin/clarp-admin", "prompt", "--text", "private words"])
    timer = threading.Timer(0.3, lambda: holder.execute("COMMIT"))
    timer.start()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("COMMIT")
    finally:
        timer.join()
        holder.close()
    waits = [d for e, d in lines if e == "sqliteWriteWait"]
    assert len(waits) == 1, lines
    assert "process='clarp-admin'" in waits[0]
    assert "private" not in waits[0] and "prompt" not in waits[0] and "/opt" not in waits[0]


def test_the_long_wait_log_knows_when_this_processs_import_holds_the_lock(tmp_path):
    db.conn()
    holding, release, done = threading.Event(), threading.Event(), threading.Event()

    def import_batch():
        try:
            db.note_import_holds_lock(True)
            connection = db.conn()
            connection.execute("BEGIN IMMEDIATE")
            holding.set()
            release.wait(5)
            connection.execute("COMMIT")
            db.note_import_holds_lock(False)
        finally:
            db.close_local()
            done.set()
    worker = threading.Thread(target=import_batch, name="test-import")
    worker.start()
    assert holding.wait(5)
    try:
        assert db._import_holds_lock() is True
    finally:
        release.set()
        worker.join(5)
    assert done.is_set() and db._import_holds_lock() is False


# Reviewer A's controls on a1aa19ce (tests/unit/test_review_rmn7_failed_wait.py on
# refactor/review-core): a stale read snapshot fails at once as before, an exhausted
# long wait still logs its one line, and the import attribution is taken while waiting.

def test_an_exhausted_long_write_wait_emits_one_wait_line(tmp_path, monkeypatch):
    from lib import log as log_module
    connection = db.conn()
    path = connection.execute('PRAGMA database_list').fetchone()[2]
    holder = _holder(path)
    lines = []
    monkeypatch.setattr(log_module, 'log', lambda event, detail='', *a, **k: lines.append((event, detail)))
    monkeypatch.setattr(db, '_report_database_locked', lambda *_a: None)
    connection.execute('PRAGMA busy_timeout=300')
    try:
        with pytest.raises(sqlite3.OperationalError, match='database is locked'):
            connection.execute('BEGIN IMMEDIATE')
    finally:
        holder.execute('ROLLBACK')
        holder.close()
    assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 300
    waits = [detail for event, detail in lines if event == 'sqliteWriteWait']
    assert len(waits) == 1 and 'outcome=gave_up' in waits[0], waits


def test_a_stale_autocommit_cursor_snapshot_is_not_polled(tmp_path, monkeypatch):
    connection = db.conn()
    connection.execute("INSERT INTO settings(key,value,updated_at) VALUES"
                       "('snap-a','old',0),('snap-b','old',0),('snap-c','old',0)")
    cursor = connection.execute('SELECT * FROM settings ORDER BY key')
    assert cursor.fetchone() is not None
    assert not connection.in_transaction
    path = connection.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path, isolation_level=None) as other:
        other.execute("UPDATE settings SET value='new' WHERE key='snap-a'")
    polls = []
    monkeypatch.setattr(db, 'time', types.SimpleNamespace(
        monotonic=time.monotonic, perf_counter=time.perf_counter, time=time.time,
        sleep=lambda s: (polls.append(s), time.sleep(s))))
    connection.execute('PRAGMA busy_timeout=30')
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            connection.execute("UPDATE settings SET value='ours' WHERE key='snap-b'")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        assert not polls, 'SQLITE_BUSY_SNAPSHOT was retried; no poll can refresh the open snapshot'
    finally:
        cursor.close()


def test_a_successful_wait_retains_import_attribution(tmp_path, monkeypatch):
    from lib import log as log_module
    connection = db.conn()
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    errors, lines = [], []

    def importing():
        try:
            db.note_import_holds_lock(True)
            c = db.conn()
            c.execute('BEGIN IMMEDIATE')
            held.set()
            assert release.wait(3)
            c.execute('COMMIT')
        except Exception as exc:
            errors.append(exc)
        finally:
            db.note_import_holds_lock(False)
            db.close_local()
            done.set()
    worker = threading.Thread(target=importing)
    worker.start()
    assert held.wait(3)
    assert db._import_holds_lock()
    started = time.monotonic()

    def sleep(seconds):
        if time.monotonic() - started >= .27:
            release.set()
            assert done.wait(3)
        else:
            time.sleep(seconds)
    monkeypatch.setattr(db, 'time', types.SimpleNamespace(
        monotonic=time.monotonic, perf_counter=time.perf_counter, time=time.time, sleep=sleep))
    monkeypatch.setattr(log_module, 'log', lambda event, detail='', *a, **k: lines.append((event, detail)))
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('COMMIT')
    finally:
        release.set()
        worker.join(3)
    assert not errors
    waits = [detail for event, detail in lines if event == 'sqliteWriteWait']
    assert len(waits) == 1
    assert 'import_in_process=True' in waits[0] and 'outcome=admitted' in waits[0], waits


_CARRY = {"consumed_client_ids": [], "current_origin": "user", "current_sender_agent_id": "",
          "current_heartbeat_key": "", "assistant_ordinal": 0, "authority_tools": {},
          "closed_authority": [], "unanswered_key": "", "current_request_trace": "",
          "adopted_final_ids": [], "newest_trace": ""}


@pytest.mark.parametrize("entry", ["full", "tail"])
def test_a_failed_import_leaves_no_import_marker_on_its_thread(tmp_path, monkeypatch, entry):
    """Reviewer C's control: an import that fails inside its locked writer,
    through either entry point, must not leave the thread's next ordinary
    transaction recorded as an import's."""
    from lib import message_writes
    agent = agents.create_agent(persona='Writer', voice_id='v', cwd=str(tmp_path), session='writer')
    original = db._TrackedConnection.execute

    def failing(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if str(sql).lstrip().startswith('INSERT INTO messages'):
            raise RuntimeError("injected after a real row write")
        return result
    monkeypatch.setattr(db._TrackedConnection, 'execute', failing)
    turns = [{'role': 'user', 'text': 'row', 'timestamp': '2026-01-01T00:00:00Z'}]
    with pytest.raises(RuntimeError):
        if entry == "full":
            message_store.store_transcript_turns(agent_id=agent, backend_session_id='one',
                                                 source_file='fixture', turns=turns)
        else:
            message_writes.store_transcript_tail(agent_id=agent, backend_session_id='one',
                                                 source_file='fixture', turns=turns,
                                                 start_seq=0, carry=dict(_CARRY))
    monkeypatch.setattr(db._TrackedConnection, 'execute', original)
    connection = db.conn()
    assert not connection.in_transaction
    assert not getattr(db._LOCAL, "import_batch", False)
    connection.execute("BEGIN IMMEDIATE")
    try:
        owner = db._TRANSACTION_OWNERS[id(connection)]
        assert owner.is_import is False
    finally:
        connection.execute("ROLLBACK")
