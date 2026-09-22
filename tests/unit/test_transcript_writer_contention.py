"""Real SQLite concurrency with synthetic per-row work, never the live DB."""
import sqlite3
import threading
import time
import pytest

from lib import agents, db, message_store


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
    try:
        if writer == 'sqlite':
            with sqlite3.connect(path, timeout=.25) as other:
                other.execute("INSERT INTO settings(key,value,updated_at) VALUES('concurrent-send','accepted',0)")
        else:
            from lib import oracle_delegations
            db.conn().execute('PRAGMA busy_timeout=250')
            identity = dict(delegation_id='concurrent-operation', trace_id='trace',
                client_msg_id='request', agent_id=agent, session='writer',
                request_text='Keep this exact request', owner_principal='phone')
            row, created = oracle_delegations.begin(**identity)
            assert created and row['status'] == 'accepted'
            same, created_again = oracle_delegations.begin(**identity)
            assert not created_again and same['delegation_id'] == row['delegation_id']
    finally:
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert db.conn().execute("SELECT count(*) FROM messages WHERE agent_id=?", (agent,)).fetchone()[0] == 240
