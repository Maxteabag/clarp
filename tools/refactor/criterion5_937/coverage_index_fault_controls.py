"""Actual early-index owner LOWER injected-IO fault table SOURCE, UNEXECUTED.

Real private fixture roots/known FDs, tool-module write/fsync/close exceptions.
Not real kernel failing-close/signals, native process or producer/drain proof.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

from . import coverage_publish_index as owner
from .coverage_copy_read import _root


def run_index_publication_fault_controls(*, root, index, index_limits, json_limits):
    root = _root(root)
    if any(root.iterdir()):
        raise ValueError('fresh EMPTY publication-fault fixture root required')
    raw = (json.dumps(index, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')) + '\n').encode('ascii')

    def contains(error, target):
        return (error is target or error is not None and
                (contains(error.__cause__, target) or isinstance(error, BaseExceptionGroup)
                 and any(contains(child, target) for child in error.exceptions)))

    rows = (
        ('write_control', 'write', KeyboardInterrupt('injected write'), OSError('injected file close'), None),
        ('write_ordinary_close_control', 'write', ValueError('injected write'), SystemExit(9), None),
        ('file_fsync_control', 'file_fsync', SystemExit(11), OSError('injected file close'), None),
        ('directory_fsync_close_control', 'directory_fsync', OSError('injected directory fsync'),
         KeyboardInterrupt('injected file close'), None),
        ('short_write', 'short_write', None, None, None),
        ('file_close_control', 'none', None, KeyboardInterrupt('injected file close'), None),
        ('both_close', 'none', None, OSError('injected file close'), SystemExit(13)),
    )
    outcomes = []
    for label, failure_stage, primary, file_cleanup, directory_cleanup in rows:
        case_root = root / label
        case_root.mkdir(mode=0o700)
        handles, close_calls, writes, syncs = {}, [], [], []
        optional = {'observe': 0, 'identity': 0, 'next_phase': 0}
        real_os, original_observe, original_identity = owner.os, owner.observe_file, owner._identity

        def opened(path, flags, *args, **kwargs):
            fd = real_os.open(path, flags, *args, **kwargs)
            kind = 'directory' if flags & real_os.O_DIRECTORY else 'file'
            assert kind not in handles
            handles[kind] = fd
            return fd

        def write(fd, chunk):
            assert fd == handles['file']
            writes.append(fd)
            if failure_stage == 'write':
                raise primary
            if failure_stage == 'short_write':
                assert len(chunk) > 1
                return real_os.write(fd, chunk[:len(chunk) // 2])
            return real_os.write(fd, chunk)

        def sync(fd):
            kind = 'file' if fd == handles['file'] else 'directory'
            assert fd == handles[kind]
            syncs.append(kind)
            if failure_stage == kind + '_fsync':
                raise primary
            return real_os.fsync(fd)

        def close(fd):
            kind = 'file' if fd == handles['file'] else 'directory'
            assert fd == handles[kind]
            close_calls.append(kind)
            # The injected exception happens AFTER actual controlled fixture
            # cleanup. It exercises selection, not a real uncertain kernel close.
            real_os.close(fd)
            failure = file_cleanup if kind == 'file' else directory_cleanup
            if failure is not None:
                raise failure

        def forbidden_observe(*args, **kwargs):
            optional['observe'] += 1
            raise AssertionError('pending publication error reached optional verification')

        def identity(path):
            optional['identity'] += 1
            return original_identity(path)

        aliases = SimpleNamespace(
            open=opened, fstat=real_os.fstat, write=write, fsync=sync, close=close,
            geteuid=real_os.geteuid, O_RDONLY=real_os.O_RDONLY, O_DIRECTORY=real_os.O_DIRECTORY,
            O_CLOEXEC=real_os.O_CLOEXEC, O_NOFOLLOW=real_os.O_NOFOLLOW,
            O_WRONLY=real_os.O_WRONLY, O_CREAT=real_os.O_CREAT, O_EXCL=real_os.O_EXCL)
        caught, ordinary = None, False
        try:
            owner.os, owner.observe_file, owner._identity = aliases, forbidden_observe, identity
            try:
                try:
                    owner._publish_index(root=case_root, index=index, index_limits=index_limits,
                                         json_limits=json_limits, path_bytes_limit=4096)
                    optional['next_phase'] += 1
                except Exception as error:
                    caught, ordinary = error, True
            except BaseException as error:
                caught = error
        finally:
            owner.os, owner.observe_file, owner._identity = real_os, original_observe, original_identity
        assert owner.os is real_os and owner.observe_file is original_observe
        assert owner._identity is original_identity
        assert set(handles) == {'file', 'directory'}
        assert close_calls == ['file', 'directory']  # independent after first close fault
        assert optional == {'observe': 0, 'identity': 1, 'next_phase': 0}
        assert writes
        if failure_stage in ('write', 'short_write'):
            assert len(writes) == 1 and syncs == []
        elif failure_stage == 'file_fsync':
            assert syncs == ['file']
        else:
            assert syncs == ['file', 'directory']
        for expected in (primary, file_cleanup, directory_cleanup):
            if expected is not None:
                assert contains(caught, expected)
        if failure_stage == 'short_write':
            assert ordinary and isinstance(caught, OSError) and 'short index write' in str(caught)
        else:
            assert not ordinary  # at least one non-Exception control in every other row
            if primary is not None and not isinstance(primary, Exception):
                assert caught is primary
        preserved = case_root / 'index.json'
        assert preserved.is_file()
        actual = preserved.read_bytes()
        if failure_stage == 'write':
            assert actual == b''
        elif failure_stage == 'short_write':
            assert actual and len(actual) < len(raw) and raw.startswith(actual)
        else:
            assert actual == raw
        assert set(p.name for p in case_root.iterdir()) == {'index.json'}
        outcomes.append(label)
    return {'scope': 'LOWER_ACTUAL_OWNER_INJECTED_IO_NOT_KERNEL_OR_SIGNAL_PROOF',
            'outcomes': outcomes, 'actual_process_or_producer_accepted': False}
