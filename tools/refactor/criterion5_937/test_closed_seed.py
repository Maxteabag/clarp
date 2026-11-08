"""Finite admitted-remote controls, source only / NOT EXECUTED.

Owner: physical copy/birth/reap evidence, distinct from logical SQLite snapshot
tests. Header fixtures below are byte-format inputs, NOT valid DB integrity or
native lifecycle proof. The only child is an owned standalone Python process.
"""
from dataclasses import asdict
from pathlib import Path
import selectors
import socket
import subprocess
import sys

import pytest

from .closed_seed import main_binding, physical_copy, require_root_reaped
from .proc_samples import bind


@pytest.fixture(autouse=True)
def admitted_buildbox_only():
    if socket.gethostname() == 'laptopstudio':
        pytest.fail('BUILDBOX_ONLY; no local finite control authorization')


def header_file(path):
    header = bytearray(4096)
    header[:16] = b'SQLite format 3\0'
    header[40:44] = (10).to_bytes(4, 'big')
    header[60:64] = (121).to_bytes(4, 'big')
    header[100:108] = b'fixture!'
    path.write_bytes(header)
    return path


def test_distinct_physical_copy_preserves_exact_bytes_and_versions_without_overwrite(tmp_path):
    source = header_file(tmp_path / 'source.sqlite')
    expected = main_binding(source)
    target = tmp_path / 'target.sqlite'
    result = physical_copy(source, target, expected_binding=expected)
    assert source.read_bytes() == target.read_bytes()
    assert expected['header_versions'] == {'schema_version': 10, 'user_version': 121}
    assert result['physical_copy']['sha256'] == expected['sha256']
    assert source.stat().st_ino != target.stat().st_ino
    assert main_binding(source) == expected
    with pytest.raises(ValueError, match='exclusive'):
        physical_copy(source, target, expected_binding=expected)


@pytest.mark.parametrize('fault', ['wal', 'hardlink', 'symlink', 'bad-header', 'changed-source'])
def test_ambiguous_or_changed_file_cannot_become_a_valid_copy(tmp_path, fault):
    source = header_file(tmp_path / 'source.sqlite')
    expected = main_binding(source)
    if fault == 'wal':
        Path(str(source) + '-wal').write_bytes(b'uncheckpointed')
    elif fault == 'hardlink':
        (tmp_path / 'alias.sqlite').hardlink_to(source)
    elif fault == 'symlink':
        alias = tmp_path / 'alias.sqlite'
        alias.symlink_to(source)
        source = alias
    elif fault == 'bad-header':
        source.write_bytes(b'unknown format' * 300)
    else:
        with source.open('r+b') as stream:
            stream.seek(100)
            stream.write(b'changed!')
    target = tmp_path / 'refused.sqlite'
    with pytest.raises(ValueError):
        physical_copy(source, target, expected_binding=expected)
    assert not target.exists()  # refusal occurred before creation, not deletion


@pytest.mark.parametrize('exitcode', [0, 3])
def test_actual_owned_child_must_exit_successfully_not_merely_publish_an_exit_flag(tmp_path, exitcode):
    code = 'import sys; print("READY",flush=True); sys.stdin.buffer.read(1); raise SystemExit(int(sys.argv[1]))'
    argv = [sys.executable, '-B', '-I', '-c', code, str(exitcode)]
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    identity = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(4), 'owned synthetic child never reached hold'
            assert child.stdout.readline() == b'READY\n'
        identity = asdict(bind(child.pid))
        with pytest.raises(ValueError, match='still running/unknown'):
            require_root_reaped(child, identity=identity, expected_argv=argv)
    finally:
        try:
            child.stdin.write(b'x')
            child.stdin.flush()
        except BrokenPipeError:
            pass
        child.stdin.close()
        try:
            child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            child.kill()  # exact owned standalone Popen, no descendants/provider
            child.wait(timeout=2)
            raise AssertionError('owned control failed to exit after release')
        finally:
            child.stdout.close()
            child.stderr.close()
    assert identity is not None
    if exitcode:
        with pytest.raises(ValueError, match='did not exit successfully'):
            require_root_reaped(child, identity=identity, expected_argv=argv)
    else:
        result = require_root_reaped(child, identity=identity, expected_argv=argv)
        assert result['actual_Popen_wait_exit'] == 0
        with pytest.raises(ValueError, match='argv mismatch'):
            require_root_reaped(child, identity=identity, expected_argv=argv + ['not-launched'])
