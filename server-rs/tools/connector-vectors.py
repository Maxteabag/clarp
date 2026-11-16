#!/usr/bin/env python3
"""Writes the vectors the Rust Host's connection relay and its rules of trust are tested against.

A Host without a public address is reached through a relay: the Host keeps one outbound
WebSocket to it, and a phone's requests come down that socket as frames
(`server/lib/relay_connector.py`, `server/lib/relay_settings.py`). What came that way is remote:
only a request from this machine itself, with no sign of a proxy on it, is trusted as local
(`server/lib/request_security.py`).

This runs those modules themselves and writes to
`crates/clarp-kernel/tests/fixtures/connector-vectors.json`: frames, the headers a request is
handed on with, when the relay is dialled again, which settings are taken and why not, what the
connector does with each frame through scripts of frames made up here, which requests are
local, whose failure a failed authentication is, and when failures are answered with a wait.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/connector-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. Nothing is dialled: the connector is given frames by hand and its streams are
stood in for. No Host is touched or started.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'connector-vectors.json'

REQ, REQ_BODY, REQ_END = 0x01, 0x02, 0x03
RES_HEAD, RES_BODY, RES_END = 0x11, 0x12, 0x13
WS_OPEN, WS_ACCEPT, WS_MSG, WS_CLOSE = 0x21, 0x22, 0x23, 0x24
ABORT = 0x7F

# ---- frames ------------------------------------------------------------------------------------

FRAMES = [
    (REQ_END, 1, ''), (RES_BODY, 7, 'hello'), (ABORT, 0, ''), (RES_END, 4294967295, ''), (WS_MSG, 65536, '\x01hi'),
    (RES_BODY, 258, '\x00\xff\x7f'),
]
JSON_FRAMES = [
    (RES_HEAD, 3, {'status': 200, 'headers': {'Content-Type': 'application/json', 'X-A': 'b c'}}),
    (RES_HEAD, 3, {'status': 404, 'headers': {}}),
    (WS_ACCEPT, 9, {'ok': True}),
    (WS_CLOSE, 9, {'code': 1012, 'reason': 'Host stream gone'}),
    (WS_CLOSE, 9, {'code': 1000, 'reason': 'Host connection closed'}),
    (RES_HEAD, 5, {'status': 200, 'headers': {'X-Name': 'Zoë — “x”', 'X-Q': 'a"b\\c'}}),
]

# ---- the headers a request is handed on with ---------------------------------------------------

PHONE = {'accept': '*/*', 'authorization': 'Bearer device-token', 'user-agent': 'Clarp/141 CFNetwork',
         'x-clarp-client': 'ios 141 contract 64', 'accept-encoding': 'gzip, deflate, br',
         'x-forwarded-for': '203.0.113.7', 'x-forwarded-proto': 'https', 'x-forwarded-host': 'relay.example',
         'x-forwarded-prefix': '/h/elitebook2', 'accept-language': 'en-GB,en;q=0.9'}
FORWARDED = [
    {'headers': PHONE, 'body': 0, 'passthrough': False},
    {'headers': PHONE, 'body': 0, 'passthrough': True},
    {'headers': PHONE, 'body': 17, 'passthrough': False},
    {'headers': {}, 'body': 0, 'passthrough': False},
    {'headers': {}, 'body': 5, 'passthrough': True},
    # What a hop adds, and what names the hop: none of it goes on.
    {'headers': {'Connection': 'keep-alive, X-Secret-Hop', 'X-Secret-Hop': '1', 'Keep-Alive': 'timeout=5',
                 'Transfer-Encoding': 'chunked', 'TE': 'trailers', 'Trailer': 'x', 'Upgrade': 'websocket',
                 'Proxy-Connection': 'close', 'Proxy-Authorization': 'Basic x', 'Host': 'relay.example',
                 'Content-Length': '99', 'Content-Type': 'application/json'}, 'body': 3, 'passthrough': False},
    # What says where a request came from, or how: only the relay's own word for it stays.
    {'headers': {'Forwarded': 'for=1.2.3.4', 'X-Real-IP': '1.2.3.4', 'X-Forwarded-For': '198.51.100.9',
                 'X-Forwarded-Port': '443', 'X-Clarp-Transport': 'local', 'X-Clarp-Client': 'ios',
                 'Tailscale-User-Login': 'owner@example.com', 'CF-Connecting-IP': '198.51.100.9', 'CF-Ray': 'abc',
                 'Sec-WebSocket-Key': 'k', 'Sec-WebSocket-Version': '13', 'Sec-Fetch-Site': 'cross-site',
                 'Origin': 'https://app.example', 'Cookie': 'claude_pwa_token=t', 'X-Session': 'theo'},
     'body': 0, 'passthrough': False},
    # Whose address it is said to be.
    {'headers': {'x-forwarded-for': '2001:DB8:0:0:0:0:0:1'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '::ffff:1.2.3.4'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '::1'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '1.2.3.4, 5.6.7.8'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': ' 1.2.3.4'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '01.2.3.4'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '1.2.3'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '256.1.1.1'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': ''}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': 'unknown'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '2001:db8::'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '0:0:0:0:0:0:0:0'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '1:0:0:2:0:0:0:3'}, 'body': 0, 'passthrough': False},
    {'headers': {'X-Forwarded-For': '10.0.0.1', 'x-forwarded-for': '10.0.0.2'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-forwarded-for': '10.0.0.2', 'X-Forwarded-For': '10.0.0.1'}, 'body': 0, 'passthrough': False},
    # Whether the answer may come packed.
    {'headers': {'Accept-Encoding': 'gzip'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'gzip'}, 'body': 0, 'passthrough': False},
    {'headers': {'Accept-Encoding': 'br, GZIP;q=0.5'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'gzip;q=0'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'gzip; q=0.0, br'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'gzip;q=0.000'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'gzip;q=0.0000'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'gzip;Q=0'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'identity, *;q=0'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'deflate, br'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': ' gzip '}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'x-gzip'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': ''}, 'body': 0, 'passthrough': True},
    {'headers': {'accept-encoding': 'br', 'Accept-Encoding': 'gzip'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'gzip', 'accept-encoding': 'br'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'gzip;q=1;x=0'}, 'body': 0, 'passthrough': True},
    {'headers': {'Accept-Encoding': 'gzip ;q=0'}, 'body': 0, 'passthrough': True},
    # Names in any case, and a name that only looks like one of them.
    {'headers': {'HOST': 'x', 'content-LENGTH': '4', 'X-FORWARDED-FOR': '192.0.2.1', 'X-Forwardedness': 'kept',
                 'Cfg': 'kept', 'Tailscale': 'kept', 'X-Clarpish': 'kept', 'Second-WebSocket': 'kept',
                 'Sec-Websocket-Protocol': 'gone'}, 'body': 0, 'passthrough': False},
    {'headers': {'connection': ' Upgrade ,  x-one ,, X-TWO', 'x-one': '1', 'X-Two': '2', 'x-three': '3'},
     'body': 0, 'passthrough': False},
    # What is no header HTTP can carry: the request is refused whole.
    {'headers': {'x-ok': '1', 'x bad': '1'}, 'body': 0, 'passthrough': False},
    {'headers': {'': 'x'}, 'body': 0, 'passthrough': False},
    {'headers': {'x:colon': '1'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-\u00f6': '1'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-ok\r\nx-more': '1'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-ok': 'a\nb'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-ok': 'a\rb'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-ok': 'a\x00b'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-ok': 'a\x7fb'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-count': 5}, 'body': 0, 'passthrough': False},
    {'headers': {'x-none': None}, 'body': 0, 'passthrough': False},
    {'headers': {'connection': 'x-ok', 'x-ok': 'a\nb'}, 'body': 0, 'passthrough': False},
    {'headers': {'cf-ray': 'a\nb'}, 'body': 0, 'passthrough': False},
    # And what is one, though it looks odd.
    {'headers': {'x-ok': 'a\tb'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-ok': 'Zo\u00eb'}, 'body': 0, 'passthrough': False},
    {'headers': {'x-ok': ''}, 'body': 0, 'passthrough': False},
    {'headers': {"!#$%&'*+-.^_`|~09azAZ": 'every sign a name may have'}, 'body': 0, 'passthrough': False},
]

# ---- when the relay is dialled again -----------------------------------------------------------

DELAYS = [
    {'previous': None, 'was_connected': False, 'close': None},
    {'previous': 1.0, 'was_connected': False, 'close': None},
    {'previous': 2.0, 'was_connected': False, 'close': None},
    {'previous': 16.0, 'was_connected': False, 'close': None},
    {'previous': 30.0, 'was_connected': False, 'close': None},
    {'previous': 0.3, 'was_connected': False, 'close': None},
    {'previous': 5.0, 'was_connected': False, 'close': None},
    {'previous': None, 'was_connected': True, 'close': None},
    {'previous': 30.0, 'was_connected': True, 'close': {}},
    {'previous': 30.0, 'was_connected': True, 'close': {'rcvd_code': 1012, 'rcvd_reason': 'replaced'}},
    {'previous': None, 'was_connected': True, 'close': {'rcvd_code': 1012, 'rcvd_reason': 'host disconnected'}},
    {'previous': None, 'was_connected': True, 'close': {'rcvd_code': 1001, 'rcvd_reason': 'replaced'}},
    {'previous': None, 'was_connected': True, 'close': {'code': 1012, 'reason': 'replaced'}},
    {'previous': None, 'was_connected': False, 'close': {'rcvd_code': 1012, 'rcvd_reason': 'replaced'}},
    {'previous': 5.0, 'was_connected': True, 'close': {'rcvd_code': 1006, 'rcvd_reason': None}},
]

# ---- the relay's settings ----------------------------------------------------------------------

HOST_ID, KEY = 'elitebook-test', 'ab' * 32
SETTINGS = [
    ('https://relay.example', HOST_ID, KEY), ('https://relay.example/', HOST_ID, KEY),
    ('wss://relay.example', HOST_ID, KEY), ('wss://relay.example/', HOST_ID, KEY),
    ('https://relay.example:8443', HOST_ID, KEY), ('https://relay.example:443/', HOST_ID, KEY),
    ('HTTPS://Relay.Example', HOST_ID, KEY), ('https://relay.example:', HOST_ID, KEY),
    ('https://[::1]:8443', HOST_ID, KEY), ('https://[2001:db8::1]', HOST_ID, KEY),
    ('https://clarp-relay.someone.workers.dev', HOST_ID, KEY), ('https://127.0.0.1:9', HOST_ID, KEY),
    ('https://relay.example:0', HOST_ID, KEY), ('https://relay.example:65535', HOST_ID, KEY),
    ('https://relay.example:00443', HOST_ID, KEY), (' https://relay.example ', HOST_ID, KEY),
    ('https://re\tlay.example', HOST_ID, KEY), ('https://relay.example\n', HOST_ID, KEY),
    # What is no address of a relay.
    ('http://relay.example', HOST_ID, KEY), ('ws://relay.example', HOST_ID, KEY), ('relay.example', HOST_ID, KEY),
    ('', HOST_ID, KEY), ('https://', HOST_ID, KEY), ('https:///', HOST_ID, KEY), ('https:relay.example', HOST_ID, KEY),
    ('https://user@relay.example', HOST_ID, KEY), ('https://user:secret@relay.example', HOST_ID, KEY),
    ('https://@relay.example', HOST_ID, KEY), ('https://:@relay.example', HOST_ID, KEY),
    ('https://relay.example/h', HOST_ID, KEY), ('https://relay.example//', HOST_ID, KEY),
    ('https://relay.example?x=1', HOST_ID, KEY), ('https://relay.example/?x=1', HOST_ID, KEY),
    ('https://relay.example?', HOST_ID, KEY), ('https://relay.example#f', HOST_ID, KEY),
    ('https://relay.example#', HOST_ID, KEY), ('https://relay.example:99999', HOST_ID, KEY),
    ('https://relay.example:65536', HOST_ID, KEY), ('https://relay.example:abc', HOST_ID, KEY),
    ('https://relay.example:-1', HOST_ID, KEY), ('https://relay.example:4 4', HOST_ID, KEY),
    ('https://:8443', HOST_ID, KEY), ('ftp://relay.example', HOST_ID, KEY), ('//relay.example', HOST_ID, KEY),
    ('https://relay.example\\x', HOST_ID, KEY), ('https://relay.example;x', HOST_ID, KEY),
    # An address in brackets, and what only looks like one.
    ('https://[::1]:8443/', HOST_ID, KEY), ('https://[fe80::1%25eth0]', HOST_ID, KEY), ('https://[v1.x]', HOST_ID, KEY),
    ('https://[::1', HOST_ID, KEY), ('https://::1]', HOST_ID, KEY), ('https://[127.0.0.1]', HOST_ID, KEY),
    ('https://[::1]x', HOST_ID, KEY), ('https://x[::1]', HOST_ID, KEY), ('https://[vz.x]', HOST_ID, KEY),
    ('https://[v1.]', HOST_ID, KEY), ('https://[notanip]', HOST_ID, KEY), ('https://[]', HOST_ID, KEY),
    ('https://u[x]@relay.example', HOST_ID, KEY), ('https://[::1]:x', HOST_ID, KEY), ('https://[::1]:', HOST_ID, KEY),
    # A name in letters this Host does not read in an address (the one row the port answers otherwise).
    ('https://r\u00e9lay.example', HOST_ID, KEY),
    # A Host's name at the relay.
    ('https://relay.example', 'elitebook', KEY), ('https://relay.example', 'elitebook2', KEY),
    ('https://relay.example', 'a' * 8, KEY), ('https://relay.example', 'a' * 64, KEY),
    ('https://relay.example', '--------', KEY), ('https://relay.example', '01234567', KEY),
    ('https://relay.example', 'a' * 7, KEY), ('https://relay.example', 'a' * 65, KEY),
    ('https://relay.example', 'Elitebook', KEY), ('https://relay.example', 'elite_book', KEY),
    ('https://relay.example', 'elitebook\n', KEY), ('https://relay.example', ' elitebook', KEY),
    ('https://relay.example', 'elite book', KEY), ('https://relay.example', 'élitebook', KEY),
    ('https://relay.example', '', KEY), ('https://relay.example', 'elitebook٢', KEY),
    # Its key there.
    ('https://relay.example', HOST_ID, '0123456789abcdef' * 4), ('https://relay.example', HOST_ID, 'AB' * 32),
    ('https://relay.example', HOST_ID, 'ab' * 31 + 'a'), ('https://relay.example', HOST_ID, 'ab' * 32 + 'a'),
    ('https://relay.example', HOST_ID, 'g' * 64), ('https://relay.example', HOST_ID, ''),
    ('https://relay.example', HOST_ID, 'ab' * 32 + '\n'), ('https://relay.example', HOST_ID, ' ' + 'ab' * 32),
    # More than one thing wrong: what is said first.
    ('http://relay.example', 'x', 'y'), ('https://relay.example', 'x', 'y'), ('https://relay.example:99999', 'x', 'y'),
]

# A file of settings, by what is in it and who may read it.
GOOD = {'url': 'https://relay.example', 'host_id': HOST_ID, 'key': KEY}
FILES = [
    {'name': 'as it is written', 'text': json.dumps(GOOD) + '\n', 'mode': 0o600},
    {'name': 'for its owner to read only', 'text': json.dumps(GOOD), 'mode': 0o400},
    {'name': 'with more in it', 'text': json.dumps({**GOOD, 'note': 'x', 'n': 1}), 'mode': 0o600},
    {'name': 'readable by the group', 'text': json.dumps(GOOD), 'mode': 0o640},
    {'name': 'readable by everybody', 'text': json.dumps(GOOD), 'mode': 0o644},
    {'name': 'writable by others', 'text': json.dumps(GOOD), 'mode': 0o602},
    {'name': 'runnable by the group', 'text': json.dumps(GOOD), 'mode': 0o610},
    {'name': 'for its owner to run too', 'text': json.dumps(GOOD), 'mode': 0o700},
    {'name': 'no JSON', 'text': 'url = https://relay.example', 'mode': 0o600},
    {'name': 'nothing in it', 'text': '', 'mode': 0o600},
    {'name': 'a list', 'text': '["https://relay.example"]', 'mode': 0o600},
    {'name': 'a word', 'text': '"https://relay.example"', 'mode': 0o600},
    {'name': 'nothing', 'text': 'null', 'mode': 0o600},
    {'name': 'without a key', 'text': json.dumps({'url': GOOD['url'], 'host_id': HOST_ID}), 'mode': 0o600},
    {'name': 'without an address', 'text': json.dumps({'host_id': HOST_ID, 'key': KEY}), 'mode': 0o600},
    {'name': 'an address that is none', 'text': json.dumps({**GOOD, 'url': 'http://relay.example'}), 'mode': 0o600},
    {'name': 'a name that is none', 'text': json.dumps({**GOOD, 'host_id': 'x'}), 'mode': 0o600},
    {'name': 'a key that is none', 'text': json.dumps({**GOOD, 'key': 'x'}), 'mode': 0o600},
    {'name': 'a name that is nothing', 'text': json.dumps({**GOOD, 'host_id': None}), 'mode': 0o600},
    {'name': 'a key that is a number', 'text': json.dumps({**GOOD, 'key': 5}), 'mode': 0o600},
    {'name': 'no JSON and readable by everybody', 'text': 'x', 'mode': 0o644},
]

# ---- what the connector does with each frame ---------------------------------------------------

# The limits are made small here, so that a script reaches them: the connector reads both when
# it decides, and the port takes them as numbers.
MAX_BODY, MAX_STREAMS = 40, 3


def meta(path='/server-info', **more):
    return {'method': 'GET', 'path': path, 'headers': {'accept': '*/*'}, **more}


def fr(kind, sid, payload=b''):
    return {'frame': [kind, sid, payload.hex() if isinstance(payload, bytes) else payload]}


def js(kind, sid, obj):
    return {'frame': [kind, sid, json.dumps(obj, separators=(',', ':')).encode().hex()]}


def raw(hexed):
    return {'raw': hexed}


def done(sid):
    return {'done': sid}


SCRIPTS = [
    {'name': 'a request with no body, one with a body, and both answered',
     'steps': [js(REQ, 1, meta()), fr(REQ_END, 1), js(REQ, 2, meta('/send', method='POST')), fr(REQ_BODY, 2, b'{"a":'),
               fr(REQ_BODY, 2, b'1}'), fr(REQ_END, 2), done(1), done(2), js(REQ, 1, meta('/again')), fr(REQ_END, 1)]},
    {'name': 'what is no frame, and a stream opened twice',
     'steps': [raw(''), raw('01'), raw('01000000'), js(REQ, 5, meta()), js(REQ, 5, meta())]},
    {'name': 'a WebSocket opened over a request',
     'steps': [js(REQ, 5, meta()), js(WS_OPEN, 5, meta('/terminal/x'))]},
    {'name': 'a request that says nothing a Host can read',
     'steps': [fr(REQ, 1, b'not json')]},
    {'name': 'a request with no path', 'steps': [js(REQ, 1, {'headers': {}})]},
    {'name': 'a request that is a list', 'steps': [fr(REQ, 1, b'[]')]},
    {'name': 'a request that is a number', 'steps': [fr(WS_OPEN, 1, b'5')]},
    {'name': 'a request that is nothing', 'steps': [fr(REQ, 1, b'null')]},
    {'name': 'a request with nothing in it', 'steps': [fr(REQ, 1)]},
    {'name': 'a path that does not begin at the root', 'steps': [js(REQ, 1, meta('server-info'))]},
    {'name': 'a path that is no text', 'steps': [js(REQ, 1, meta(5))]},
    {'name': 'a path with a line end in it', 'steps': [js(REQ, 1, meta('/a\nb'))]},
    {'name': 'a path with the sign that deletes', 'steps': [js(REQ, 1, meta('/a\x7fb'))]},
    {'name': 'a path with a nothing in it', 'steps': [js(REQ, 1, meta('/a\x00b'))]},
    {'name': 'headers that are a list', 'steps': [js(REQ, 1, {'path': '/x', 'headers': []})]},
    {'name': 'no headers', 'steps': [js(REQ, 1, {'path': '/x'})]},
    {'name': 'a WebSocket with no headers', 'steps': [js(WS_OPEN, 1, {'path': '/x', 'headers': None})]},
    {'name': 'paths a Host is asked for as they are',
     'steps': [js(REQ, 1, meta('/')), js(REQ, 2, meta('/a b?q=ü&x=%0a')), js(REQ, 3, {'path': '/x', 'headers': {}})]},
    {'name': 'more streams than a Host carries at once',
     'steps': [js(REQ, 1, meta()), js(REQ, 2, meta()), js(WS_OPEN, 3, meta('/terminal/x')), js(REQ, 4, meta()),
               fr(REQ, 5, b'not json'), js(WS_OPEN, 6, meta()), fr(REQ_END, 4), fr(WS_MSG, 6, b'\x01hi'), done(2),
               js(REQ, 4, meta()), js(REQ, 7, meta())]},
    {'name': 'frames for a stream this Host never saw',
     'steps': [fr(REQ_BODY, 9, b'x'), fr(REQ_END, 9), fr(WS_MSG, 9, b'\x01hi'), fr(WS_MSG, 9), fr(ABORT, 9),
               js(WS_CLOSE, 9, {'code': 1000, 'reason': ''}), fr(RES_HEAD, 9, b'{}'), fr(RES_BODY, 9, b'x'),
               fr(RES_END, 9), fr(WS_ACCEPT, 9, b'{}'), fr(0x55, 9, b'x'), fr(0, 9)]},
    {'name': 'a body after its end, and a request ended twice',
     'steps': [js(REQ, 1, meta()), fr(REQ_BODY, 1, b'ab'), fr(REQ_END, 1), fr(REQ_END, 1), fr(REQ_BODY, 1, b'c')]},
    {'name': 'a body sent to a WebSocket',
     'steps': [js(WS_OPEN, 1, meta('/terminal/x')), fr(REQ_BODY, 1, b'ab')]},
    {'name': 'an end sent to a WebSocket',
     'steps': [js(WS_OPEN, 1, meta('/terminal/x')), fr(REQ_END, 1)]},
    {'name': 'bodies larger than a Host holds at once',
     'steps': [js(REQ, 1, meta('/upload')), fr(REQ_BODY, 1, b'a' * 30), js(REQ, 2, meta('/upload')),
               fr(REQ_BODY, 2, b'b' * 10), fr(REQ_BODY, 2, b'c'), fr(REQ_BODY, 1, b'd'), fr(REQ_END, 1),
               js(REQ, 3, meta('/upload')), fr(REQ_BODY, 3, b'e' * 10), fr(REQ_BODY, 3, b'f'), done(1),
               js(REQ, 4, meta('/upload')), fr(REQ_BODY, 4, b'g' * 40), fr(REQ_BODY, 4, b'h'),
               js(REQ, 5, meta('/upload')), fr(REQ_BODY, 5, b'i' * 41)]},
    {'name': 'a request its asker gave up, and a WebSocket its phone closed',
     'steps': [js(REQ, 1, meta('/events')), fr(REQ_END, 1), fr(ABORT, 1), fr(REQ_END, 1),
               js(WS_OPEN, 2, meta('/terminal/x')), fr(WS_MSG, 2, b'\x01ls'), js(WS_CLOSE, 2, {'code': 1001, 'reason': 'gone'}),
               fr(WS_MSG, 2, b'\x01ls'), js(REQ, 3, meta()), js(WS_CLOSE, 3, {'code': 1000, 'reason': ''}),
               js(WS_OPEN, 4, meta('/terminal/x')), fr(ABORT, 4), fr(ABORT, 4)]},
    {'name': 'what a phone says on a WebSocket',
     'steps': [js(WS_OPEN, 1, meta('/terminal/x')), fr(WS_MSG, 1, b'\x01text'), fr(WS_MSG, 1, b'\x00\x01\x02\xff'),
               fr(WS_MSG, 1, b'\x01'), fr(WS_MSG, 1, b'\x00'), fr(WS_MSG, 1), fr(WS_MSG, 1, b'\x02odd'),
               fr(WS_MSG, 1, '01' + 'Zoë'.encode().hex())]},
    {'name': 'text on a WebSocket that is no text', 'steps': [js(WS_OPEN, 1, meta('/terminal/x')), fr(WS_MSG, 1, b'\x01\xff\xfe')]},
    {'name': 'what only a Host says, said to it',
     'steps': [js(REQ, 1, meta()), fr(RES_HEAD, 1, b'{}'), fr(RES_BODY, 1, b'x'), fr(RES_END, 1), fr(WS_ACCEPT, 1, b'{}'),
               fr(0x55, 1, b'x'), fr(0, 1), fr(REQ_END, 1)]},
    {'name': 'the largest and the smallest number a stream has',
     'steps': [js(REQ, 0, meta()), js(REQ, 4294967295, meta()), fr(REQ_END, 0), fr(ABORT, 4294967295)]},
]

# ---- which requests are local ------------------------------------------------------------------

PEERS = ['127.0.0.1', '127.5.5.5', '::1', '::ffff:127.0.0.1', '::ffff:7f00:1', '0:0:0:0:0:0:0:1', '10.0.0.5',
         '100.64.0.1', '192.168.1.5', '::ffff:10.0.0.5', 'fe80::1%eth0', '::1%lo', '127.0.0.1%lo', 'localhost', '',
         '0.0.0.0', '::', '127.0.0.256', '2130706433', '127.1', ' 127.0.0.1', '127.0.0.1:5']

LOCAL = [
    ('127.0.0.1', []), ('::1', []), ('10.0.0.5', []), ('::ffff:127.0.0.1', []), ('localhost', []), ('', []),
    ('127.0.0.1', [('Authorization', 'Bearer x'), ('User-Agent', 'curl/8'), ('Accept', '*/*')]),
    # Where a page that asks was loaded from.
    ('127.0.0.1', [('Origin', 'http://localhost:7682')]), ('127.0.0.1', [('Origin', 'https://127.0.0.1')]),
    ('127.0.0.1', [('Origin', 'http://[::1]:5')]), ('127.0.0.1', [('Origin', 'http://LOCALHOST')]),
    ('127.0.0.1', [('Origin', 'HTTP://localhost')]), ('127.0.0.1', [('Origin', 'http://user@localhost')]),
    ('127.0.0.1', [('Origin', 'http://localhost:notaport')]), ('127.0.0.1', [('Origin', 'http://localhost/path?q#f')]),
    ('127.0.0.1', [('Origin', 'http://[::ffff:127.0.0.1]')]), ('127.0.0.1', [('Origin', 'http://127.9.9.9:1')]),
    ('127.0.0.1', [('Origin', 'https://evil.example')]), ('127.0.0.1', [('Origin', 'null')]),
    ('127.0.0.1', [('Origin', 'file://')]), ('127.0.0.1', [('Origin', 'file:///home/x')]),
    ('127.0.0.1', [('Origin', 'http://127.0.0.1.evil.example')]), ('127.0.0.1', [('Origin', 'http://localhost.evil.example')]),
    ('127.0.0.1', [('Origin', 'chrome-extension://abcdef')]), ('127.0.0.1', [('Origin', 'capacitor://localhost')]),
    ('127.0.0.1', [('Origin', 'http://localhost.')]), ('127.0.0.1', [('Origin', 'http://2130706433')]),
    ('127.0.0.1', [('Origin', 'http://127.1')]), ('127.0.0.1', [('Origin', 'http://[::1')]),
    ('127.0.0.1', [('Origin', 'http://::1')]), ('127.0.0.1', [('Origin', 'localhost')]),
    ('127.0.0.1', [('Origin', 'http://')]), ('127.0.0.1', [('Origin', 'http:localhost')]),
    ('127.0.0.1', [('Origin', '//localhost')]), ('127.0.0.1', [('Origin', ' http://localhost')]),
    ('127.0.0.1', [('Origin', 'http://localhost ')]), ('127.0.0.1', [('Origin', 'http://10.0.0.5')]),
    ('127.0.0.1', [('Origin', 'http://[fe80::1%25eth0]')]), ('127.0.0.1', [('Origin', 'ws://localhost')]),
    ('127.0.0.1', [('Origin', 'https://localhost:443')]), ('127.0.0.1', [('Origin', 'http://localhost@evil.example')]),
    ('127.0.0.1', [('Origin', 'http://evil.example@localhost')]), ('127.0.0.1', [('Origin', 'http://localhost#@evil.example')]),
    ('127.0.0.1', [('Origin', 'http://evil.example#@localhost')]), ('127.0.0.1', [('Origin', 'http://localhost\\@evil.example')]),
    ('127.0.0.1', [('Origin', 'http://[::1]x')]), ('127.0.0.1', [('Origin', 'http://[127.0.0.1]')]),
    ('127.0.0.1', [('Origin', 'http://0x7f.0.0.1')]), ('127.0.0.1', [('Origin', 'http://127.0.0.01')]),
    ('127.0.0.1', [('origin', 'https://evil.example')]), ('127.0.0.1', [('ORIGIN', 'http://localhost')]),
    ('127.0.0.1', [('Origin', '')]), ('127.0.0.1', [('Origin', 'http://localhost'), ('Origin', 'https://evil.example')]),
    ('127.0.0.1', [('Origin', 'https://evil.example'), ('Origin', 'http://localhost')]),
    # How a browser says the asking page stands to this Host.
    ('127.0.0.1', [('Sec-Fetch-Site', 'cross-site')]), ('127.0.0.1', [('Sec-Fetch-Site', 'Cross-Site')]),
    ('127.0.0.1', [('sec-fetch-site', 'CROSS-SITE')]), ('127.0.0.1', [('Sec-Fetch-Site', 'same-origin')]),
    ('127.0.0.1', [('Sec-Fetch-Site', 'same-site')]), ('127.0.0.1', [('Sec-Fetch-Site', 'none')]),
    ('127.0.0.1', [('Sec-Fetch-Site', ' cross-site')]), ('127.0.0.1', [('Sec-Fetch-Site', '')]),
    ('127.0.0.1', [('Origin', 'http://localhost'), ('Sec-Fetch-Site', 'cross-site')]),
    ('127.0.0.1', [('Sec-Fetch-Site', 'same-origin'), ('Sec-Fetch-Site', 'cross-site')]),
    # The signs of a proxy, or of the relay.
    ('127.0.0.1', [('X-Forwarded-For', '203.0.113.7')]), ('127.0.0.1', [('x-forwarded-proto', 'https')]),
    ('127.0.0.1', [('X-FORWARDED-HOST', 'x')]), ('127.0.0.1', [('X-Forwarded-', '')]),
    ('127.0.0.1', [('X-Forwarded', 'for=1.2.3.4')]), ('127.0.0.1', [('Forwarded', 'for=1.2.3.4')]),
    ('127.0.0.1', [('forwarded', '')]), ('127.0.0.1', [('X-Real-IP', '1.2.3.4')]),
    ('127.0.0.1', [('X-Real-Ip', '')]), ('127.0.0.1', [('X-Clarp-Transport', 'relay')]),
    ('127.0.0.1', [('x-clarp-transport', '')]), ('127.0.0.1', [('X-Clarp-Client', 'ios 141')]),
    ('127.0.0.1', [('Tailscale-User-Login', 'owner@example.com')]), ('127.0.0.1', [('tailscale-headers-info', 'x')]),
    ('127.0.0.1', [('Tailscale', 'x')]), ('127.0.0.1', [('CF-Connecting-IP', '1.2.3.4')]),
    ('127.0.0.1', [('cf-ray', 'x')]), ('127.0.0.1', [('Cfg', 'x')]), ('127.0.0.1', [('CF', 'x')]),
    ('127.0.0.1', [('Via', '1.1 proxy')]), ('127.0.0.1', [('X-Forwardedness', 'x')]),
    ('127.0.0.1', [('Accept', '*/*'), ('X-Forwarded-For', '127.0.0.1')]),
    ('::1', [('X-Forwarded-For', '::1')]), ('10.0.0.5', [('Origin', 'http://localhost')]),
]

# Whose failure a failed authentication is.
SOURCES = [
    ('127.0.0.1', []), ('10.0.0.5', []), ('10.0.0.5', [('X-Forwarded-For', '203.0.113.7')]),
    ('127.0.0.1', [('X-Forwarded-For', '203.0.113.7')]), ('::1', [('X-Forwarded-For', '203.0.113.7')]),
    ('127.0.0.1', [('X-Forwarded-For', '198.51.100.1, 203.0.113.7')]),
    ('127.0.0.1', [('X-Forwarded-For', '203.0.113.7, 198.51.100.1 ')]),
    ('127.0.0.1', [('X-Forwarded-For', '203.0.113.7,')]), ('127.0.0.1', [('X-Forwarded-For', ',203.0.113.7')]),
    ('127.0.0.1', [('X-Forwarded-For', 'unknown')]), ('127.0.0.1', [('X-Forwarded-For', '')]),
    ('127.0.0.1', [('X-Forwarded-For', '2001:DB8::7')]), ('127.0.0.1', [('X-Forwarded-For', '::ffff:1.2.3.4')]),
    ('127.0.0.1', [('X-Forwarded-For', '203.0.113.7:4711')]), ('127.0.0.1', [('X-Forwarded-For', '[2001:db8::7]')]),
    ('127.0.0.1', [('x-forwarded-for', '203.0.113.8')]),
    ('127.0.0.1', [('X-Forwarded-For', '203.0.113.7'), ('X-Forwarded-For', '203.0.113.9')]),
    ('::ffff:127.0.0.1', [('X-Forwarded-For', '203.0.113.7')]), ('localhost', [('X-Forwarded-For', '203.0.113.7')]),
    ('127.0.0.1', [('X-Real-IP', '203.0.113.7')]), ('127.0.0.1', [('X-Forwarded-For', '010.0.0.1')]),
    ('127.0.0.1', [('X-Forwarded-For', 'fe80::1%eth0')]),
]

# When failures are answered with a wait: steps of (seconds gone by, whose failure).
LIMITS = [
    {'name': 'twenty in a minute, then a wait, then a new minute', 'limit': 20, 'window': 60.0, 'max_sources': 4096,
     'steps': [(0.0, 'a')] * 20 + [(0.5, 'a'), (0.0, 'a'), (10.0, 'a'), (0.0, 'b'), (48.4, 'a'), (0.6, 'a'), (0.4, 'a'),
                                   (0.2, 'a'), (0.0, 'a')] + [(0.0, 'a')] * 20 + [(59.9, 'a'), (0.2, 'a')]},
    {'name': 'a small budget, counted for each asker apart', 'limit': 2, 'window': 10.0, 'max_sources': 4096,
     'steps': [(0.0, 'a'), (1.0, 'b'), (1.0, 'a'), (1.0, 'a'), (1.0, 'b'), (1.0, 'b'), (4.5, 'a'), (0.4, 'a'), (0.2, 'a'),
               (0.0, 'b'), (2.0, 'b'), (0.0, 'b'), (0.0, 'b'), (9.99, 'b'), (0.01, 'b')]},
    {'name': 'more askers than are kept: who failed longest ago is forgotten', 'limit': 1, 'window': 100.0, 'max_sources': 3,
     'steps': [(0.0, 'a'), (1.0, 'b'), (1.0, 'c'), (1.0, 'a'), (1.0, 'd'), (1.0, 'b'), (1.0, 'a'), (1.0, 'c'), (1.0, 'e'),
               (1.0, 'd'), (1.0, 'a')]},
    {'name': 'no budget at all', 'limit': 0, 'window': 1.5, 'max_sources': 2,
     'steps': [(0.0, 'a'), (0.6, 'a'), (0.5, 'a'), (0.39, 'a'), (0.02, 'a')]},
]


def run_all() -> dict:
    """Runs in a process of its own: what the Python Host's connector and its rules of trust do."""
    import asyncio
    from http.client import HTTPMessage
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-connector-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import relay_connector as rc
    from lib import relay_settings, request_security

    def words(error):
        return f'{type(error).__name__}: {error}'

    frames = [{'kind': kind, 'sid': sid, 'payload': payload.encode('latin-1').hex(),
               'frame': rc.frame(kind, sid, payload.encode('latin-1')).hex()} for kind, sid, payload in FRAMES]
    json_frames = [{'kind': kind, 'sid': sid, 'said': said, 'frame': rc.jframe(kind, sid, said).hex()}
                   for kind, sid, said in JSON_FRAMES]

    forwarded = []
    for case in FORWARDED:
        row = {**case, 'headers': list(case['headers'].items())}
        try:
            handed = rc.forward_request_headers(case['headers'], '127.0.0.1:7690', case['body'],
                                                passthrough_encoding=case['passthrough'])
            row['handed'] = list(handed.items())
        except Exception as error:  # noqa: BLE001
            row['refused'] = words(error)
        forwarded.append(row)

    delays = [{**case, 'delay': rc.reconnect_delay(case['previous'], was_connected=case['was_connected'],
                                                   close=case['close'])} for case in DELAYS]

    settings = []
    for url, host_id, key in SETTINGS:
        row = {'url': url, 'host_id': host_id, 'key': key}
        try:
            taken = relay_settings.RelaySettings(url, host_id, key)
            row['taken'] = {'url': taken.url, 'pairing_url': taken.pairing_url, 'websocket_url': taken.websocket_url,
                            'status': taken.public_status(), 'shown': repr(taken)}
        except Exception as error:  # noqa: BLE001
            row['refused'] = words(error)
        settings.append(row)

    files = []
    for at, case in enumerate(FILES):
        path = root / f'relay-{at}.json'
        path.write_text(case['text'])
        path.chmod(case['mode'])
        row = dict(case)
        try:
            taken = relay_settings.load(path)
            row['taken'] = taken.public_status()
        except Exception as error:  # noqa: BLE001
            row['refused'] = words(error)
        files.append(row)
    missing = {'name': 'no file at all'}
    try:
        relay_settings.load(root / 'nothing-here.json')
    except Exception as error:  # noqa: BLE001
        missing['refused'] = type(error).__name__
    files.append(missing)

    rc.MAX_BODY, rc.MAX_STREAMS = MAX_BODY, MAX_STREAMS

    async def played(script):
        connector = rc.Connector('wss://relay.example', HOST_ID, KEY, 'http://127.0.0.1:1')
        connector._loop = asyncio.get_running_loop()
        connector.generation = 1
        finish = {}

        def stand_in(kind):
            async def serve(stream, _meta):
                finish[stream.sid] = asyncio.Event()
                try:
                    await finish[stream.sid].wait()
                finally:
                    connector._forget(stream)
            return serve

        opened = []

        def served(kind):
            inner = stand_in(kind)

            async def serve(stream, said):
                opened.append({'sid': stream.sid, 'kind': kind, 'meta': said})
                await inner(stream, said)
            return serve

        connector._serve_http = served('http')
        connector._serve_ws = served('ws')
        steps = []
        for step in script['steps']:
            row = dict(step)
            opened.clear()
            error = None
            if 'done' in step:
                if step['done'] in finish:
                    finish[step['done']].set()
            else:
                data = bytes.fromhex(step['raw']) if 'raw' in step else rc.frame(
                    step['frame'][0], step['frame'][1], bytes.fromhex(step['frame'][2]))
                row['data'] = data.hex()
                try:
                    await connector._dispatch(data)
                except Exception as failed:  # noqa: BLE001
                    error = words(failed)
            for _ in range(5):
                await asyncio.sleep(0)
            sent = []
            while not connector.out.empty():
                stream, data = connector.out.get_nowait()
                # What the connector's sender does with it.
                if stream.generation == connector.generation and not stream.cancelled.is_set():
                    sent.append(data.hex())
            streams = {}
            for sid, stream in sorted(connector.streams.items()):
                queued = []
                while not stream.websocket_messages.empty():
                    said = stream.websocket_messages.get_nowait()
                    queued.append({'text': said} if isinstance(said, str) else {'bytes': said.hex()})
                streams[str(sid)] = {'http': stream.is_http, 'ended': stream.ended.is_set(), 'body': bytes(stream.body).hex(),
                                     'cancelled': stream.cancelled.is_set(), 'heard': queued}
            row.pop('frame', None)
            row.pop('raw', None)
            row.update({'sent': sent, 'error': error, 'opened': list(opened), 'streams': streams,
                        'buffered': connector._buffered_body})
            steps.append(row)
            if error:
                # A frame the connector cannot take ends the session, and every stream with it.
                break
        await connector._teardown()
        return {'name': script['name'], 'steps': steps}

    scripts = [asyncio.run(played(script)) for script in SCRIPTS]

    def message(pairs):
        headers = HTTPMessage()
        for name, value in pairs:
            headers[name] = value
        return headers

    peers = [{'peer': peer, 'loopback': request_security.is_loopback(peer)} for peer in PEERS]
    local = [{'peer': peer, 'headers': [list(pair) for pair in pairs],
              'local': request_security.local_request(peer, message(pairs))} for peer, pairs in LOCAL]
    sources = [{'peer': peer, 'headers': [list(pair) for pair in pairs],
                'source': request_security.failure_source(peer, message(pairs))} for peer, pairs in SOURCES]

    limits = []
    for case in LIMITS:
        now = [1000.0]
        limiter = request_security.FailureLimiter(limit=case['limit'], window=case['window'],
                                                  max_sources=case['max_sources'], clock=lambda: now[0])
        steps = []
        for gone, source in case['steps']:
            now[0] += gone
            retry = limiter.failure(source)
            steps.append({'gone': gone, 'source': source, 'retry': retry, 'kept': list(limiter._sources.keys())})
        limits.append({**case, 'steps': steps})

    return {'limits_made_small': {'max_body': MAX_BODY, 'max_streams': MAX_STREAMS},
            'real_limits': {'chunk': rc.CHUNK, 'max_body': 64 * 1024 * 1024, 'max_streams': 64,
                            'hop': sorted(rc.HOP), 'reconnect': [rc.RECONNECT_FAST, rc.RECONNECT_MIN, rc.RECONNECT_MAX,
                                                                  rc.RECONNECT_REPLACED],
                            'stream_gone': rc.STREAM_GONE_CODE},
            'frames': frames, 'json_frames': json_frames, 'forwarded': forwarded, 'delays': delays,
            'settings': settings, 'files': files, 'scripts': scripts, 'peers': peers, 'local': local,
            'sources': sources, 'limits': limits}


