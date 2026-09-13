#!/usr/bin/env python3
"""Print a disposable paired-device token for a loopback-published test container.

Callers capture stdout; this helper never reads the Host administrator token.
Pairing is performed through the container's published HTTP endpoint, so smoke
and browser tests exercise the same exchange boundary as a real paired client.
"""
import json
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit


def main():
    container, base = sys.argv[1:]
    url = urlsplit(base)
    if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1', 'localhost'}
            or url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}):
        raise SystemExit('test pairing requires a loopback-published container URL')
    code = subprocess.check_output([
        'docker', 'exec', container, 'python3', '-c',
        'from lib.device_pairing import issue; print(issue(device_name="Disposable CI client", scope="full")["code"])',
    ], text=True).strip()
    request = urllib.request.Request(base.rstrip('/') + '/pairing/exchange',
                                     data=json.dumps({'code': code}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        payload = json.load(response)
    token = payload['device']['token']
    if not isinstance(token, str) or not token.startswith('cld_'):
        raise SystemExit('pairing did not return a device credential')
    print(token)


if __name__ == '__main__':
    main()
