"""Real dispatcher + fake stdio process, no credentials or paid provider calls."""
import json
import time
from types import SimpleNamespace

import pytest
from lib import agents, codex_app_server, codex_runner
from lib.activity import state_activity_event
from lib.turn_dispatch import TurnDispatchService, clear_for_agent
from test_codex_app_server import FAKE_CODEX, _Stream


@pytest.mark.parametrize('mode,expected,turns', [
    ('stale', 'done', 2),
    ('blocked', 'interrupted', 1),
    ('unknown-blocked', 'interrupted', 2),
    ('partial', 'interrupted', 1),
])
def test_connection_recovery_real_dispatch(tmp_path, monkeypatch, mode, expected, turns):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    monkeypatch.setenv('CLARP_QA_PROVIDER_ROOT', str(tmp_path))
    monkeypatch.setattr(codex_runner, 'CODEX_BIN', str(FAKE_CODEX))
    (tmp_path / 'quota-mode').write_text(mode)
    codex_app_server._CLIENTS.clear()
    agent = agents.create_agent(persona='Recovery', voice_id='v', cwd=str(tmp_path), session='recovery', backend='codex')
    agents.start_runtime(agent, 'recovery')
    stream = _Stream()
    ctx = SimpleNamespace(default_session='recovery', stream=stream, agents_path=tmp_path/'unused.json')
    service = TurnDispatchService(ctx, home=tmp_path, retry_scheduler=lambda _delay, fn: fn())
    try:
        service.dispatch(text='hello', requested_session='recovery', trace_id='recover-me')
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            state = agents.latest_state(agent)
            if state['kind'] in ('done', 'interrupted'):
                break
            time.sleep(.05)
        assert state['kind'] == expected
        if expected == 'interrupted' and mode != 'blocked':
            assert 'provider_limit_event_id' not in state['detail']
            assert 'Try again' in state['detail']['summary']
        thread = agents.live_backend_session(agent)
        records = [json.loads(line) for line in (tmp_path/'sessions'/f'rollout-{thread}.jsonl').read_text().splitlines()]
        requests = [row for row in records if row['payload'].get('type') == 'user_message']
        assert len(requests) == turns
        assert all(row['payload']['message'] == 'hello' for row in requests)
        # Retries retain the admission identity and native conversation.
        user_rows = [r for r in agents.list_messages(agent_id=agent, backend_session_id=thread) if r['role']=='user']
        assert len(user_rows) == 1
        rows = agents.conn().execute(
            'SELECT kind, ts, detail FROM state_log WHERE agent_id = ?', (agent,)).fetchall()
        activities = [state_activity_event(agent_id=agent, session='recovery',
            persona='Recovery', kind=row['kind'], ts=row['ts'],
            detail=json.loads(row['detail'] or '{}')) for row in rows]
        if turns == 2:
            assert any(e.get('action')=='reconnecting' and 'saved' in e['summary'] for e in activities)
    finally:
        clear_for_agent(agent)
        codex_app_server.recycle_clients()
