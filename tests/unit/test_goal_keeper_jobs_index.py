"""The additive terminal-job index is optional at boot and never versions data."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from lib import db, goal_keeper, maintenance

INDEX = 'idx_background_jobs_agent_terminal'


def _has_index(con):
    return con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)).fetchone()[0] == 1


def _existing_without_index():
    con = db.conn()
    con.execute(f'DROP INDEX IF EXISTS {INDEX}')
    return db.DB_PATH, con.execute('PRAGMA user_version').fetchone()[0]


def _start(monkeypatch, path, role):
    db.reset_for_tests(path)
    monkeypatch.setattr(db, '_SERVICE', role)
    return db.conn()


def test_fresh_schema_has_covering_terminal_searches_without_new_version():
    con = db.conn()
    version = con.execute('PRAGMA user_version').fetchone()[0]
    assert _has_index(con)
    for sql, args in [
        ('SELECT terminal_at FROM background_jobs WHERE agent_id IN (?,?) AND terminal_at>?', ('owner','helper',100)),
        ('SELECT MAX(terminal_at) FROM background_jobs WHERE agent_id IN (?,?)', ('owner','helper')),
    ]:
        plan = ' '.join(row[3] for row in con.execute('EXPLAIN QUERY PLAN '+sql, args))
        assert 'SEARCH' in plan and 'COVERING INDEX '+INDEX in plan
        assert '(agent_id=?' in plan, 'terminal-first index can still walk other agents'
        if 'terminal_at>?' in sql:
            assert 'AND terminal_at>?' in plan
    assert con.execute('PRAGMA user_version').fetchone()[0] == version


@pytest.mark.parametrize('role', ['host','runtime',None])
def test_only_host_service_adds_index_to_an_existing_same_version_db(monkeypatch, role):
    path, version = _existing_without_index()
    con = _start(monkeypatch, path, role)
    assert _has_index(con) is (role == 'host')
    assert con.execute('PRAGMA user_version').fetchone()[0] == version
    assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_held_writer_does_not_turn_optional_index_into_a_startup_failure(monkeypatch, capsys):
    path, version = _existing_without_index()
    db.close_local()
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    try:
        at = time.monotonic()
        con = _start(monkeypatch, path, 'host')
        duration = time.monotonic() - at
        assert con.execute('SELECT 1').fetchone()[0] == 1
        assert not _has_index(con)
        assert con.execute('PRAGMA user_version').fetchone()[0] == version
        assert duration < 0.5, 'optional index used the normal multi-second writer deadline'
        lines = [line for line in capsys.readouterr().err.splitlines() if INDEX in line]
        assert len(lines) == 1 and ('BUSY' in lines[0] or 'timeout' in lines[0])
    finally:
        blocker.rollback(); blocker.close()
    con = _start(monkeypatch, path, 'host')
    assert _has_index(con)
    lines = [line for line in capsys.readouterr().err.splitlines() if INDEX in line]
    assert len(lines) == 1 and ('ready' in lines[0] or 'created' in lines[0])
    assert con.execute('PRAGMA user_version').fetchone()[0] == version


def test_existing_index_start_never_requests_a_writer(monkeypatch, capsys):
    con = db.conn()
    con.execute(f'CREATE INDEX IF NOT EXISTS {INDEX} ON background_jobs(agent_id,terminal_at)')
    path = db.DB_PATH
    db.close_local()
    opened = db._open_connection
    statements = []
    def traced():
        connection = opened(); connection.set_trace_callback(statements.append); return connection
    monkeypatch.setattr(db, '_open_connection', traced)
    blocker = sqlite3.connect(path, isolation_level=None); blocker.execute('BEGIN IMMEDIATE')
    try:
        at = time.monotonic(); con = _start(monkeypatch, path, 'host')
        assert time.monotonic() - at < 0.5
        assert _has_index(con)
    finally:
        blocker.rollback(); blocker.close()
    assert not any(sql.strip().upper().startswith(('BEGIN IMMEDIATE','CREATE INDEX')) for sql in statements)
    assert not [line for line in capsys.readouterr().err.splitlines() if INDEX in line]


def test_maintenance_size_pass_is_the_existing_owner_of_index_retry(monkeypatch, tmp_path):
    path, version = _existing_without_index()
    monkeypatch.setattr(db, '_SERVICE', 'host')
    worker = maintenance.MaintenanceWorker(audio_dir=tmp_path)
    monkeypatch.setattr(worker, '_prune_event_logs', lambda: 0)
    from lib import telemetry
    monkeypatch.setattr(telemetry, 'over_budget', lambda: False)
    worker.check_sizes()
    assert _has_index(db.conn())
    assert db.conn().execute('PRAGMA user_version').fetchone()[0] == version
    assert worker.policy.size_check_interval_sec == 300


CHILD_START = '''import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
from lib import db,db_schema,schema_guard
path=pathlib.Path(sys.argv[2]);db.reset_for_tests(path)
schema_guard.check_host_start(path,path.parent,code=db_schema._SCHEMA_VERSION)
db.run_as_service('host')
print('ready',flush=True)
assert sys.stdin.readline().strip()=='go'
c=db.conn()
print(json.dumps({'version':c.execute('PRAGMA user_version').fetchone()[0], 'indexes':c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_background_jobs_agent_terminal'").fetchone()[0]}),flush=True)
'''


def test_concurrent_service_process_starts_create_one_index(monkeypatch):
    path, version = _existing_without_index()
    db.close_local()
    children = [subprocess.Popen([sys.executable,'-c',CHILD_START,
        str(Path(__file__).resolve().parents[2]/'server'),str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    for child in children:
        assert child.stdout.readline().strip() == 'ready'
    for child in children:
        child.stdin.write('go\n'); child.stdin.flush()
    outputs = []
    notices = []
    for child in children:
        out, err = child.communicate(timeout=5)
        assert child.returncode == 0, err
        assert not any('failed' in line for line in err.splitlines()), err
        outputs.append(json.loads(out))
        notices.extend(line for line in err.splitlines() if INDEX in line)
    assert outputs == [{'version':version,'indexes':1}]*2
    assert len(notices) == 1 and 'ready' in notices[0]


def test_unexpected_index_error_is_logged_but_startup_still_succeeds(monkeypatch, capsys):
    path, version = _existing_without_index()
    connect = sqlite3.connect
    def fail_short_attempt(*args, **kwargs):
        if 0 < kwargs.get('timeout', 5) < 1:
            raise RuntimeError('synthetic index opener failure')
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', fail_short_attempt)
    con = _start(monkeypatch, path, 'host')
    assert con.execute('SELECT 1').fetchone()[0] == 1
    assert not _has_index(con)
    assert con.execute('PRAGMA user_version').fetchone()[0] == version
    lines = [line for line in capsys.readouterr().err.splitlines() if INDEX in line]
    assert len(lines) == 1 and 'RuntimeError' in lines[0]


def test_due_job_passes_are_byte_equal_with_and_without_the_index(monkeypatch, tmp_path):
    from dataclasses import asdict
    from lib import agents, background_jobs
    from tests.unit.test_goal_keeper import _goal
    owner = agents.create_agent(persona='Jonatan', voice_id='v', cwd=str(tmp_path), session='jonatan')
    agents.start_runtime(owner, 'jonatan'); agents.bind_backend_session(owner, 'synthetic-native')
    helper = agents.create_agent(persona='Helper', voice_id='v', cwd=str(tmp_path), session='synthetic-helper')
    db.conn().execute("UPDATE agents SET parent_agent_id=?,role='helper' WHERE agent_id=?", (owner,helper))
    outsider = agents.create_agent(persona='Other', voice_id='v', cwd=str(tmp_path), session='synthetic-other')
    monkeypatch.setattr(db, 'now_ms', lambda: 1000)
    plan = _goal()
    clock = [1100]
    monkeypatch.setattr(db, 'now_ms', lambda: clock[0])
    jobs = [background_jobs.upsert(session=session,job_id='synthetic-'+session,kind='test',title='Synthetic')
            for session in ('jonatan','synthetic-helper','synthetic-other')]
    config = {'scope':{},'attachments':[
        {'trigger_id':'agent-work-completed','trigger_version':1,'enabled':True,'attachment_id':'synthetic','config':{'coalesce_seconds':0}},
        {'trigger_id':'goal-activity-observed','trigger_version':1,'enabled':True,'config':{}}]}
    real_seen = goal_keeper._seen
    observed = []
    def capture(*args, **kwargs):
        result = real_seen(*args, **kwargs)
        observed.append(asdict(result))
        return result
    monkeypatch.setattr(goal_keeper, '_seen', capture)
    progress = {}
    def paired(now):
        nonlocal progress
        initial = json.loads(json.dumps(progress))
        snapshots = []
        for indexed in (False,True):
            db.conn().execute(f'DROP INDEX IF EXISTS {INDEX}')
            if indexed:
                with monkeypatch.context() as patch:
                    patch.setattr(db, '_SERVICE', 'host')
                    assert db.ensure_service_indexes()
            observed.clear(); state=json.loads(json.dumps(initial))
            ready=goal_keeper.due(config,state,now)
            snapshots.append(json.dumps({'ready':ready,'progress':state,'activity':list(observed)},sort_keys=True))
        assert snapshots[0] == snapshots[1]
        result=json.loads(snapshots[1]);progress=result['progress'];return result
    assert paired(1200)['ready'] == []
    clock[0]=2000
    # Same-millisecond terminal events across several agents in one goal.
    for job in jobs:
        background_jobs.finish(job['job_id'],generation=job['generation'])
    result=paired(2100)
    assert result['ready'] == [plan['plan_id']]
    assert result['activity'][0]['triggers'] == [[2000,'job end'],[2000,'job end']]
    # Re-registration removes the old terminal stamp from the current table.
    clock[0]=2200
    background_jobs.upsert(session='jonatan',job_id=jobs[0]['job_id'],kind='test',title='Synthetic again')
    background_jobs.upsert(session='synthetic-helper',job_id=jobs[1]['job_id'],kind='test',title='Synthetic again')
    result=paired(2300)
    assert result['activity'][0]['triggers'] == []
    assert result['activity'][0]['newest_ms'] == 1000
    # The already-folded pending trigger remains policy state, not cache state.
    assert result['ready'] == [plan['plan_id']]


def test_index_build_exhausting_budget_rolls_back_without_losing_jobs(monkeypatch, capsys):
    from lib import db_migrations
    path, version = _existing_without_index()
    con = db.conn()
    con.executemany("INSERT INTO background_jobs(job_id,agent_id,session,title,started_at,updated_at,terminal_at) VALUES(?,?,?,?,?,?,?)",
                    [(str(i),'owner','owner','Synthetic',1,2,2) for i in range(500)])
    # The CREATE itself exceeds its progress deadline, after admission succeeds.
    ticks = iter([0.0, 0.0])
    monkeypatch.setattr(db_migrations.time, 'monotonic', lambda: next(ticks, 1.0))
    monkeypatch.setattr(db, '_SERVICE', 'host')
    assert not db.ensure_service_indexes(con)
    assert not _has_index(con)
    assert con.execute('SELECT COUNT(*) FROM background_jobs').fetchone()[0] == 500
    assert con.execute('PRAGMA user_version').fetchone()[0] == version
    assert not con.in_transaction
    lines = [line for line in capsys.readouterr().err.splitlines() if INDEX in line]
    assert len(lines) == 1 and 'INTERRUPT' in lines[0]
