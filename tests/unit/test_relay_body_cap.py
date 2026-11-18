"""The relay answers a body over the Host's cap with the Host's 413 itself (issue 360, R81).

The Host refuses such a body without reading it, and the relay's http.client sends a body whole
before reading the answer: forwarded, it would end in a broken pipe and a 502."""
from __future__ import annotations

import asyncio
import json
import struct

import pytest

from lib.relay_connector import REQ, REQ_BODY, REQ_END, RES_BODY, RES_END, RES_HEAD, Connector, frame


JSON_MOST = 16 * 1024 * 1024


def _relayed(method, path, size):
    """(the frames the phone was answered with, whether the Host was asked)."""
    asked = []

    async def scenario():
        connector = Connector('ws://127.0.0.1:1', 'host-test', 'test', 'http://127.0.0.1:2')
        connector._loop = asyncio.get_running_loop()
        connector._do_http = lambda stream, meta, body: asked.append(len(body))
        await connector._dispatch(frame(REQ, 5, json.dumps(
            {'method': method, 'path': path, 'headers': {}}).encode()))
        for start in range(0, size, 4 * 1024 * 1024):
            await connector._dispatch(frame(REQ_BODY, 5, b'x' * min(4 * 1024 * 1024, size - start)))
        await connector._dispatch(frame(REQ_END, 5))
        task = connector.streams[5].task if 5 in connector.streams else None
        if task:
            await asyncio.wait_for(asyncio.shield(task), 5)
        answered = []
        while not connector.out.empty():
            data = connector.out.get_nowait()[1]
            kind, sid = struct.unpack('!BI', data[:5])
            answered.append((kind, data[5:]))
        return answered
    return asyncio.run(scenario()), asked


def test_a_relayed_json_body_over_the_cap_is_answered_413_and_never_forwarded():
    answered, asked = _relayed('POST', '/artifacts?x=1', JSON_MOST + 1)
    assert asked == []
    assert [kind for kind, _ in answered] == [RES_HEAD, RES_BODY, RES_END]
    assert json.loads(answered[0][1])['status'] == 413
    assert json.loads(answered[1][1]) == {'error': 'request body too large'}


@pytest.mark.parametrize('method, path, size', [
    ('POST', '/artifacts', JSON_MOST),
    ('POST', '/upload?session=a', JSON_MOST + 1),
    ('POST', '/pairing/exchange', 64 * 1024),
])
def test_a_relayed_body_within_its_cap_is_forwarded_whole(method, path, size):
    answered, asked = _relayed(method, path, size)
    assert asked == [size]
    assert all(kind != RES_HEAD or json.loads(payload)['status'] != 413 for kind, payload in answered)


def test_a_relayed_pairing_one_byte_over_64_kib_is_answered_413():
    answered, asked = _relayed('POST', '/pairing/exchange', 64 * 1024 + 1)
    assert asked == [] and json.loads(answered[0][1])['status'] == 413
