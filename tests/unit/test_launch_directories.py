from types import SimpleNamespace
from lib import agents, launch_directories


def test_home_and_recent_directories_first(tmp_path, monkeypatch):
    recent = tmp_path / 'recent'; recent.mkdir()
    ranked = tmp_path / 'ranked'; ranked.mkdir()
    agents.record_path_usage(str(recent))
    monkeypatch.setattr(launch_directories.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=str(ranked)))
    result = launch_directories.lookup(home=tmp_path)
    assert [m['path'] for m in result['matches']] == [str(tmp_path), str(recent), str(ranked)]
    assert result['matches'][0]['label'] == '~'


def test_uses_zoxide_order_without_shell_execution(tmp_path, monkeypatch):
    project = tmp_path / 'my project'; project.mkdir()
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert not kwargs.get('shell')
        return SimpleNamespace(returncode=0, stdout=str(project))
    monkeypatch.setattr(launch_directories.subprocess, 'run', run)
    assert launch_directories.lookup('my project', home=tmp_path)['matches'][0]['path'] == str(project)
    assert calls[0] == ['zoxide', 'query', '--list', '--', 'my', 'project']


def test_explicit_path_works_without_zoxide_and_missing_recent_is_hidden(tmp_path, monkeypatch):
    project = tmp_path / 'project'; project.mkdir()
    agents.record_path_usage(str(tmp_path / 'removed'))
    def missing(*a, **k): raise FileNotFoundError()
    monkeypatch.setattr(launch_directories.subprocess, 'run', missing)
    assert launch_directories.lookup('~/project', home=tmp_path)['matches'][0]['path'] == str(project)
    assert all('removed' not in m['path'] for m in launch_directories.lookup(home=tmp_path)['matches'])
    assert launch_directories.lookup('no-such-directory', home=tmp_path)['matches'] == []


def test_subsequence_fallback_for_zoxide_history(tmp_path, monkeypatch):
    project = tmp_path / 'clarp'; project.mkdir()
    def run(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout='') if '--' in args else SimpleNamespace(returncode=0, stdout=str(project))
    monkeypatch.setattr(launch_directories.subprocess, 'run', run)
    assert launch_directories.lookup('clrp', home=tmp_path)['matches'][0]['path'] == str(project)


def test_recent_list_ignores_automation_only_workspaces(tmp_path, monkeypatch):
    from lib import db
    automated = tmp_path / 'automation'; automated.mkdir()
    agent = agents.create_agent(persona='Fixture janitor', voice_id='', cwd=str(automated), session='fixture-janitor')
    db.conn().execute('UPDATE agents SET is_janitor=1 WHERE agent_id=?', (agent,))
    agents.record_path_usage(str(automated))
    monkeypatch.setattr(launch_directories.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout=''))
    assert str(automated) not in [m['path'] for m in launch_directories.lookup(home=tmp_path)['matches']]
