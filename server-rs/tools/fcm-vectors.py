#!/usr/bin/env python3
"""Record Android data messages and fleet activity states with the reference serializers."""
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/fcm-vectors.json'


def record(root):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    return {**android_cases(), "deliveries": deliveries(), "rings": rings(), "budgets": budgets(), "lanes": lanes()}


def android_cases():
    """Outputs from real Python payload, truncation, collapse and chip functions."""
    from lib import fcm, live_activity_fcm as chip
    now = 1795010000000
    cases = []

    def take(payload, context=None, priority='10', collapse=''):
        data = fcm.data_payload(payload, context, collapse_id=collapse, sent_at_ms=now)
        body = fcm.message_body('android-device-of-test', data, priority=priority,
                                collapse_key=collapse) if data is not None else None
        # Describe repeated input compactly; compare the entire output by digest for
        # the long boundary cases, including every byte of the truncation suffix.
        repeats = {k: [v[0], len(v)] for k, v in payload.items()
                   if isinstance(v, str) and len(v) > 100 and v == v[0] * len(v)}
        if repeats:
            payload = {k: v for k, v in payload.items() if k not in repeats}
            data = None if data is None else hashlib.sha256(json.dumps(
                data, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            body = None
        cases.append(dict(repeats=repeats, payload=payload, context=context, collapse=collapse, priority=priority,
                          data=data, message=body))

    for value in (None, False, True, 0, -1, 1.0, 3.125, '', 'plain', [], [1, False],
                  {'b': 2, 'a': '"\\'}, chr(0x1f4f1)):
        take({'aps': {'alert': {'title': value, 'subtitle': value, 'body': value},
                      'interruption-level': value, 'sound': value, 'category': value, 'thread-id': value},
              'unknown': value, 'sent_at': value}, {'unknown': 'context', 'unread': value})
    for aps in (None, True, 1, ['wrong'], {'alert': 'wrong'}, {}):
        take({'aps': aps, 'google': 'x', 'gcm.x': 'x', 'from': 'x', 'notification': 'x',
              'message_type': 'x', 'collapse_key': 'x', 'Google': 'kept'},
             {'payload_version': 'old', 'sent_at': 'old', 'collapse_id': 'from-context'}, '5', 'id')
    for key in ('body', 'opening_line', 'subtitle', 'title', 'reason', 'notification_id'):
        for kind in ('agent-ring', 'user-notification'):
            for symbol in ('a', chr(0x1f4f1), '"', '\\'):
                take({'kind': kind, key: symbol * (fcm.MAX_DATA_BYTES + 1)})
    for length in range(fcm.MAX_DATA_BYTES - 90, fcm.MAX_DATA_BYTES + 1):
        take({'notification_id': 'x' * length})
    chips = []
    for working in (0, 1, None):
        for needs in (0, 1, None):
            state = {'working': working, 'needs_you': needs, 'lead': None, 'agents': []}
            for server in ('', 'test-server'):
                payload = chip.message(state, server_instance_id=server)
                chips.append(dict(state=state, server=server, payload=payload))
                take(payload, priority='5', collapse='agents-working')
    return dict(cases=cases, chips=chips)


def lanes():
    import itertools
    from lib import apns
    apns.log = lambda *_: None
    out = []
    for n in range(6):
        for decisions in itertools.product((False,True), repeat=n):
            for elapsed in (4999,5000,5001):
                lane = apns._AndroidLane()
                lane.busy = True
                lane.started = 1.0
                lane.queue.extend(apns._AndroidTicket(str(i), decision) for i,decision in enumerate(decisions))
                lane.prune('session', 1 + elapsed / 1000)
                out.append(dict(decisions=decisions, elapsed=elapsed, kept=[int(t.label) for t in lane.queue]))
    return out


def budgets():
    import contextlib
    from lib import db, live_activity_fcm as chip, settings_store
    saved = {}
    settings_store.get = saved.get
    settings_store.transaction = contextlib.nullcontext
    db.busy_timeout_at_most = lambda _: contextlib.nullcontext()
    settings_store.set_text = lambda key, text, **_: saved.update({key: text})
    rows = []
    def reserve(owner, at, update):
        allowed, wait = chip.reserve(owner, at, update=update, day_limit=300, burst=10, refill=288)
        rows.append(dict(owner=owner, at=at, update=update, allowed=allowed, wait=wait))
    # Exhaust a burst, retry on either side of a refill, and end despite exhaustion.
    for _ in range(11):
        reserve('a', 1000, True)
    for t in (999,1287,1288,1289):
        reserve('a', t, True)
    reserve('a',1290,False)
    reserve('b',1290,True)
    # Ends count towards the day but can still clear the notification when the day is spent.
    for _ in range(298):
        reserve('b',2000,False)
    reserve('b',2000,True)
    reserve('b',2001,True)
    reserve('b',2001,False)
    reserve('b',86399,True)
    reserve('b',86400,True)
    return rows


def rings():
    from lib import agent_rings, apns
    sender = agent_rings._LiveRingSender('reference')
    tried = []
    apns.send_agent_ring = lambda ring, kind, tokens: tried.append(tokens[0]['token'])
    agent_rings.log = lambda *_: None
    steps = []
    def step(ident, kind, now, expires, later, tokens):
        tried.clear()
        agent_rings.now_ms = lambda: now
        sender._send({'ring_id': ident, 'expires_at': expires}, kind,
                     [{'token': token} for token in tokens], later=later)
        steps.append(dict(id=ident, kind=kind, now=now, expires=expires, later=later,
                          tokens=list(tokens), tried=tried.copy()))
    for offset in (-1, 0, 1):
        ident = 'expiry' + str(offset)
        step(ident, 'ring', 100 + offset, 100, False, 'ab')
        step(ident, 'cancelled', 102, 100, False, 'abc')
    for offset in (-1, 0, 1):
        ident = 'forget' + str(offset)
        step(ident, 'ring', 100, 200, False, 'abc')
        step(ident, 'answered', 600200 + offset, 200, False, 'ad')
    step('later', 'ring', 100, 200, True, 'ab')
    step('later', 'answered', 101, 200, False, 'abc')
    step('x', 'ring', 100, 200, False, 'ab')
    step('y', 'ring', 101, 201, False, 'de')
    step('x', 'declined', 102, 200, True, 'ab')
    step('x', 'cancelled', 103, 200, False, 'bc')
    step('y', 'missed', 201, 201, False, 'ef')
    step('unknown', 'missed', 201, 201, False, 'abc')
    return steps


def deliveries():
    from types import SimpleNamespace
    from lib import fcm

    # The production sender decides all retries. The stand-in records every send
    # before returning its scripted result. Time and token minting are synthetic.
    sa = SimpleNamespace(project_id='test', client_email='test@example.invalid')
    cfg = SimpleNamespace(fcm_service_account_file=lambda: 'synthetic')
    fcm.load_service_account = lambda _: sa
    fcm.log = lambda *_: None
    fcm.report_auth_problem = lambda *_: None
    fcm._auth_recovered = lambda: None
    fcm.time.time = lambda: 1795010000
    responses = [(0, '', '', [], ''), (401, '', '', [], ''), (401, 'SENDER_ID_MISMATCH', '', [], ''),
                 (403, '', 'PERMISSION_DENIED', [], ''), (403, 'SENDER_ID_MISMATCH', '', [], ''),
                 (404, '', '', [], ''), (404, 'OTHER', '', [], ''), (400, 'UNREGISTERED', '', [], ''),
                 (400, 'INVALID_ARGUMENT', '', [], ''), (400, '', 'INVALID_ARGUMENT', ['message.token'], ''),
                 (400, 'INVALID_ARGUMENT', '', ['message.token', 'message.data'], ''),
                 (400, '', '', ['message.token'], ''), (409, '', 'INVALID_ARGUMENT', [], ''),
                 (499, '', '', [], ''), (500, '', '', [], '')]
    responses += [(400, code, '', [], '') for code in ('QUOTA_EXCEEDED', 'INTERNAL', 'UNAVAILABLE')]
    responses += [(429, '', '', [], wait) for wait in ('0', '1', '2', '9', '10', '11', '3.5', '-2', ' 6 ', chr(0x663))]
    results = []
    for response in responses:
        status, code, rpc, fields, retry = response
        for ends_well in (False, True):
            sent = []
            sleeps = []
            minted = []
            fcm._sleep = sleeps.append
            fcm.access_token = lambda *_: 'synthetic'
            fcm.reset_token_cache = lambda: minted.append(True)

            def post(url, headers, json):
                sent.append(url)
                if ends_well and len(sent) > 1:
                    return SimpleNamespace(status_code=200, json=lambda: {'name': 'taken'})
                if status == 0:
                    raise OSError('stand-in unavailable')
                details = [{'errorCode': code}, {'@type': 'google.rpc.BadRequest',
                            'fieldViolations': [{'field': field} for field in fields]}]
                return SimpleNamespace(status_code=status, headers={'retry-after': retry},
                                       json=lambda: {'error': {'status': rpc, 'details': details}})

            transport = fcm.Transport(cfg, client=SimpleNamespace(post=post))
            try:
                result = transport.send('synthetic-device', {'kind': 'user-notification'})
            except OSError:
                result = [0, 'network', '']
            results.append(dict(status=status, code=code, rpc=rpc, fields=fields, retry=retry,
                                ends_well=ends_well, result=result, sends=len(sent), sleeps=sleeps,
                                refreshed=len(minted)))
    return results


if __name__ == '__main__':
    if '--record' in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            print(json.dumps(record(Path(tmp))))
    else:
        raw = subprocess.check_output([sys.executable, '-I', __file__, '--record'],
                                      env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        OUT.write_text(json.dumps(json.loads(raw), ensure_ascii=True, indent=1) + '\n', encoding='ascii')
