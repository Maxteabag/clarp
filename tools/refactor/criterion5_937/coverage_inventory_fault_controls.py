"""Actual bounded inventory LOWER enumeration/close faults SOURCE, UNEXECUTED.

Real private fixture entry/iterator; tool-module enumeration/close exceptions.
Not genuine uncertain kernel iterator cleanup or process signal/drain proof.
"""
import os
from types import SimpleNamespace

from . import coverage_originals as owner
from .coverage_copy_read import _root


def run_inventory_fault_controls(*, root):
    root = _root(root)
    if any(root.iterdir()):
        raise ValueError('fresh EMPTY inventory-fault fixture root required')
    fixture = root / 'input.fixture'
    raw = b'SYNTHETIC_INVENTORY_STORAGE_ONLY\n'
    with fixture.open('xb') as stream:
        stream.write(raw)

    def contains(error, target):
        return (error is target or error is not None and
                (contains(error.__cause__, target) or isinstance(error, BaseExceptionGroup)
                 and any(contains(child, target) for child in error.exceptions)))

    rows = (('control_enumeration', KeyboardInterrupt('injected enumeration'), OSError('injected close'), False),
            ('control_close', ValueError('injected enumeration'), SystemExit(17), False),
            ('ordinary_pair', ValueError('injected enumeration'), OSError('injected close'), True))
    outcomes = []
    for label, primary, cleanup, ordinary_expected in rows:
        calls = {'scandir': 0, 'next': 0, 'close': 0, 'next_input': 0}
        original_os = owner.os

        class ControlledIterator:
            def __init__(self, actual):
                self.actual = actual

            def __iter__(self):
                return self

            def __next__(self):
                calls['next'] += 1
                if calls['next'] == 1:
                    return next(self.actual)  # actual entry stat/allowlist check occurs
                raise primary  # complete expected prefix is still NOT successful enumeration

            def close(self):
                calls['close'] += 1
                self.actual.close()
                raise cleanup  # injected AFTER real controlled iterator cleanup

        def scandir(path):
            assert path == root
            calls['scandir'] += 1
            return ControlledIterator(original_os.scandir(path))

        caught, ordinary = None, False
        try:
            owner.os = SimpleNamespace(scandir=scandir, geteuid=original_os.geteuid)
            try:
                try:
                    owner._inventory(root, {'input.fixture'}, entry_limit=2, metadata_bytes=4096)
                    calls['next_input'] += 1
                except Exception as error:
                    caught, ordinary = error, True
            except BaseException as error:
                caught = error
        finally:
            owner.os = original_os
        assert owner.os is original_os
        assert calls == {'scandir': 1, 'next': 2, 'close': 1, 'next_input': 0}
        assert ordinary is ordinary_expected
        assert contains(caught, primary) and contains(caught, cleanup)
        if label != 'control_close':
            assert caught is primary
        else:
            assert isinstance(caught, BaseExceptionGroup) and not isinstance(caught, Exception)
        assert fixture.read_bytes() == raw
        assert set(p.name for p in root.iterdir()) == {'input.fixture'}
        outcomes.append(label)
    return {'scope': 'LOWER_ACTUAL_INVENTORY_INJECTED_ENUMERATION_CLOSE_NOT_KERNEL_PROOF',
            'outcomes': outcomes, 'actual_process_or_producer_accepted': False}
