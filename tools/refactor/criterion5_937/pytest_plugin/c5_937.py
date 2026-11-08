"""937 pytest metadata. No product imports; primary pytest status is recorded separately.

Load explicitly from its dedicated directory (no sitecustomize/child_env patch).
Local runner/setup/source/coverage adapters remain separately qualified. This
plugin is SOURCE PREPARATION and has not been executed or certified.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time

import pytest

MAX_EVENTS = 100000
_CLOCK = time.monotonic  # observer clock only; product clocks are not patched
MAX_NODES = 50000


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def publish(path, value):
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=True) + '\n').encode()
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.partial')
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    # Atomic exclusive publication; never overwrite a previous/uncertain receipt.
    os.link(temporary, path)
    temporary.unlink()
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def checked_nodes(nodes):
    if any(type(n) is not str or not n or any(c in n for c in '\0\r\n') for n in nodes):
        raise pytest.UsageError('937 invalid node identity')
    if len(nodes) > MAX_NODES or len(nodes) != len(set(nodes)):
        raise pytest.UsageError('937 duplicate/oversized node roster')
    return sorted(nodes)


def pytest_addoption(parser):
    parser.getgroup('criterion5-937').addoption('--c5-config', required=False)



_EARLY_COVERAGE = None


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    global _EARLY_COVERAGE
    paths = []
    for i, arg in enumerate(args):
        if arg == '--c5-config':
            if i + 1 >= len(args):
                raise pytest.UsageError('937 missing config operand')
            paths.append(args[i + 1])
        elif arg.startswith('--c5-config='):
            paths.append(arg.split('=', 1)[1])
    if len(paths) != 1:
        raise pytest.UsageError('937 one exact config operand required')
    raw = Path(paths[0]).read_bytes()
    spec = json.loads(raw)
    if spec.get('mode') != 'coverage':
        return
    if socket.gethostname() == 'laptopstudio':
        raise pytest.UsageError('937 BUILDBOX_ONLY')
    if _EARLY_COVERAGE is not None:
        raise pytest.UsageError('937 coverage already active')
    root, output = Path(spec['source_root']), Path(spec['output'])
    if (not root.is_absolute() or root.is_symlink() or root.resolve() != early_config.rootpath.resolve()
            or not output.is_absolute() or output.is_symlink() or not output.is_dir()):
        raise pytest.UsageError('937 coverage roots not bound')
    owner = json.loads((output / 'owner.json').read_bytes())
    if owner != {k: spec[k] for k in ('run_id', 'pin', 'arm')}:
        raise pytest.UsageError('937 coverage output owner changed')
    profile = Path(spec['coverage_config'])
    if sha(profile.read_bytes()) != spec['coverage_config_sha256']:
        raise pytest.UsageError('937 coverage config changed')
    import coverage
    if Path(coverage.__file__).resolve().parent != Path(spec['coverage_package_root']).resolve():
        raise pytest.UsageError('937 coverage import from unexpected tool source')
    if coverage.__version__ != spec['coverage_version'] or coverage.__version__ != '7.17.0':
        raise pytest.UsageError('937 unpinned coverage tool version')
    name = 'coverage-pid-' + str(os.getpid()) + '.data'
    cov = coverage.Coverage(data_file=str(output / name), data_suffix=False,
                            config_file=str(profile), source=[str(root / 'server')], branch=True)
    cov.start()
    _EARLY_COVERAGE = {'object': cov, 'file': output / name, 'config_sha256': sha(raw),
                       'version': coverage.__version__, 'module_path': coverage.__file__,
                       'scope': 'this pytest process only; external Host/runtime/provider children UNOBSERVED'}

def pytest_configure(config):
    path = config.getoption('--c5-config')
    if not path:
        raise pytest.UsageError('937 immutable measurement configuration required')
    if socket.gethostname() == 'laptopstudio':
        raise pytest.UsageError('937 BUILDBOX_ONLY')
    if pytest.__version__ != '9.1.1':
        raise pytest.UsageError('937 pytest version differs from locked pins')
    raw = Path(path).read_bytes()
    spec = json.loads(raw)
    required = {'arm', 'pin', 'run_id', 'mode', 'source_root', 'output',
                'cache_prefix', 'expected_workers', 'cohort', 'cohort_sha256',
                'coverage_version', 'coverage_config', 'coverage_config_sha256', 'coverage_package_root'}
    if set(spec) != required or spec['arm'] not in ('A', 'B') or spec['mode'] not in ('collect', 'timing', 'coverage'):
        raise pytest.UsageError('937 invalid configuration')
    if (not isinstance(spec['run_id'], str) or not spec['run_id']
            or len(spec['pin']) != 40 or any(c not in '0123456789abcdef' for c in spec['pin'])):
        raise pytest.UsageError('937 missing source/attempt identity')
    root = Path(spec['source_root'])
    output = Path(spec['output'])
    prefix = Path(spec['cache_prefix'])
    if (not root.is_absolute() or root.is_symlink() or root.resolve() != config.rootpath.resolve()
            or not output.is_absolute() or output.is_symlink() or not output.is_dir()
            or not prefix.is_absolute() or prefix.is_symlink() or not prefix.is_dir()):
        raise pytest.UsageError('937 unqualified path binding')
    owner = json.loads((output / 'owner.json').read_bytes())
    if owner != {k: spec[k] for k in ('run_id', 'pin', 'arm')}:
        raise pytest.UsageError('937 output ownership mismatch')
    if sys.pycache_prefix != str(prefix) or not sys.dont_write_bytecode:
        raise pytest.UsageError('937 worker bytecode policy mismatch')
    expected = 0 if spec['mode'] == 'collect' else 8
    if type(spec['expected_workers']) is not int or spec['expected_workers'] != expected:
        raise pytest.UsageError('937 unfrozen worker count')
    worker = getattr(config, 'workerinput', {}).get('workerid', 'controller')
    local_workers = config.getoption('numprocesses', default=0)
    if worker == 'controller':
        if local_workers != expected:
            raise pytest.UsageError('937 actual configured worker count differs')
        if expected and str(config.getoption('maxworkerrestart', default=None)) != '0':
            raise pytest.UsageError('937 worker replacement must be disabled')
    elif worker not in {'gw' + str(i) for i in range(8)} or local_workers is not None:
        # xdist3.8 clears numprocesses in workers; eight actual starts/collections
        # and distinct PID/start receipts establish the real worker census.
        raise pytest.UsageError('937 unexpected worker role/configuration')
    start = Path('/proc/self/stat').read_text().rpartition(')')[2].split()[19]
    state = {'spec': spec, 'config_sha256': sha(raw), 'output': output,
             'worker': worker, 'local_numprocesses': local_workers, 'pid': os.getpid(), 'start_ticks': int(start),
             'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
             'started_mono': None, 'nodes_before': [], 'nodes_selected': [],
             'deselected': [], 'worker_collections': {}, 'events': [], 'subtests': [],
             'errors': [], 'python': sys.version, 'pytest': pytest.__version__, 'executable': sys.executable,
             'affinity': sorted(os.sched_getaffinity(0)), 'pycache_prefix': sys.pycache_prefix}
    if spec['mode'] == 'coverage':
        if _EARLY_COVERAGE is None or _EARLY_COVERAGE['config_sha256'] != sha(raw):
            raise pytest.UsageError('937 early coverage missing or different input')
        state['coverage'] = {k: v for k, v in _EARLY_COVERAGE.items() if k not in ('object', 'file')}
    elif _EARLY_COVERAGE is not None:
        raise pytest.UsageError('937 timing contaminated by coverage')
    global _active_state
    _active_state = config._c5_937 = state
    publish(output / (worker + '.start.json'), {k: v for k, v in state.items()
            if k not in ('output', 'events', 'subtests')})


def pytest_sessionstart(session):
    session.config._c5_937['started_mono'] = _CLOCK()


def pytest_collection_modifyitems(session, config, items):
    state = config._c5_937
    before = checked_nodes([item.nodeid for item in items])
    state['nodes_before'] = before
    if state['spec']['mode'] == 'timing':
        raw = Path(state['spec']['cohort']).read_bytes()
        wanted = json.loads(raw)
        if sha(raw) != state['spec']['cohort_sha256'] or type(wanted) is not list:
            raise pytest.UsageError('937 cohort bytes changed')
        wanted = checked_nodes(wanted)
        if not wanted or not set(wanted).issubset(before):
            raise pytest.UsageError('937 cohort missing from actual collection')
        wanted_set = frozenset(wanted)
        excluded = [item for item in items if item.nodeid not in wanted_set]
        selected = [item for item in items if item.nodeid in wanted_set]
        state['deselected'] = sorted(item.nodeid for item in excluded)
        config.hook.pytest_deselected(items=excluded)
        items[:] = selected
    state['nodes_selected'] = checked_nodes([item.nodeid for item in items])


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    state = node.config._c5_937
    worker = node.gateway.id
    if worker in state['worker_collections']:
        state['errors'].append('duplicate_worker_collection')
    state['worker_collections'][worker] = checked_nodes(list(ids))


def pytest_runtest_logreport(report):
    # The controller receives all worker reports; worker copies remain separate.
    state = _active_state
    if state is None:
        raise RuntimeError('937 report outside configured session')
    target = state['subtests'] if type(report).__name__ in ('SubtestReport', 'SubTestReport') else state['events']
    if len(state['events']) + len(state['subtests']) >= MAX_EVENTS:
        raise RuntimeError('937 report cap reached; no silent truncation')
    target.append({'nodeid': report.nodeid, 'when': report.when, 'outcome': report.outcome,
                   'duration': report.duration, 'report_type': type(report).__name__, 'wasxfail': bool(getattr(report, 'wasxfail', False))})


_active_state = None


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    state = session.config._c5_937
    primary = int(exitstatus)
    if state['worker'] == 'controller' and state['spec']['mode'] != 'collect':
        expected = {'gw' + str(i) for i in range(8)}
        if set(state['worker_collections']) != expected:
            state['errors'].append('missing_or_replaced_worker')
        elif len({tuple(nodes) for nodes in state['worker_collections'].values()}) != 1:
            state['errors'].append('worker_rosters_disagree')
        worker_identities = set()
        for worker in sorted(expected):
            try:
                finish_path = state['output'] / (worker + '.finish.json')
                start_path = state['output'] / (worker + '.start.json')
                if any(p.is_symlink() or p.stat().st_nlink != 1 for p in (finish_path, start_path)):
                    raise ValueError('worker receipt alias')
                finish = json.loads(finish_path.read_bytes())
                start = json.loads(start_path.read_bytes())
                for record in (start, finish):
                    if (record['worker'] != worker or record['config_sha256'] != state['config_sha256']
                            or record['spec'] != state['spec'] or record['boot'] != state['boot']
                            or record['affinity'] != state['affinity']
                            or record['executable'] != state['executable']):
                        raise ValueError('worker binding mismatch')
                identity = (finish['pid'], finish['start_ticks'])
                if identity != (start['pid'], start['start_ticks']) or identity in worker_identities:
                    raise ValueError('worker identity reused')
                worker_identities.add(identity)
                if finish['errors'] or (primary == 0 and finish['qualified_exit'] != 0):
                    raise ValueError('worker terminal incomplete/non-green')
                if state['spec']['mode'] == 'coverage':
                    data = finish['coverage_data']
                    file = state['output'] / data['file']
                    if (Path(data['file']).name != data['file'] or file.is_symlink()
                            or file.stat().st_nlink != 1 or file.stat().st_size != data['bytes']
                            or sha(file.read_bytes()) != data['sha256']):
                        raise ValueError('worker coverage missing/changed')
            except Exception as error:
                state['errors'].append('worker_terminal_' + worker + '_' + type(error).__name__)
                print('937_WORKER_TERMINAL_UNKNOWN ' + worker + ' ' + type(error).__name__,
                      file=sys.stderr, flush=True)
    if state['spec']['mode'] == 'coverage':
        try:
            if _EARLY_COVERAGE is None:
                raise RuntimeError('coverage observer missing')
            _EARLY_COVERAGE['object'].stop()
            _EARLY_COVERAGE['object'].save()
            data = _EARLY_COVERAGE['file']
            if data.is_symlink() or data.stat().st_nlink != 1:
                raise RuntimeError('coverage data alias')
            state['coverage_data'] = {'file': data.name, 'sha256': sha(data.read_bytes()),
                                      'bytes': data.stat().st_size,
                                      'scope': _EARLY_COVERAGE['scope']}
        except Exception as error:
            state['errors'].append('coverage_flush_' + type(error).__name__)
            print('937_COVERAGE_FLUSH_FAILED ' + type(error).__name__, file=sys.stderr, flush=True)
    ended = _CLOCK()
    value = {k: v for k, v in state.items() if k != 'output'}
    value.update(primary_pytest_exit=primary, ended_mono=ended,
                 session_hook_elapsed_seconds=(None if state['started_mono'] is None
                                                else ended - state['started_mono']),
                 timing_scope='session hooks only; outer pytest invocation/summary separately retained',
                 observer_events=len(state['events']), nested_subtests=len(state['subtests']))
    if primary == 0 and state['errors']:
        session.exitstatus = 4
    value['qualified_exit'] = int(session.exitstatus)
    try:
        publish(state['output'] / (state['worker'] + '.finish.json'), value)
    except Exception as error:
        print('937_RECEIPT_PUBLICATION_FAILED ' + type(error).__name__
              + ' primary_pytest_exit=' + str(primary), file=sys.stderr, flush=True)
        if primary == 0:
            session.exitstatus = 4
        # Failure is logged; absent terminal receipt independently refuses the
        # run. Preserve nonzero primary pytest status rather than raise a new
        # internal-error result over it. A prior zero is explicitly non-green.
        return


def pytest_unconfigure(config):
    global _active_state, _EARLY_COVERAGE
    if _active_state is getattr(config, '_c5_937', None):
        _active_state = None
    if _EARLY_COVERAGE is not None:
        try:
            _EARLY_COVERAGE['object'].stop()
        except Exception as error:
            print('937_COVERAGE_STOP_FAILED ' + type(error).__name__, file=sys.stderr, flush=True)
        finally:
            _EARLY_COVERAGE = None
