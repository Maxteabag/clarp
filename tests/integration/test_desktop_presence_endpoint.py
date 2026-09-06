import json
import uuid
import urllib.request
import urllib.error
import pytest
from lib import db
from .test_avatar_settings_endpoint import running_server, _post  # noqa: F401


def test_presence_auth_validation_and_sequence(running_server):
    body = {'instance_id': str(uuid.uuid4()), 'sequence': 1, 'active': True, 'sent_at_ms': db.now_ms()}
    request = urllib.request.Request(running_server + '/desktop-presence',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 401
    status, response = _post(running_server, '/desktop-presence', body)
    assert status == 200 and response['accepted'] and response['lease_ms'] == 45000
    _, response = _post(running_server, '/desktop-presence', {**body, 'sequence': 2, 'active': False})
    assert response['accepted']
    _, response = _post(running_server, '/desktop-presence', body)
    assert not response['accepted']
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(running_server, '/desktop-presence', {**body, 'active': 'true'})
    assert error.value.code == 400


def test_limited_device_cannot_silence_phone_alerts(running_server):
    from lib import device_pairing
    issued = device_pairing.issue(scope='limited')
    paired = device_pairing.exchange(issued['code'])
    request = urllib.request.Request(running_server + '/desktop-presence',
        data=json.dumps({'instance_id': str(uuid.uuid4()), 'sequence': 1, 'active': True, 'sent_at_ms': db.now_ms()}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + paired['token']})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 403
