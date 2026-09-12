from lib import db,janitors,janitor_builtins,janitor_design_policy
import pytest

def test_all_five_roles_and_deterministic_exemption():
    roles=janitor_builtins.ensure_builtins()
    assert set(roles)=={'message-delegator','tool-explainer','audio-bookkeeper','heartbeat-decider','quota-monitor'}
    audio=roles['audio-bookkeeper']
    assert audio['execution']=={'executor':'deterministic','provider':'local'}
    assert audio['model']==audio['effort']==''
    assert janitor_design_policy.effective_chain(audio['session'])['source']=='deterministic'
    with pytest.raises(ValueError,match='Deterministic'):
        janitor_design_policy.configure({'model_chain':[{'provider':'codex','model':'gpt-test'}],'inherit_sessions':[audio['session']]},0)
    assert janitor_builtins.begin_run('audio-bookkeeper','never-provider') is None
    assert not roles['heartbeat-decider']['enabled'] and not roles['quota-monitor']['enabled']

def test_v84_to_v85_preserves_audio_outbox_and_adds_autonomy():
    c=db.conn();roles=janitor_builtins.ensure_builtins();audio=roles['audio-bookkeeper']
    c.execute('DROP TABLE janitor_continuity');c.execute('DROP TABLE janitor_quota_receipts');c.execute('PRAGMA user_version=84')
    db._migrate(c)
    assert c.execute('PRAGMA user_version').fetchone()[0]==85
    for table in ['audio_bookkeeping_events','janitor_continuity','janitor_quota_receipts']:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
    assert janitor_builtins.get_builtin('audio-bookkeeper')['agent_id']==audio['agent_id']
