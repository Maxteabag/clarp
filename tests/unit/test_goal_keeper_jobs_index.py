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
    assert next(row for row in con.execute('PRAGMA index_list(background_jobs)') if row[1] == INDEX)[4] == 1
    for sql, args in [
        ('SELECT terminal_at FROM background_jobs WHERE agent_id IN (?,?) AND terminal_at>?', ('owner','helper',100)),
        ('SELECT MAX(terminal_at) FROM background_jobs WHERE agent_id IN (?,?) AND terminal_at IS NOT NULL', ('owner','helper')),
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
    if role == 'host':
        assert next(row for row in con.execute('PRAGMA index_list(background_jobs)') if row[1] == INDEX)[4] == 1
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
        assert 0.07 <= duration < 0.5, 'optional index must allow its short writer wait only'
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
if sys.argv[3:] == ['hold']:
 import sqlite3
 connect=sqlite3.connect
 class HeldCommit(sqlite3.Connection):
  def execute(self,sql,*args):
   if sql=='COMMIT':
    print('at_commit',flush=True)
    assert sys.stdin.readline().strip()=='commit'
   return super().execute(sql,*args)
 def builder(*args,**kwargs):
  if kwargs.get('timeout')==0.1:kwargs['factory']=HeldCommit
  return connect(*args,**kwargs)
 sqlite3.connect=builder
c=db.conn()
print(json.dumps({'version':c.execute('PRAGMA user_version').fetchone()[0], 'indexes':c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_background_jobs_agent_terminal'").fetchone()[0]}),flush=True)
'''


@pytest.mark.parametrize('hold_commit', [False, True], ids=['simultaneous','creator-held'])
def test_concurrent_service_process_starts_create_one_index(monkeypatch, hold_commit):
    path, version = _existing_without_index()
    db.close_local()
    children = [subprocess.Popen([sys.executable,'-c',CHILD_START,
        str(Path(__file__).resolve().parents[2]/'server'),str(path),
        *(['hold'] if hold_commit and n == 0 else [])],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for n in range(2)]
    sent = set(); released = False; results = {}
    def send(n, command):
        children[n].stdin.write(command+'\n'); children[n].stdin.flush()
    try:
        for child in children:
            assert child.stdout.readline().strip() == 'ready'
        send(0,'go'); sent.add(0)
        if hold_commit:
            assert children[0].stdout.readline().strip() == 'at_commit'
        send(1,'go'); sent.add(1)
        if hold_commit:
            out,err=children[1].communicate(timeout=5)
            results[1]=(children[1].returncode,out,err)
            assert 'SQLITE_BUSY' in err
            send(0,'commit'); released=True
        for n,child in enumerate(children):
            if n not in results:
                out,err=child.communicate(timeout=5)
                results[n]=(child.returncode,out,err)
    finally:
        # Complete the protocol even after a failed assertion; do not leave a
        # scratch process holding a writer or waiting for its input.
        for n,child in enumerate(children):
            if child.poll() is None:
                commands = ([] if n in sent else ['go'])
                if hold_commit and n == 0 and not released:
                    commands.append('commit')
                child.communicate(''.join(c+'\n' for c in commands) if commands else None,timeout=5)
    notices=[]
    for code,out,err in results.values():
        assert code == 0,err
        result=json.loads(out)
        assert result['version'] == version and result['indexes'] in (0,1)
        notices.extend(line for line in err.splitlines() if INDEX in line)
    assert len(results) == 2
    assert sum('ready' in line for line in notices) == 1
    failed=[line for line in notices if 'failed' in line]
    assert len(failed) <= 1 and all('SQLITE_BUSY' in line for line in failed)
    if hold_commit:
        assert len(failed) == 1
    assert _has_index(db.conn())


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
    agents.create_agent(persona='Other', voice_id='v', cwd=str(tmp_path), session='synthetic-other')
    monkeypatch.setattr(db, 'now_ms', lambda: 1000)
    plan = _goal()
    # Exercise an eligible stall check, rather than an explicitly waiting goal.
    raw = json.loads(db.conn().execute('SELECT goal_json FROM task_plans WHERE plan_id=?', (plan['plan_id'],)).fetchone()[0])
    raw['continuation'] = {}
    db.conn().execute('UPDATE task_plans SET goal_json=? WHERE plan_id=?', (json.dumps(raw), plan['plan_id']))
    clock = [1100]
    monkeypatch.setattr(db, 'now_ms', lambda: clock[0])
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
        initial = json.loads(json.dumps(progress)); snapshots = []; active_plans = []
        con = db.conn()
        for indexed in (False,True):
            con.execute(f'DROP INDEX IF EXISTS {INDEX}')
            if indexed:
                with monkeypatch.context() as patch:
                    patch.setattr(db, '_SERVICE', 'host')
                    assert db.ensure_service_indexes()
            observed.clear(); state=json.loads(json.dumps(initial)); statements=[]
            con.set_trace_callback(statements.append)
            try:
                ready=goal_keeper.due(config,state,now)
                row=con.execute('SELECT * FROM task_plans WHERE plan_id=?',(plan['plan_id'],)).fetchone()
                stalled=goal_keeper.stall(con,row,now)
            finally:
                con.set_trace_callback(None)
            for sql in statements:
                if sql.startswith('SELECT 1 FROM background_jobs'):
                    active_plans.append([r[3] for r in con.execute('EXPLAIN QUERY PLAN '+sql+f' /* {now}:{indexed} */')])
                if indexed and sql.startswith('SELECT MAX(terminal_at) FROM background_jobs'):
                    maximum_plan=' '.join(r[3] for r in con.execute('EXPLAIN QUERY PLAN '+sql+f' /* {now}:indexed */'))
                    assert 'COVERING INDEX '+INDEX in maximum_plan
            snapshots.append(json.dumps({'ready':ready,'progress':state,'activity':list(observed),'stall':stalled},sort_keys=True))
        assert active_plans[0] == active_plans[1], 'active-job lookup must keep its baseline plan'
        assert snapshots[0] == snapshots[1]
        result=json.loads(snapshots[1]);progress=result['progress'];return result
    assert paired(1050)['ready'] == []  # no jobs
    jobs = [background_jobs.upsert(session=session,job_id='synthetic-'+session,kind='test',title='Synthetic')
            for session in ('jonatan','synthetic-helper','synthetic-other')]
    assert paired(1200)['ready'] == []  # active jobs
    clock[0]=2000
    background_jobs.finish(jobs[0]['job_id'],generation=jobs[0]['generation'])
    one=paired(2050)
    assert one['ready'] == [plan['plan_id']]
    assert one['activity'][0]['triggers'] == [[2000,'job end']]
    # Same-millisecond ends across owner/helper; unrelated agent stays outside scope.
    for job in jobs[1:]:
        background_jobs.finish(job['job_id'],generation=job['generation'])
    result=paired(2100)
    assert result['activity'][0]['triggers'] == [[2000,'job end'],[2000,'job end']]
    clock[0]=2200
    jobs[0]=background_jobs.upsert(session='jonatan',job_id=jobs[0]['job_id'],kind='test',title='Synthetic again')
    jobs[1]=background_jobs.upsert(session='synthetic-helper',job_id=jobs[1]['job_id'],kind='test',title='Synthetic again')
    result=paired(2250)
    assert result['activity'][0]['triggers'] == [] and result['activity'][0]['newest_ms'] == 1000
    assert result['ready'] == [plan['plan_id']]  # the folded trigger is preserved
    clock[0]=2300
    background_jobs.finish(jobs[0]['job_id'],generation=jobs[0]['generation'])
    assert paired(2350)['activity'][0]['triggers'] == [[2300,'job end']]
    background_jobs.finish(jobs[1]['job_id'],generation=jobs[1]['generation'])
    idle=paired(2300+goal_keeper.STALL_IDLE_MS+1)
    assert idle['stall'] == [2300,'no activity from the owner or its helpers','idle']


def test_index_build_exhausting_budget_rolls_back_without_losing_jobs(monkeypatch, capsys):
    from lib import db_migrations
    path, version = _existing_without_index()
    con = db.conn()
    con.executemany("INSERT INTO background_jobs(job_id,agent_id,session,title,started_at,updated_at,terminal_at) VALUES(?,?,?,?,?,?,?)",
                    [(str(i),'owner','owner','Synthetic',1,2,2) for i in range(500)])
    # The CREATE itself exceeds its progress deadline, after admission succeeds.
    ticks = iter([0.0, 0.0])
    monkeypatch.setattr(db_migrations.time, 'monotonic', lambda: next(ticks, 3.0))
    monkeypatch.setattr(db, '_SERVICE', 'host')
    assert not db.ensure_service_indexes(con)
    assert not _has_index(con)
    assert con.execute('SELECT COUNT(*) FROM background_jobs').fetchone()[0] == 500
    assert con.execute('PRAGMA user_version').fetchone()[0] == version
    assert not con.in_transaction
    lines = [line for line in capsys.readouterr().err.splitlines() if INDEX in line]
    assert len(lines) == 1 and 'INTERRUPT' in lines[0]


def test_writer_wait_does_not_consume_the_separate_build_budget(monkeypatch):
    from lib import db_migrations
    path, version = _existing_without_index()
    con = db.conn()
    con.executemany("INSERT INTO background_jobs(job_id,agent_id,session,title,started_at,updated_at,terminal_at) VALUES(?,?,?,?,?,?,?)",
                    [(str(i),'owner','owner','Synthetic',1,2,2) for i in range(500)])
    clock = [0.0]
    class TimedBuild(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith('CREATE INDEX'):
                # 90 ms writer wait plus 1.95 s build: within each own bound.
                clock[0] = 2.04
            result = super().execute(sql, *args)
            if sql == 'BEGIN IMMEDIATE':
                clock[0] = 0.09
            return result
    connect = sqlite3.connect
    def timed(*args, **kwargs):
        if 0 < kwargs.get('timeout', 5) < 1:
            kwargs['factory'] = TimedBuild
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', timed)
    monkeypatch.setattr(db_migrations.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(db, '_SERVICE', 'host')
    assert db.ensure_service_indexes(con)
    assert _has_index(con)
    assert con.execute('PRAGMA user_version').fetchone()[0] == version


def test_artificially_slow_build_hits_build_deadline_and_rolls_back(monkeypatch, capsys):
    path, version = _existing_without_index()
    con = db.conn()
    con.executemany("INSERT INTO background_jobs(job_id,agent_id,session,title,started_at,updated_at,terminal_at) VALUES(?,?,?,?,?,?,?)",
                    [(str(i),'owner','owner','Synthetic',1,2,2) for i in range(5000)])
    class SlowBuild(sqlite3.Connection):
        def set_progress_handler(self, callback, n):
            if callback is None:
                return super().set_progress_handler(None, n)
            def delayed():
                time.sleep(0.05)  # finite simulated storage latency, no CPU load
                return callback()
            return super().set_progress_handler(delayed, n)
    connect = sqlite3.connect
    def slow(*args, **kwargs):
        if 0 < kwargs.get('timeout', 5) < 1:
            kwargs['factory'] = SlowBuild
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', slow)
    monkeypatch.setattr(db, '_SERVICE', 'host')
    at = time.monotonic()
    assert not db.ensure_service_indexes(con)
    elapsed = time.monotonic() - at
    assert 1.8 <= elapsed < 2.8, elapsed
    assert not _has_index(con)
    assert con.execute('SELECT COUNT(*) FROM background_jobs').fetchone()[0] == 5000
    assert con.execute('PRAGMA user_version').fetchone()[0] == version
    assert not con.in_transaction
    lines = [line for line in capsys.readouterr().err.splitlines() if INDEX in line]
    assert len(lines) == 1 and 'failed' in lines[0] and 'INTERRUPT' in lines[0]


def test_connection_setup_checks_the_index_once_per_host_process(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    con = db.conn()
    path = db.DB_PATH
    opened = db._open_connection
    statements = []
    def traced():
        connection = opened()
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(db, '_open_connection', traced)
    _start(monkeypatch, path, 'host')
    db.conn().execute('SELECT 1')
    def other_thread():
        try:
            return db.conn().execute('SELECT 1').fetchone()[0]
        finally:
            db.close_local()
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(other_thread).result(timeout=5) == 1
    checks = [sql for sql in statements if sql.startswith('SELECT 1 FROM sqlite_master') and INDEX in sql]
    assert len(checks) == 1


@pytest.mark.parametrize('stage', ['progress-reset','rollback'])
@pytest.mark.parametrize('error_type', [sqlite3.OperationalError, RuntimeError])
def test_cleanup_failure_always_closes_builder_and_releases_real_writer(monkeypatch, capsys, stage, error_type):
    path, version = _existing_without_index()
    con = db.conn(); connect = sqlite3.connect; workers = []
    class BrokenCleanup(sqlite3.Connection):
        close_calls = 0
        def execute(self, sql, *args):
            if sql.startswith('CREATE INDEX'):
                raise sqlite3.OperationalError('synthetic CREATE failure')
            return super().execute(sql, *args)
        def set_progress_handler(self, callback, n):
            if callback is None and stage == 'progress-reset':
                raise error_type('synthetic progress reset failure')
            return super().set_progress_handler(callback, n)
        def rollback(self):
            if stage == 'rollback':
                raise error_type('synthetic rollback failure')
            return super().rollback()
        def close(self):
            self.close_calls += 1
            return super().close()
    def broken(*args, **kwargs):
        if kwargs.get('timeout') == 0.1:
            kwargs['factory'] = BrokenCleanup
            connection = connect(*args, **kwargs); workers.append(connection); return connection
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', broken)
    monkeypatch.setattr(db, '_SERVICE', 'host')
    other = connect(path, isolation_level=None, timeout=0.03)
    try:
        assert db.ensure_service_indexes(con) is False
        other.execute('BEGIN IMMEDIATE')
        other.execute("INSERT INTO background_jobs(job_id,agent_id,session,title,started_at,updated_at) "
                      "VALUES('cleanup-proof','synthetic','synthetic','Synthetic',1,1)")
        other.rollback()
        assert workers[0].close_calls == 1
        assert not _has_index(con)
        assert con.execute('PRAGMA user_version').fetchone()[0] == version
        notices = [line for line in capsys.readouterr().err.splitlines() if INDEX in line]
        assert len(notices) == 1 and 'failed' in notices[0]
    finally:
        other.close()
        for worker in workers:
            sqlite3.Connection.close(worker)  # baseline negative control must not retain its writer


@pytest.mark.parametrize('role', ['', 'runtime', 'tests'], ids=['cli','runtime','tests'])
@pytest.mark.parametrize('entry', ['explicit','maintenance'])
def test_non_host_explicit_and_maintenance_ensures_never_open_a_builder(monkeypatch, tmp_path, role, entry):
    path, version = _existing_without_index()
    con = _start(monkeypatch, path, role)
    connect = sqlite3.connect; builders = []
    def counted(*args, **kwargs):
        if kwargs.get('timeout') == 0.1:
            builders.append(args[0])
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', counted)
    if entry == 'explicit':
        assert db.ensure_service_indexes(con) is False
    else:
        from lib import telemetry
        worker = maintenance.MaintenanceWorker(audio_dir=tmp_path)
        monkeypatch.setattr(worker, '_prune_event_logs', lambda: 0)
        monkeypatch.setattr(telemetry, 'over_budget', lambda: False)
        worker.check_sizes()
    assert builders == []
    assert not _has_index(con)
    assert con.execute('PRAGMA user_version').fetchone()[0] == version
