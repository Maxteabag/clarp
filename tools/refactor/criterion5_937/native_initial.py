"""Native A initial-seed construction function, SOURCE ONLY / NOT EXECUTED.

No command line entrypoint or launch permission. The complete runner must bind
source/environment/owned roots, admit remote setup, enforce its absolute hard
deadline, retain the native guard result, and prove process exit before copying
or any measured roots. Cooperative checks here are NOT a kill/drain certificate.
Real runtime/Host and independent reference/plan oracles remain unwritten.
"""
import datetime as dt
from dataclasses import asdict
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import runpy
import socket
import sqlite3
import sys
import time

from .initial_seed_clock import initial_fixture_clock
from .native_isolation import Guard
from .proc_samples import bind
from .raw_products import capture
from .recipe import APPEND_EXCHANGES, COUNTS, INITIAL_BYTES, record
from .seed_paths import audit_initial_paths

A_PIN = '4f33cdc9287eafcddd0adfb17b51487c84ace28b'
EPOCH_MS = 1767225600000


def deadline(end):
    if type(end) not in (int, float) or not math.isfinite(end) or not time.monotonic() < end:
        raise TimeoutError('unmeasured initial-seed phase deadline; remaining campaign stops')


def source_check(source, files):
    actual = {}
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('project source link forbidden')
        if path.name == '__pycache__' or path.suffix in ('.pyc', '.pyo'):
            raise ValueError('project bytecode cache forbidden, including untracked paths')
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError('project hardlink forbidden')
            actual[path.relative_to(source).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != files:
        raise ValueError('complete prepared source inventory differs')


def environment(root, source):
    expected = {
        'HOME': str(root / 'home'), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC',
        'XDG_CONFIG_HOME': str(root / 'home/.config'),
        'XDG_CACHE_HOME': str(root / 'home/.cache'),
        'XDG_DATA_HOME': str(root / 'home/.local/share'),
        'CLARP_CONFIG_DIR': str(root / 'config'), 'CLARP_CACHE_DIR': str(root / 'cache'),
        'CLARP_DATA_DIR': str(root / 'data'), 'CLARP_SHARE_DIR': str(root / 'data'),
        'CLAUDE_PWA_CONFIG': str(root / 'config/config.toml'),
        'CLAUDE_PWA_ROOT': str(source), 'CLAUDE_PWA_DB': str(root / 'state.sqlite'),
        'CLAUDE_PWA_AUDIO_DIR': str(root / 'cache/audio'),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONHASHSEED': '0'}
    # Coordinator supplies this exact private child environment, not inherited
    # credentials/proxy/config/provider variables. It must additionally qualify
    # its helper-only PYTHONPATH and fresh empty external bytecode prefix.
    extras = {'PYTHONPATH', 'PYTHONPYCACHEPREFIX', 'LC_CTYPE'}
    if (set(os.environ) - set(expected) - extras
            or any(os.environ.get(key) != value for key, value in expected.items())
            or Path.home() != root / 'home' or Path.cwd() != root
            or not sys.dont_write_bytecode or not sys.pycache_prefix):
        raise ValueError('exact private seed environment/cache/cwd not supplied')
    return expected


def build_initial_fixture(*, run_id, pin, mode, source, root, output, source_files,
                          corpus_manifest, absolute_deadline):
    """Actual native schema/context/importer on the common A physical fixture.

    Creates no provider, fake status, index order, user_version or planner hint.
    Source/guard/raw-data binding here still needs exact C/B review and finite
    controls; this function is not certified runnable merely because it exists.
    """
    if pin != A_PIN or mode != 'initial_seed':
        raise ValueError('only exact common A INITIAL seed operation implemented')
    if type(run_id) is not str or not run_id or len(run_id) > 128:
        raise ValueError('missing immutable seed attempt identity')
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY')
    source, root, output = map(Path, (source, root, output))
    for path in (source, root, output):
        if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
            raise ValueError('source/state/output must be exact physical directories')
    for left, right in ((source, root), (source, output), (root, output)):
        if left == right or left.is_relative_to(right) or right.is_relative_to(left):
            raise ValueError('source/state/output isolation overlap')
    database = root / 'state.sqlite'
    if any(path.exists() for path in (database, Path(str(database) + '-wal'), Path(str(database) + '-shm'))):
        raise ValueError('initial seed is not fresh; no overwrite/retry')
    size = corpus_manifest['size']
    if size not in COUNTS or len(corpus_manifest['files']) != 3:
        raise ValueError('undeclared initial corpus')
    expected_inputs = {root / 'home/.claude/projects/project' / (f'bs-synthetic-{i}.jsonl')
                       for i in range(3)}
    actual_inputs = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('private seed tree contains link')
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError('private seed input hardlink')
            actual_inputs.add(path)
    if actual_inputs != expected_inputs:
        raise ValueError('fresh private seed contains files beyond the three synthetic inputs')
    expected_env = environment(root, source)
    producer = asdict(bind(os.getpid()))
    cache = Path(sys.pycache_prefix)
    if (not cache.is_absolute() or cache.is_symlink() or cache.resolve(strict=True) != cache
            or not cache.is_dir() or any(cache.iterdir())
            or os.environ.get('PYTHONPYCACHEPREFIX') != str(cache)
            or any(cache.is_relative_to(path) or path.is_relative_to(cache)
                   for path in (source, root, output))):
        raise ValueError('seed child prefix not unique/empty/external; source bytecode provenance unqualified')
    deadline(absolute_deadline)
    source_check(source, source_files)
    failure_file = output / 'initial-seed-guard-failures.jsonl'
    if failure_file.exists() or failure_file.is_symlink():
        raise ValueError('guard output reused')
    guard = Guard(source=source, usage_file='server/lib/backend_usage.py',
                  usage_sha256=source_files['server/lib/backend_usage.py'], failure_file=failure_file,
                  unix_paths=(str(root / 'cache/runtime.sock'),), tcp_endpoints=(),
                  search_dirs=('/usr/bin', '/bin'), expected_cwd=root)
    guard.install()  # irreversible process-local audit, before every product import
    sys.path.insert(0, str(source / 'server'))
    began, began_wall = time.monotonic(), time.time_ns()
    records, imports = [], []
    with initial_fixture_clock(mode=mode, source=source, files=source_files, epoch_ms=EPOCH_MS) as clock:
        namespace = runpy.run_path(str(source / 'server/server.py'), run_name='_criterion5_937_initial_seed')
        db = importlib.import_module('lib.db')
        importer = importlib.import_module('lib.transcript_incremental')
        agents = importlib.import_module('lib.agents')
        diagnostics = importlib.import_module('lib.diagnostics_settings')
        # Unmeasured schema/invariants only; no running server or workers.
        context = namespace['ServerContext'].production(connect_runtime=False)
        namespace['_seed_startup_invariants'](context)
        diagnostics.update({'enabled': True, 'categories': list(diagnostics.CATEGORIES)})
        connection = db.conn()
        native_connection = {'class': type(connection).__qualname__, 'module': type(connection).__module__,
                             'stdlib_subclass': isinstance(connection, sqlite3.Connection),
                             'sqlite_version': sqlite3.sqlite_version}
        if not native_connection['stdlib_subclass'] or not isinstance(connection, db._TrackedConnection):
            raise RuntimeError('native SQLite tracked connection binding absent')
        for index, count in enumerate(COUNTS[size]):
            deadline(absolute_deadline)
            agent, backend = f'synthetic-{index}', f'bs-synthetic-{index}'
            item = corpus_manifest['files'][index]
            path = root / 'home/.claude/projects/project' / (backend + '.jsonl')
            before = path.stat()
            if (path.is_symlink() or path.resolve(strict=True) != path
                    or item['file'] != path.name or item['agent'] != agent
                    or item['exchanges'] != count or item['records'] != 2 * count
                    or before.st_size != item['bytes']
                    or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']):
                raise ValueError('frozen corpus bytes/identity not supplied')
            def stamp(number, role):
                value = record(agent, number, role)['timestamp']
                return int(dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('INSERT INTO agents(agent_id,persona,voice_id,cwd,session,created_at,backend) VALUES(?,?,?,?,?,?,?)',
                                   (agent, agent, '', str(root / 'home'), agent, EPOCH_MS, 'claude'))
                connection.executemany("INSERT INTO turns(agent_id,source,trace_id,started_at,settled_at,outcome) VALUES(?,'local',?,?,?,'completed')",
                    [(agent, f'trace-{index}-{i}', stamp(i, 'user'), stamp(i, 'assistant'))
                     for i in range(count + APPEND_EXCHANGES)])
                connection.executemany("INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,text,updated_at,revision,origin,trace_id,timestamp) VALUES(?,?,?,'client:synthetic',?,'user',?,1,1,'user',?,?)",
                    [(f'u-{index}-{i}', agent, backend, -i-1, f'request {agent} {i}', f'trace-{index}-{i}',
                      record(agent, i, 'user')['timestamp']) for i in range(0, count, 10)])
                connection.execute('COMMIT')
            except BaseException as primary:
                try:
                    connection.execute('ROLLBACK')
                except Exception as cleanup:
                    primary.add_note('seed rollback failed: ' + type(cleanup).__name__)
                raise
            entered = time.monotonic()
            importer.import_transcript(backend='claude', path=path, agent_id=agent, backend_session_id=backend)
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise RuntimeError('initial immutable transcript changed during native import')
            imports.append({'agent': agent, 'started_mono': entered, 'ended_mono': time.monotonic(),
                            'source_sha256': item['sha256'], 'bytes': before.st_size})
            agents.start_runtime(agent, agent)
            agents.bind_backend_session(agent, backend)
            records.append({'agent_id': agent, 'session': agent, 'backend_session_id': backend,
                            'path': str(path), 'baseline_exchanges': count})
        if sum(row['bytes'] for row in imports) != INITIAL_BYTES[size]:
            raise RuntimeError('initial aggregate corpus mismatch')
        deadline(absolute_deadline)
        products = capture(database, owned_root=root)
        source_path_census = audit_initial_paths(products, root=str(root))
        if connection.in_transaction:
            raise RuntimeError('native seed transaction left open')
        versions = {name: connection.execute('PRAGMA ' + name).fetchone()[0]
                    for name in ('schema_version', 'user_version')}
        db._close_stamp_connection()
        checkpoint = list(connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
        if checkpoint != [0, 0, 0]:
            raise RuntimeError('native checkpoint not complete; no main-file copy')
        db.close_local()
        try:
            connection.execute('SELECT 1')
        except sqlite3.ProgrammingError:
            pass
        else:
            raise RuntimeError('native connection did not close')
        clock_receipt = clock.receipt()
        if (not clock_receipt['all_targets_observed'] or not clock_receipt['all_calls_terminal']
                or len(clock_receipt['calls']) != 3
                or any(row['outcome'] != 'RETURNED' for row in clock_receipt['calls'])):
            raise RuntimeError('exact three native INITIAL imports not observed successfully')
    source_check(source, source_files)
    if guard.failed or failure_file.exists():
        raise RuntimeError('native private isolation failed, including swallowed errors')
    deadline(absolute_deadline)
    return {'run_id': run_id, 'mode': mode, 'pin': pin, 'producer': producer,
            'source_manifest_sha256': hashlib.sha256(json.dumps(
                source_files, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'size': size, 'records': records, 'products': products,
            'source_path_census': source_path_census,
            'native_imports': imports, 'native_connection': native_connection,
            'native_versions': versions, 'checkpoint': checkpoint, 'clock': clock_receipt,
            'guard_counts': dict(guard.counts), 'guard_failure_file': str(failure_file),
            'private_environment_keys': sorted(expected_env),
            'started_mono': began, 'ended_mono': time.monotonic(),
            'native_setup_wall_bracket_ns': [began_wall, time.time_ns()],
            'scope': 'unmeasured INITIAL fixture only; no independent reference/plan/readiness or process-exit/copy proof'}
