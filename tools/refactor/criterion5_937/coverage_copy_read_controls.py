"""Pinned-API storage controls SOURCE, UNEXECUTED; not dynamic tracing proof.

Actual CoverageData writers create positives, including add_arcs({}) for empty
branch data. Recorded sentinel arcs are explicit storage inputs, NOT evidence
that Python or pytest executed those lines. No passing database is hand-built.
"""
import hashlib
import os
from pathlib import Path
import shutil
import socket
import sqlite3

from .coverage_copy_read import read_closed_copy


def run_copy_read_storage_controls(*, root, coverage_data_class):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; source-only until exact admission')
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root
            or not root.is_dir() or any(root.iterdir())):
        raise ValueError('fresh exact physical owned root required')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private owned control root required')
    results = []
    for case in ('branch_store', 'valid_empty_store', 'missing_copy', 'zero_bytes',
                 'schemaless_sqlite', 'corrupt_bytes', 'hardlink_alias'):
        case_root = root / case
        case_root.mkdir(mode=0o700)
        original_root, copy_root = case_root / 'original', case_root / 'copy'
        original_root.mkdir(mode=0o700)
        copy_root.mkdir(mode=0o700)
        original, copy = original_root / 'coverage.data', copy_root / 'coverage.data'
        if case == 'zero_bytes':
            original.write_bytes(b'')
        elif case == 'corrupt_bytes':
            original.write_bytes(b'deliberately not a SQLite coverage file')
        elif case == 'schemaless_sqlite':
            # A real unrelated SQLite database, only a NEGATIVE input. No
            # coverage_schema is fabricated and no SQL decoder is under test.
            connection = sqlite3.connect(original)
            try:
                connection.execute('PRAGMA user_version=17')
                connection.commit()
            finally:
                connection.close()
        else:
            producer = coverage_data_class(basename=str(original))
            try:
                producer.add_arcs({} if case == 'valid_empty_store' else
                                  {'server/sentinel.py': {(-1, 1), (1, -1)}})
                producer.write()
            finally:
                producer.close()
        raw = original.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if case == 'hardlink_alias':
            os.link(original, copy)
        elif case != 'missing_copy':
            shutil.copyfile(original, copy)
        constructs = []

        def actual_class(**kwargs):
            constructs.append(kwargs)
            return coverage_data_class(**kwargs)

        selected, value = None, None
        try:
            value = read_closed_copy(original_root=original_root, copy_root=copy_root,
                                     filename='coverage.data', expected_sha256=digest,
                                     expected_bytes=len(raw), coverage_data_class=actual_class)
        except Exception as failure:
            selected = failure  # process controls escape; no optional evidence on them
        if case in ('branch_store', 'valid_empty_store'):
            assert selected is None, case + ': actual pinned API copy read failed'
            assert value['public_read_returned'] and value['public_close_returned']
            assert value['copy_bytes_unchanged'] and value['original_bytes_unchanged']
            assert value['schema_valid_empty_union_or_producer_accepted'] is False
            assert len(constructs) == 1
            assert copy.read_bytes() == raw
        elif case == 'schemaless_sqlite':
            assert type(selected) is ValueError and 'mutated/reinitialized' in str(selected)
            assert len(constructs) == 1 and value is None
            assert copy.read_bytes() != raw, 'initialization sensitivity was not exercised'
        elif case == 'corrupt_bytes':
            assert type(selected).__name__ == 'DataError' and value is None
            assert len(constructs) == 1
        else:
            expected = FileNotFoundError if case == 'missing_copy' else ValueError
            assert type(selected) is expected and value is None
            assert constructs == [], 'preflight refusal unexpectedly invoked library'
        assert original.read_bytes() == raw, case + ': original bytes changed'
        results.append({'case': case, 'result': 'EXPECTED_STORAGE_BOUNDARY_OUTCOME',
                        'tier': 'PINNED_API_STORAGE_ONLY_NOT_TRACED_OR_PYTEST_PRODUCER',
                        'original_sha256': digest})
    return {'cases': results, 'scope': 'strict copy-read storage sensitivity only; no aggregation/coverage/run proof'}
