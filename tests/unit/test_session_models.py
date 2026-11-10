import json
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize('case,expected', [
    ('latest-valid', 'latest'),
    ('wide-record', 'wide'),
    ('partial-small-window', 'earlier'),
    ('legacy-tail-boundary', 'edge'),
    ('outside-tail-limit', ''),
    ('invalid-utf8', 'earlier'),
])
def test_recorded_model_preserves_tail_and_record_boundaries(tmp_path, monkeypatch, case, expected):
    """Window growth must not invent a model from a cropped JSON line."""
    def record(model, **fields):
        return (json.dumps({'type': 'assistant', 'message': {'model': model, **fields}})
                + '\n').encode()

    small_window, tail_limit = 64 * 1024, 1024 * 1024
    if case == 'latest-valid':
        data = (record('earlier') + record('<synthetic>')
                + b'{"type":"turn_context","payload":{"model":"latest"}}\n'
                + b'not-json\n')
    elif case == 'wide-record':
        data = record('wide', content='x' * 90000)
    elif case == 'partial-small-window':
        edge = record('must-not-be-invented')
        data = (record('earlier') + b'invalid-prefix' + edge
                + b'x' * (small_window - len(edge) - 1) + b'\n')
    elif case == 'legacy-tail-boundary':
        edge = record('edge')
        # The old 1 MiB reader parses this first cropped line. Preserve that
        # exact legacy window behavior even though smaller trial windows must
        # discard their potentially partial first record.
        data = b'invalid-prefix' + edge + b'x' * (tail_limit - len(edge) - 1) + b'\n'
    elif case == 'outside-tail-limit':
        data = record('outside') + b'x' * tail_limit
    else:
        data = record('earlier') + b'\xff\xfe\n'
    path = tmp_path / 'bounded.jsonl'
    path.write_bytes(data)
    monkeypatch.setattr(session_models, '_transcript', lambda *args: path)
    session_models._read_model.cache_clear()
    try:
        assert session_models.transcript_model('claude', 'bounded') == expected
    finally:
        session_models._read_model.cache_clear()
