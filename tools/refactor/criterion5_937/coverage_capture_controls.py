"""Composed public-API STORAGE control SOURCE, UNEXECUTED.

Actual pinned API produces files, but explicit supplied lines/arcs are NOT
traced Python or pytest participation. No actual process/custody proof follows.
"""
import hashlib
import os
from pathlib import Path
import shutil
import socket

from .coverage_copy_read import _retain, capture_closed_copy


def run_capture_storage_controls(*, root, coverage_data_class):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; source-only until exact admission')
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root
            or not root.is_dir() or any(root.iterdir())):
        raise ValueError('fresh physical owned composition-control root required')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private owned control root required')
    outcomes = []
    for case in ('branch_store', 'empty_branch_store', 'empty_line_store'):
        case_root = root / case
        case_root.mkdir(mode=0o700)
        original_root, copy_root = case_root / 'original', case_root / 'copy'
        original_root.mkdir(mode=0o700)
        copy_root.mkdir(mode=0o700)
        original, copy = original_root / 'coverage.data', copy_root / 'coverage.data'
        producer = coverage_data_class(basename=str(original))
        primary = None
        try:
            if case == 'empty_line_store':
                producer.add_lines({})
            else:
                producer.add_arcs({'server/sentinel.py': {(-1, 1), (1, -1)}}
                                  if case == 'branch_store' else {})
            producer.write()
        except BaseException as failure:
            primary = failure
        finally:
            try:
                producer.close()
            except BaseException as cleanup:
                primary = _retain(primary, cleanup)
        if primary is not None:
            raise primary  # required close attempted; no optional raw/copy/next case
        raw = original.read_bytes()
        shutil.copyfile(original, copy)
        instances = []

        def actual_class(**kwargs):
            instances.append(kwargs['basename'])
            return coverage_data_class(**kwargs)

        result = capture_closed_copy(
            original_root=original_root, copy_root=copy_root, filename='coverage.data',
            expected_sha256=hashlib.sha256(raw).hexdigest(), expected_bytes=len(raw),
            coverage_data_class=actual_class,
            limits={'files': 1, 'contexts': 1, 'pairs': 1, 'items': 20,
                    'name_bytes': 128, 'total_name_bytes': 1024})
        assert instances == [str(copy)], 'composition reopened original or created another input object'
        assert result['qualification'] == 'SAME_VERSION_COPY_API_CAPTURE_ONLY_NOT_INPUT_ACCEPTANCE'
        assert result['source_producer_schema_valid_empty_or_union_accepted'] is False
        assert result['read_close_receipt']['copy_bytes_unchanged'] is True
        assert result['read_close_receipt']['original_bytes_unchanged'] is True
        captured = result['accessor_sets']
        if case == 'branch_store':
            assert captured['mode'] == 'branch'
            row = captured['files']['server/sentinel.py']
            assert row['values'] == ((-1, 1), (1, -1))
            assert row['contexts'] == {'': ((-1, 1), (1, -1))}
            assert row['lines'] == (1,)
        else:
            assert captured['files'] == {} and captured['contexts'] == []
            assert captured['mode'] == ('branch' if case == 'empty_branch_store' else 'UNRESOLVED_EMPTY_MODE')
        assert original.read_bytes() == raw and copy.read_bytes() == raw
        outcomes.append({'case': case, 'result': 'EXPECTED_SAME_VERSION_CAPTURE',
                         'tier': 'COMPOSED_PINNED_API_STORAGE_NOT_TRACED_PYTEST'})
    return {'cases': outcomes, 'scope': 'private-copy capture storage tier only; no coverage/input acceptance'}