def rows(name, items, last=False):
    out = [f' "{name}": [']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if at < len(items) - 1 else '') for at, row in enumerate(items))
    out.append(' ]' + ('' if last else ','))
    return out


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run_all()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{', ' "limits_made_small": ' + json.dumps(said['limits_made_small']) + ',',
           ' "real_limits": ' + json.dumps(said['real_limits']) + ',']
    for name in ('frames', 'json_frames', 'forwarded', 'delays', 'settings', 'files'):
        out.extend(rows(name, said[name]))
    out.append(' "scripts": [')
    for at, script in enumerate(said['scripts']):
        out.append('  {"name": ' + json.dumps(script['name']) + ', "steps": [')
        steps = script['steps']
        out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(steps) - 1 else '')
                   for index, row in enumerate(steps))
        out.append('  ]}' + (',' if at < len(said['scripts']) - 1 else ''))
    out.append(' ],')
    for name in ('peers', 'local', 'sources'):
        out.extend(rows(name, said[name]))
    out.append(' "limits": [')
    for at, case in enumerate(said['limits']):
        head = {key: value for key, value in case.items() if key != 'steps'}
        out.append('  {"case": ' + json.dumps(head) + ', "steps": [')
        steps = case['steps']
        out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(steps) - 1 else '')
                   for index, row in enumerate(steps))
        out.append('  ]}' + (',' if at < len(said['limits']) - 1 else ''))
    out.append(' ]')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    taken = sum(1 for row in said['settings'] if 'taken' in row)
    steps = sum(len(script['steps']) for script in said['scripts'])
    ended = sum(1 for script in said['scripts'] for row in script['steps'] if row['error'])
    local = sum(1 for row in said['local'] if row['local'])
    print(f'{OUT.relative_to(REPO)}: {len(said["frames"]) + len(said["json_frames"])} frames, '
          f'{len(said["forwarded"])} requests handed on, {len(said["delays"])} delays, '
          f'{len(said["settings"])} settings ({taken} taken), {len(said["files"])} files, '
          f'{len(said["scripts"])} scripts of {steps} frames ({ended} end the session), '
          f'{len(said["peers"])} peers, {len(said["local"])} requests ({local} local), '
          f'{len(said["sources"])} sources, {sum(len(case["steps"]) for case in said["limits"])} failures counted')
    return 0


if __name__ == '__main__':
    sys.exit(main())
