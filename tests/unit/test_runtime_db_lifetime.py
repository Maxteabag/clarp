"""Real disposable SQLite and Unix RPC lifetime/resource-policy witnesses."""
import json
import queue
import socket
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from lib import db, runtime_bridge as bridge, telemetry


def closed(con):
    try:
        con.execute('SELECT 1')
    except sqlite3.ProgrammingError:
        return True
    return False


def open_stores():
    state, metrics = db.conn(), telemetry.conn()
    state.execute('CREATE TABLE IF NOT EXISTS memory_probe (value INTEGER)')
    metrics.execute('CREATE TABLE IF NOT EXISTS memory_probe (value INTEGER)')
    return state, metrics


@pytest.fixture
def rpc(tmp_path, monkeypatch):
    servers = []
    original_finish = bridge._RuntimeRequestHandler.finish
    finished = queue.Queue()
    def observe_finish(handler):
        handles = (getattr(db._LOCAL, 'conn', None), getattr(telemetry._LOCAL, 'conn', None))
        try:
            original_finish(handler)
        finally:
            finished.put(handles)
    monkeypatch.setattr(bridge._RuntimeRequestHandler, 'finish', observe_finish)
    def create(work):
        path = tmp_path / f'rpc-{len(servers)}.sock'
        server = bridge.RuntimeRPCServer(path, dispatch_service=SimpleNamespace())
        server.dispatch_request = work
        server.handle_error = lambda *_: None  # test-owned deliberate transport errors
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        servers.append((server, thread))
        return path, server, finished
    yield create
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(path, raw=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        client.sendall(bridge.encode_request('status') if raw is None else raw)
        client.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := client.recv(65536):
            chunks.append(chunk)
        return b''.join(chunks)


def test_real_connection_budget_reopen_and_existing_policies():
    con = db.conn()
    with sqlite3.connect(':memory:') as default:
        native_default = default.execute('PRAGMA cache_size').fetchone()[0]
    print(json.dumps({'sqlite_default_cache_kib': native_default, 'telemetry_cache_kib': telemetry.conn().execute('PRAGMA cache_size').fetchone()[0]}))
    assert con.execute('PRAGMA cache_size').fetchone()[0] == -8192
    before = [con.execute(f'PRAGMA {name}').fetchone()[0] for name in
              ('mmap_size', 'journal_mode', 'synchronous', 'foreign_keys', 'busy_timeout')]
    assert before[:4] == [1073741824, 'wal', 1, 1]
    assert con is db.conn()
    con.execute('CREATE TABLE memory_reopen(value INTEGER)')
    con.execute('INSERT INTO memory_reopen VALUES(7)')
    db.close_local()
    again = db.conn()
    assert again is not con and closed(con)
    assert again.execute('SELECT value FROM memory_reopen').fetchone()[0] == 7
    assert [again.execute(f'PRAGMA {name}').fetchone()[0] for name in
            ('mmap_size', 'journal_mode', 'synchronous', 'foreign_keys', 'busy_timeout')] == before
    with sqlite3.connect(':memory:') as default:
        baseline = default.execute('PRAGMA cache_size').fetchone()[0]
    metric = telemetry.conn()
    assert metric.execute('PRAGMA cache_size').fetchone()[0] == -2000
    assert abs(metric.execute('PRAGMA cache_size').fetchone()[0]) <= abs(baseline)
    telemetry.close_local()
    assert telemetry.conn() is not metric and closed(metric)


@pytest.mark.parametrize('outcome', ['success', 'protocol', 'exception', 'abandoned', 'write-fail', 'flush-fail'])
def test_handler_finish_closes_both_and_preserves_transaction_outcome(rpc, monkeypatch, outcome):
    captured = []
    def work(*_):
        state, metric = open_stores()
        captured.extend((state, metric))
        if outcome == 'abandoned':
            state.execute('BEGIN IMMEDIATE')
            state.execute('INSERT INTO memory_probe VALUES(99)')
            raise RuntimeError('synthetic failure')
        assert state.execute('INSERT INTO memory_probe VALUES(7) RETURNING value').fetchone()[0] == 7
        metric.execute('INSERT INTO memory_probe VALUES(7)')
        if outcome == 'protocol':
            raise bridge.RuntimeProtocolError('synthetic protocol refusal')
        if outcome == 'exception':
            raise RuntimeError('synthetic failure')
        return {'ok': True, 'result': {'counter': 7}}
    if outcome == 'flush-fail':
        setup = bridge._RuntimeRequestHandler.setup
        class FlushFailure:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, key):
                return getattr(self.wrapped, key)
            def flush(self):
                raise RuntimeError('synthetic finish flush failure')
        def setup_with_failed_flush(handler):
            setup(handler)
            handler.wfile = FlushFailure(handler.wfile)
        monkeypatch.setattr(bridge._RuntimeRequestHandler, 'setup', setup_with_failed_flush)
    if outcome == 'write-fail':
        def fail(*_):
            raise BrokenPipeError('synthetic disconnect')
        monkeypatch.setattr(bridge._RuntimeRequestHandler, '_write', fail)
    path, _, finished = rpc(work)
    response = request(path)
    handles = finished.get(timeout=5)
    assert len(captured) == 2 and all(closed(c) for c in handles)
    state, _ = open_stores()
    count = state.execute('SELECT count(*) FROM memory_probe').fetchone()[0]
    assert count == (0 if outcome == 'abandoned' else 1)
    assert all(id(c) not in db._TRANSACTION_OWNERS for c in captured)
    if outcome in ('success', 'protocol', 'exception', 'abandoned', 'flush-fail'):
        result = json.loads(response)
        assert result['ok'] is (outcome in ('success', 'flush-fail'))
    else:
        assert not response


