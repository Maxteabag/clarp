"""Finite isolated-interpreter clock controls, source only / NOT EXECUTED.

Protect the measured-vs-initial boundary and alias/thread/exception semantics.
No real product, schema, provider or server is imported by this control. Real
native compatibility, actual loader/cache provenance and seed process exit
remain separate mandatory runner evidence, not certified by synthetic modules.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest


def test_initial_only_clock_aliases_thread_scope_primary_exception_and_no_reuse(tmp_path):
    if socket.gethostname() == 'laptopstudio':
        pytest.fail('BUILDBOX_ONLY; no local control execution')
    script = tmp_path / 'control.py'
    helper = Path(__file__).with_name('initial_seed_clock.py').resolve()
    script.write_text(r'''
import hashlib, importlib, importlib.util, json
from pathlib import Path
import sys, threading

helper, root = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location('c5_initial_clock_control', helper)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lib = root / 'server/lib'
lib.mkdir(parents=True)
(lib / '__init__.py').write_text('')
(lib / 'clock.py').write_text('def now_ms():\n    return 123\n')
(lib / 'db.py').write_text('from .clock import now_ms\n')
(lib / 'transcript_incremental.py').write_text(
    'from . import db\nfrom .clock import now_ms\n'
    'def import_transcript(*, boom=False, entered=None, release=None):\n'
    '    if entered is not None: entered.set()\n'
    '    if release is not None and not release.wait(10): raise TimeoutError("finite hold")\n'
    '    if boom: raise KeyError("native primary")\n'
    '    return (db.now_ms(), now_ms())\n')
files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
         for name in module.TARGETS.values()}
args = dict(source=root, files=files, epoch_ms=1767225600000)
for mode in ('host', 'runtime', 'reference_after_ready', 'unknown'):
    try: module.InitialSeedClock(mode=mode, **args)
    except ValueError as error: assert 'forbidden outside INITIAL' in str(error)
    else: raise AssertionError('measured mode accepted')
bad = dict(files); bad['server/lib/clock.py'] = '0' * 64
try: module.InitialSeedClock(mode='initial_seed', **{**args, 'files': bad})
except ValueError as error: assert 'source mismatch' in str(error)
else: raise AssertionError('wrong source accepted')
sys.path.insert(0, str(root / 'server'))
with module.initial_fixture_clock(mode='initial_seed', **args) as hook:
    clock = importlib.import_module('lib.clock')
    importer = importlib.import_module('lib.transcript_incremental')
    db = importlib.import_module('lib.db')
    assert clock.now_ms() == db.now_ms() == 123
    assert importer.import_transcript() == (1767225600000, 1767225600000)
    try: importer.import_transcript(boom=True)
    except KeyError as error: assert error.args == ('native primary',)
    else: raise AssertionError('native exception swallowed')
    assert clock.now_ms() == db.now_ms() == 123
    entered, release = threading.Event(), threading.Event()
    values, errors = [], []
    def call():
        try: values.append(importer.import_transcript(entered=entered, release=release))
        except BaseException as error: errors.append(error)
    worker = threading.Thread(target=call)
    worker.start()
    try:
        assert entered.wait(4), 'control importer never entered'
        assert clock.now_ms() == db.now_ms() == 123, 'clock leaked into non-importing thread'
    finally:
        release.set()
        worker.join(4)
    assert not worker.is_alive() and errors == []
    assert values == [(1767225600000, 1767225600000)]
    receipt = hook.receipt()
    assert receipt['all_targets_observed'] and receipt['all_calls_terminal']
    assert [row['outcome'] for row in receipt['calls']] == ['RETURNED', 'ERROR', 'RETURNED']
    assert receipt['calls'][1]['error_type'] == 'KeyError'
    assert receipt['loaded']['lib.clock']['source_sha256'] == files['server/lib/clock.py']
    for row in receipt['calls']:
        assert row['native_code_path'] == str(lib / 'transcript_incremental.py')
        assert row['ended_mono'] >= row['started_mono']
assert hook not in sys.meta_path
for retired in (clock.now_ms, db.now_ms, importer.import_transcript):
    try: retired()
    except RuntimeError as error: assert 'seed process must exit' in str(error)
    else: raise AssertionError('retired initial alias silently reused')
# Aliases persist. A reused seed interpreter cannot certify measured clocks.
try: module.InitialSeedClock(mode='initial_reference', **args).install()
except RuntimeError as error: assert 'before ANY product import' in str(error)
else: raise AssertionError('contaminated interpreter reused')
print(json.dumps({'result':'synthetic initial-only scope/alias/thread/error/no-reuse controls passed',
                  'product_imported':False, 'native_seed_or_measured_process_certified':False}))
''')
    env = os.environ.copy()
    env.update(PYTHONPYCACHEPREFIX=str(tmp_path / 'unique-child-cache'), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-B', '-I', '-X', 'pycache_prefix=' + str(tmp_path / 'unique-child-cache'), str(script), str(helper), str(tmp_path / 'source')],
                            env=env, text=True, capture_output=True, timeout=25)
    (tmp_path / 'clock-control.stdout').write_text(result.stdout)
    (tmp_path / 'clock-control.stderr').write_text(result.stderr)
    (tmp_path / 'clock-control.exit').write_text(str(result.returncode) + '\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"native_seed_or_measured_process_certified": false' in result.stdout
