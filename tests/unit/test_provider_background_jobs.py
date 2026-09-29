"""Representative Claude 2.1.283 receipts; no paid model or live processes."""
import json
import os
from datetime import datetime, timezone

import pytest

from lib import agents, background_jobs as jobs, db, provider_background_jobs as provider

NATIVE = '1ab7df52-76aa-423e-83b3-6eafc7ef8eb2'
TOOL = 'toolu_01FdHka33VdcPRqDDyLBPdJ1'
TASK = 'b3mni59xr'


def record(kind, *, at=None, **kw):
    return {'type': kind, 'sessionId': NATIVE,
            'timestamp': datetime.fromtimestamp((at or db.now_ms()) / 1000, timezone.utc).isoformat(), **kw}


def launch(at=None, **inp):
    return record('assistant', at=at, message={'content': [{'type': 'tool_use', 'id': TOOL,
        'name': 'Bash', 'input': {'run_in_background': True,
        'description': 'Wait for iOS TestFlight run to finish',
        'command': 'TOKEN=command-secret gh run watch 123', **inp}}]})


def receipt(at=None):
    return record('user', at=at, message={'content': [{'type': 'tool_result', 'tool_use_id': TOOL,
        'content': f'Command running in background with ID: {TASK}. Output is being written to: /etc/passwd',
        'is_error': False}]}, toolUseResult={'stdout': '', 'stderr': '', 'backgroundTaskId': TASK})


def notice(state='completed', at=None, task=TASK, tool=TOOL):
    return record('queue-operation', at=at, operation='enqueue', content=(
        f'<task-notification><task-id>{task}</task-id><tool-use-id>{tool}</tool-use-id>'
        f'<status>{state}</status><summary>private provider command text</summary></task-notification>'))


@pytest.fixture
def case(tmp_path):
    aid = agents.create_agent(persona='Bella fixture', voice_id='', cwd=str(tmp_path), session='bella-fixture')
    owner = {'agent_id': aid, 'session': 'bella-fixture', 'backend':'claude'}
    path = tmp_path / '-fixture' / f'{NATIVE}.jsonl'
    path.parent.mkdir()
    path.touch()
    return owner, path, provider.job_id(aid, NATIVE, TOOL)


def append(path, *records):
    with path.open('a') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def test_original_gap_launch_is_not_running_then_receipt_is_visible(case):
    owner, path, ident = case
    assert jobs.snapshot()['jobs'] == []
    append(path, launch())
    provider.observe(owner, NATIVE, path)
    first = jobs.detail(ident)
    assert first['job']['status'] == 'queued'
    assert first['job']['metadata']['provider_state'] == 'launching'
    append(path, receipt())
    provider.observe(owner, NATIVE, path)
    detail = jobs.detail(ident)
    assert detail['owner_agent_id'] == owner['agent_id']
    assert detail['owner_session'] == 'bella-fixture'
    job = detail['job']
    assert job['title'] == 'Wait for iOS TestFlight run to finish'
    assert job['metadata']['tool_use_id'] == TOOL
    assert job['metadata']['provider_task_id'] == TASK
    assert job['metadata']['native_session_id'] == NATIVE
    assert job['status'] == 'running' and job['elapsed_ms'] >= 0
    assert job['worker_pid'] is None and job['heartbeat_at'] is None
    assert job['worker_freshness'] == 'unknown' and not job['can_cancel']
    assert len(jobs.snapshot()['jobs']) == 1
    assert 'command-secret' not in json.dumps(detail)
    assert 'passwd' not in json.dumps(detail)
    append(path, notice())
    provider.observe(owner, NATIVE, path)
    assert jobs.detail(ident)['job']['status'] == 'succeeded'
    assert len(jobs.detail(ident)['timeline']) == 3


