"""C1 control-priority SOURCE: LOWER adapter fault tier, UNEXECUTED.

Calls the actual read adapter with real file preflight and a controlled failing
API-shaped object. This is NOT pinned CoverageData/read/SQLite/close proof.
No genuine process signal is sent; exceptions are known finite injected inputs.
"""
import hashlib
import os
from pathlib import Path
import socket

from . import coverage_copy_read as adapter


def run_copy_control_priority(*, root):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; source only until exact admission')
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root
            or not root.is_dir() or any(root.iterdir())):
        raise ValueError('fresh physical owned priority-control root required')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private owned control root required')
    outcomes = []
    for case in ('ordinary_read_control_close', 'control_read_ordinary_close', 'both_ordinary'):
        case_root = root / case
        case_root.mkdir(mode=0o700)
        original_root, copy_root = case_root / 'original', case_root / 'copy'
        original_root.mkdir(mode=0o700)
        copy_root.mkdir(mode=0o700)
        raw = b'opaque adapter-fault input; NOT a passing coverage database'
        (original_root / 'input.data').write_bytes(raw)
        (copy_root / 'input.data').write_bytes(raw)
        read_error = (KeyboardInterrupt('controlled read cancellation') if case == 'control_read_ordinary_close'
                      else ValueError('controlled ordinary read error'))
        close_error = (SystemExit(11) if case == 'ordinary_read_control_close'
                       else OSError('controlled close failure'))
        prior_cause = LookupError('original read cause')
        read_error.__cause__ = prior_cause
        events, constructors, ordinary_catches, optional_steps = [], [], [], []
        original_snapshot = adapter._snapshot

        def counted_snapshot(path):
            events.append('snapshot')
            return original_snapshot(path)

        class FailingAPIAdapter:
            def __init__(self, *, basename):
                constructors.append(basename)

            def read(self):
                events.append('read')
                raise read_error

            def close(self):
                events.append('close')
                raise close_error

        selected = None
        adapter._snapshot = counted_snapshot  # TOOL alias only, no native/API patch
        try:
            try:
                try:
                    adapter.read_closed_copy(
                        original_root=original_root, copy_root=copy_root, filename='input.data',
                        expected_sha256=hashlib.sha256(raw).hexdigest(), expected_bytes=len(raw),
                        coverage_data_class=FailingAPIAdapter)
                except Exception as ordinary:
                    ordinary_catches.append(ordinary)
                    optional_steps.append('ordinary-caller-continuation')
            except BaseException as control_bearing:
                selected = control_bearing
        finally:
            adapter._snapshot = original_snapshot
        assert constructors == [str(copy_root / 'input.data')], case + ': wrong API object/copy'
        assert events == ['snapshot'] * 4 + ['read', 'close'], case + ': retry or optional posthash'
        if case == 'ordinary_read_control_close':
            assert type(selected) is BaseExceptionGroup, 'ordinary read hid cleanup cancellation'
            assert selected.exceptions == (read_error, close_error), 'failure/control objects changed'
            assert read_error.__cause__ is prior_cause, 'read cause chain changed'
            assert ordinary_catches == optional_steps == [], 'ordinary caller swallowed control'
        elif case == 'control_read_ordinary_close':
            assert selected is read_error, 'original process-control identity changed'
            assert isinstance(selected.__cause__, BaseExceptionGroup)
            assert selected.__cause__.exceptions == (prior_cause, close_error)
            assert ordinary_catches == optional_steps == [], 'ordinary caller swallowed original control'
        else:
            assert selected is None and ordinary_catches == [read_error]
            assert optional_steps == ['ordinary-caller-continuation']
            assert isinstance(read_error.__cause__, BaseExceptionGroup)
            assert read_error.__cause__.exceptions == (prior_cause, close_error)
        outcomes.append({'case': case, 'result': 'EXPECTED_ADAPTER_ERROR_CONTROL_EXPORT',
                         'tier': 'LOWER_TOOLING_FAULT_ADAPTER_NOT_ACTUAL_API_CLOSE'})
    return {'cases': outcomes, 'scope': 'C1 propagation sensitivity only; no actual signal/SQLite lifecycle proof'}