@pytest.mark.parametrize('raw', [b'', b'not-json\n', b'{}\n', b'x' * (bridge.MAX_MESSAGE_BYTES + 1)])
def test_early_request_cleanup_opens_neither_store(rpc, monkeypatch, raw):
    def unexpected(*_):
        pytest.fail('unused SQLite store was opened during cleanup')
    monkeypatch.setattr(db, '_open_connection', unexpected)
    monkeypatch.setattr(telemetry, '_open', unexpected)
    path, _, finished = rpc(lambda *_: pytest.fail('early request reached dispatch'))
    request(path, raw)
    assert finished.get(timeout=5) == (None, None)


@pytest.mark.parametrize('failed_store', ['state', 'telemetry'])
def test_cleanup_failure_attempts_both_without_telemetry_logging_or_result_change(rpc, monkeypatch, capsys, failed_store):
    calls = []
    for name, module in [('state', db), ('telemetry', telemetry)]:
        original = module.close_local
        def close(original=original, name=name):
            calls.append(name)
            original()
            if name == failed_store and threading.current_thread() is not threading.main_thread():
                raise RuntimeError('synthetic close failure')
        monkeypatch.setattr(module, 'close_local', close)
    opens = []
    original_open = telemetry._open
    def observe_open(path):
        opens.append(1)
        return original_open(path)
    monkeypatch.setattr(telemetry, '_open', observe_open)
    def work(*_):
        open_stores()
        return {'ok': True, 'result': 7}
    path, _, finished = rpc(work)
    assert json.loads(request(path)) == {'ok': True, 'result': 7}
    assert all(closed(con) for con in finished.get(timeout=5))
    assert len(opens) == 1, 'cleanup logging reopened telemetry'
    assert calls == ['state', 'telemetry']
    assert 'runtimeDbCloseFail' in capsys.readouterr().err


def test_live_stream_keeps_its_handles_until_finish_and_preserves_other_thread(rpc):
    active, release = threading.Event(), threading.Event()
    captured = []
    class Fanout:
        def subscribe(self):
            captured.extend(open_stores())
            active.set()
            assert release.wait(5), 'stream release not signalled'
            assert all(not closed(c) for c in captured)
            return SimpleNamespace(dropped=True)
        def unsubscribe(self, _):
            pass
    path, server, finished = rpc(lambda *_: {'ok': True})
    server.live_fanout = Fanout()
    other = db.conn()
    other.execute('CREATE TABLE memory_other(value INTEGER)')
    other.execute('BEGIN IMMEDIATE')
    other.execute('INSERT INTO memory_other VALUES(3)')
    # Stream performs DDL before the independent writer starts its transaction.
    other.rollback()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        client.sendall(bridge.encode_request('live_stream'))
        assert active.wait(5), 'live stream did not reach its held window'
        other.execute('BEGIN IMMEDIATE')
        other.execute('INSERT INTO memory_other VALUES(3)')
        try:
            assert all(not closed(c) for c in captured)
        finally:
            release.set()
        assert client.recv(65536)
    handles = finished.get(timeout=5)
    assert all(closed(c) for c in handles)
    assert not closed(other) and other.in_transaction
    assert id(other) in db._TRANSACTION_OWNERS
    other.commit()
    assert other.execute('SELECT count(*) FROM memory_other').fetchone()[0] == 1
