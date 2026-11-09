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

from .coverage_bound_union import build_bound_union_graph
from . import coverage_bound_union as bound_adapter
from .coverage_build_union import _exclusive_copy
from .coverage_graph_publish import verify_graph
from .coverage_copy_read import _same_capture
from .coverage_copy_read import _retain, _snapshot, capture_closed_copy
from .suite_evidence import ROLES
from .test_suite_evidence import NODES, _decoder_fixture, _decoder_raw

LIMITS = {'files': 4, 'contexts': 4, 'pairs': 64, 'items': 1024,
          'name_bytes': 128, 'total_name_bytes': 8192}



def _write_fixture(path, raw):
    """Fixture setup only, NOT the actual process receipt-index producer.

    Explicit known FD ownership preserves close controls; no overwrite/retry.
    All setup bytes/fsync/control time count in the same unadmitted budget.
    """
    if type(raw) is not bytes or not 0 < len(raw) <= 1000000:
        raise ValueError('finite fixture metadata bytes required')
    directory_fd, file_fd, primary = None, None, None
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        file_fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                          | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        if os.write(file_fd, raw) != len(raw):
            raise OSError('short fixture write; preserve/no retry')
        os.fsync(file_fd)
        os.fsync(directory_fd)
    except BaseException as error:
        primary = error
    finally:
        for fd in (file_fd, directory_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as cleanup:
                    primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary
    version = _snapshot(path)
    if version['bytes'] != len(raw) or version['sha256'] != hashlib.sha256(raw).hexdigest():
        raise ValueError('fixture bytes changed after closed write')



def _binding_preflight_faults(owner_args):
    """LOWER actual-orchestrator adapter, not real failing IO/native positive.

    Existing real fixture/preflight, controlled first hash and forbidden-native
    continuation; module aliases restored. No alternate successful union object.
    """
    for expected in (OSError('bound input read'), KeyboardInterrupt('bound input control')):
        original_hash, original_union = bound_adapter.hash_retained_bytes, bound_adapter.build_union_with_graph
        hashes, unions, caught = [], [], None

        def failing_hash(observation, **kwargs):
            hashes.append(observation['path'])
            raise expected

        def forbidden_union(*args, **kwargs):
            unions.append(True)
            raise AssertionError('native union after failed retained input')

        try:
            bound_adapter.hash_retained_bytes = failing_hash
            bound_adapter.build_union_with_graph = forbidden_union
            try:
                bound_adapter.build_bound_union_graph(**owner_args)
            except BaseException as error:
                caught = error
        finally:
            bound_adapter.hash_retained_bytes = original_hash
            bound_adapter.build_union_with_graph = original_union
        assert bound_adapter.hash_retained_bytes is original_hash
        assert bound_adapter.build_union_with_graph is original_union
        assert caught is expected
        assert hashes == [owner_args['bindings']['catalog']['path']]
        assert unions == []


def _run_build_union_storage_fixture(*, root, coverage_data_class, coverage_archive_path,
                                     report_module=None, source_root=None):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local control execution authority')
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root
            or not root.is_dir() or any(root.iterdir())):
        raise ValueError('fresh physical owned control root required')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private owned control root required')
    original_root, copy_root, aggregate_root, verify_root, graph_root, binding_root, report_root = [
        root / n for n in ('original', 'copy', 'aggregate', 'verify', 'graph', 'bindings', 'report')]
    for path in (original_root, copy_root, aggregate_root, verify_root, graph_root, binding_root, report_root):
        path.mkdir(mode=0o700)
    # Real immutable fixture bytes, not a fake catalog/archive/profile hash.
    package = Path(__file__).parent
    for source, name in ((package / 'coverage-source-B.json', 'catalog.json'),
                         (package / 'coverage.ini', 'coverage.ini'),
                         (Path(coverage_archive_path), 'coverage.tar.gz')):
        _exclusive_copy(source, binding_root / name, _snapshot(source))
    raw, starts, finishes = _decoder_fixture()  # full schema, SYNTHETIC process records
    spec = json.loads(raw)
    spec['output'] = str(original_root)
    if source_root is not None:
        spec['source_root'] = str(source_root)
    spec['coverage_config'] = str(binding_root / 'coverage.ini')
    spec['coverage_config_sha256'] = _snapshot(binding_root / 'coverage.ini')['sha256']
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
    _write_fixture(binding_root / 'config.json', raw)
    raw_observations = {}
    for role in ROLES:
        raw_observations[role] = {}
        for phase, container in (('start', starts), ('finish', finishes)):
            path = original_root / (role + '.' + phase + '.json')
            _write_fixture(path, _decoder_raw(container[role]))
            raw_observations[role][phase] = {'path': str(path), **_snapshot(path)}
    index = {'format': 'criterion5-937-run-receipt-index-v1', 'pin': spec['pin'],
             'run_id': spec['run_id'], 'config_sha256': config_sha, 'roles': raw_observations}
    _write_fixture(binding_root / 'index.json', _decoder_raw(index))
    bindings = {key: {'path': str(binding_root / name), **_snapshot(binding_root / name)}
                for key, name in (('catalog', 'catalog.json'), ('archive', 'coverage.tar.gz'),
                                  ('profile', 'coverage.ini'), ('config', 'config.json'), ('index', 'index.json'))}
    artifact_limits = {key: row['bytes'] for key, row in bindings.items()}
    artifact_limits.update({role + '.' + phase: raw_observations[role][phase]['bytes']
                            for role in ROLES for phase in ('start', 'finish')})
    input_limits = {'artifact_bytes': artifact_limits,
                    'total_artifact_bytes': sum(artifact_limits.values()), 'path_bytes': 4096,
                    'data_file_bytes': max(len(v) for v in source_bytes.values()),
                    'total_data_bytes': 2 * sum(len(v) for v in source_bytes.values())}
    original_profile = sys.getprofile()
    if original_profile is not None:
        raise ValueError('storage control requires isolated unprofiled interpreter')
    owner_args = {
        'bindings': bindings, 'receipts': raw_observations, 'captures': captures,
        'receipt_root': str(original_root),
        'output_roots': {'aggregate': str(aggregate_root), 'verify': str(verify_root),
                         'graph': str(graph_root), 'report': str(report_root)},
        'input_limits': input_limits,
        'json_limits': {'bytes': 1000000, 'nodes': 100000, 'depth': 64, 'string_characters': 1000000},
        'coverage_limits': LIMITS,
        'wire_limits': {'nodes': 100000, 'depth': 64, 'string_characters': 100000, 'wire_bytes': 2000000},
        'coverage_data_class': coverage_data_class, 'actual_exit': 0,
        'outer_seconds': 10.0, 'cohort': NODES}
    _binding_preflight_faults(owner_args)  # LOWER owner propagation; zero native union calls
    calls, primary, result = [], None, None
    update_code = coverage_data_class.update.__code__

    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is update_code:
            # Primitive path only; no retained data/frame/native object. No
            # class method/SQL/clock patch and no product measurement profiling.
            calls.append(frame.f_locals['other_data'].data_filename())

    try:
        sys.setprofile(profile)
        if report_module is None:
            result = build_bound_union_graph(**owner_args)
            combined = None
        else:
            from .coverage_bound_report import render_bound_report
            report_args = {k: v for k, v in owner_args.items() if k != 'coverage_data_class'}
            combined = render_bound_report(coverage_module=report_module, source_root=source_root,
                                           **report_args)
            result = combined['union']
    except BaseException as error:
        primary = error
    finally:
        try:
            sys.setprofile(original_profile)
        except BaseException as cleanup:
            primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary
    expected_updates = [str(copy_root / finishes[role]['coverage_data']['file']) for role in ROLES]
    if report_module is not None:
        expected_updates.append(str(report_root / 'input' / 'coverage-aggregate.data'))
    assert calls == expected_updates  #9union, plus ONE report-copy update when requested
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
    # Actual retained binding bytes are checked, but process records remain
    # SYNTHETIC. Nothing here authenticates a real plugin/process/loaded source.
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
    assert graph['bindings']['run_receipts_sha256'] == bindings['index']['sha256']
    for key in bindings:
        assert _same_capture(result['retained_binding_observations'][key], bindings[key])
    assert _same_capture(result['retained_receipt_observations'], raw_observations)
    for role in ROLES:
        name = finishes[role]['coverage_data']['file']
        assert (original_root / name).read_bytes() == source_bytes[role]
        assert (copy_root / name).read_bytes() == source_bytes[role]
    return {'union': result, 'combined_report': combined,
            'bindings': bindings, 'receipts': raw_observations, 'captures': captures,
            'owner_args': owner_args}


def run_build_union_storage_control(*, root, coverage_data_class, coverage_archive_path):
    _run_build_union_storage_fixture(root=root, coverage_data_class=coverage_data_class,
                                    coverage_archive_path=coverage_archive_path)
    return {'result': 'EXPECTED_NINE_API_STORAGE_UNION',
            'scope': 'SYNTHETIC run metadata+stored API arcs only; not actual pytest/process/measurement proof'}
