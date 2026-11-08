"""Nine API-storage sources + SYNTHETIC run receipts SOURCE, UNEXECUTED.

Not nine pytest processes or source/kernel/custody authentication. Stored arcs
are deliberately supplied inputs, not traced Python statements or performance.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys

from .coverage_union_graph import (build_union_with_graph, SOURCE_CATALOG_SHA256,
                                   COVERAGE_ARCHIVE_SHA256)
from .coverage_graph_publish import verify_graph
from .coverage_copy_read import _same_capture
from .coverage_copy_read import _retain, capture_closed_copy
from .suite_evidence import ROLES
from .test_suite_evidence import NODES, receipts

LIMITS = {'files': 4, 'contexts': 4, 'pairs': 64, 'items': 1024,
          'name_bytes': 128, 'total_name_bytes': 8192}


def run_build_union_storage_control(*, root, coverage_data_class):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local control execution authority')
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root
            or not root.is_dir() or any(root.iterdir())):
        raise ValueError('fresh physical owned control root required')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private owned control root required')
    original_root, copy_root, aggregate_root, verify_root, graph_root = [
        root / n for n in ('original', 'copy', 'aggregate', 'verify', 'graph')]
    for path in (original_root, copy_root, aggregate_root, verify_root, graph_root):
        path.mkdir(mode=0o700)
    raw, starts, finishes = receipts('coverage')  # supplied LOWER consumer records
    spec = json.loads(raw)
    spec['output'] = str(original_root)
    raw = json.dumps(spec).encode()
    config_sha = hashlib.sha256(raw).hexdigest()
    for container in (starts, finishes):
        for row in container.values():
            row['spec'] = deepcopy(spec)
            row['config_sha256'] = config_sha
            row['coverage']['config_sha256'] = config_sha
    captures, source_bytes = {}, {}
    for i, role in enumerate(ROLES):
        filename = 'coverage-pid-' + str(finishes[role]['pid']) + '.data'
        original = original_root / filename
        producer = coverage_data_class(basename=str(original))
        primary = None
        try:
            producer.set_context('1' if i % 2 == 0 else 'context[a].*')
            producer.add_arcs({'server/shared.py': {(-1, 1), (1, i + 2), (i + 2, -1)}})
            if i == 0:
                producer.add_arcs({'server/negative.py': {(-10, -10)}})
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
        content = original.read_bytes()
        source_bytes[role] = content
        digest = hashlib.sha256(content).hexdigest()
        finishes[role]['coverage_data'].update(file=filename, sha256=digest, bytes=len(content))
        shutil.copyfile(original, copy_root / filename)
        captures[role] = capture_closed_copy(
            original_root=original_root, copy_root=copy_root, filename=filename,
            expected_sha256=digest, expected_bytes=len(content),
            coverage_data_class=coverage_data_class, limits=LIMITS)
    original_profile = sys.getprofile()
    if original_profile is not None:
        raise ValueError('storage control requires isolated unprofiled interpreter')
    calls, primary, result = [], None, None
    update_code = coverage_data_class.update.__code__

    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is update_code:
            # Primitive path only; no retained data/frame/native object. No
            # class method/SQL/clock patch and no product measurement profiling.
            calls.append(frame.f_locals['other_data'].data_filename())

    try:
        sys.setprofile(profile)
        result = build_union_with_graph(raw, starts, finishes, actual_exit=0, outer_seconds=10.0,
                             cohort=NODES, captures=captures, limits=LIMITS,
                             aggregate_root=aggregate_root, verify_root=verify_root,
                             coverage_data_class=coverage_data_class, graph_root=graph_root,
                             wire_limits={'nodes': 100000, 'depth': 64,
                                          'string_characters': 100000, 'wire_bytes': 2000000},
                             bindings={'source_pin': '0073254687d22ac5a6c1d01b8af29fa678dcf4a2',
                                       'source_catalog_sha256': SOURCE_CATALOG_SHA256,
                                       'config_sha256': config_sha,
                                       'coverage_archive_sha256': COVERAGE_ARCHIVE_SHA256,
                                       'run_receipts_sha256': 'c' * 64})
    except BaseException as error:
        primary = error
    finally:
        try:
            sys.setprofile(original_profile)
        except BaseException as cleanup:
            primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary
    assert calls == [str(copy_root / finishes[role]['coverage_data']['file']) for role in ROLES]
    assert [edge['role'] for edge in result['edges']] == list(ROLES)
    actual = result['closed_aggregate_capture']['accessor_sets']
    shared = actual['files']['server/shared.py']
    expected_arcs = {(-1, 1)} | {(1, i + 2) for i in range(9)} | {(i + 2, -1) for i in range(9)}
    assert set(shared['values']) == expected_arcs
    assert set(shared['contexts']) == {'1', 'context[a].*'}
    assert actual['files']['server/negative.py']['values'] == ((-10, -10),)
    assert actual['files']['server/negative.py']['contexts']['context[a].*'] == ()
    assert result['actual_run_source_producer_schema_denominator_accepted'] is False
    assert result['source_config_tool_run_binding_bytes_authenticated'] is False
    # Source/tool are frozen digest declarations; run hash is SYNTHETIC.
    # No declaration above authenticates actual retained/loaded inputs. Actual graph bytes/typed sets are checked here.
    published = result['published_graph']
    reopened = verify_graph(
        root=graph_root, observation=published['observation'],
        expected_graph=published['decoded_graph'],
        limits={'nodes': 100000, 'depth': 64, 'string_characters': 100000,
                'wire_bytes': 2000000})
    graph = reopened['decoded_graph']
    assert set(graph['role_captures']) == set(ROLES)
    for role in ROLES:
        assert _same_capture(graph['role_captures'][role], captures[role])
    assert _same_capture(graph['reference_union'], result['reference_union'])
    assert _same_capture(graph['closed_aggregate_capture'], result['closed_aggregate_capture'])
    assert graph['closed_aggregate_capture']['accessor_sets']['files'][
        'server/negative.py']['contexts']['context[a].*'] == []
    assert graph['bindings']['config_sha256'] == config_sha
    for role in ROLES:
        name = finishes[role]['coverage_data']['file']
        assert (original_root / name).read_bytes() == source_bytes[role]
        assert (copy_root / name).read_bytes() == source_bytes[role]
    return {'result': 'EXPECTED_NINE_API_STORAGE_UNION',
            'scope': 'SYNTHETIC run metadata+stored API arcs only; not actual pytest/process/measurement proof'}
