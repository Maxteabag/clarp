import hashlib,json
from types import SimpleNamespace
import pytest
from lib import oracle_voice_context as context,oracle_live_stable,oracle_live


def fixture(tmp_path):
    package=tmp_path/'prepared.md';package.write_text('Confirmed person: Example Person. Product: ExampleSystem.')
    sidecar=tmp_path/'voice.json';sidecar.write_text(json.dumps({'version':1,'primary_session':'primary',
        'prepared_context':{'path':str(package),'sha256':hashlib.sha256(package.read_bytes()).hexdigest()},
        'terms':['Example Person','ExampleSystem']}))
    return package,sidecar


def test_optional_loader_is_bound_and_does_not_rewrite_transcript(tmp_path,monkeypatch):
    monkeypatch.delenv(context.ENV,raising=False)
    assert context.load('primary') is None
    package,path=fixture(tmp_path);monkeypatch.setenv(context.ENV,str(path))
    loaded=context.load('primary')
    assert loaded['terms']==['Example Person','ExampleSystem']
    for module in (oracle_live_stable,oracle_live):
        prompt=module.live_config(voice_context=loaded)['instructions']
        assert 'Example Person' in prompt and 'silently rewrite a transcript' in prompt
    with pytest.raises(ValueError,match='selected primary'):context.load('other')
    package.write_text('Changed source')
    with pytest.raises(ValueError,match='hash changed'):context.load('primary')


def test_unprepared_term_rejected(tmp_path):
    _,path=fixture(tmp_path);value=json.loads(path.read_text());value['terms']=['Invented Person'];path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='absent from bound source'):context.load('primary',path)


def test_configured_path_and_environment_override(tmp_path,monkeypatch):
    from lib import config
    _,path=fixture(tmp_path)
    monkeypatch.delenv(context.ENV,raising=False)
    monkeypatch.setattr(config,'load',lambda:config.Config(oracle_voice_context_file=str(path)))
    assert context.load('primary')['terms']==['Example Person','ExampleSystem']
    monkeypatch.setenv(context.ENV,str(tmp_path/'missing.json'))
    with pytest.raises(OSError):context.load('primary')
