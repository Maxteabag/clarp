#!/usr/bin/env python3
"""Writes down how the Python Host pushes to a phone with a key of its own at the push service.

A Host that has such a key (a .p8 file, its key id and its team) signs a short-lived token with it and
hands each push to the push service itself, over HTTP/2. Only so a ring reaches a phone as an incoming
call: the relay carries no pushes of calls. What is pure in that is written down here, from
`server/lib/apns.py` itself: where the service is, the request for one push of every kind (its address,
its headers, its body), what an answer of the service means, what the token says and for how long one
is used, and when a push is tried a second time.

    <the Python Host's interpreter> server-rs/tools/direct-push-vectors.py

The key of the fixture is made here, at every run another, and is in nothing that is written: the file
is the same at every run. Nothing is sent anywhere and nothing is logged.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'direct-push-vectors.json'
sys.path.insert(0, str(REPO / 'server'))

from lib import apns  # noqa: E402

FACE = chr(0x1f44b)
RING = {'kind': 'agent-ring', 'ring_id': 'rng_1', 'ring_state': 'ringing', 'session': 'theo-97e5', 'persona': 'Theo',
        'reason': 'Possible water leak', 'expires_at': 1_800_000_045_000}
TOKEN = 'ab' * 32


class Answer:
    """What the push service answers one push with."""

    def __init__(self, status, body='', headers=None):
        self.status_code = status
        self.text = body
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class Client:
    """A stand-in for the connection to the push service: it writes down what it is asked and answers what it is
    told to."""

    def __init__(self, answers=None):
        self.asked = []
        self.answers = list(answers or [])

    def post(self, url, headers=None, content=None):
        self.asked.append({'url': url, 'headers': dict(headers or {}), 'body': content.decode('utf-8')})
        answer = self.answers.pop(0) if self.answers else Answer(200, '', {'apns-id': 'id-1'})
        if isinstance(answer, Exception):
            raise answer
        return answer


def requests() -> list[dict]:
    cases = [
        ('an alert', dict(payload={'aps': {'alert': {'title': 'Theo', 'body': 'Done'}}, 'session': 'theo-97e5'})),
        ('an alert with a name of its own', dict(payload={'notification_id': 'pn-1', 'session': 'theo-97e5'})),
        ('an alert of nobody', dict(payload={'aps': {}})),
        ('a ring as an incoming call', dict(payload=RING, push_type='voip', priority='10', collapse_id='ring-rng_1',
                                            expiration=1_800_000_045)),
        ('a ring as a call with nothing said of how long it is kept', dict(payload=RING, push_type='voip',
                                                                         collapse_id='ring-rng_1')),
        ('a ring as a call kept until the first second', dict(payload=RING, push_type='voip', collapse_id='ring-rng_1',
                                                            expiration=0)),
        ('a push for the chip on the lock screen', dict(payload={'aps': {'event': 'update'}}, push_type='liveactivity',
                                                       priority='5', collapse_id='chip')),
        ('a quiet push', dict(payload={'aps': {'content-available': 1}, 'session': 's'}, push_type='background',
                              priority='5')),
        ('a body of other alphabets', dict(payload={'aps': {'alert': {'body': 'Bl' + chr(0xe5) + ' ' + FACE}},
                                                    'session': 's'})),
        ('a collapse id that is given empty', dict(payload={'session': 'theo-97e5'}, collapse_id='')),
        ('another bundle', dict(payload={'session': 's'}, bundle_id='org.example.app', push_type='voip')),
    ]
    rows = []
    for name, case in cases:
        client = Client()
        bundle = case.pop('bundle_id', 'com.maxteabag.clarp')
        payload = case.pop('payload')
        environment = 'production'
        result = apns._send_one(client, apns._host(environment), 'the-token', bundle, TOKEN, payload, **case)
        asked = client.asked[0]
        rows.append({'name': name, 'bundle_id': bundle, 'token': TOKEN, 'payload': payload,
                     'push_type': case.get('push_type', 'alert'), 'priority': case.get('priority', '10'),
                     'collapse_id': case.get('collapse_id'), 'expiration': case.get('expiration'),
                     'url': asked['url'], 'headers': asked['headers'], 'body': asked['body'], 'result': list(result)})
    return rows


def answers() -> list[dict]:
    cases = [
        ('taken', Answer(200, '', {'apns-id': 'id-7'})),
        ('taken, with no name for it', Answer(200, '')),
        ('a token that is gone', Answer(410, '{"reason": "Unregistered", "timestamp": 1}')),
        ('a token that is none', Answer(400, '{"reason": "BadDeviceToken"}', {'apns-id': 'id-8'})),
        ('a token of another app', Answer(400, '{"reason": "DeviceTokenNotForTopic"}')),
        ('a provider token that ran out', Answer(403, '{"reason": "ExpiredProviderToken"}')),
        ('a provider token that is none', Answer(403, '{"reason": "InvalidProviderToken"}')),
        ('too many', Answer(429, '{"reason": "TooManyRequests"}')),
        ('an answer without a reason', Answer(500, '{}')),
        ('an answer that is nothing', Answer(500, 'null')),
        ('an answer that is a list', Answer(500, '[1]')),
        ('an answer that is no JSON', Answer(502, '<html>Bad gateway</html>')),
        ('a long answer that is no JSON', Answer(503, 'x' * 300)),
        ('an empty answer', Answer(503, '')),
        ('a reason that is a number', Answer(400, '{"reason": 5}')),
        ('a reason that is nothing', Answer(400, '{"reason": null}')),
        ('an answer that is false', Answer(500, 'false')),
        ('an answer that is nought', Answer(500, '0')),
        ('an answer that is an empty text', Answer(500, '""')),
        ('an answer that is an empty list', Answer(500, '[]')),
        ('an answer that is true', Answer(500, 'true')),
        ('an answer that is a text', Answer(500, '"no"')),
        ('an answer that is a number', Answer(500, ' 7 ')),
        ('a long answer of other alphabets', Answer(500, chr(0xe5) * 201)),
        ('taken, with a body all the same', Answer(200, '{"reason": "Ignored"}')),
    ]
    rows = []
    for name, answer in cases:
        client = Client([answer])
        try:
            result = list(apns._send_one(client, apns._host('production'), 'the-token', 'com.maxteabag.clarp', TOKEN,
                                         {'session': 's'}))
        except Exception as error:  # noqa: BLE001 - what the function does with such an answer is the finding
            result = {'raised': type(error).__name__}
        rows.append({'name': name, 'status': answer.status_code, 'body': answer.text, 'headers': answer.headers,
                     'result': result})
    return rows


def decoded(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))


def provider_tokens(folder: pathlib.Path) -> dict:
    """What a provider token says, and for how long one is used: a story of asking for it."""
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def key_file(name: str) -> pathlib.Path:
        key = ec.generate_private_key(ec.SECP256R1())
        path = folder / name
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        return path

    first, second = key_file('AuthKey_ONE.p8'), key_file('AuthKey_TWO.p8')
    state = {'now': 1_800_000_000, 'minted': 0}
    encode = jwt.encode

    def counted(*args, **kwargs):
        state['minted'] += 1
        return encode(*args, **kwargs)

    jwt.encode = counted
    apns.time.time = lambda: state['now']

    class Config:
        def __init__(self, team, key_id, path):
            self.apns_team_id, self.apns_key_id, self.path = team, key_id, path

        def apns_key_file(self):
            return str(self.path)

    steps = [
        ('the first asking', 0, ('TEAM1', 'KEY1', 'one'), None),
        ('asked again at once', 0, ('TEAM1', 'KEY1', 'one'), None),
        ('a second before it is fifty minutes old', 2999, ('TEAM1', 'KEY1', 'one'), None),
        ('fifty minutes old', 1, ('TEAM1', 'KEY1', 'one'), None),
        ('another team', 10, ('TEAM2', 'KEY1', 'one'), None),
        ('the first team again', 10, ('TEAM1', 'KEY1', 'one'), None),
        ('another key id', 10, ('TEAM1', 'KEY2', 'one'), None),
        ('another key file', 10, ('TEAM1', 'KEY2', 'two'), None),
        ('the same key file, written anew', 10, ('TEAM1', 'KEY2', 'two'), 'touched'),
        ('asked again', 10, ('TEAM1', 'KEY2', 'two'), None),
        ('a clock that went back', -5000, ('TEAM1', 'KEY2', 'two'), None),
    ]
    rows, apns._jwt_cache = [], None
    for name, later, (team, key_id, which), change in steps:
        state['now'] += later
        path = first if which == 'one' else second
        if change == 'touched':
            stamp = path.stat().st_mtime_ns + 5_000_000_000
            os.utime(path, ns=(stamp, stamp))
        before = state['minted']
        token = apns._auth_jwt(Config(team, key_id, path))
        header, claims, _signature = token.split('.')
        key = jwt.algorithms.ECAlgorithm.from_jwk  # noqa: F841 - the token is checked against its key below
        public = serialization.load_pem_private_key(path.read_bytes(), None).public_key()
        jwt.decode(token, public, algorithms=['ES256'], options={'verify_iat': False})
        rows.append({'name': name, 'now': state['now'], 'team_id': team, 'key_id': key_id, 'key_file': which,
                     'written_anew': change == 'touched', 'minted': state['minted'] > before,
                     'header': decoded(header), 'claims': decoded(claims)})
    jwt.encode = encode
    return {'used_for_s': apns._TOKEN_TTL_SEC, 'steps': rows}


def second_tries() -> list[dict]:
    """When one push is handed to the push service a second time, and with what."""
    closed = RuntimeError('Cannot send a request, as the client has been closed.')
    cases = [
        ('taken at once', [Answer(200)]),
        ('a provider token that ran out, then taken', [Answer(403, '{"reason": "ExpiredProviderToken"}'), Answer(200)]),
        ('a provider token that is none, twice', [Answer(403, '{"reason": "InvalidProviderToken"}'),
                                                Answer(403, '{"reason": "InvalidProviderToken"}'), Answer(200)]),
        ('refused for another reason', [Answer(403, '{"reason": "Forbidden"}'), Answer(200)]),
        ('a token that is gone', [Answer(410, '{"reason": "Unregistered"}'), Answer(200)]),
        ('a provider token said to have run out, under another status',
         [Answer(400, '{"reason": "ExpiredProviderToken"}'), Answer(200)]),
        ('refused, with a reason that is no text', [Answer(403, '{"reason": 7}'), Answer(200)]),
        ('a connection that was closed, then taken', [closed, Answer(200)]),
        ('a connection that was closed twice', [closed, closed, Answer(200)]),
        ('a connection closed, then a provider token that ran out, then taken',
         [closed, Answer(403, '{"reason": "ExpiredProviderToken"}'), Answer(200)]),
        ('a provider token that ran out, then a connection closed, then taken',
         [Answer(403, '{"reason": "ExpiredProviderToken"}'), closed, Answer(200)]),
        ('another failure of the connection', [RuntimeError('no route'), Answer(200)]),
        ('a failure that is no runtime error', [OSError('reset'), Answer(200)]),
    ]
    rows = []
    for name, said in cases:
        client = Client(said)
        minted = {'count': 0}

        def auth(cfg):
            minted['count'] += 1
            return f'token-{minted["count"]}'

        apns._auth_jwt, apns._pooled_client, apns.reset_jwt_cache = auth, lambda: client, lambda: None

        class Config:
            apns_bundle_id = 'com.maxteabag.clarp'

        transport = apns._DirectTransport(Config())
        try:
            result = list(transport.send(TOKEN, 'production', RING, push_type='voip', priority='10',
                                         collapse_id='ring-rng_1', expiration=1_800_000_045))
        except Exception as error:  # noqa: BLE001 - that it fails, and as what, is the finding
            result = {'raised': type(error).__name__}
        rows.append({'name': name,
                     'answers': [{'raised': type(a).__name__, 'closed': 'client has been closed' in str(a)}
                                 if isinstance(a, Exception) else {'status': a.status_code, 'body': a.text} for a in said],
                     'sent_with': [asked['headers']['authorization'] for asked in client.asked], 'result': result})
    return rows


def main() -> int:
    with tool_scratch.directory(prefix='clarp-direct-push-vectors-') as folder:
        tokens = provider_tokens(pathlib.Path(folder))
    answered = {
        'what': 'server/lib/apns.py: a push handed to the push service with a key of the Host own',
        'hosts': {environment: apns._host(environment) for environment in ('production', 'sandbox', 'Sandbox', 'SANDBOX',
                                                                              '', 'other', ' sandbox')},
        'gone_for_good': sorted(apns._DEAD_REASONS), 'token_refused': sorted(apns._PROVIDER_TOKEN_REJECTED),
        'requests': requests(), 'answers': answers(), 'provider_tokens': tokens, 'second_tries': second_tries(),
    }
    text = json.dumps(answered, indent=1, ensure_ascii=True) + '\n'
    OUT.write_text(text, encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(answered["requests"])} requests, {len(answered["answers"])} answers, '
          f'{len(tokens["steps"])} askings for a provider token, {len(answered["second_tries"])} stories of a second try, '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
