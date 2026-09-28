"""Observe Claude-owned Bash tasks, never adopt or signal arbitrary processes.

The native transcript is the evidence ledger. Cursor and projections commit
atomically; replay cannot revive terminal tasks. Silence is uncertainty, not
failure, and neither a tool request nor an output file is proof of liveness.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import time
from datetime import datetime

from . import background_jobs as jobs, db
from .janitor_context import redact
from .log import log_exception

SCHEMA = """CREATE TABLE IF NOT EXISTS provider_job_cursors (
 agent_id TEXT NOT NULL, native_id TEXT NOT NULL, path TEXT NOT NULL,
 file_identity TEXT NOT NULL, position INTEGER NOT NULL,
 discarding INTEGER NOT NULL DEFAULT 0,
 action_offset INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(agent_id,native_id))"""
MAX_BYTES = 256 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_ACTIONS = 64
PARSE_BUDGET_SEC = 0.020
STALE_MS = 600_000
_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_NOTICE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
_SECRET = re.compile(r"(?i)((?:[\w-]*(?:token|password|secret|api[_-]?key))[\"']?\s*[:=]\s*[\"']?)[^\s\"';,]+")


def safe_text(text: str) -> str:
    return _SECRET.sub(r"\1[redacted]", redact(text))


def job_id(agent_id: str, native_id: str, tool_id: str) -> str:
    digest = hashlib.sha256(f"{agent_id}\0{native_id}\0{tool_id}".encode()).hexdigest()
    return "claude-task:" + digest


def _stamp(record: dict) -> int | None:
    try:
        value = record.get('timestamp')
        if not isinstance(value, str):
            return None
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _field(body: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", body, re.S)
    return match.group(1).strip() if match else ''


def _write(c, owner: dict, native: str, tool: str, at: int, *, state: str,
           title: str = '', task: str = '', project: str = '', exit_code=None) -> None:
    if not isinstance(tool, str) or not _ID.fullmatch(tool):
        return
    ident = job_id(owner['agent_id'], native, tool)
    jobs.observe_native_task(c, owner, ident, native=native, tool=tool, at=at,
                             provider='claude', state=state, title=safe_text(title),
                             task=task, project=project, exit_code=exit_code,
                             evidence_stale=db.now_ms() - at > STALE_MS)


def parse_actions(native: str, project: str, record: dict) -> list[dict]:
    """Parse/redact native data outside the SQLite writer transaction."""
    actions = []
    def emit(tool, at, **fields):
        if 'title' in fields:
            fields['title'] = safe_text(fields['title'])[:120]
        actions.append({'tool': tool, 'at': at, **fields})
    if record.get('sessionId', native) != native or record.get('isSidechain'):
        return actions
    at = _stamp(record)
    if at is None or at > db.now_ms() + 60_000:
        return actions
    message = record.get('message')
    content = message.get('content', []) if isinstance(message, dict) else []
    blocks = content if isinstance(content, list) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if record.get('type') == 'assistant' and block.get('type') == 'tool_use' and block.get('name') == 'Bash':
            inp = block.get('input') or {}
            if not isinstance(inp, dict):
                continue
            if inp.get('run_in_background') is True:
                emit(block.get('id', ''), at, state='launching',
                       title=str(inp.get('description') or ''), project=project)
        elif record.get('type') == 'user' and block.get('type') == 'tool_result':
            tool = block.get('tool_use_id', '')
            if block.get('is_error'):
                emit(tool, at, state='failed')
                continue
            meta = record.get('toolUseResult') or {}
            if not isinstance(meta, dict):
                continue
            task = meta.get('backgroundTaskId', '')
            if isinstance(task, str) and _ID.fullmatch(task):
                emit(tool, at, state='running', task=task)
            # A request can complete synchronously despite background=true.
            elif type(meta.get('exitCode')) is int:
                emit(tool, at,
                       state='completed' if meta['exitCode'] == 0 else 'failed', exit_code=meta['exitCode'])
    # Only native queue envelopes; arbitrary assistant/user text is not evidence.
    if record.get('type') != 'queue-operation' or record.get('operation') not in (None, 'enqueue'):
        return actions
    text = record.get('content')
    if not isinstance(text, str):
        return actions
    for match in _NOTICE.finditer(text):
        body = match.group(1)
        state = _field(body, 'status')
        task, tool = _field(body, 'task-id'), _field(body, 'tool-use-id')
        if state not in {'completed', 'failed', 'stopped', 'killed'} or not _ID.fullmatch(task):
            continue
        emit(tool, at, state='stopped' if state == 'killed' else state,
             task=task, require_receipt=True)
    return actions


def apply_actions(c, owner: dict, native: str, actions: list[dict]) -> None:
    for action in actions:
        fields = dict(action)
        if fields.pop('require_receipt', False):
            row = c.execute('SELECT metadata_json FROM background_jobs WHERE job_id=?',
                            (job_id(owner['agent_id'], native, fields['tool']),)).fetchone()
            if not row or json.loads(row[0]).get('provider_task_id') != fields['task']:
                continue
        _write(c, owner, native, **fields)


def ingest(c, owner: dict, native: str, project: str, record: dict) -> None:
    """Compatibility for single-record fixtures; the observer pre-parses batches."""
    apply_actions(c, owner, native, parse_actions(native, project, record))


def observe(owner: dict, native: str, path: pathlib.Path, *, ended: bool = False) -> bool:
    """Read/parse outside the writer lock; atomically apply a bounded batch.

    A concurrent observer advancing the cursor wins. This batch is discarded
    rather than replaying stale parsed data over its position.
    """
    if not _ID.fullmatch(native) or path.stem != native:
        return
    c = db.conn()
    if pending_reconciliation(c, owner, native, ended=ended, limit=1):
        reconcile_if_needed(owner, native, ended=ended)
        return True  # Existing stale evidence takes this visit; replay resumes next tick.
    cursor = c.execute('SELECT * FROM provider_job_cursors WHERE agent_id=? AND native_id=?',
                       (owner['agent_id'], native)).fetchone()
    before = dict(cursor) if cursor else None
    with path.open('rb') as f:
        stat = os.fstat(f.fileno())
        identity = f'{stat.st_dev}:{stat.st_ino}'
        if (cursor and cursor['file_identity'] == identity and cursor['path'] == str(path)
                and cursor['position'] == stat.st_size):
            return reconcile_if_needed(owner, native, ended=ended)
        pos = int(cursor['position']) if cursor and cursor['file_identity'] == identity and cursor['path'] == str(path) else 0
        if pos > stat.st_size:
            pos = 0
        discarding = bool(cursor and cursor['discarding'] and pos)
        action_offset = int(cursor['action_offset']) if cursor and pos == cursor['position'] and cursor['file_identity'] == identity else 0
        f.seek(pos)
        consumed, actions = 0, []
        deadline = time.monotonic() + PARSE_BUDGET_SEC
        while consumed < MAX_BYTES and len(actions) < MAX_ACTIONS:
            line = f.readline(MAX_RECORD_BYTES + 1)
            if not line:
                break
            if discarding or len(line) > MAX_RECORD_BYTES:
                discarding = not line.endswith(b'\n')
                consumed += len(line)
                pos += len(line)
                action_offset = 0
                break
            if not line.endswith(b'\n'):
                break
            consumed += len(line)
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                record = None
            parsed = parse_actions(native, path.parent.name, record) if isinstance(record, dict) else []
            remaining = MAX_ACTIONS - len(actions)
            tail = parsed[action_offset:]
            actions.extend(tail[:remaining])
            if len(tail) > remaining:
                action_offset += remaining
                break  # Persist the within-record cursor; no actions are dropped.
            action_offset = 0
            pos += len(line)
            if time.monotonic() >= deadline:
                break
    if not consumed:
        return
    latest = path.stat()
    if (latest.st_dev, latest.st_ino) != (stat.st_dev, stat.st_ino) or latest.st_size < pos:
        return
    c.execute('BEGIN IMMEDIATE')
    try:
        current = c.execute('SELECT * FROM provider_job_cursors WHERE agent_id=? AND native_id=?',
                            (owner['agent_id'], native)).fetchone()
        if (dict(current) if current else None) != before:
            c.execute('ROLLBACK')
            return
        apply_actions(c, owner, native, actions)
        c.execute('''INSERT INTO provider_job_cursors VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(agent_id,native_id) DO UPDATE SET path=excluded.path,
            file_identity=excluded.file_identity,position=excluded.position,discarding=excluded.discarding,action_offset=excluded.action_offset''',
            (owner['agent_id'], native, str(path), identity, pos, int(discarding), action_offset))
        if pos >= stat.st_size:
            reconcile(c, owner, native, ended=ended, limit=MAX_ACTIONS - len(actions))
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise
    return pos < stat.st_size or bool(pending_reconciliation(c, owner, native, ended=ended, limit=1))


def pending_reconciliation(c, owner: dict, native: str, *, ended: bool, limit: int):
    return c.execute("""SELECT * FROM background_jobs
        WHERE agent_id=? AND heartbeat_source='provider_event' AND terminal_at IS NULL
        AND json_extract(metadata_json,'$.native_session_id')=?
        AND json_extract(metadata_json,'$.provider_state')!='unknown'
        AND (? OR ? - json_extract(metadata_json,'$.provider_observed_at') > ?)
        ORDER BY job_id LIMIT ?""",
        (owner['agent_id'], native, int(ended), db.now_ms(), STALE_MS, limit)).fetchall()


def reconcile(c, owner: dict, native: str, *, ended: bool, limit: int = MAX_ACTIONS) -> None:
    for row in pending_reconciliation(c, owner, native, ended=ended, limit=limit):
        meta = json.loads(row['metadata_json'])
        _write(c, owner, native, meta['tool_use_id'], meta['provider_observed_at'], state='unknown')


def reconcile_if_needed(owner: dict, native: str, *, ended: bool) -> bool:
    """An idle observer does not acquire a SQLite write lock or fake activity."""
    c = db.conn()
    if not pending_reconciliation(c, owner, native, ended=ended, limit=1):
        return False
    c.execute('BEGIN IMMEDIATE')
    try:
        reconcile(c, owner, native, ended=ended)
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise

    return bool(pending_reconciliation(c, owner, native, ended=ended, limit=1))


class ProviderJobObserver:
    """Fair, bounded visits; cursor belongs to the watcher instance, not a global."""
    MAX_TRANSCRIPTS = 2
    BUDGET_SEC = 0.050

    def __init__(self):
        self.after = ('', '')
        self.continue_soon = False
        self._sweep_backlog = False

    def poll_once(self) -> None:
        from . import backends
        rows = db.conn().execute('''SELECT a.agent_id,a.session,r.backend_session_id,
            MIN(r.started_at) AS bound_at,
            MAX(CASE WHEN r.ended_at IS NULL THEN 1 ELSE 0 END) AS live
            FROM agents a JOIN runtimes r ON r.agent_id=a.agent_id
            WHERE (a.backend='claude' OR EXISTS (SELECT 1 FROM provider_job_cursors pc
              WHERE pc.agent_id=a.agent_id AND pc.native_id=r.backend_session_id))
            AND r.backend_session_id!=''
            AND (r.ended_at IS NULL OR r.ended_at>? OR EXISTS (
              SELECT 1 FROM background_jobs j WHERE j.agent_id=a.agent_id
              AND j.kind='provider-task' AND j.terminal_at IS NULL))
            GROUP BY a.agent_id,r.backend_session_id ORDER BY a.agent_id,r.backend_session_id''', (db.now_ms() - 86_400_000,)).fetchall()
        ordered = [r for r in rows if (r['agent_id'], r['backend_session_id']) > self.after]
        ordered = ordered or rows
        self.continue_soon = False
        selected = ordered[:self.MAX_TRANSCRIPTS]
        deadline = time.monotonic() + self.BUDGET_SEC
        for row in selected:
            if time.monotonic() >= deadline:
                break
            self.after = (row['agent_id'], row['backend_session_id'])
            owner = dict(row)
            native = row['backend_session_id']
            try:
                path = backends.by_id('claude').find_transcript(native)
                if path:
                    pending = observe(owner, native, pathlib.Path(path), ended=not row['live'])
                    self._sweep_backlog |= bool(pending)
                else:
                    self._sweep_backlog |= reconcile_if_needed(owner, native, ended=not row['live'])
            except Exception as exc:
                log_exception('providerBackgroundObservationFail', exc)

        if rows:
            round_complete = self.after == (rows[-1]['agent_id'], rows[-1]['backend_session_id'])
            self.continue_soon = not round_complete or self._sweep_backlog
            if round_complete:
                self._sweep_backlog = False


def poll_once() -> None:
    """One bounded visit for standalone callers. Watchers retain an observer."""
    ProviderJobObserver().poll_once()


def output(job: dict) -> dict:
    """Derive the exact task output path; never trust a tool's arbitrary path."""
    meta = job['metadata']
    project, native, task = (meta.get(k, '') for k in
                             ('provider_project', 'native_session_id', 'provider_task_id'))
    if not all(isinstance(v, str) and _ID.fullmatch(v) for v in (project, native, task)):
        return jobs.read_log_tail('', [])
    root = pathlib.Path('/tmp') / f'claude-{os.getuid()}' / project / native / 'tasks'
    path = root / f'{task}.output'
    # Do not let symlinks expand the allowed root to an unrelated directory.
    if root.resolve() != root:
        return {'path': '', 'available': False, 'reason': 'outside_allowed_roots',
                'text': '', 'size': 0, 'truncated': False}
    result = jobs.read_log_tail(str(path), [str(root)])
    result['text'] = safe_text(result['text'])
    return result


def registered_mirror(job: dict) -> str:
    """Deduplicate only an explicit exact identity link, never title/command similarity."""
    meta = job.get('metadata') or {}
    if not isinstance(meta, dict):
        return ''
    if not meta.get('provider') or not meta.get('tool_use_id'):
        return ''
    for row in db.conn().execute("SELECT metadata_json,job_id FROM background_jobs WHERE agent_id=? AND heartbeat_source!='provider_event'", (job['agent_id'],)):
        try:
            other = json.loads(row[0])
        except (ValueError, TypeError):
            continue
        if not isinstance(other, dict):
            continue
        if all(other.get(key) == meta.get(key) for key in ('provider', 'native_session_id', 'tool_use_id')):
            return str(row['job_id'])
    return ''


def mirrors_registered(job: dict) -> bool:
    return bool(registered_mirror(job))
