"""Finite public-API STORAGE attribution control SOURCE, UNEXECUTED.

Explicit arcs are API input, not measured Python/pytest execution. Outer owner
supplies qualified pinned CoverageData and one private fresh physical root.
"""
import os
from pathlib import Path
import shutil
import socket

from .coverage_sets import capture_sets


def run_set_storage_control(*, root, coverage_data_class):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; source only until exact admission')
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root
            or not root.is_dir() or any(root.iterdir())):
        raise ValueError('fresh physical owned control root required')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private owned control root required')
    original, copy = root / 'source.data', root / 'copy.data'
    producer = coverage_data_class(basename=str(original))
    try:
        producer.set_context('context[a].*')
        producer.add_arcs({'server/positive.py': {(-1, 1), (1, 2), (2, -1)},
                           'server/negative.py': {(-2, -2)}})
        producer.set_context('contextaXYZ')
        producer.add_arcs({'server/positive.py': {(-1, 1), (1, 3), (3, -1)}})
        producer.write()
    finally:
        producer.close()
    original_bytes = original.read_bytes()
    shutil.copyfile(original, copy)
    data = coverage_data_class(basename=str(copy))
    try:
        data.read()
        # Deliberately leave a regex query active: reader must reset it before
        # inventory and after capture, and use exact context equality within.
        data.set_query_contexts(['^contextaXYZ$'])
        snapshot = capture_sets(data, limits={'files': 2, 'contexts': 2, 'pairs': 4,
                                             'items': 100, 'name_bytes': 128,
                                             'total_name_bytes': 1024})
        positive = snapshot['files']['server/positive.py']
        assert positive['contexts']['context[a].*'] == ((-1, 1), (1, 2), (2, -1))
        assert positive['contexts']['contextaXYZ'] == ((-1, 1), (1, 3), (3, -1))
        negative = snapshot['files']['server/negative.py']
        assert negative['exhaustive_context_query'] is True
        assert negative['contexts'] == {'context[a].*': ((-2, -2),), 'contextaXYZ': ()}
        assert negative['lines'] == () and negative['values'] == ((-2, -2),)
        assert set(data.arcs('server/positive.py')) == {(-1, 1), (1, 2), (2, -1), (1, 3), (3, -1)}
        assert snapshot['accessor_cost_counts']['pairs'] == 4
        assert snapshot['valid_empty_or_source_producer_custody_accepted'] is False
    finally:
        data.close()
    assert original.read_bytes() == original_bytes
    assert copy.read_bytes() == original_bytes
    return {'result': 'EXPECTED_PUBLIC_API_STORAGE_ATTRIBUTION',
            'scope': 'explicit stored arcs only; no tracing/pytest/input-custody/union aggregation proof'}
