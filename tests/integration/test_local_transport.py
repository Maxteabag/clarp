import hashlib
import http.client
import json
from pathlib import Path
import socket
import ssl
import threading

import pytest
from lib import device_pairing
from lib.local_transport import LocalHTTPS, ensure_identity
from tests.integration.test_server_di import fake_ctx, server_module


def test_local_identity_is_persistent_private_and_not_silently_replaced(tmp_path):
    path, fingerprint = ensure_identity(tmp_path/'tls', 'test-host')
    assert path.stat().st_mode & 0o777 == 0o600
    original = path.read_bytes()
    assert ensure_identity(tmp_path/'tls', 'test-host') == (path, fingerprint)
    assert path.read_bytes() == original
    path.write_bytes(b'broken')
    with pytest.raises(ValueError):
        ensure_identity(tmp_path/'tls', 'test-host')
    assert path.read_bytes() == b'broken'


def test_tls_auth_pin_revocation_and_nonblocking_handshake(fake_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr('lib.bonjour.BonjourAdvertiser.start', lambda self: True)
    fake_ctx.auth_token = 'local-admin'
    owner = server_module.ContextHTTPServer(('127.0.0.1',0),server_module.Handler,fake_ctx)
    local = LocalHTTPS(owner,server_module.Handler,tmp_path/'tls',0,bind='127.0.0.1')
    owner.local_transport=local
    local.start()
    pending = socket.create_connection(('127.0.0.1',local.server.server_port))
    def get(token):
        # The test emulates native leaf pinning before sending any credentials.
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname=False;context.verify_mode=ssl.CERT_NONE
        connection=http.client.HTTPSConnection('127.0.0.1',local.server.server_port,context=context,timeout=2)
        connection.connect()
        assert hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()==local.fingerprint
        connection.request('GET','/server-info',headers={'Authorization':'Bearer '+token})
        response=connection.getresponse(); body=response.read();connection.close()
        return response.status,body
    try:
        assert get('')[0]==401
        assert get('local-admin')[0]==401
        device=device_pairing.exchange(device_pairing.issue()['code'])
        status,body=get(device['token'])
        assert status==200
        assert json.loads(body)['local_connection']['certificate_sha256']==local.fingerprint
        assert device_pairing.revoke(device['device_id'])
        assert get(device['token'])[0]==401
    finally:
        pending.close();local.close();owner.server_close()


@pytest.mark.parametrize('value,expected', [('192.168.1.2',True),('10.1.2.3',True),('172.31.1.1',True),('100.64.1.2',False),('8.8.8.8',False),('0.0.0.0',False),('127.0.0.1',False)])
def test_local_listener_accepts_only_private_ipv4_peers(value, expected):
    from lib.local_transport import private_ipv4
    assert private_ipv4(value) is expected
