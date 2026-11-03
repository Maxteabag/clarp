"""Run one scenario against one build and capture what it produced.

A capture holds the raw observables of one run: every HTTP exchange the
scenario made (status, contract headers, body), every SSE frame the Host sent
(event name, id presence, data), the database (schema, primary keys, rows),
the files under the state directory (JSON parsed, anything else by digest)
and any CLI output. Captures stay in the harness's scratch directory;
``normalize`` and ``diff`` turn them into the report, which prints structure
and codes only.

The cut is frozen: at quiescence the SSE reader is stopped and joined, the
Host's process group is killed, and only then are the database (one read
transaction) and the files read, so no writer runs during the dump.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
TOKEN = 'replay-host-token'
READY_TIMEOUT_S = 30
WAIT_TIMEOUT_S = 15
QUIET_S = 1.0
OWNED = '.clarp-replay-owned'
# Files the Host writes for its own diagnostics or that the capture reads separately.
SKIP_PREFIXES = ('logs/', 'home/.cache/')
SKIP_NAMES = {'host.json', OWNED, 'state.sqlite', 'state.sqlite-wal', 'state.sqlite-shm',
              'telemetry.sqlite', 'telemetry.sqlite-wal', 'telemetry.sqlite-shm'}
# Response headers that belong to the HTTP contract (the rest are server/date noise).
CONTRACT_HEADERS = ('content-type', 'location', 'content-range', 'accept-ranges', 'content-encoding',
                    'cache-control', 'content-disposition', 'etag', 'www-authenticate', 'retry-after')
# The child gets this environment and nothing it was not given (review finding 4);
# host.py then points every home, data, config and provider directory into the state dir.
CHILD_ENV_KEEP = ('LANG', 'LC_ALL', 'TZ')
SYSTEM_PATH = '/usr/local/bin:/usr/bin:/bin'


class StepFailed(AssertionError):
    pass


def child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in CHILD_ENV_KEEP if k in os.environ}
    env.update({'PATH': SYSTEM_PATH, 'PYTHONHASHSEED': '0', 'PYTHONDONTWRITEBYTECODE': '1',
                'CLARP_QA_STREAM_DELAY': os.environ.get('CLARP_QA_STREAM_DELAY', '0.02')})
    return env


def fresh_root(root: Path) -> None:
    """Create a scenario root that did not exist. Nothing is ever deleted here: a
    root that exists belongs to someone (another job, an earlier run) and is refused."""
    if root.is_symlink() or root.exists():
        raise StepFailed('scenario root already exists; refusing to reuse it')
    root.mkdir(parents=False)


class Host:
    def __init__(self, build: Path, root: Path, python: str = sys.executable):
        self.build, self.root, self.python = build, root, python
        self.process = None
        self.log = None
        self.url = ''

    def start(self):
        """Spawn the Host; the caller owns cleanup (call stop() in a finally)."""
        (self.root / OWNED).touch()
        self.log = (self.root.parent / f'{self.root.name}.host.log').open('a')
        self.process = subprocess.Popen(
            [self.python, str(HERE / 'host.py'), '--build', str(self.build), '--state-dir', str(self.root)],
            stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=child_env(), cwd=str(self.root), start_new_session=True)
        ready = self.root / 'host.json'
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not ready.exists():
            if self.process.poll() is not None:
                raise StepFailed(f'host exited during start ({self.process.returncode})')
            if time.monotonic() > deadline:
                raise StepFailed('host did not become ready')
            time.sleep(0.02)
        try:
            self.url = json.loads(ready.read_text())['url']
        except (ValueError, KeyError) as exc:
            raise StepFailed('host metadata unreadable') from exc
        return self

    def stop(self):
        """Kill the Host's own process group (it leads one), which takes the fake
        provider processes with it. Graceful shutdown costs ~10 s and is not compared."""
        if self.process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def request(self, method: str, path: str, body=None, raw: dict | None = None, headers: dict | None = None):
        sent = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json', **(headers or {})}
        if raw is not None:
            data = raw['bytes'].encode()
            sent['Content-Type'] = raw['content_type']
        else:
            data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=sent)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, raw, headers = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            status, raw, headers = err.code, err.read(), err.headers
        kept = {h: headers.get(h) for h in CONTRACT_HEADERS if headers.get(h) is not None}
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = {'<bytes>': len(raw), '<sha256>': hashlib.sha256(raw).hexdigest()}
        return status, kept, payload


class SSE:
    """Collects complete SSE frames (event name, wire id value, data); heartbeats
    are dropped (pure timing). Transport or parse errors are recorded, never
    turned into quiet (review finding 7)."""

    def __init__(self, host: Host, sessions):
        self.frames: list[dict] = []
        self.errors: list[str] = []
        self.ended = False
        self.last_at = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        req = urllib.request.Request(host.url + '/events?live=' + ','.join(sessions),
                                     headers={'Authorization': f'Bearer {TOKEN}'})
        self._resp = urllib.request.urlopen(req, timeout=60)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _frame(self, event, wire_id, data_lines):
        if event == 'heartbeat' or not data_lines:
            return
        text = '\n'.join(data_lines)
        try:
            data = json.loads(text)
        except ValueError:
            self.errors.append('non-json data frame')
            data = {'<bytes>': len(text), '<sha256>': hashlib.sha256(text.encode()).hexdigest()}
        with self._lock:
            self.frames.append({'event': event, 'id': wire_id, 'data': data})
            self.last_at = time.monotonic()

    def _read(self):
        event, wire_id, data = None, None, []
        try:
            for raw in self._resp:
                line = raw.decode('utf-8').rstrip('\r\n')
                if not line:
                    self._frame(event, wire_id, data)
                    event, wire_id, data = None, None, []
                elif line.startswith(':'):
                    continue
                else:
                    field, _, value = line.partition(':')
                    value = value[1:] if value.startswith(' ') else value
                    if field == 'event':
                        event = value
                    elif field == 'id':
                        wire_id = value  # the value itself (may be empty): Last-Event-ID depends on it
                    elif field == 'data':
                        data.append(value)
            if not self._stop.is_set():
                self.ended = True
        except Exception as exc:  # noqa: BLE001 - recorded as a capture error unless we closed it
            if not self._stop.is_set():
                self.errors.append(f'stream error {type(exc).__name__}')

    def close(self) -> list[dict]:
        self._stop.set()
        with contextlib.suppress(Exception):
            self._resp.close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            self.errors.append('reader did not stop')
        with self._lock:
            return list(self.frames)


def _ro(root: Path):
    return contextlib.closing(sqlite3.connect(f"file:{root / 'state.sqlite'}?mode=ro", uri=True, timeout=1))


def _poll(what: str, check, timeout=WAIT_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise StepFailed(f'timed out waiting for {what}')
        time.sleep(0.03)


def _turns_settled(root: Path, message_id: str, settled: bool):
    try:
        with _ro(root) as db:
            row = db.execute('SELECT trace_id FROM messages WHERE message_id = ?', (message_id,)).fetchone()
            if not row:
                return False
            turns = db.execute('SELECT settled_at FROM turns WHERE trace_id = ?', (row[0],)).fetchall()
    except sqlite3.Error:
        return False
    if not turns:
        return False
    return all(t[0] is not None for t in turns) if settled else (len(turns) == 1 and turns[0][0] is None)


def _provider_started(root: Path, marker: str):
    path = root / 'provider' / 'turn-requests.jsonl'
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        req = json.loads(line)
        if req.get('method') == 'turn/start' and marker in ''.join(
                i.get('text', '') for i in req.get('input', [])):
            return True
    return False


def run_step(host: Host, sse, step: dict, http: list):
    op = step['op']
    if op in ('get', 'post', 'put', 'delete'):
        body = step.get('body')
        path = step['path']
        if step.get('from_last'):
            # Fill {name} in the path from the previous response, e.g. a queue item id.
            last = http[-1]['body'] if http else None
            values = {}
            for name, dotted in step['from_last'].items():
                value = last
                for part in dotted.split('.'):
                    value = value[int(part)] if isinstance(value, list) else (value or {}).get(part)
                if value is None:
                    raise StepFailed(f'no {name} in the previous response')
                values[name] = urllib.parse.quote(str(value), safe='')
            path = path.format(**values)
        if step.get('body_from_last'):
            # Carry fields from the previous response, e.g. /transcribe's trace_id into /send.
            last = http[-1]['body'] if http and isinstance(http[-1]['body'], dict) else {}
            body = {**(body or {}), **{k: last.get(src) for k, src in step['body_from_last'].items()}}
        status, headers, payload = host.request(op.upper(), path, body, step.get('raw'),
                                                step.get('headers'))
        http.append({'step': step.get('name') or f"{op} {step['path']}", 'method': op.upper(),
                     'path': step['path'].split('?')[0], 'status': status, 'headers': headers,
                     'body': payload})
        expect = step.get('expect_status')
        if expect is not None and status != expect:
            raise StepFailed(f'unexpected status {status}')
    elif op == 'wait_settled':
        _poll('turn settled', lambda: _turns_settled(host.root, step['message_id'], True))
    elif op == 'wait_open':
        _poll('turn open', lambda: _turns_settled(host.root, step['message_id'], False))
    elif op == 'wait_provider_started':
        _poll('provider turn start', lambda: _provider_started(host.root, step['marker']))
    elif op == 'provider_file':
        # Control files the fake provider reads (tests/qa/fake_codex.py), e.g. quota-mode.
        (host.root / 'provider' / step['name']).write_text(step['text'])
    elif op == 'wait_state':
        # The agent's state_log has at least `count` rows of this kind, together
        # carrying each named detail key.
        def rows():
            try:
                with _ro(host.root) as db:
                    found = [json.loads(r[0] or '{}') for r in db.execute(
                        'SELECT s.detail FROM state_log s JOIN agents a ON a.agent_id = s.agent_id '
                        'WHERE a.session = ? AND s.kind = ?', (step['session'], step['kind']))]
            except (sqlite3.Error, ValueError):
                return False
            return (len(found) >= step.get('count', 1)
                    and all(any(key in d for d in found) for key in step.get('detail_keys', ())))
        _poll('state rows', rows)
    elif op == 'quiesce':
        _quiesce(host, sse, step.get('sessions', ('rachel',)))
    elif op == 'wait_busy':
        def busy():
            status = host.request('GET', f"/status?session={step['session']}")[2]
            return isinstance(status, dict) and status.get('busy') is step['busy']
        _poll('busy state', busy)
    else:
        raise StepFailed(f'unknown step op {op!r}')


def _state_tail(root: Path, session: str) -> str:
    """The agent's last state_log kinds (codes), for a failed wait."""
    try:
        with _ro(root) as db:
            kinds = [r[0] for r in db.execute(
                'SELECT s.kind FROM state_log s JOIN agents a ON a.agent_id = s.agent_id '
                'WHERE a.session = ? ORDER BY s.state_id DESC LIMIT 6', (session,))]
    except sqlite3.Error as exc:
        return f'state unreadable ({type(exc).__name__})'
    return 'last states ' + ','.join(reversed(kinds))


