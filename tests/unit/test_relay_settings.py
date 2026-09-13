from pathlib import Path
import json
import pytest
from lib.relay_settings import RelaySettings, load, write, import_legacy


@pytest.mark.parametrize('url', ['http://relay.test', 'https://user:secret@relay.test',
                                 'https://relay.test/?key=secret', 'https://relay.test/prefix', 'https://relay.test:bad'])
def test_provisioning_rejects_unsafe_origin(url):
    with pytest.raises(ValueError):
        RelaySettings(url, 'host-test', 'a' * 64)


def test_credentials_are_private_and_status_contains_no_key(tmp_path):
    settings = RelaySettings('wss://relay.test/', 'host-test', 'a' * 64)
    path = tmp_path / 'relay.json'
    write(path, settings)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load(path) == settings
    assert settings.pairing_url == 'https://relay.test/h/host-test'
    assert settings.key not in repr(settings)
    assert settings.key not in json.dumps(settings.public_status())
    path.chmod(0o644)
    with pytest.raises(ValueError, match='private'):
        load(path)


def test_legacy_import_is_data_not_shell_execution(tmp_path):
    path = tmp_path / 'relay.env'
    path.write_text('CLARP_RELAY_URL="wss://relay.test"\nCLARP_RELAY_HOST_ID=host-test\nCLARP_RELAY_HOST_KEY=' + 'a' * 64 + '\n')
    path.chmod(0o600)
    assert import_legacy(path).host_id == 'host-test'
    path.write_text('CLARP_RELAY_URL=$(touch evil)\n')
    with pytest.raises(ValueError):
        import_legacy(path)
    assert not Path('evil').exists()
