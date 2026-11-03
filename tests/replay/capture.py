"""Run one scenario against one build and capture what it produced.

A capture holds the raw observables of one run: every HTTP exchange the
scenario made, every SSE event the Host sent, the database (schema and rows),
the files under the state directory and any CLI output. Captures stay in the
harness's scratch directory; ``normalize`` and ``diff`` turn them into the
report, which prints structure and codes only.
"""
from __future__ import annotations

import contextlib
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
import urllib.request

HERE = Path(__file__).resolve().parent
TOKEN = 'replay-host-token'
READY_TIMEOUT_S = 30
WAIT_TIMEOUT_S = 15
QUIET_S = 0.4
# Files the Host writes for its own diagnostics: their content is timing, not behaviour.
SKIP_FILES = ('logs/', 'host.json', '.clarp-replay-owned', 'state.sqlite-', 'telemetry.sqlite',
              'host.log', 'home/.cache/')


class StepFailed(AssertionError):
    pass


class Host:
    def __init__(self, build: Path, root: Path, python: str = sys.executable):
        self.build, self.root, self.python = build, root, python
        self.process = None
        self.url = ''

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = (self.root.parent / f'{self.root.name}.host.log').open('a')
        self.process = subprocess.Popen(
            [self.python, str(HERE / 'host.py'), '--build', str(self.build), '--state-dir', str(self.root)],
            stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
        ready = self.root / 'host.json'
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not ready.exists():
            if self.process.poll() is not None:
                raise StepFailed(f'replay host exited during start: {self.process.returncode}')
            if time.monotonic() > deadline:
                self.stop()
                raise StepFailed('replay host did not become ready')
            time.sleep(0.02)
        self.url = json.loads(ready.read_text())['url']
        return self

    def stop(self):
        """Kill the host's own process group (it leads one: start_new_session),
        which takes the fake provider processes with it. The capture has already
        read everything it needs; a graceful shutdown costs ~10 s per host."""
        if self.process is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=5)
        self.log.close()
        self.process = None

    def request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, raw, ctype = resp.status, resp.read(), resp.headers.get('Content-Type', '')
        except urllib.error.HTTPError as err:
            status, raw, ctype = err.code, err.read(), err.headers.get('Content-Type', '')
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = {'<non-json>': {'content_type': ctype.split(';')[0], 'bytes': len(raw)}}
        return status, payload


class SSE:
    """Collects the Host's SSE stream; heartbeats are dropped (pure timing)."""

    def __init__(self, host: Host, sessions):
        self.events: list[dict] = []
        self.last_at = time.monotonic()
        self._stop = threading.Event()
        req = urllib.request.Request(host.url + '/events?live=' + ','.join(sessions), headers={'Authorization': f'Bearer {TOKEN}'})
        self._resp = urllib.request.urlopen(req, timeout=60)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        kind = None
        with contextlib.suppress(Exception):
            for raw in self._resp:
                line = raw.decode('utf-8', 'replace').rstrip('\n')
                if line.startswith('event:'):
                    kind = line[6:].strip()
                elif line.startswith('data:'):
                    if kind != 'heartbeat':
                        with contextlib.suppress(ValueError):
                            self.events.append(json.loads(line[5:].strip()))
                            self.last_at = time.monotonic()
                elif not line:
                    kind = None
                if self._stop.is_set():
                    return

    def close(self):
        self._stop.set()
        with contextlib.suppress(Exception):
            self._resp.close()


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


def run_step(host: Host, sse: SSE, step: dict, http: list):
    op = step['op']
    if op in ('get', 'post', 'put', 'delete'):
        status, payload = host.request(op.upper(), step['path'], step.get('body'))
        http.append({'step': step.get('name') or f"{op} {step['path']}", 'method': op.upper(),
                     'path': step['path'].split('?')[0], 'status': status, 'body': payload})
        expect = step.get('expect_status')
        if expect is not None and status != expect:
            raise StepFailed(f"{op} {step['path']}: status {status}, expected {expect}")
    elif op == 'wait_settled':
        _poll(f"turn of {step['message_id']} settled",
              lambda: _turns_settled(host.root, step['message_id'], True))
    elif op == 'wait_open':
        _poll(f"turn of {step['message_id']} open",
              lambda: _turns_settled(host.root, step['message_id'], False))
    elif op == 'wait_provider_started':
        _poll('provider turn start', lambda: _provider_started(host.root, step['marker']))
    elif op == 'wait_busy':
        def busy():
            _, status = host.request('GET', f"/status?session={step['session']}")
            return isinstance(status, dict) and status.get('busy') is step['busy']
        _poll(f"{step['session']} busy={step['busy']}", busy)
    else:
        raise StepFailed(f'unknown step op {op!r}')


def _quiesce(host: Host, sse: SSE, sessions):
    for session in sessions:
        _poll(f'{session} idle', lambda s=session: host.request('GET', f'/status?session={s}')[1].get('busy') is False)
    _poll('SSE quiet', lambda: time.monotonic() - sse.last_at >= QUIET_S)


def dump_db(path: Path) -> dict:
    out = {'schema': [], 'tables': {}}
    if not path.exists():
        return out
    with contextlib.closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        schema = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        out['schema'] = [dict(r) for r in schema]
        for r in schema:
            if r['type'] != 'table':
                continue
            rows = db.execute(f'SELECT * FROM "{r["name"]}"').fetchall()
            if rows:
                out['tables'][r['name']] = [{k: (v.hex() if isinstance(v, bytes) else v) for k, v in dict(row).items()}
                                            for row in rows]
    return out


def list_files(root: Path) -> list[dict]:
    files = []
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(SKIP_FILES) or rel in ('state.sqlite',):
            continue
        files.append({'path': rel, 'bytes': path.stat().st_size})
    return files


def capture(build: Path, scenario: dict, scratch: Path, python: str = sys.executable) -> dict:
    root = scratch / scenario['id']
    if root.exists():
        shutil.rmtree(root)
    started = time.monotonic()
    timings = {}
    host = Host(build, root, python).start()
    timings['start'] = round(time.monotonic() - started, 2)
    sse = None
    http: list = []
    error = None
    sessions = scenario.get('sessions', ('rachel',))
    db, files = {'schema': [], 'tables': {}}, []
    try:
        sse = SSE(host, sessions)
        _poll('SSE connected', lambda: sse.events)
        for step in scenario['steps']:
            run_step(host, sse, step, http)
        timings['steps'] = round(time.monotonic() - started, 2)
        _quiesce(host, sse, sessions)
        timings['quiet'] = round(time.monotonic() - started, 2)
        db = dump_db(root / 'state.sqlite')
        files = list_files(root)
    except StepFailed as exc:
        error = str(exc)
    finally:
        if sse is not None:
            sse.close()
        host.stop()
    timings['stopped'] = round(time.monotonic() - started, 2)
    return {
        'timings': timings,
        'scenario': scenario['id'],
        'error': error,
        'seconds': round(time.monotonic() - started, 2),
        'root': str(root),
        'build': str(build),
        'http': http,
        'sse': sse.events if sse else [],
        'db': db,
        'files': files,
    }
