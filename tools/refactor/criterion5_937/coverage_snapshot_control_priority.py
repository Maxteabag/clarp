"""Actual _snapshot boundary LOWER lifecycle adapter controls, UNEXECUTED.

Controlled tool-module OS/hash bindings, no real descriptors/files/signals/API.
Original module bindings restored; isolated child/no concurrent helper users
is a mandatory future control setup condition. No product/global OS patch.
"""
import hashlib
import os
import stat
from types import SimpleNamespace

from . import coverage_copy_read as adapter


def run_snapshot_priority_controls(*, owner='snapshot'):
    if owner not in ('snapshot', 'report_source'):
        raise ValueError('fixed source fault-control owner required')
    if owner == 'report_source':
        from . import coverage_render as selected
    else:
        selected = adapter
    def contains(root, target):
        if root is target:
            return True
        return (root is not None and
                (contains(root.__cause__, target)
                 or isinstance(root, BaseExceptionGroup)
                 and any(contains(child, target) for child in root.exceptions)))

    outcomes = []
    for label, primary, cleanup, ordinary_expected in (
            ('control_primary', KeyboardInterrupt('snapshot read'), OSError('snapshot close'), False),
            ('control_cleanup', ValueError('snapshot read'), SystemExit(9), False),
            ('ordinary_pair', ValueError('snapshot read'), OSError('snapshot close'), True)):
        calls = {'open': 0, 'read': 0, 'close': 0, 'lstat': 0,
                 'sidecar': 0, 'hexdigest': 0, 'next_phase': 0}
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=123,
                               st_size=4, st_dev=5, st_ino=6, st_mtime_ns=7, st_ctime_ns=8)

        class PathFixture:
            def __str__(self):
                return '/fixture/attribution.json'

            def lstat(self):
                calls['lstat'] += 1
                return info

        def lexists(path):
            calls['sidecar'] += 1
            return False

        def opened(path, flags):
            calls['open'] += 1
            return 101  # opaque adapter identity, NEVER a real descriptor

        def read(fd, count):
            assert fd == 101 and count > 0
            calls['read'] += 1
            raise primary

        def close(fd):
            assert fd == 101
            calls['close'] += 1
            raise cleanup

        # Faithful old buffered-owner shape supports the prior helper, too.
        # Its __exit__ intentionally exposes Python context-manager selection:
        # ordinary close can replace a control raised by read (the C1 defect).
        class OldStream:
            def __enter__(self):
                return self

            def __exit__(self, *ignored):
                close(101)

            def fileno(self):
                return 101

            def read(self, count):
                return read(101, count)

        class Digest:
            def __init__(self):
                self.real = hashlib.sha256()

            def update(self, raw):
                self.real.update(raw)

            def hexdigest(self):
                calls['hexdigest'] += 1
                return self.real.hexdigest()

        fake_os = SimpleNamespace(path=SimpleNamespace(lexists=lexists), geteuid=lambda: 123,
                                  open=opened, read=read, close=close, fstat=lambda fd: info,
                                  fdopen=lambda fd, mode: OldStream(), O_RDONLY=os.O_RDONLY,
                                  O_CLOEXEC=os.O_CLOEXEC, O_NOFOLLOW=os.O_NOFOLLOW)
        original_os, original_hashlib = selected.os, selected.hashlib
        caught, ordinary = None, False
        try:
            selected.os = fake_os
            selected.hashlib = SimpleNamespace(sha256=Digest)
            try:
                try:
                    if owner == 'snapshot':
                        selected._snapshot(PathFixture())
                    else:
                        selected._source_version(PathFixture(), {'bytes': 4, 'sha256': 'a' * 64})
                    calls['next_phase'] += 1
                except Exception as error:
                    caught, ordinary = error, True
            except BaseException as error:
                caught = error
        finally:
            selected.os = original_os
            selected.hashlib = original_hashlib
        assert selected.os is original_os and selected.hashlib is original_hashlib
        assert ordinary is ordinary_expected
        assert contains(caught, primary) and contains(caught, cleanup)
        if label != 'control_cleanup':
            assert caught is primary
        else:
            assert isinstance(caught, BaseExceptionGroup) and not isinstance(caught, Exception)
        assert calls == {'open': 1, 'read': 1, 'close': 1, 'lstat': 1,
                         'sidecar': 3, 'hexdigest': 0, 'next_phase': 0}
        outcomes.append(label)
    return {'outcomes': outcomes,
            'scope': 'LOWER_ACTUAL_SNAPSHOT_FAULT_ADAPTER_NOT_REAL_FILESYSTEM_OR_SIGNAL_PROOF'}