def _quiesce(host: Host, sse: SSE, sessions):
    for session in sessions:
        try:
            _poll('sessions idle',
                  lambda s=session: (host.request('GET', f'/status?session={s}')[2] or {}).get('busy') is False)
        except StepFailed as exc:
            raise StepFailed(f'{exc}; {_state_tail(host.root, session)}') from exc
    _poll('SSE quiet', lambda: time.monotonic() - sse.last_at >= QUIET_S)


def dump_db(path: Path) -> dict:
    """Schema, primary keys and all rows of every non-empty table, in one read transaction."""
    out = {'schema': [], 'pk': {}, 'tables': {}}
    if not path.exists():
        return out
    with contextlib.closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        schema = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        out['schema'] = [dict(r) for r in schema]
        for r in schema:
            if r['type'] != 'table':
                continue
            info = db.execute(f'PRAGMA table_info("{r["name"]}")').fetchall()
            out['pk'][r['name']] = [c['name'] for c in sorted(info, key=lambda c: c['pk']) if c['pk']]
            rows = db.execute(f'SELECT * FROM "{r["name"]}"').fetchall()
            if rows:
                out['tables'][r['name']] = [{k: (v.hex() if isinstance(v, bytes) else v)
                                             for k, v in dict(row).items()} for row in rows]
        db.execute('COMMIT')
    return out


