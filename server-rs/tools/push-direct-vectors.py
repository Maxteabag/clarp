#!/usr/bin/env python3
"""Capture alert route attempts and grant revocations at the reference transports."""
import importlib.util
import itertools
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/push-direct-vectors.json'


def record(root):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import apns, config
    from types import SimpleNamespace

    grant = 'pg1.' + 'a' * 64 + '.' + 'b' * 16 + '.' + 'c' * 64
    paths = []
    bind_status = 200
    cleared = []
    outcomes = {}
    apns.log = lambda *_: None
    apns._clear_grant = cleared.append

    def request(method, url, *, headers, json):
        if url.endswith('/devices'):
            return SimpleNamespace(status_code=bind_status)
        path = 'grant' if headers['authorization'].endswith(grant) else 'account'
        paths.append(path)
        return SimpleNamespace(status_code=outcomes[path], json=lambda: {'results': [{'status': 200}]})

    apns._relay_client = lambda: SimpleNamespace(request=request)

    class Direct:
        name = 'direct'

        def __init__(self, cfg):
            pass

        def send(self, *args, **kwargs):
            paths.append('direct')
            return 200, '', ''

    apns._DirectTransport = Direct
    test_key = root / "direct.p8"
    test_key.write_text("stand-in key; signing is captured by Direct")
    rows = []
    for mode, relay, account, key in itertools.product(
            ('auto', 'direct', 'relay', 'off', 'other'), (False, True), (False, True), (False, True)):
        cfg = config.Config(apns_mode=mode, audio_central_url='https://127.0.0.1' if relay else '',
                            audio_central_credential='cav1.x.y.z' if account else '',
                            apns_key_path=str(test_key) if key else '', apns_key_id='TEST', apns_team_id='TEST')
        for grant_status, account_status in ((200, 200), (401, 200), (503, 200), (403, 503), (401, 503)):
            outcomes.update(grant=grant_status, account=account_status)
            router = apns._Router(cfg)
            steps = []
            for granted in (True, False, True, False):
                paths.clear()
                cleared.clear()
                try:
                    status, reason, _ = router.send('device', 'production', {}, grant=grant if granted else '')
                except apns._RelayUnavailable:
                    status, reason = 0, 'down'
                steps.append([granted, paths.copy(), bool(cleared), status, reason])
            rows.append(dict(mode=mode, relay=relay, account=account, key=key, enabled=bool(cfg.apns_transport()),
                             grant_status=grant_status, account_status=account_status, steps=steps))
    activities = []
    for mode in ('auto', 'direct', 'off', 'relay'):
        for account, key in itertools.product((False, True), repeat=2):
            cfg = config.Config(apns_mode=mode, audio_central_url='https://127.0.0.1',
                                audio_central_credential='cav1.x.y.z' if account else '',
                                apns_key_path=str(test_key) if key else '', apns_key_id='TEST', apns_team_id='TEST')
            for bind_status, send_status in ((200,200),(200,403),(200,404),(200,405),(200,503),(503,200),(401,200)):
                apns._RELAY_REGISTERED.clear()
                outcomes['account'] = send_status
                router = apns._Router(cfg)
                steps = []
                for token in ('chip-a','chip-b','chip-c'):
                    paths.clear()
                    try:
                        status, reason, _ = router.send(token, 'production', {}, push_type='liveactivity')
                    except apns._RelayUnavailable:
                        status, reason = 0, 'down'
                    steps.append([paths.copy(),status,reason])
                activities.append(dict(mode=mode, account=account, key=key, bind=bind_status, send=send_status, steps=steps))
    return rows, activities


if __name__ == '__main__':
    if '--record' in sys.argv:
        with tool_scratch.directory() as tmp:
            print(json.dumps(record(Path(tmp))))
    else:
        output = subprocess.check_output([sys.executable, '-I', __file__, '--record'],
                                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}))
        rows, activities = json.loads(output)
        activity_file = OUT.with_name("direct-activity-vectors.json")
        activity_file.write_text("[\n" + ",\n".join(json.dumps(row) for row in activities) + "\n]\n", encoding="ascii")
        OUT.write_text('[\n' + ',\n'.join(json.dumps(row) for row in rows) + '\n]\n', encoding='ascii')