@pytest.mark.parametrize('state,status,outcome', [('failed','failed','failed'), ('stopped','failed','unknown'), ('killed','failed','unknown')])
def test_terminal_evidence(case, state, status, outcome):
    owner, path, ident = case
    append(path, launch(), receipt(), notice(state))
    provider.observe(owner, NATIVE, path)
    job = jobs.detail(ident)['job']
    assert (job['status'], job['outcome_state']) == (status, outcome)


def test_restart_duplicate_partial_stale_and_wrong_task(case):
    owner, path, ident = case
    at = db.now_ms() - 1000
    append(path, launch(at), receipt(at+1), notice(task='wrong'))
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'running'
    # A fresh observer reads its durable cursor, no duplicate event.
    provider.observe(owner, NATIVE, path)
    assert len(jobs.timeline(ident)) == 2
    payload = json.dumps(notice(at=at+2)) + '\n'
    with path.open('a') as f:
        f.write(payload[:30])
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'running'
    with path.open('a') as f:
        f.write(payload[30:])
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'succeeded'
    append(path, launch(at), receipt(at+1), notice('failed', at=at+3))
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'succeeded'
    assert len(jobs.timeline(ident)) == 3
    # Replacement/truncation replay also preserves the terminal tombstone.
    path.unlink()
    append(path, launch(at), receipt(at+1))
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'succeeded'


@pytest.mark.parametrize('ended', [False, True])
def test_silence_and_session_end_are_unknown_not_failure(case, ended):
    owner, path, ident = case
    at = db.now_ms() - (1000 if ended else provider.STALE_MS + 1000)
    append(path, launch(at), receipt(at+1))
    provider.observe(owner, NATIVE, path, ended=ended)
    job = jobs.get(ident)
    assert job['status'] == 'queued' and job['outcome_state'] == 'unknown'
    assert job['terminal_at'] is None and job['heartbeat_at'] is None
    append(path, receipt(at+1))
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['metadata']['provider_state'] == 'unknown'
    append(path, notice())
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'succeeded'


def test_cancel_cannot_claim_or_kill_provider_task(case, monkeypatch):
    owner, path, ident = case
    append(path, launch(), receipt())
    provider.observe(owner, NATIVE, path)
    monkeypatch.setattr(jobs, 'terminate_worker', lambda *a, **k: pytest.fail('must not signal'))
    outcome = jobs.cancel_run(ident, expected_generation=1)
    assert outcome['mismatch'] == 'unsupported' and not outcome['changed']
    assert outcome['job']['status'] == 'running'


def test_only_bash_background_and_exact_owner_are_imported(case):
    owner, path, ident = case
    agent = launch()
    agent['message']['content'][0]['name'] = 'Agent'
    wrong_native = launch(); wrong_native['sessionId'] = 'other'
    append(path, agent, launch(run_in_background=False), wrong_native, receipt(), notice())
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident) is None


def test_explicit_identity_registration_is_not_double_counted(case):
    owner, path, ident = case
    append(path, launch(), receipt())
    provider.observe(owner, NATIVE, path)
    jobs.upsert(session=owner['session'], job_id='explicit', title='Visible watcher',
                metadata={'provider':'claude','native_session_id':NATIVE,'tool_use_id':TOOL})
    assert [j['job_id'] for j in jobs.snapshot()['jobs']] == ['explicit']
    assert [j['job_id'] for j in jobs.active_by_agent()[owner['agent_id']]] == ['explicit']
    assert jobs.detail(ident)['job']['metadata']['provider_task_id'] == TASK