def list_files(root: Path) -> list[dict]:
    files = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(SKIP_PREFIXES) or rel in SKIP_NAMES or rel.startswith('.host.json'):
            continue
        raw = path.read_bytes()
        entry = {'path': rel, 'bytes': len(raw), 'kind': 'bytes', 'digest': hashlib.sha256(raw).hexdigest()}
        if path.suffix not in ('.json', '.jsonl') and len(raw) <= 65536:
            try:
                entry.update(kind='text', text=raw.decode('utf-8'), digest=None)
            except UnicodeDecodeError:
                pass
        if path.suffix in ('.json', '.jsonl'):
            try:
                text = raw.decode()
                parsed = (json.loads(text) if path.suffix == '.json'
                          else [json.loads(line) for line in text.splitlines() if line.strip()])
                entry.update(kind='json', json=parsed, digest=None)
            except ValueError:
                pass
        files.append(entry)
    return files


def capture(build: Path, scenario: dict, root: Path, python: str = sys.executable) -> dict:
    """Run ``scenario`` on ``build`` in the new directory ``root`` (its parent must exist)."""
    fresh_root(root)
    started = time.monotonic()
    timings = {}
    host = Host(build, root, python)
    sse = None
    frames: list = []
    http: list = []
    error = None
    sessions = scenario.get('sessions', ('rachel',))
    db, files = {'schema': [], 'pk': {}, 'tables': {}}, []
    try:
        host.start()
        timings['start'] = round(time.monotonic() - started, 2)
        sse = SSE(host, sessions)
        _poll('SSE connected', lambda: sse.frames)
        for index, step in enumerate(scenario['steps']):
            try:
                run_step(host, sse, step, http)
            except StepFailed as exc:
                raise StepFailed(f'step {index} ({step["op"]}): {exc}') from exc
        timings['steps'] = round(time.monotonic() - started, 2)
        _quiesce(host, sse, sessions)
        timings['quiet'] = round(time.monotonic() - started, 2)
    except StepFailed as exc:
        error = str(exc)
    finally:
        if sse is not None:
            frames = sse.close()
        host.stop()
    # Dumped on a failed run too: the frozen state is the diagnostic (structure only in reports).
    db = dump_db(root / 'state.sqlite')
    files = list_files(root)
    health = {'errors': sse.errors if sse else ['not connected'], 'ended_early': bool(sse and sse.ended)}
    if error is None and (health['errors'] or health['ended_early']):
        error = 'SSE stream unhealthy: ' + ', '.join(health['errors'] + (['ended early'] if health['ended_early'] else []))
    timings['done'] = round(time.monotonic() - started, 2)
    return {
        'timings': timings,
        'scenario': scenario['id'],
        'error': error,
        'root': str(root),
        'build': str(build),
        'http': http,
        'sse': frames,
        'sse_health': health,
        'db': db,
        'files': files,
    }
