"""Custom task admission with the real isolated store, no provider calls."""
import pytest
from lib import agents, backends, db, janitors
from lib.janitor_runner import JanitorRunner


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setattr(backends, 'active_handles', lambda *_: [])
    now = [1_800_000]
    monkeypatch.setattr(db, 'now_ms', lambda: now[0])
    agents.create_agent(persona='Custom', voice_id='', cwd='/tmp', session='custom', backend='codex')
    cfg = janitors.create('custom', 'custom-task', options={'instructions': 'Read the installed skill.\nCheck new feedback only.'},
        attachments=[{'trigger_id': 'schedule', 'config': {'cron': '* * * * *', 'timezone': 'UTC'}}])
    cfg = janitors.set_enabled('custom', cfg['revision'], True)
    calls, terminals = [], {}
    class Source:
        def terminal(self, run): return terminals.get(run['run_id'])
        def application_active(self, _): return True
    def dispatch(run, prompt):
        calls.append((run, prompt))
        return True
    def runner(): return JanitorRunner(dispatch, source=Source(), clock=lambda: now[0])
    return cfg, now, calls, terminals, runner


def test_schedule_runs_without_task_agents_and_freezes_instructions(lane):
    cfg, now, calls, terminals, runner = lane
    assert runner().tick() == 0
    now[0] += 60_000
    assert runner().tick() == 1
    run, prompt = calls[0]
    assert 'Check new feedback only.' in prompt
    assert run['candidates'] == []
    assert janitors.run_context(run['run_id'])['configuration']['options']['instructions'] in prompt
    assert runner().tick() == 0
    terminals[run['run_id']] = {'kind': 'done'}
    assert runner().tick() == 0
    assert janitors.get_run(run['run_id'])['outcome'] == 'completed'
    assert janitors.get_run(run['run_id'])['results'] == []


def test_pause_fences_previously_admitted_work(lane):
    cfg, now, calls, _, runner = lane
    runner().tick(); now[0] += 60_000; runner().tick()
    run = calls[0][0]
    janitors.set_enabled('custom', cfg['revision'], False)
    assert not janitors.validate_dispatch('custom', run['run_id'], run['trace_id'])
    with pytest.raises(janitors.JanitorError):
        janitors.run_context(run['run_id'])


def test_failed_or_missed_runs_do_not_replay_external_actions(lane):
    _, now, calls, terminals, runner = lane
    runner().tick(); now[0] += 600_000; runner().tick()
    assert len(calls) == 1
    terminals[calls[0][0]['run_id']] = {'kind': 'error'}
    runner().tick(); runner().tick()
    assert len(calls) == 1
    assert janitors.get_run(calls[0][0]['run_id'])['status'] == 'failed'
    now[0] += 60_000
    runner().tick()
    assert len(calls) == 2


def test_edit_fences_old_instructions_and_reset_preserves_definition(lane):
    cfg, now, calls, _, runner = lane
    runner().tick(); now[0] += 60_000; runner().tick()
    run = calls[0][0]
    cfg = janitors.configure('custom', cfg['revision'], options={'instructions': 'New task'})
    assert not janitors.validate_dispatch('custom', run['run_id'], run['trace_id'])
    cfg = janitors.reset_defaults('custom', cfg['revision'])
    assert cfg['options']['instructions'] == 'New task'
    assert not cfg['enabled']


def test_empty_instructions_cannot_be_enabled(monkeypatch):
    monkeypatch.setattr(backends, 'active_handles', lambda *_: [])
    agents.create_agent(persona='Empty', voice_id='', cwd='/tmp', session='empty')
    cfg = janitors.create('empty', 'custom-task')
    with pytest.raises(janitors.JanitorError, match='instructions'):
        janitors.set_enabled('empty', cfg['revision'], True)


def test_custom_tasks_cannot_admit_label_effects(lane):
    cfg, *_ = lane
    with pytest.raises(janitors.JanitorError, match='task-label'):
        janitors.create_run(cfg['attachments'][0]['attachment_id'], cfg['generation'], [{}])


def test_two_custom_tasks_can_coexist(lane):
    agents.create_agent(persona='Second', voice_id='', cwd='/tmp', session='second')
    cfg = janitors.create('second', 'custom-task', options={'instructions': 'A separate task'})
    assert janitors.set_enabled('second', cfg['revision'], True)['enabled']


def test_activity_gated_custom_task(lane, monkeypatch):
    cfg, now, calls, _, _ = lane
    cfg = janitors.configure('custom', cfg['revision'], attachments=[{'trigger_id':'active-interval',
        'config':{'interval_seconds':60,'idle_timeout_seconds':60,'run_on_resume':True}}])
    janitors.set_enabled('custom', cfg['revision'], True)
    class Source:
        active = False
        def application_active(self, _): return self.active
        def terminal(self, _): return None
    source = Source()
    from lib import application_activity
    monkeypatch.setattr(application_activity, "active", lambda _: source.active)
    runner = JanitorRunner(lambda run,prompt: calls.append((run,prompt)) or True, source=source, clock=lambda:now[0])
    assert runner.tick() == 0
    source.active = True
    assert runner.tick() == 1
    assert runner.tick() == 0


def test_uncertain_custom_dispatch_reuses_frozen_run(lane):
    _, now, _, _, _ = lane
    attempted = []
    def dispatch(run, prompt):
        attempted.append((run['run_id'],prompt))
        raise TimeoutError('Ambiguous acceptance')
    class Source:
        def terminal(self, _): return None
    def runner(): return JanitorRunner(dispatch, source=Source(), clock=lambda:now[0])
    runner().tick(); now[0] += 60_000; runner().tick()
    now[0] += 30_000; runner().tick()
    assert len(attempted) == 2
    assert attempted[0] == attempted[1]


def test_schedule_list_formats_next_run_without_import_error(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    path = Path(__file__).resolve().parents[2]/'bin/clarp-admin.py'
    spec = importlib.util.spec_from_file_location('custom_janitor_admin_test', path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'api_request', lambda *_: {'schedules': [{
        'name':'Example','schedule_id':'s1','session':'worker','cron_expression':'*/5 * * * *',
        'prompt':'Read installed skill','enabled':True,'next_run_at':1800000}]})
    assert cli.cmd_schedule(SimpleNamespace(schedule_command='list',session=None)) == 0
    assert '1970-01-01 00:30:00 UTC' in capsys.readouterr().out