def test_output_is_exact_bounded_redacted_and_symlink_fenced(case, tmp_path, monkeypatch):
    owner, path, ident = case
    append(path, launch(), receipt())
    provider.observe(owner, NATIVE, path)
    job = jobs.get(ident)
    # Substitute only /tmp so real path validation and file reads still execute.
    real_path = provider.pathlib.Path
    def mapped(value):
        return tmp_path / 'provider-tmp' if value == '/tmp' else real_path(value)
    monkeypatch.setattr(provider.pathlib, 'Path', mapped)
    root = tmp_path / 'provider-tmp' / f'claude-{os.getuid()}' / '-fixture' / NATIVE / 'tasks'
    root.mkdir(parents=True)
    output = root / f'{TASK}.output'
    output.write_text('x' * 70000 + '\nstep 3/10\nTOKEN=secret-value\nAuthorization: Bearer bearer-value\n')
    result = jobs.detail(ident)['log']
    assert result['available'] and result['truncated']
    assert 'step 3/10' in result['text']
    assert 'secret-value' not in result['text'] and 'bearer-value' not in result['text']
    assert jobs.detail(ident, include_log=False)['log']['reason'] == 'forbidden'
    output.unlink()
    sensitive = tmp_path / 'sensitive'; sensitive.write_text('private')
    output.symlink_to(sensitive)
    assert not jobs.detail(ident)['log']['available']


def test_cursor_rolls_back_with_projection_failure(case, monkeypatch):
    owner, path, ident = case
    append(path, launch(), receipt())
    original = provider.apply_actions
    def failure(c, owner, native, actions):
        original(c, owner, native, actions)
        raise RuntimeError('interrupted')
    monkeypatch.setattr(provider, 'apply_actions', failure)
    with pytest.raises(RuntimeError):
        provider.observe(owner, NATIVE, path)
    assert jobs.get(ident) is None
    monkeypatch.setattr(provider, 'apply_actions', original)
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'running'


def test_manual_registry_updates_cannot_forge_provider_lifecycle(case):
    owner, path, ident = case
    append(path, launch(), receipt())
    provider.observe(owner, NATIVE, path)
    with pytest.raises(ValueError, match='reserved'):
        jobs.upsert(session=owner['session'], job_id=ident, title='fake')
    assert jobs.heartbeat(ident, generation=1) is None
    assert jobs.finish(ident, generation=1) is None
    assert jobs.get(ident)['heartbeat_at'] is None
    assert jobs.get(ident)['status'] == 'running'


def test_oversized_non_lifecycle_record_does_not_block_future_tasks(case, monkeypatch):
    owner, path, ident = case
    monkeypatch.setattr(provider, 'MAX_BYTES', 1000)
    path.write_text(json.dumps({'type':'image','data':'a' * 5000}) + '\n')
    append(path, launch(), receipt(), notice())
    for _ in range(10):
        provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'succeeded'


def test_v99_migration_creates_cursor_without_changing_jobs():
    c = db.conn()
    c.execute('DROP TABLE provider_job_cursors')
    c.execute('PRAGMA user_version=99')
    db._migrate(c)
    assert 'discarding' in {r[1] for r in c.execute('PRAGMA table_info(provider_job_cursors)')}


def test_native_history_before_owner_binding_is_not_adopted(case):
    owner, path, ident = case
    owner['bound_at'] = db.now_ms()
    append(path, launch(owner['bound_at'] - 2000), receipt(owner['bound_at'] - 1000))
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident) is None


def test_malformed_records_and_cross_native_notice_do_not_poison_cursor(case):
    owner, path, ident = case
    append(path, {'type':'assistant','timestamp':123}, record('assistant',message=[]),
           launch(), receipt())
    wrong = notice(); wrong['sessionId'] = 'other-session'
    append(path, wrong)
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'running'
    append(path, notice())
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['status'] == 'succeeded'


def test_launch_error_with_string_metadata_is_terminal(case):
    owner, path, ident = case
    append(path, launch(), record('user', message={'content':[
        {'type':'tool_result','tool_use_id':TOOL,'is_error':True,'content':'hidden error'}]},
        toolUseResult='provider launch failed'))
    provider.observe(owner, NATIVE, path)
    assert jobs.get(ident)['metadata']['provider_state'] == 'failed'


def test_idle_observer_does_not_write_or_refresh_evidence(case):
    owner, path, ident = case
    append(path, launch(), receipt())
    provider.observe(owner, NATIVE, path)
    before = db.conn().total_changes
    provider.observe(owner, NATIVE, path)
    assert db.conn().total_changes == before


