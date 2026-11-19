#!/usr/bin/env python3
"""HTTP dispatch decisions from the reference's request_body functions, without a server."""
import contextlib
import importlib.util
import io
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/body-limits-vectors.json'


def generate(directory):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    (directory / 'scenario-config.toml').write_text('')
    replay.isolate(directory)
    replay.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import log, request_body

    cases = []
    methods = ['POST', 'PUT', 'PATCH', 'DELETE', 'GET', 'HEAD', 'OPTIONS']
    paths = ['/nested/action', '/media', '/upload', '/transcribe', '/pairing/exchange',
             '/pairing/exchange/extra', '/']
    sizes = [0, 65535, 65536, 65537, 1048575, 1048576, 1048577, 2097151,
             2097152, 2097153, 16777215, 16777216, 16777217]
    for method, path, size in itertools.product(methods, paths, sizes):
        logged = []
        response = None

        class Handler:
            headers = {'Content-Length': str(size)}

            def _send(self, code, data, mime, extra_headers):
                nonlocal response
                response = [code, json.loads(data), mime, extra_headers]

        with patch.object(log, 'log', lambda kind, text: logged.append([kind, text])):
            refused = request_body.refused(Handler(), method, path)
        cases.append([method, path, size, request_body.limit(method, path),
                      request_body.family(path), refused, logged, response])
    return cases


if __name__ == '__main__':
    if '--isolated' in sys.argv:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            print(json.dumps(generate(Path(temp)), ensure_ascii=True))
    else:
        result = subprocess.check_output([sys.executable, '-I', __file__, '--isolated'],
                                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, text=True)
        cases = json.loads(result.splitlines()[-1])
        OUT.write_text('[\n' + ',\n'.join(json.dumps(c) for c in cases) + '\n]\n')
