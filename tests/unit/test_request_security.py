from __future__ import annotations

from email.message import Message
from types import SimpleNamespace

from tests.integration.test_server_di import server_module
from lib import device_pairing
import pytest


def handler(path='/status', headers=None, peer='127.0.0.1', token='admin-secret'):
    request = object.__new__(server_module.Handler)
    request.path = path
    request.client_address = (peer, 1234)
    request.headers = Message()
    for k, v in (headers or {}).items():
        request.headers[k] = v
    request.server = SimpleNamespace(ctx=SimpleNamespace(auth_token=token))
    return request


@pytest.mark.parametrize('headers,peer', [
    ({}, '192.0.2.20'),
    ({'X-Forwarded-For': '192.0.2.20'}, '127.0.0.1'),
    ({'X-Clarp-Transport': 'relay'}, '127.0.0.1'),
    ({'Forwarded': 'for=192.0.2.20'}, '::1'),
    ({'Tailscale-User-Login': 'test@example.test'}, '127.0.0.1'),
])
def test_administrator_credential_is_local_only(headers, peer):
    request = handler(headers={'Authorization': 'Bearer admin-secret', **headers}, peer=peer)
    assert not request._authorized()


def test_local_admin_and_query_access_remain_available():
    assert handler(headers={'Authorization': 'Bearer admin-secret'})._authorized()
    assert handler(path='/status?token=admin-secret')._authorized()


def test_remote_device_headers_and_cookies_work_but_query_credentials_do_not():
    device = device_pairing.exchange(device_pairing.issue()['code'])
    for transport in [{'Authorization': 'Bearer ' + device['token']},
                      {'Cookie': 'claude_pwa_token=' + device['token']}]:
        assert handler(headers={'X-Clarp-Transport': 'relay', **transport})._authorized()
    assert not handler('/status?token=' + device['token'], {'X-Clarp-Transport': 'relay'})._authorized()


def test_proxied_server_without_auth_fails_closed():
    assert not handler(headers={'X-Clarp-Transport': 'relay'}, token='')._authorized()


def test_empty_bearer_header_is_rejected_without_crashing():
    assert not handler(headers={'Authorization': 'Bearer   '})._authorized()


def test_access_log_redacts_credentials(capsys):
    request = handler('/status?token=admin-secret&key=relay-secret&code=pair-secret')
    request.log_message('"%s" %s', 'GET ' + request.path + ' HTTP/1.1', '401')
    text = capsys.readouterr().out
    assert 'admin-secret' not in text
    assert 'relay-secret' not in text
    assert 'pair-secret' not in text


def test_failure_limiter_expires_and_bounds_sources():
    from lib.request_security import FailureLimiter
    clock = [0.0]
    limiter = FailureLimiter(limit=2, window=60, max_sources=2, clock=lambda: clock[0])
    assert limiter.failure('a') == 0
    assert limiter.failure('a') == 0
    assert limiter.failure('a') == 60
    clock[0] = 61
    assert limiter.failure('a') == 0
    limiter.failure('b'); limiter.failure('c')
    assert len(limiter._sources) == 2


def test_remote_forwarded_ip_cannot_evade_failure_budget():
    from lib.request_security import failure_source
    assert failure_source('192.0.2.20', {'X-Forwarded-For': '192.0.2.30'}) == '192.0.2.20'
    assert failure_source('127.0.0.1', {'X-Forwarded-For': '192.0.2.30'}) == '192.0.2.30'


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://attacker.example'},
    {'Origin': 'null'},
    {'Sec-Fetch-Site': 'cross-site'},
])
def test_browser_request_from_another_site_is_not_local_administration(headers):
    assert not handler(headers={'Authorization': 'Bearer admin-secret', **headers})._authorized()


def test_local_browser_bootstrap_accepts_localhost_origin():
    assert handler(headers={'Origin': 'http://localhost:7682', 'Authorization': 'Bearer admin-secret'})._authorized()