def test_real_hook_shell_and_legacy_helper_automatically_deduplicate(case):
    """No registration metadata authored by the agent; execute the actual helper."""
    import pathlib
    import shlex
    import subprocess
    import sys
    owner, path, ident = case
    root = pathlib.Path(__file__).resolve().parents[2]
    db.conn().execute('INSERT INTO runtimes(agent_id,session,backend_session_id,started_at) VALUES (?,?,?,?)',
                      (owner['agent_id'], owner['session'], NATIVE, db.now_ms() - 1000))
    command = shlex.join([sys.executable, str(root / 'scripts/agent_bg.py'), owner['session'],
                          'job-upsert', 'explicit-shell', 'testing', 'Disposable shell watcher'])
    inputs = {'command': command, 'description':'Disposable shell watcher', 'run_in_background':True}
    env = {**os.environ, 'CLAUDE_PWA_DB':str(db.DB_PATH), 'CLARP_CODE_ROOT':str(root / 'server'),
           'CLAUDE_PWA_SESSION':owner['session']}
    env.pop('CLARP_BACKGROUND_WORKER_PID', None)
    hook = subprocess.run([sys.executable,str(root / 'plugin/hooks/tool_activity.py')],
        input=json.dumps({'session_id':NATIVE,'tool_use_id':TOOL,'tool_name':'Bash','tool_input':inputs}),
        env=env,capture_output=True,text=True,timeout=15)
    assert hook.returncode == 0, hook.stderr
    response = json.loads(hook.stdout)['hookSpecificOutput']
    assert 'permissionDecision' not in response
    updated = response['updatedInput']
    assert updated['command'].endswith(command)
    assert {k:v for k,v in updated.items() if k != 'command'} == {k:v for k,v in inputs.items() if k != 'command'}
    result = subprocess.run(['bash','-c',updated['command']], env=env,
                             capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'bg1:1:explicit-shell'
    registered = jobs.get('explicit-shell')
    assert registered['metadata']['tool_use_id'] == TOOL
    append(path, launch(), receipt())
    provider.observe(owner, NATIVE, path)
    assert [j['job_id'] for j in jobs.snapshot()['jobs']] == ['explicit-shell']
    assert len(jobs.active_by_agent()[owner['agent_id']]) == 1
    assert jobs.get(ident)['metadata']['provider_task_id'] == TASK


def test_origin_rejects_wrong_owner_and_ended_binding(case, monkeypatch):
    owner, path, ident = case
    origin = {'agent_id':owner['agent_id'],'provider':'claude','native_session_id':NATIVE,'tool_use_id':TOOL}
    monkeypatch.setenv('CLARP_BACKGROUND_ORIGIN',json.dumps(origin))
    # No matching live native binding: inherited state is not sufficient.
    assert jobs.upsert(session=owner['session'],job_id='unbound',title='Unbound')['metadata'] == {}
    db.conn().execute('INSERT INTO runtimes(agent_id,session,backend_session_id,started_at) VALUES (?,?,?,?)',
                      (owner['agent_id'], owner['session'], NATIVE, db.now_ms()))
    other = agents.create_agent(persona='Other', voice_id='',cwd=str(path.parent),session='other')
    assert jobs.upsert(session='other',job_id='other',title='Other')['metadata'] == {}
    db.conn().execute('UPDATE runtimes SET ended_at=? WHERE agent_id=?',(db.now_ms(),owner['agent_id']))
    assert jobs.upsert(session=owner['session'],job_id='ended',title='Ended')['metadata'] == {}


def test_origin_wrapper_is_scoped_and_idempotent(case):
    from lib.backend.claude_background_provenance import tool_input_with_origin
    owner, path, ident = case
    assert tool_input_with_origin(owner,NATIVE,TOOL,{'command':'pwd'}) is None
    assert tool_input_with_origin(owner,NATIVE,"bad'; touch /tmp/no",{'command':'pwd','run_in_background':True}) is None
    inputs = {'command':"printf '%s\\n' 'unchanged $content'",'run_in_background':True}
    updated = tool_input_with_origin(owner,NATIVE,TOOL,inputs)
    assert tool_input_with_origin(owner,NATIVE,TOOL,updated) == updated


def test_parser_runs_outside_write_transaction_and_cursor_compare_and_swap(case, monkeypatch):
    owner,path,ident=case
    append(path,launch(),receipt())
    original=provider.parse_actions
    once=[False]
    def concurrent(native,project,record):
        assert not db.conn().in_transaction
        actions=original(native,project,record)
        if not once[0]:
            once[0]=True
            provider.observe(owner,native,path)
        return actions
    monkeypatch.setattr(provider,'parse_actions',concurrent)
    provider.observe(owner,NATIVE,path)
    assert jobs.get(ident)['status']=='running'
    assert len(jobs.timeline(ident))==2
    assert db.conn().execute('SELECT position FROM provider_job_cursors').fetchone()[0]==path.stat().st_size


def test_bounded_poll_rotates_fairly_across_large_backlogs(tmp_path,monkeypatch):
    from lib import backends
    paths={}
    line=json.dumps(record('assistant',message={'content':[]}))+'\n'
    for i in range(9):
        native=f'fair-{i}'
        aid=agents.create_agent(persona=native,voice_id='',cwd=str(tmp_path),session=native)
        db.conn().execute('INSERT INTO runtimes(agent_id,session,backend_session_id,started_at) VALUES(?,?,?,?)',
                          (aid,native,native,db.now_ms()-1000))
        path=tmp_path/f'{native}.jsonl';path.write_text(line*10000);paths[native]=path
    monkeypatch.setattr(backends.by_id('claude'),'find_transcript',lambda native: paths[native])
    observer=provider.ProviderJobObserver()
    for _ in range(5):
        before=dict(db.conn().execute('SELECT native_id,position FROM provider_job_cursors'))
        observer.poll_once()
        after=dict(db.conn().execute('SELECT native_id,position FROM provider_job_cursors'))
        assert sum(before.get(k)!=v for k,v in after.items())<=2
        assert all(v-before.get(k,0)<=provider.MAX_BYTES+len(line) for k,v in after.items())
    assert set(after)==set(paths)
    assert all(position>0 for position in after.values())


def test_one_large_record_is_applied_in_bounded_resumable_action_batches(case):
    owner,path,ident=case
    blocks=[{'type':'tool_use','name':'Bash','id':f'toolu_batch_{i}',
             'input':{'run_in_background':True,'description':f'Task {i}'}} for i in range(130)]
    append(path,record('assistant',message={'content':blocks}))
    for expected in (64,128,130):
        provider.observe(owner,NATIVE,path)
        assert db.conn().execute('SELECT count(*) FROM background_jobs').fetchone()[0]==expected
    cursor=db.conn().execute('SELECT * FROM provider_job_cursors').fetchone()
    assert cursor['position']==path.stat().st_size and cursor['action_offset']==0
    provider.observe(owner,NATIVE,path)
    assert db.conn().execute('SELECT count(*) FROM background_jobs').fetchone()[0]==130


def test_stale_reconciliation_shares_the_per_visit_mutation_budget(case):
    owner,path,ident=case
    blocks=[{'type':'tool_use','name':'Bash','id':f'toolu_stale_{i}',
             'input':{'run_in_background':True}} for i in range(130)]
    append(path,record('assistant',at=db.now_ms()-2*provider.STALE_MS,message={'content':blocks}))
    for _ in range(6):
        before=jobs.latest_event_id()
        provider.observe(owner,NATIVE,path)
        assert len(jobs.events_after(before,limit=500))<=provider.MAX_ACTIONS
    rows=db.conn().execute('SELECT metadata_json FROM background_jobs').fetchall()
    assert len(rows)==130 and all(json.loads(r[0])['provider_state']=='unknown' for r in rows)


def test_fair_sweep_catches_up_at_fast_ticks_then_returns_to_idle(tmp_path,monkeypatch):
    from lib import backends
    paths={}
    for i in range(5):
        native=f'cadence-{i}'
        aid=agents.create_agent(persona=native,voice_id='',cwd=str(tmp_path),session=native)
        db.conn().execute('INSERT INTO runtimes(agent_id,session,backend_session_id,started_at) VALUES(?,?,?,?)',
                         (aid,native,native,db.now_ms()))
        path=tmp_path/f'{native}.jsonl'
        path.write_text((json.dumps({'type':'noise'})+'\n')*20000)
        paths[native]=path
    monkeypatch.setattr(backends.by_id('claude'),'find_transcript',lambda native:paths[native])
    # Only the byte and transcript bounds decide the visit count; the wall-clock
    # budgets would make it depend on the machine's speed.
    monkeypatch.setattr(provider,'PARSE_BUDGET_SEC',60)
    monkeypatch.setattr(provider.ProviderJobObserver,'BUDGET_SEC',60)
    observer=provider.ProviderJobObserver()
    visits=0
    while True:
        observer.poll_once();visits+=1
        if not observer.continue_soon:break
        assert visits<20
    assert visits>=6
    positions=dict(db.conn().execute('SELECT native_id,position FROM provider_job_cursors'))
    assert positions=={n:p.stat().st_size for n,p in paths.items()}
    observer.poll_once()
    assert observer.continue_soon  # remaining idle members still get their turn


def test_stale_receipt_is_unknown_before_long_backfill_reaches_eof(case, monkeypatch):
    owner,path,ident=case
    old=db.now_ms()-2*provider.STALE_MS
    append(path,launch(old),receipt(old+1))
    with path.open('a') as f:
        f.write((json.dumps(record('assistant',message={'content':[{'type':'text','text':'history'}]}))+'\n')*10000)
    monkeypatch.setattr(provider,'MAX_BYTES',1024)
    provider.observe(owner,NATIVE,path)
    assert db.conn().execute('SELECT position FROM provider_job_cursors').fetchone()[0] < path.stat().st_size
    job=jobs.get(ident)
    assert job['metadata']['provider_task_id']==TASK
    assert job['metadata']['provider_state']=='unknown'
    assert job['status']=='queued' and job['outcome_state']=='unknown'
    assert job['heartbeat_at'] is None and job['worker_pid'] is None


def test_already_recorded_stale_receipt_reconciles_before_more_backfill(case, monkeypatch):
    owner,path,ident=case
    old=db.now_ms()-2*provider.STALE_MS
    append(path,launch(old),receipt(old+1))
    with path.open('a') as f:
        f.write((json.dumps(record('assistant',message={'content':[]}))+'\n')*10000)
    monkeypatch.setattr(provider,'MAX_BYTES',1024)
    with monkeypatch.context() as legacy:
        legacy.setattr(provider,'STALE_MS',10**12)
        provider.observe(owner,NATIVE,path)
        assert jobs.get(ident)['status']=='running'
    before=db.conn().execute('SELECT position FROM provider_job_cursors').fetchone()[0]
    assert provider.observe(owner,NATIVE,path) is True
    job=jobs.get(ident)
    assert job['status']=='queued' and job['metadata']['provider_state']=='unknown'
    assert db.conn().execute('SELECT position FROM provider_job_cursors').fetchone()[0]==before
    assert before<path.stat().st_size


def test_stale_launch_and_receipt_with_same_timestamp_keep_task_identity(case):
    owner,path,ident=case
    old=db.now_ms()-2*provider.STALE_MS
    append(path,launch(old),receipt(old),notice('completed',at=old+1))
    provider.observe(owner,NATIVE,path)
    job=jobs.get(ident)
    assert job['metadata']['provider_task_id']==TASK
    assert job['status']=='succeeded'
