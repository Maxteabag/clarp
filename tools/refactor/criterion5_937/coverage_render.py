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

PACKAGE_HASHES = {
    'coverage.ini': '3cc8a9f20b04a5bd3e287364480ddfc039e996954da359540f1fe24472cf7de7',
    'coverage-source-B.json': 'c060901c910043552254a72d5c727646a84ac77bbca58a7bc61d10ea685d9f19',
    'coverage-report-policy.json': '6445deaf24c52dd297cae36c465c6f0323aefed349036bf4ef26ac622d2838e8'}


def _source_versions(root, inventory):
    server = root / 'server'
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
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size != expected['bytes']):
            raise ValueError('source file kind/size/custody differs')
        raw = path.read_bytes()  # SOURCE bytes only; no import/compile/execution
        after = path.lstat()
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
        if (any(getattr(before, k) != getattr(after, k) for k in fields)
                or hashlib.sha256(raw).hexdigest() != expected['sha256']):
            raise ValueError('source byte version changed')
        versions[name] = tuple(getattr(after, k) for k in fields)
    return versions


def _options(cov, policy, source_root, data_path):
    expected = dict(policy['options'])
    expected.update({'run:source': [str(source_root / 'server')], 'run:data_file': str(data_path)})
    for name, value in expected.items():
        if not _same_capture(cov.get_option(name), value):
            raise ValueError('report option mismatch: ' + name)


def render_report(*, coverage_module, source_root, aggregate_capture, input_root,
                  output_root, limits):
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
    raw_package = {}
    for filename, digest in PACKAGE_HASHES.items():
        p = package / filename
        if p.is_symlink() or p.stat().st_nlink != 1:
            raise ValueError('report package source alias')
        raw_package[filename] = p.read_bytes()
        if hashlib.sha256(raw_package[filename]).hexdigest() != digest:
            raise ValueError('frozen report package/profile changed')
    inventory = json.loads(raw_package['coverage-source-B.json'])
    policy = json.loads(raw_package['coverage-report-policy.json'])
    before_source = _source_versions(source_root, inventory)
    observed = aggregate_capture['read_close_receipt']['original_observation']
    original = Path(observed['path'])
    _root(original.parent)
    version = {k: v for k, v in observed.items() if k != 'path'}
    if not _same_capture(_snapshot(original), version):
        raise ValueError('closed aggregate source changed')
    roots = (source_root, original.parent, input_root, output_root)
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
                        contexts=None, show_contexts=True)
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
        if (package / filename).read_bytes() != raw:
            raise ValueError('report profile/catalog/policy changed during API work')
    report_observation = _snapshot(report_path)  # finite refusal cap, NOT pre-generation resource proof
    raw_report = report_path.read_bytes()
    if hashlib.sha256(raw_report).hexdigest() != report_observation['sha256']:
        raise ValueError('report changed while read')
    result = reconcile_report(json.loads(raw_report), analyses, inventory=inventory)
    return {'raw_report_observation': {'path': str(report_path), **report_observation},
            'analysis_records': analyses, 'consistency': result,
            'qualification': 'PUBLIC_REPORT_SOURCE_CONSISTENCY_ONLY_NOT_MEASUREMENT_ACCEPTANCE',
            'actual_run_input_producer_coverage_or_no_worse_accepted': False}
