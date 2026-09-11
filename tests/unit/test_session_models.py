import json
from types import SimpleNamespace
from lib import session_models


def test_recorded_codex_model_survives_default_change(tmp_path, monkeypatch):
    path = tmp_path / 'session.jsonl'
    path.write_text(json.dumps({'type':'turn_context','payload':{'model':'original'}})+'\n')
    monkeypatch.setattr(session_models, '_transcript', lambda *args: path)
    assert session_models.agent_model({'backend':'codex'}, 'session') == 'original'
    path.write_text(path.read_text()+json.dumps({'type':'turn_context','payload':{'model':'changed-in-session'}})+'\n')
    assert session_models.agent_model({'backend':'codex'}, 'session') == 'changed-in-session'
    assert session_models.agent_model({'backend':'codex','model':'explicit'}, 'session') == 'explicit'


def test_unknown_is_not_current_global_default():
    assert session_models.agent_model({'backend':'codex'}, '') == ''


def test_launch_captures_cli_model(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    (tmp_path/'config.toml').write_text('model = "cli-model"\n')
    assert session_models.launch_default('codex', SimpleNamespace(codex_model='')) == 'cli-model'
    assert session_models.launch_default('codex', SimpleNamespace(codex_model='host-model')) == 'host-model'


def test_codex_index_reports_model_when_turn_context_is_far_back(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    with sqlite3.connect(tmp_path/'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads (id TEXT, model TEXT)')
        db.execute('INSERT INTO threads VALUES (?,?)', ('old-session','original-model'))
    monkeypatch.setattr(session_models, '_transcript', lambda *args: (_ for _ in ()).throw(AssertionError('No file scan needed')))
    assert session_models.recorded_model('codex', 'old-session') == 'original-model'
