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
    return {**android_cases(), "deliveries": deliveries()}


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
