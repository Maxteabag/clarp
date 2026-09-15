import pytest
from lib import oracle_mode, oracle_live, oracle_live_stable, oracle_realtime, config

@pytest.mark.parametrize('path', ['/oracle/v2','/oracle/v2?oracle_session=sage',
    '/oracle/v2?tinkered=0','/oracle/v2?tinkered=true','/oracle/v2?tinkered=1&tinkered=0'])
def test_existing_or_unknown_choice_uses_stable_handler(path):
    assert oracle_mode.voice_handler(path) is oracle_live_stable.serve


def test_explicit_tinkered_choice_selects_research_handler():
    assert oracle_mode.voice_handler('/oracle/v2?tinkered=1&oracle_session=sage') is oracle_live.serve


def test_subscription_configuration_does_not_replace_stable_capability(monkeypatch):
    cfg=config.Config(openai_api_key='fixture',oracle_voice_backend='subscription',oracle_router_backend='codex')
    monkeypatch.setattr(oracle_realtime.config,'load',lambda:cfg)
    from lib import oracle_live_provider
    monkeypatch.setattr(oracle_live_provider,'available',lambda *a:True)
    value=oracle_realtime.capability()
    assert value['v2']['model']=='gpt-live-1' and value['v2']['voice']=='marin'
    assert value['v2'].get('webrtc') is None
    assert value['v2_tinkered']['mode']=='subscription'
    assert value['v2_tinkered']['webrtc'] is True
