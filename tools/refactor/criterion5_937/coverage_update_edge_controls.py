"""Single update-edge actual-API STORAGE controls SOURCE, UNEXECUTED.

Supplied arcs are storage inputs, not traced Python/pytest/process evidence.
Actual nine-role update/custody/producer/report qualification is not exercised.
"""
import hashlib
import os
from pathlib import Path
import shutil
import socket

from .coverage_copy_read import _retain, _snapshot, capture_closed_copy, update_closed_copy

LIMITS = {'files': 4, 'contexts': 4, 'pairs': 8, 'items': 100,
          'name_bytes': 128, 'total_name_bytes': 2048}


def run_update_edge_storage_controls(*, root, coverage_data_class):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no control execution authority')
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root
            or not root.is_dir() or any(root.iterdir())):
        raise ValueError('fresh physical owned control root required')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private owned control root required')
    results = []
    for case in ('branch_update', 'changed_target', 'slash_spelling'):
        home = root / case
        home.mkdir(mode=0o700)
        original_root, copy_root, aggregate_root, verify_root = [home / s for s in ('original', 'copy', 'aggregate', 'verify')]
        for path in (original_root, copy_root, aggregate_root, verify_root):
            path.mkdir(mode=0o700)
        original, copy = original_root / 'coverage.data', copy_root / 'coverage.data'
        producer = coverage_data_class(basename=str(original))
        primary = None
        try:
            producer.set_context('context[a].*')
            file = 'server/a\\b.py' if case == 'slash_spelling' else 'server/sentinel.py'
            producer.add_arcs({file: {(-1, 1), (1, -1)}})
            producer.write()
        except BaseException as error:
            primary = error
        finally:
            try:
                producer.close()
            except BaseException as cleanup:
                primary = _retain(primary, cleanup)
        if primary is not None:
            raise primary
        raw = original.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        shutil.copyfile(original, copy)
        captured = capture_closed_copy(original_root=original_root, copy_root=copy_root,
                                       filename='coverage.data', expected_sha256=digest,
                                       expected_bytes=len(raw), coverage_data_class=coverage_data_class,
                                       limits=LIMITS)
        destination = aggregate_root / 'coverage-aggregate.data'
        aggregate = coverage_data_class(basename=str(destination))
        before, result, primary, cleanup_error = None, None, None, None
        try:
            if case == 'changed_target':
                aggregate.add_arcs({'server/retained.py': {(-2, 2), (2, -2)}})
                aggregate.write()
                before = _snapshot(destination)
                with destination.open('ab') as stream:
                    stream.write(b'controlled target mutation')
            result = update_closed_copy(original_root=original_root, copy_root=copy_root,
                                        filename='coverage.data', expected_sha256=digest,
                                        expected_bytes=len(raw), coverage_data_class=coverage_data_class,
                                        limits=LIMITS, expected_sets=captured['accessor_sets'],
                                        aggregate=aggregate, aggregate_root=aggregate_root,
                                        aggregate_before=before)
        except BaseException as error:
            primary = error
        finally:
            try:
                aggregate.close()
            except BaseException as cleanup:
                cleanup_error = cleanup
                primary = _retain(primary, cleanup)
        if cleanup_error is not None or (primary is not None and not isinstance(primary, Exception)):
            raise primary  # no optional assertions/copies/next case after control/unknown cleanup
        if case == 'branch_update':
            assert primary is None and result['public_update_returned'] is True
            assert result['aggregate_close_validated'] is False, 'edge claimed outer target close'
            target_raw = destination.read_bytes()
            shutil.copyfile(destination, verify_root / destination.name)
            final = capture_closed_copy(original_root=aggregate_root, copy_root=verify_root,
                                        filename=destination.name,
                                        expected_sha256=hashlib.sha256(target_raw).hexdigest(),
                                        expected_bytes=len(target_raw), coverage_data_class=coverage_data_class,
                                        limits=LIMITS)
            row = final['accessor_sets']['files']['server/sentinel.py']
            assert row['values'] == ((-1, 1), (1, -1))
            assert row['contexts'] == {'context[a].*': ((-1, 1), (1, -1))}
        elif case == 'changed_target':
            assert type(primary) is ValueError and 'target version changed' in str(primary)
            assert result is None
        else:
            assert type(primary) is ValueError and 'normalize slash spelling' in str(primary)
            assert result is None and not destination.exists(), 'refused spelling still mutated target'
        assert original.read_bytes() == raw and copy.read_bytes() == raw
        results.append({'case': case, 'result': 'EXPECTED_SINGLE_UPDATE_EDGE_STORAGE_OUTCOME',
                        'scope': 'actual API storage only; not traced or nine-input/producer acceptance'})
    return {'cases': results, 'scope': 'single update edge and refusals only; no complete aggregation/report proof'}
