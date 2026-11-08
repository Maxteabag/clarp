"""Retained-reader fault/metadata table SOURCE, UNEXECUTED.

Invoked by the existing graph filesystem storage case; no new campaign/selector.
Read-only real preflight, then controlled tool-module read/close fault adapters:
NOT actual process signals, failing kernel close or producer authentication.
"""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

from . import coverage_retained_bytes as reader


def run_retained_reader_fault_table(observation):
    # Same real file established by the existing storage owner. No fabricated
    # successful receipt, new file creation or independent native API workload.
    info = Path(observation['path']).stat()
    limits = {'byte_limit': 200000, 'path_bytes_limit': 4096}
    for field in ('dev', 'ino', 'bytes', 'mtime_ns', 'ctime_ns'):
        bad = dict(observation); bad[field] = True
        try:
            reader.check_observation(bad, **limits)
        except ValueError:
            pass
        else:
            raise AssertionError('boolean physical identity accepted')

    def contains(root, target):
        return (root is target or root is not None and
                (contains(root.__cause__, target)
                 or isinstance(root, BaseExceptionGroup)
                 and any(contains(v, target) for v in root.exceptions)))

    labels = []
    for label, primary, cleanup, ordinary_expected in (
            ('control_read', KeyboardInterrupt('retained read'), OSError('retained close'), False),
            ('control_close', ValueError('retained read'), SystemExit(9), False),
            ('ordinary_pair', ValueError('retained read'), OSError('retained close'), True)):
        calls = {'open': 0, 'read': 0, 'close': 0, 'root': 0,
                 'hexdigest': 0, 'next_input_or_decode': 0}
        original_os, original_hashlib, original_root = reader.os, reader.hashlib, reader._root

        def opened(path, flags):
            calls['open'] += 1
            return 101  # opaque adapter value, NEVER a real descriptor

        def read(fd, count):
            assert fd == 101 and count > 0
            calls['read'] += 1
            raise primary

        def close(fd):
            assert fd == 101
            calls['close'] += 1
            raise cleanup

        def root(path):
            calls['root'] += 1
            return original_root(path)  # actual read-only physical-root preflight

        class Digest:
            def __init__(self):
                self.real = hashlib.sha256()

            def update(self, raw):
                self.real.update(raw)

            def hexdigest(self):
                calls['hexdigest'] += 1
                return self.real.hexdigest()

        fake_os = SimpleNamespace(path=os.path, open=opened, read=read, close=close, fstat=lambda fd: info,
                                  O_RDONLY=os.O_RDONLY, O_CLOEXEC=os.O_CLOEXEC,
                                  O_NOFOLLOW=os.O_NOFOLLOW)
        caught, ordinary = None, False
        try:
            # Only owned tool-module aliases change, not the global OS or
            # shared snapshot module. Isolated/no-concurrent-reader setup owed.
            reader.os = fake_os
            reader.hashlib = SimpleNamespace(sha256=Digest)
            reader._root = root
            try:
                try:
                    reader.read_retained_bytes(observation, **limits)
                    calls['next_input_or_decode'] += 1
                except Exception as error:
                    caught, ordinary = error, True
            except BaseException as error:
                caught = error
        finally:
            reader.os = original_os
            reader.hashlib = original_hashlib
            reader._root = original_root
        assert reader.os is original_os and reader.hashlib is original_hashlib
        assert reader._root is original_root
        assert ordinary is ordinary_expected
        assert contains(caught, primary) and contains(caught, cleanup)
        if label != 'control_close':
            assert caught is primary
        else:
            assert isinstance(caught, BaseExceptionGroup) and not isinstance(caught, Exception)
        assert calls == {'open': 1, 'read': 1, 'close': 1, 'root': 1,
                         'hexdigest': 0, 'next_input_or_decode': 0}
        labels.append(label)
    return {'scope': 'LOWER_ACTUAL_READER_FAULT_ADAPTER_NOT_REAL_SIGNAL_OR_PRODUCER_PROOF',
            'fault_outcomes': labels, 'boolean_observation_refusals': 5}
