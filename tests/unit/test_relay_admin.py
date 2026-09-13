import argparse
import json
import tomllib
import pytest
from tests.unit.test_clarp_admin import admin
from lib.relay_settings import RelaySettings, write


@pytest.fixture
def configured(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('[server]\nport=7682\nauth_token="local-secret"\npublic_base_url="https://host.tail.test"\n[network]\nmode="tailscale"\n')
    monkeypatch.setattr(admin, 'CONFIG_FILE', path)
    write(tmp_path / 'relay.json', RelaySettings('https://relay.test', 'host-test', 'a' * 64))
    monkeypatch.setattr(admin, '_legacy_relay_active', lambda: False)
    monkeypatch.setattr(admin, 'api_request', lambda *a: {'enabled': True, 'state': 'connected'})
    calls = []
    monkeypatch.setattr(admin.service_manager, 'restart', lambda: calls.append('restart'))
    return path, calls


def test_enable_and_disable_preserve_tailscale(configured, capsys):
    path, calls = configured
    for action, expected in [('enable', True), ('disable', False)]:
        admin.cmd_relay(argparse.Namespace(relay_command=action, pair=False))
        data = tomllib.loads(path.read_text())
        assert data['network']['mode'] == 'tailscale'
        assert data['server']['public_base_url'] == 'https://host.tail.test'
        assert data['network']['relay_enabled'] is expected
    assert calls == ['restart', 'restart']
    assert 'a' * 64 not in capsys.readouterr().out


def test_failed_restart_restores_original_config_and_service(configured, monkeypatch):
    path, calls = configured
    original = path.read_bytes()
    def restart():
        calls.append('restart')
        if len(calls) == 1:
            raise RuntimeError('simulated startup failure')
    monkeypatch.setattr(admin.service_manager, 'restart', restart)
    with pytest.raises(RuntimeError, match='simulated'):
        admin.cmd_relay(argparse.Namespace(relay_command='enable', pair=False))
    assert path.read_bytes() == original
    assert calls == ['restart', 'restart']


def test_migration_adopts_existing_pairing_identity(configured, monkeypatch):
    path, calls = configured
    legacy = path.parent / 'relay.env'
    legacy.write_text('CLARP_RELAY_URL=wss://relay.test\nCLARP_RELAY_HOST_ID=old-host-id\nCLARP_RELAY_HOST_KEY=' + 'b' * 64 + '\n')
    legacy.chmod(0o600)
    monkeypatch.setattr(admin, '_legacy_relay_active', lambda: True)
    monkeypatch.setattr(admin, '_legacy_relay_control', lambda action: calls.append(action))
    admin.cmd_relay(argparse.Namespace(relay_command='migrate', pair=False))
    assert calls == ['stop', 'restart', 'disable']
    assert json.loads((path.parent / 'relay.json').read_text())['host_id'] == 'old-host-id'
    assert legacy.exists()


def test_enable_does_not_compete_with_legacy_connector(configured, monkeypatch):
    path, calls = configured
    original = path.read_bytes()
    monkeypatch.setattr(admin, '_legacy_relay_active', lambda: True)
    with pytest.raises(ValueError, match='migrate'):
        admin.cmd_relay(argparse.Namespace(relay_command='enable', pair=False))
    assert path.read_bytes() == original
    assert not calls


def test_failed_migration_restores_settings_and_old_connector(configured, monkeypatch):
    path, calls = configured
    original = path.read_bytes()
    old_credentials = (path.parent / 'relay.json').read_bytes()
    legacy = path.parent / 'relay.env'
    legacy.write_text('CLARP_RELAY_URL=wss://relay.test\nCLARP_RELAY_HOST_ID=old-host-id\nCLARP_RELAY_HOST_KEY=' + 'b' * 64 + '\n')
    legacy.chmod(0o600)
    monkeypatch.setattr(admin, '_legacy_relay_active', lambda: True)
    monkeypatch.setattr(admin, '_legacy_relay_control', lambda action: calls.append(action))
    def restart():
        calls.append('restart')
        if calls.count('restart') == 1:
            raise RuntimeError('candidate failed')
    monkeypatch.setattr(admin.service_manager, 'restart', restart)
    with pytest.raises(RuntimeError, match='candidate failed'):
        admin.cmd_relay(argparse.Namespace(relay_command='migrate', pair=False))
    assert calls == ['stop', 'restart', 'restart', 'start']
    assert path.read_bytes() == original
    assert (path.parent / 'relay.json').read_bytes() == old_credentials


def test_cli_device_revocation_uses_http_to_close_live_connections(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(admin, 'api_request', lambda *args: calls.append(args) or {'ok': True})
    admin.cmd_pair(argparse.Namespace(pair_command='revoke', device_id='device-test'))
    assert calls == [('POST', '/paired-devices/revoke', {'device_id': 'device-test'})]
