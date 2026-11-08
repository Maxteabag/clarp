"""Remote-only single-child native SQLite control entry, UNEXECUTED SOURCE.

Primary owner: observation/transaction evidence, not product semantics. Cases
assert actual SQLite data and native API outcomes; no production seams or
servers. The child guard forbids returned provider processes and all endpoints.
The admitted owner still supplies absolute deadline/unknown-stop/owned cleanup.
"""
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest


def test_one_native_component_control_packet(tmp_path):
    if socket.gethostname() == 'laptopstudio':
        pytest.fail('BUILDBOX_ONLY; no local native component control permission')
    source = Path(__file__).resolve().parents[3]
    script = Path(__file__).with_name('transaction_controls.py')
    work = tmp_path / 'native-control'
    work.mkdir(mode=0o700)
    for name in ('home', 'config', 'cache', 'data', 'child-prefix'):
        (work / name).mkdir(mode=0o700)
    env = {'HOME': str(work / 'home'), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC',
           'CLARP_CONFIG_DIR': str(work / 'config'), 'CLARP_CACHE_DIR': str(work / 'cache'),
           'CLARP_DATA_DIR': str(work / 'data'), 'CLARP_SHARE_DIR': str(work / 'data'),
           'CLAUDE_PWA_DB': str(work / 'state.sqlite'), 'CLAUDE_PWA_CONFIG': str(work / 'config/config.toml'),
           'CLAUDE_PWA_ROOT': str(source), 'XDG_CONFIG_HOME': str(work / 'home/.config'),
           'XDG_CACHE_HOME': str(work / 'home/.cache'), 'XDG_DATA_HOME': str(work / 'home/.local/share'),
           'PYTHONPYCACHEPREFIX': str(work / 'child-prefix'), 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONNOUSERSITE': '1', 'PYTHONHASHSEED': '0'}
    argv = [sys.executable, '-B', '-I', '-X', 'pycache_prefix=' + str(work / 'child-prefix'),
            str(script), '--source', str(source), '--workspace', str(work)]
    result = subprocess.run(argv, env=env, cwd=work, capture_output=True, text=True, timeout=25)
    (work / 'stdout.log').write_text(result.stdout)
    (work / 'stderr.log').write_text(result.stderr)
    (work / 'actual.exit').write_text(str(result.returncode) + '\n')
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((work / 'transaction-control-report.json').read_bytes())
    assert report['native_class'] == 'lib.db._TrackedConnection'
    assert report['cases'] and report['clocks_unchanged'] is True
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary['native_component_cases'] == len(report['cases'])
    assert summary['product_or_measurement_green'] is False
