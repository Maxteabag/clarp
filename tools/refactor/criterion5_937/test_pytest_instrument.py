"""Finite REAL-pytest instrumentation controls, authored only, NOT EXECUTED.

Runs only inside a qualified remote setup, synthetic project/no Clarp imports.
These consume the campaign budget. Do not run on laptopstudio.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

PLUGIN = Path(__file__).parent / 'pytest_plugin'
PROFILE = Path(__file__).parent / 'coverage.ini'


def prepare(tmp_path):
    if socket.gethostname() == 'laptopstudio':
        pytest.fail('BUILDBOX_ONLY; control execution was not authorized here')
    root = tmp_path / 'project'
    (root / 'tests').mkdir(parents=True)
    (root / 'server').mkdir()
    (root / 'pytest.ini').write_text('[pytest]\ntestpaths=tests\n')
    (root / 'server/__init__.py').write_text('')
    (root / 'server/subject.py').write_text(
        'def reached():\n    return 7\n\ndef intentionally_unreached():\n    return 19\n')
    (root / 'tests/test_sample.py').write_text(
        'from server.subject import reached\n'
        'def test_covered():\n    assert reached() == 7\n'
        'def test_subtests(subtests):\n'
        '    for i in range(2):\n'
        '        with subtests.test(i=i):\n            assert i < 2\n'
        'def test_real_failure():\n    assert False, "intended control failure"\n')
    return root


def run(tmp_path, root, name, mode, *, wanted=None):
    out = tmp_path / name
    out.mkdir()
    cache = tmp_path / (name + '-cache')
    cache.mkdir()
    coverage_root = None
    if mode == 'coverage':
        import coverage
        coverage_root = Path(coverage.__file__).resolve().parent
    spec = {'arm': 'B', 'pin': '0073254687d22ac5a6c1d01b8af29fa678dcf4a2',
            'run_id': 'synthetic-instrument-control-' + name, 'mode': mode,
            'source_root': str(root), 'output': str(out), 'cache_prefix': str(cache),
            'expected_workers': 0 if mode == 'collect' else 8,
            'cohort': None, 'cohort_sha256': None,
            'coverage_version': '7.17.0' if mode == 'coverage' else None,
            'coverage_package_root': str(coverage_root) if coverage_root else None,
            'coverage_config': str(PROFILE) if mode == 'coverage' else None,
            'coverage_config_sha256': hashlib.sha256(PROFILE.read_bytes()).hexdigest() if mode == 'coverage' else None}
    if wanted is not None:
        cohort = out / 'cohort.json'
        raw = (json.dumps(sorted(wanted), separators=(',', ':')) + '\n').encode()
        cohort.write_bytes(raw)
        spec.update(cohort=str(cohort), cohort_sha256=hashlib.sha256(raw).hexdigest())
    (out / 'owner.json').write_text(json.dumps({k: spec[k] for k in ('run_id', 'pin', 'arm')}))
    config = out / 'config.json'
    config.write_text(json.dumps(spec))
    env = os.environ.copy()
    for key in ('COVERAGE_PROCESS_START', 'COVERAGE_FILE', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        env.pop(key, None)  # synthetic controls only, NOT the product measurement environment
    env.update(PYTHONPYCACHEPREFIX=str(cache), PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=str(PLUGIN) + os.pathsep + str(root))
    if coverage_root:
        env['PYTHONPATH'] += os.pathsep + str(coverage_root.parent)
    argv = [sys.executable, '-B', '-m', 'pytest', '--rootdir', str(root),
            '-n', str(spec['expected_workers']), '-q', '-p', 'no:cacheprovider',
            '-p', 'c5_937', '--max-worker-restart=0', '--c5-config', str(config)]
    if mode == 'collect':
        argv.append('--collect-only')
    result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=25)
    (out / 'stdout.log').write_text(result.stdout)
    (out / 'stderr.log').write_text(result.stderr)
    (out / 'actual.exit').write_text(str(result.returncode) + '\n')
    return result, out


def test_real_roster_and_eight_workers_separate_nested_subtests(tmp_path):
    root = prepare(tmp_path)
    result, out = run(tmp_path, root, 'collection', 'collect')
    assert result.returncode == 0, result.stdout + result.stderr
    collection = json.loads((out / 'controller.finish.json').read_text())
    assert len(collection['nodes_selected']) == 3
    assert collection['primary_pytest_exit'] == 0
    wanted = ['tests/test_sample.py::test_covered', 'tests/test_sample.py::test_subtests']
    result, out = run(tmp_path, root, 'timing', 'timing', wanted=wanted)
    assert result.returncode == 0, result.stdout + result.stderr
    final = json.loads((out / 'controller.finish.json').read_text())
    assert set(final['worker_collections']) == {'gw' + str(i) for i in range(8)}
    assert all(nodes == sorted(wanted) for nodes in final['worker_collections'].values())
    assert final['nested_subtests'] == 2
    assert sum(row['when'] == 'call' for row in final['events']) == 2
    assert final['errors'] == [] and final['qualified_exit'] == 0


def test_genuine_pytest_failure_is_not_converted_to_metadata_success(tmp_path):
    root = prepare(tmp_path)
    result, out = run(tmp_path, root, 'failure', 'timing',
                      wanted=['tests/test_sample.py::test_real_failure'])
    assert result.returncode == 1, result.stdout + result.stderr
    final = json.loads((out / 'controller.finish.json').read_text())
    assert final['primary_pytest_exit'] == final['qualified_exit'] == 1
    assert any(row['when'] == 'call' and row['outcome'] == 'failed' for row in final['events'])


def test_missing_cohort_refuses_before_any_test_execution(tmp_path):
    root = prepare(tmp_path)
    result, out = run(tmp_path, root, 'missing-cohort', 'timing',
                      wanted=['tests/test_sample.py::test_not_in_actual_collection'])
    assert result.returncode != 0
    assert '937 cohort missing' in result.stdout + result.stderr
    if (out / 'controller.finish.json').exists():
        final = json.loads((out / 'controller.finish.json').read_text())
        assert final['events'] == []


def test_owned_worker_coverage_records_execution_not_unreached_line_and_keeps_failure(tmp_path):
    # Tool version/artifact availability must be qualified before this control;
    # missing coverage is a failure, NOT a skip or a passing negative.
    import coverage
    assert coverage.__version__ == '7.17.0'
    root = prepare(tmp_path)
    result, out = run(tmp_path, root, 'coverage', 'coverage')
    assert result.returncode == 1, result.stdout + result.stderr  # intentional failed test remains failed
    final = json.loads((out / 'controller.finish.json').read_text())
    assert final['primary_pytest_exit'] == final['qualified_exit'] == 1
    assert final['errors'] == []
    files = [json.loads((out / (worker + '.finish.json')).read_text())['coverage_data']
             for worker in ['controller', *('gw' + str(i) for i in range(8))]]
    executed = set()
    for row in files:
        path = out / row['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']
        data = coverage.CoverageData(basename=str(path))
        data.read()
        for filename in data.measured_files():
            if filename.endswith('server/subject.py'):
                executed.update(data.lines(filename))
    assert 2 in executed  # real reached function body
    assert 5 not in executed  # independently named intentionally-unreached body


def add_publication_faults(root):
    with (root / 'tests/test_sample.py').open('a') as stream:
        stream.write(
            "def _break_finish_publication():\n"
            "    import c5_937\n"
            "    original = c5_937.publish\n"
            "    def fail(path, value):\n"
            "        if path.name.endswith('.finish.json'):\n"
            "            raise OSError('intended finite publication fault')\n"
            "        return original(path, value)\n"
            "    c5_937.publish = fail\n"
            "def test_lost_success_receipt():\n"
            "    _break_finish_publication()\n"
            "    assert reached() == 7\n"
            "def test_lost_failed_receipt():\n"
            "    _break_finish_publication()\n"
            "    assert False, 'intended assertion still fails'\n")


@pytest.mark.parametrize('failed', [False, True])
def test_publication_loss_cannot_green_success_or_replace_primary_failure(tmp_path, failed):
    root = prepare(tmp_path)
    add_publication_faults(root)
    name = 'test_lost_failed_receipt' if failed else 'test_lost_success_receipt'
    result, out = run(tmp_path, root, 'publication-loss', 'timing',
                      wanted=['tests/test_sample.py::' + name])
    assert result.returncode != 0, result.stdout + result.stderr
    assert '937_RECEIPT_PUBLICATION_FAILED' in result.stdout + result.stderr
    final = json.loads((out / 'controller.finish.json').read_text())
    assert final['errors'] and final['qualified_exit'] != 0
    if failed:
        assert result.returncode == final['primary_pytest_exit'] == final['qualified_exit'] == 1
        assert any(row['when'] == 'call' and row['outcome'] == 'failed' for row in final['events'])
    else:
        assert any(row['when'] == 'call' and row['outcome'] == 'passed' for row in final['events'])
