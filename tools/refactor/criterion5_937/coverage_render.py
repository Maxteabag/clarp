"""Public report-only owner SOURCE, UNEXECUTED, no tracing/measurement launcher.

Actual module/source/ABI, run/full-B/producer custody and worker isolation must
be qualified externally. This owner does not grant input or measurement MET.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import warnings

from .coverage_build_union import _exclusive_copy
from .coverage_copy_read import _root, _snapshot, _retain, _same_capture, update_closed_copy
from .coverage_sets import capture_sets, check_limits
from .coverage_report_evidence import reconcile_report
from .coverage_region_projection import validate_region_projection
from .coverage_graph_publish import verify_graph
from .coverage_retained_bytes import read_retained_bytes
from .coverage_copy_read import CHUNK

PACKAGE_HASHES = {
    'coverage.ini': '3cc8a9f20b04a5bd3e287364480ddfc039e996954da359540f1fe24472cf7de7',
    'coverage-source-B.json': 'c060901c910043552254a72d5c727646a84ac77bbca58a7bc61d10ea685d9f19',
    'coverage-report-policy.json': '8bfd58de794a81e68eb648ff4fe8bcf445c6f09e925f28a02d368713fd82d58e',
    'coverage-region-identities-B.json': '303bb289ae064040887fe9286961f2675758b7d59d5412dc26dde0968fbbd6c5'}


PACKAGE_BYTES = {'coverage.ini': 499, 'coverage-source-B.json': 77807, 'coverage-report-policy.json': 1425, 'coverage-region-identities-B.json': 371842}

def _source_version(path, expected):
    """Source-only bounded hash, including catalogued zero-byte Python files.

    This does NOT relax the nonempty SQLite/retained-artifact input policy.
    Source root/namespace ownership is the enclosing report owner's prerequisite.
    """
    if (type(expected['bytes']) is not int or expected['bytes'] < 0
            or type(expected['sha256']) is not str or len(expected['sha256']) != 64):
        raise ValueError('exact catalogued source size/hash required')
    if any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('unexpected source sidecar')
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.geteuid() or before.st_size != expected['bytes']):
        raise ValueError('source file kind/owner/size differs')
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_uid')
    fd, primary, count, digest = None, None, 0, hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(fd)
        if any(getattr(opened, k) != getattr(before, k) for k in fields):
            raise ValueError('source descriptor version changed')
        while True:
            raw = os.read(fd, min(CHUNK, expected['bytes'] - count + 1))
            if not raw:
                break
            count += len(raw)
            if count > expected['bytes']:
                raise ValueError('source grew during bounded hash')
            digest.update(raw)
        after_fd = os.fstat(fd)
        if count != expected['bytes'] or any(getattr(after_fd, k) != getattr(before, k) for k in fields):
            raise ValueError('source short read or changed version')
    except BaseException as error:
        primary = error
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except BaseException as cleanup:
                primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # no post-path/hash/next source after pending failure/control
    after = path.lstat()
    if (any(getattr(after, k) != getattr(before, k) for k in fields)
            or any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))
            or digest.hexdigest() != expected['sha256']):
        raise ValueError('source byte version/hash changed')
    return tuple(getattr(after, k) for k in fields)


def _source_versions(root, inventory):
    server = root / 'server'
    if server.is_symlink():
        raise ValueError('source server root link forbidden')
    found = set()
    for path in server.rglob('*'):
        if path.is_symlink():
            raise ValueError('source link outside frozen physical source policy')
        if path.is_file() and path.suffix == '.py':
            found.add(str(path.relative_to(root)))
    if found != set(inventory['files']):
        raise ValueError('source inventory changed; no omitted/new server Python files')
    versions = {}
    for name, expected in inventory['files'].items():
        path = root / name
        versions[name] = _source_version(path, expected)
    return versions


def _options(cov, policy, source_root, data_path):
    expected = dict(policy['options'])
    expected.update({'run:source': [str(source_root / 'server')], 'run:data_file': str(data_path)})
    for name, value in expected.items():
        if not _same_capture(cov.get_option(name), value):
            raise ValueError('report option mismatch: ' + name)


def _render_report(*, coverage_module, source_root, aggregate_capture, input_root,
                   output_root, limits, graph_root, graph_observation, expected_graph,
                   wire_limits, tool_archive_sha256):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no report/API execution authority')
    check_limits(limits)
    if coverage_module.__version__ != '7.17.0':
        raise ValueError('unsupported supplied coverage tool')
    if any(k.startswith('COVERAGE_') for k in os.environ):
        raise ValueError('coverage environment overrides/startup are not qualified')
    source_root, input_root, output_root = [_root(p) for p in (source_root, input_root, output_root)]
    if Path.cwd() != source_root:
        raise ValueError('report process must already have exact frozen source cwd')
    package = Path(__file__).parent
    _root(package)  # prepared tooling metadata directory must be physically private
    raw_package, package_observations = {}, {}
    for filename, digest in PACKAGE_HASHES.items():
        p = package / filename
        if p.lstat().st_size != PACKAGE_BYTES[filename]:
            raise ValueError('report metadata size differs before snapshot')
        observed = {'path': str(p), **_snapshot(p)}
        if observed['sha256'] != digest:
            raise ValueError('frozen report package/profile changed')
        raw_package[filename] = read_retained_bytes(
            observed, byte_limit=observed['bytes'], path_bytes_limit=4096)
        package_observations[filename] = observed
    inventory = json.loads(raw_package['coverage-source-B.json'])
    policy = json.loads(raw_package['coverage-report-policy.json'])
    if (expected_graph['bindings']['source_pin'] != inventory['pin']
            or expected_graph['bindings']['source_catalog_sha256'] != PACKAGE_HASHES['coverage-source-B.json']
            or expected_graph['bindings']['coverage_archive_sha256'] != tool_archive_sha256):
        raise ValueError('full graph source/tool differs from report package')
    # Independent complete expected graph, supplied ONLY by the actual bound
    # owner composition. Verification is BEFORE any public report generation.
    if not _same_capture(expected_graph['closed_aggregate_capture'], aggregate_capture):
        raise ValueError('report aggregate differs from independent full graph expectation')
    verify_graph(root=graph_root, observation=graph_observation,
                 expected_graph=expected_graph, limits=wire_limits)
    before_source = _source_versions(source_root, inventory)
    observed = aggregate_capture['read_close_receipt']['original_observation']
    original = Path(observed['path'])
    _root(original.parent)
    version = {k: v for k, v in observed.items() if k != 'path'}
    if not _same_capture(_snapshot(original), version):
        raise ValueError('closed aggregate source changed')
    graph_root = _root(graph_root)
    roots = (source_root, original.parent, input_root, output_root, graph_root)
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ValueError('report source/input/output custody roots overlap')
    if any(input_root.iterdir()) or any(output_root.iterdir()):
        raise ValueError('report input/output roots must be fresh and EMPTY')
    _exclusive_copy(original, input_root / original.name, version)
    destination = output_root / 'coverage-aggregate.data'
    report_path = output_root / 'coverage.json'
    scope = warnings.catch_warnings()
    entered, primary, cov, data, edge, analyses = False, None, None, None, None, {}
    owned_data = []

    def get_owned_data():
        value = cov.get_data()
        if not any(value is known for known in owned_data):
            owned_data.append(value)
        return value
    try:
        scope.__enter__()
        entered = True
        warnings.simplefilter('error')  # isolated report worker only; no silent warning/skip
        cov = coverage_module.Coverage(data_file=str(destination), data_suffix=False, auto_data=False,
                                       config_file=str(package / 'coverage.ini'), branch=True,
                                       source=[str(source_root / 'server')])
        _options(cov, policy, source_root, destination)
        data = get_owned_data()  # never start tracing or default .coverage load
        if type(data) is not coverage_module.CoverageData or data.data_filename() != str(destination):
            raise ValueError('public report data object/path differs')
        edge = update_closed_copy(
            original_root=original.parent, copy_root=input_root, filename=original.name,
            expected_sha256=version['sha256'], expected_bytes=version['bytes'],
            coverage_data_class=coverage_module.CoverageData, limits=limits,
            expected_sets=aggregate_capture['accessor_sets'], aggregate=data,
            aggregate_root=output_root, aggregate_before=None)
        captured = capture_sets(data, limits=limits)
        if not _same_capture({k: captured[k] for k in ('mode', 'contexts', 'files')},
                             {k: aggregate_capture['accessor_sets'][k] for k in ('mode', 'contexts', 'files')}):
            raise ValueError('report data differs from complete closed aggregate')
        if get_owned_data() is not data:
            raise ValueError('public report data identity changed')
        _options(cov, policy, source_root, destination)
        if os.path.lexists(report_path):
            raise ValueError('report output already exists; no overwrite/retry')
        morfs = [str(source_root / name) for name in sorted(inventory['files'])]
        cov.json_report(morfs=morfs, outfile=str(report_path), ignore_errors=False,
                        contexts=None, show_contexts=False, pretty_print=False)
        if get_owned_data() is not data:
            raise ValueError('report remapped/replaced its data')
        for name in sorted(inventory['files']):
            filename, statements, excluded, missing, _ = cov.analysis2(str(source_root / name))
            # Frozen relative_files=True maps the absolute morf to this
            # exact inventory identity; do not normalize a foreign result.
            if type(filename) is not str or filename != name:
                raise ValueError('public analysis filename remapped')
            stats = cov.branch_stats(str(source_root / name))
            analyses[name] = {'source_sha256': inventory['files'][name]['sha256'],
                              'statements': statements, 'excluded': excluded, 'missing': missing,
                              'branch_stats': [{'line': line, 'total': total, 'taken': taken}
                                               for line, (total, taken) in sorted(stats.items())]}
        _options(cov, policy, source_root, destination)
        if get_owned_data() is not data:
            raise ValueError('analysis changed public report data identity')
        data.set_query_contexts(None)
    except BaseException as error:
        primary = error
    finally:
        for known in reversed(owned_data):
            try:
                known.close()  # each known public result owned here, once
            except BaseException as cleanup:
                primary = _retain(primary, cleanup)
        if entered:
            try:
                scope.__exit__(None, None, None)  # required global warning-policy restoration
            except BaseException as cleanup:
                primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # no optional parse/hash/reconciliation/publication/next step
    if (not _same_capture(_snapshot(original), version)
            or not _same_capture(_snapshot(destination), edge['aggregate_after_observation'])
            or _source_versions(source_root, inventory) != before_source or Path.cwd() != source_root):
        raise ValueError('report changed input/data/source epoch')
    for filename, raw in raw_package.items():
        observed = package_observations[filename]
        if read_retained_bytes(observed, byte_limit=observed['bytes'], path_bytes_limit=4096) != raw:
            raise ValueError('report profile/catalog/policy changed during API work')
    verify_graph(root=graph_root, observation=graph_observation,
                 expected_graph=expected_graph, limits=wire_limits)
    report_observation = _snapshot(report_path)  # finite refusal cap, NOT pre-generation resource proof
    raw_report = read_retained_bytes({'path': str(report_path), **report_observation},
                                     byte_limit=report_observation['bytes'], path_bytes_limit=4096)
    if hashlib.sha256(raw_report).hexdigest() != report_observation['sha256']:
        raise ValueError('report changed while read')
    parsed_report = json.loads(raw_report)
    identity = validate_region_projection(
        parsed_report, inventory=inventory,
        region_catalog_raw=raw_package['coverage-region-identities-B.json'],
        tool_archive_sha256=tool_archive_sha256)
    result = reconcile_report(parsed_report, analyses, inventory=inventory)
    return {'raw_report_observation': {'path': str(report_path), **report_observation},
            'analysis_records': analyses, 'consistency': result, 'region_identity': identity,
            'region_catalog_observation': package_observations['coverage-region-identities-B.json'],
            'graph_observation': graph_observation,
            'captured_files_without_frozen_source': sorted(
                set(aggregate_capture['accessor_sets']['files']) - set(inventory['files'])),
            'qualification': 'PUBLIC_REPORT_SOURCE_CONSISTENCY_ONLY_NOT_MEASUREMENT_ACCEPTANCE',
            'actual_run_input_producer_coverage_or_no_worse_accepted': False}
