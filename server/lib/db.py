"""SQLite-backed source of truth for agent/runtime/turn/clip state.

Replaces the previous file-marker state and ~/.config/clarp/agents.json
with a single ACID store at ~/.local/share/clarp/state.sqlite.

Schema is created on first open. user_version drives migrations: to change the
schema, edit _SCHEMA_SQL, bump _SCHEMA_VERSION, and add a `_migrate_to_vN`
that upgrades an existing database (see `_migrate`).

The hooks and the server both use this module — they share the file via
WAL mode + a short busy_timeout. Concurrent writes serialise without
losing rows.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import dataclass

from .timing import SQLITE_BUSY_TIMEOUT_MS, SQLITE_CONNECT_TIMEOUT_SEC
from . import xdg


DB_PATH = pathlib.Path(os.environ.get(
    "CLAUDE_PWA_DB",
    str(xdg.data_dir() / "state.sqlite"),
))

_LOCAL = threading.local()  # per-thread connection store
_CONN_LOCK = threading.Lock()
_MIGRATED = False
_SCHEMA_VERSION = 63

_LOCK_REPORT_INTERVAL_SEC = 30.0
_TRANSACTION_LOCK = threading.Lock()


@dataclass(frozen=True)
class _TransactionOwner:
    connection_id: int
    thread_id: int
    thread_name: str
    started_monotonic: float
    begin_sql: str
    last_sql: str
    stack: tuple[str, ...]


_TRANSACTION_OWNERS: dict[int, _TransactionOwner] = {}
_LAST_LOCK_REPORT_AT = 0.0


def begin_request_metrics(*, enabled: bool) -> None:
    _LOCAL.request_metrics = ({"query_count": 0, "sqlite_ms": 0.0,
                               "max_query_ms": 0.0, "max_query": "",
                               "query_templates": {}}
                              if enabled else None)


def finish_request_metrics() -> dict:
    value = getattr(_LOCAL, "request_metrics", None)
    _LOCAL.request_metrics = None
    if not value:
        return {}
    templates = value.pop("query_templates", {})
    repeated = sorted(
        ({"count": count, "sql": sql} for sql, count in templates.items()
         if count >= 5), key=lambda row: row["count"], reverse=True)[:5]
    if repeated:
        value["repeated_queries"] = repeated
    return dict(value)


def _record_query_metric(sql: object, started: float | None) -> None:
    metrics = getattr(_LOCAL, "request_metrics", None)
    if metrics is None or started is None:
        return
    duration = (time.perf_counter() - started) * 1000
    metrics["query_count"] += 1
    metrics["sqlite_ms"] += duration
    if duration > metrics["max_query_ms"]:
        metrics["max_query_ms"] = duration
        metrics["max_query"] = _sql_template(sql)
    templates = metrics["query_templates"]
    template = _sql_template(sql)
    if template in templates or len(templates) < 128:
        templates[template] = templates.get(template, 0) + 1


def _sql_template(sql: object) -> str:
    """Return a bounded SQL template without ever including bound values."""
    compact = " ".join(str(sql).split())
    return compact[:240]


def _record_transaction_begin(connection: sqlite3.Connection, sql: object) -> None:
    owner = _TransactionOwner(
        connection_id=id(connection),
        thread_id=threading.get_ident(),
        thread_name=threading.current_thread().name,
        started_monotonic=time.monotonic(),
        begin_sql=_sql_template(sql),
        last_sql=_sql_template(sql),
        stack=tuple(traceback.format_stack(limit=14)[:-1]),
    )
    with _TRANSACTION_LOCK:
        _TRANSACTION_OWNERS[id(connection)] = owner


def _record_transaction_statement(connection: sqlite3.Connection, sql: object) -> None:
    with _TRANSACTION_LOCK:
        owner = _TRANSACTION_OWNERS.get(id(connection))
        if owner is not None:
            _TRANSACTION_OWNERS[id(connection)] = _TransactionOwner(
                **{**owner.__dict__, "last_sql": _sql_template(sql)}
            )


def _clear_transaction_owner(connection: sqlite3.Connection) -> None:
    with _TRANSACTION_LOCK:
        _TRANSACTION_OWNERS.pop(id(connection), None)


def _report_database_locked(waiting_sql: object) -> None:
    """Log transaction owners to stderr without touching SQLite recursively."""
    global _LAST_LOCK_REPORT_AT
    now = time.monotonic()
    waiting = _sql_template(waiting_sql)
    with _TRANSACTION_LOCK:
        owners = tuple(_TRANSACTION_OWNERS.values())
        if now - _LAST_LOCK_REPORT_AT < _LOCK_REPORT_INTERVAL_SEC:
            return
        _LAST_LOCK_REPORT_AT = now
    waiter = threading.current_thread()
    print(
        "sqlite_lock_wait "
        f"waiter_thread={waiter.name!r} waiter_ident={threading.get_ident()} "
        f"sql={waiting!r} owner_count={len(owners)}",
        file=sys.stderr,
        flush=True,
    )
    for owner in owners:
        stack = "".join(owner.stack).replace("\n", "\\n")
        print(
            "sqlite_lock_owner "
            f"connection={owner.connection_id} thread={owner.thread_name!r} "
            f"thread_ident={owner.thread_id} age_sec={now - owner.started_monotonic:.3f} "
            f"begin_sql={owner.begin_sql!r} last_sql={owner.last_sql!r} "
            f"stack={stack!r}",
            file=sys.stderr,
            flush=True,
        )


class _TrackedConnection(sqlite3.Connection):
    """Connection that records explicit transaction ownership for diagnostics."""

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        template = _sql_template(sql)
        measured = getattr(_LOCAL, "request_metrics", None) is not None
        started = time.perf_counter() if measured else None
        try:
            cursor = super().execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                _report_database_locked(template)
            raise
        finally:
            _record_query_metric(template, started)
        verb = template.partition(" ")[0].upper()
        if verb == "BEGIN" and self.in_transaction:
            _record_transaction_begin(self, template)
        elif verb in {"COMMIT", "ROLLBACK", "END"}:
            if not self.in_transaction:
                _clear_transaction_owner(self)
        elif self.in_transaction:
            _record_transaction_statement(self, template)
        return cursor

    def executemany(self, sql, seq_of_parameters, /):  # type: ignore[override]
        template = _sql_template(sql)
        measured = getattr(_LOCAL, "request_metrics", None) is not None
        started = time.perf_counter() if measured else None
        try:
            cursor = super().executemany(sql, seq_of_parameters)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                _report_database_locked(template)
            raise
        finally:
            _record_query_metric(template, started)
        if self.in_transaction:
            _record_transaction_statement(self, template)
        return cursor

    def executescript(self, sql_script, /):  # type: ignore[override]
        template = _sql_template(sql_script)
        try:
            cursor = super().executescript(sql_script)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                _report_database_locked(template)
            raise
        if not self.in_transaction:
            _clear_transaction_owner(self)
        return cursor

    def commit(self):  # type: ignore[override]
        try:
            return super().commit()
        finally:
            if not self.in_transaction:
                _clear_transaction_owner(self)

    def rollback(self):  # type: ignore[override]
        try:
            return super().rollback()
        finally:
            if not self.in_transaction:
                _clear_transaction_owner(self)

    def close(self):  # type: ignore[override]
        _clear_transaction_owner(self)
        return super().close()


def _open_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=SQLITE_CONNECT_TIMEOUT_SEC,
                          isolation_level=None,
                          check_same_thread=False,
                          factory=_TrackedConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return con


def conn() -> sqlite3.Connection:
    """Return this thread's sqlite connection.

    ThreadingHTTPServer runs each request on its own thread. A single shared
    sqlite3.Connection is NOT safe for concurrent use across threads — two
    threads stepping on the same cursor/statement raise "bad parameter or
    other API misuse" and make `UPDATE ... RETURNING` + fetchone() return None
    (→ int(None) crashes in the message store). check_same_thread=False only
    silences the guard; it doesn't make concurrent use safe. So each thread
    gets its own connection.

    Request threads close theirs via `close_local()` at request end (see
    Handler.finish) so short-lived threads don't leak FDs — the bug that made
    a previous thread-local attempt regress. The handful of long-lived worker
    threads keep one connection each for the process lifetime.
    """
    global _MIGRATED
    c = getattr(_LOCAL, "conn", None)
    if c is not None:
        return c
    c = _open_connection()
    # Migrate exactly once per process (idempotent + serialized). WAL means
    # every per-thread connection sees the migrated schema on the shared file.
    with _CONN_LOCK:
        if not _MIGRATED:
            _migrate(c)
            _MIGRATED = True
    _LOCAL.conn = c
    return c


def close_local() -> None:
    """Close and drop this thread's connection. Call at request end so a
    short-lived request thread releases its FDs instead of leaking them."""
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        return
    try:
        c.close()
    except sqlite3.Error:
        pass
    _LOCAL.conn = None


# The schema below is the complete current shape. It is applied in one step to
# a fresh database. `_migrate_to_vN` functions upgrade an existing database one
# version at a time; each one is the single place that knows how to move a
# v(N-1) database to vN and stays here until the version it upgrades from is no
# longer supported. Databases older than _MIN_UPGRADABLE_VERSION predate the
# public release and are refused instead of being dragged through pre-release
# history.
_MIN_UPGRADABLE_VERSION = 61

_SCHEMA_SQL = """
CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    persona TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    session TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER,
    backend TEXT NOT NULL DEFAULT 'claude',
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    mcp_servers TEXT NOT NULL DEFAULT '[]',
    heartbeat_enabled INTEGER NOT NULL DEFAULT 0,
    dreaming_enabled INTEGER NOT NULL DEFAULT 0,
    dreaming_last_local_date TEXT,
    muted INTEGER NOT NULL DEFAULT 0,
    custom_status TEXT NOT NULL DEFAULT '',
    avatar_symbol TEXT NOT NULL DEFAULT '',
    personality TEXT NOT NULL DEFAULT '',
    avatar_path TEXT NOT NULL DEFAULT '',
    archived_at INTEGER
);

CREATE TABLE runtimes (
    runtime_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    backend_session_id TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE INDEX idx_runtimes_live_session ON runtimes(session) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX idx_runtimes_live_backend_session_unique ON runtimes(backend_session_id) WHERE ended_at IS NULL AND backend_session_id IS NOT NULL AND backend_session_id != '';
CREATE INDEX idx_runtimes_live_backend_session ON runtimes(backend_session_id) WHERE ended_at IS NULL;

CREATE TABLE turns (
    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    runtime_id INTEGER,
    source TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    synthesize_audio INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_turns_agent_ts ON turns(agent_id, started_at);

CREATE TABLE turn_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    backend TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    trace_id TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    duration_ms INTEGER,
    at INTEGER NOT NULL
);
CREATE INDEX idx_turn_usage_backend_at ON turn_usage(backend, at);

CREATE TABLE clips (
    clip_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    runtime_id INTEGER,
    turn_id INTEGER,
    path TEXT NOT NULL UNIQUE,
    voice_id TEXT,
    bytes INTEGER,
    trace_id TEXT,
    created_at INTEGER NOT NULL,
    status TEXT,
    broadcast_at INTEGER,
    queued_at INTEGER,
    play_started_at INTEGER,
    played_at INTEGER,
    error TEXT,
    producer_status TEXT,
    completed_at INTEGER
);
CREATE INDEX idx_clips_agent_ts ON clips(agent_id, created_at);

CREATE TABLE state_log (
    state_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    runtime_id INTEGER,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX idx_state_log_agent_ts ON state_log(agent_id, ts DESC);

CREATE TABLE focus (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
    agent_id TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE traces (
    agent_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE cursor_positions (
    backend_session_id TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    spoken_first INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE tts_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    text TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    session TEXT NOT NULL,
    source TEXT NOT NULL,
    trace_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    enqueued_at INTEGER NOT NULL,
    claimed_at INTEGER,
    completed_at INTEGER,
    clip_id INTEGER
);
CREATE INDEX idx_tts_queue_pending ON tts_queue(enqueued_at) WHERE status = 'queued';
CREATE INDEX idx_tts_queue_recent ON tts_queue(enqueued_at);

CREATE TABLE sse_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    type TEXT NOT NULL,
    session TEXT,
    agent_id TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX idx_sse_events_event_id ON sse_events(event_id);
CREATE INDEX idx_sse_events_ts ON sse_events(ts);

CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    backend_session_id TEXT,
    source_file TEXT,
    seq INTEGER NOT NULL,
    role TEXT,
    timestamp TEXT,
    text TEXT NOT NULL,
    kind TEXT,
    tool_name TEXT,
    tools_json TEXT NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    display_cells_json TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'user',
    sender_agent_id TEXT,
    prompt_admission_id TEXT,
    UNIQUE(agent_id, backend_session_id, seq)
);
CREATE INDEX idx_messages_agent_seq ON messages(agent_id, backend_session_id, seq);
CREATE INDEX idx_messages_agent_timestamp ON messages(agent_id, timestamp);
CREATE INDEX idx_messages_agent_revision ON messages(agent_id, backend_session_id, revision);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE message_clock (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
    revision INTEGER NOT NULL
);

CREATE TABLE conversation_heads (
    agent_id TEXT NOT NULL,
    backend_session_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    replace_revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, backend_session_id)
);

CREATE TABLE orchestrator_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    utterance TEXT NOT NULL,
    requested_session TEXT,
    hands_free INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    model TEXT,
    effort TEXT,
    latency_ms INTEGER,
    context_hash TEXT,
    context_agent_count INTEGER NOT NULL DEFAULT 0,
    context_message_count INTEGER NOT NULL DEFAULT 0,
    decision_kind TEXT NOT NULL,
    target_session TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    addressing INTEGER NOT NULL DEFAULT 0,
    mentioned_sessions_json TEXT NOT NULL DEFAULT '[]',
    name_corrections_json TEXT NOT NULL DEFAULT '[]',
    candidate_scores_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT,
    raw_response_json TEXT NOT NULL DEFAULT '{}',
    final_action TEXT NOT NULL,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    phrase_key TEXT,
    error TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_orchestrator_decisions_trace ON orchestrator_decisions(trace_id, created_at);
CREATE INDEX idx_orchestrator_decisions_created ON orchestrator_decisions(created_at DESC);
CREATE INDEX idx_orchestrator_decisions_target ON orchestrator_decisions(target_session, created_at DESC);

CREATE TABLE agent_routing_messages (
    routing_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    trace_id TEXT,
    source TEXT NOT NULL DEFAULT 'orchestrator',
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_agent_routing_recent ON agent_routing_messages(agent_id, role, created_at DESC);
CREATE INDEX idx_agent_routing_session_recent ON agent_routing_messages(session, created_at DESC);

CREATE TABLE orchestrator_pending_utterances (
    pending_id TEXT PRIMARY KEY,
    trace_id TEXT,
    utterance TEXT NOT NULL,
    requested_session TEXT,
    candidate_session TEXT,
    speak_as_session TEXT,
    reason TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    prompt_admission_json TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_orchestrator_pending_status ON orchestrator_pending_utterances(status, expires_at);

CREATE TABLE orchestrator_phrase_cache (
    phrase_key TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    session TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    audio_path TEXT,
    provider TEXT,
    model TEXT,
    generated_at INTEGER,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (phrase_key, voice_id, session)
);

CREATE TABLE device_tokens (
    token TEXT PRIMARY KEY,
    session TEXT,
    platform TEXT NOT NULL DEFAULT 'ios',
    environment TEXT NOT NULL DEFAULT 'production',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_push_at INTEGER,
    disabled_at INTEGER,
    base_url TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_device_tokens_active ON device_tokens(updated_at) WHERE disabled_at IS NULL;

CREATE TABLE path_usage (
    path TEXT PRIMARY KEY,
    use_count INTEGER NOT NULL DEFAULT 0,
    first_used_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL
);
CREATE INDEX idx_path_usage_favorites ON path_usage(use_count DESC, last_used_at DESC);

CREATE TABLE client_locations (
    session TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    accuracy REAL,
    ts INTEGER NOT NULL
);

CREATE TABLE media_assets (
    asset_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    source_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    storage_path TEXT NOT NULL,
    caption TEXT,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER
);
CREATE INDEX idx_media_assets_session_created ON media_assets(session, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX idx_media_assets_agent_created ON media_assets(agent_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX idx_media_assets_sha ON media_assets(sha256);

CREATE TABLE teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    archived_at INTEGER,
    nudge_enabled INTEGER NOT NULL DEFAULT 1,
    leader_agent_id TEXT
);
CREATE INDEX idx_teams_active ON teams(archived_at, updated_at DESC);

CREATE TABLE team_members (
    team_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (team_id, agent_id)
);
CREATE INDEX idx_team_members_agent ON team_members(agent_id, team_id);

CREATE TABLE team_messages (
    team_message_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    source_agent_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    trace_id TEXT,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(team_id, source_message_id, text)
);
CREATE INDEX idx_team_messages_team_created ON team_messages(team_id, created_at DESC);

CREATE TABLE team_inbox (
    team_message_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unread',
    injected_at INTEGER,
    read_at INTEGER,
    PRIMARY KEY (team_message_id, agent_id)
);
CREATE INDEX idx_team_inbox_agent_status ON team_inbox(agent_id, status, team_message_id);

CREATE TABLE decisions (
    id TEXT PRIMARY KEY,
    canonical_question TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '{}',
    user_answer TEXT NOT NULL,
    normalized_answer TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '{}',
    tags TEXT NOT NULL DEFAULT '[]',
    risk_class TEXT NOT NULL DEFAULT 'low',
    time_horizon TEXT NOT NULL DEFAULT 'until_changed',
    confidence REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'active',
    supersedes_id TEXT,
    source_trace TEXT,
    source_message_id TEXT,
    source_agent_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER,
    FOREIGN KEY (supersedes_id) REFERENCES decisions(id)
);
CREATE UNIQUE INDEX idx_decisions_question_hash_active ON decisions(question_hash) WHERE status = 'active';
CREATE INDEX idx_decisions_created ON decisions(created_at DESC);
CREATE INDEX idx_decisions_type ON decisions(decision_type, created_at DESC);

CREATE TABLE decision_applications (
    application_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    task_id TEXT,
    run_id TEXT,
    trace_id TEXT,
    applied_context TEXT NOT NULL DEFAULT '{}',
    outcome TEXT NOT NULL DEFAULT 'used',
    reason TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);
CREATE INDEX idx_decision_applications_decision ON decision_applications(decision_id, created_at DESC);
CREATE INDEX idx_decision_applications_trace ON decision_applications(trace_id, created_at DESC);

CREATE TABLE user_value_facts (
    fact_id TEXT PRIMARY KEY,
    decision_id TEXT,
    statement TEXT NOT NULL,
    category TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '{}',
    tags TEXT NOT NULL DEFAULT '[]',
    evidence_count INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 0.7,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);
CREATE INDEX idx_user_value_facts_status ON user_value_facts(status, updated_at DESC);
CREATE INDEX idx_user_value_facts_category ON user_value_facts(category, updated_at DESC);

CREATE TABLE goals (
    goal_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source_decision_ids TEXT NOT NULL DEFAULT '[]',
    scope TEXT NOT NULL DEFAULT '{}',
    tags TEXT NOT NULL DEFAULT '[]',
    success_criteria TEXT NOT NULL DEFAULT '[]',
    risk_guardrails TEXT NOT NULL DEFAULT '[]',
    time_budget TEXT,
    owner_agent_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    satisfied_at INTEGER
);
CREATE INDEX idx_goals_status ON goals(status, updated_at DESC);

CREATE TABLE goal_runs (
    run_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    evidence TEXT NOT NULL DEFAULT '{}',
    started_at INTEGER,
    finished_at INTEGER,
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);
CREATE INDEX idx_goal_runs_goal ON goal_runs(goal_id, started_at DESC);
CREATE INDEX idx_goal_runs_status ON goal_runs(status, started_at DESC);

CREATE TABLE user_notifications (
    notification_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    persona TEXT NOT NULL,
    backend_session_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    done_ts INTEGER NOT NULL,
    source_message_id TEXT NOT NULL DEFAULT '',
    cause_message_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    notify INTEGER NOT NULL DEFAULT 0,
    push INTEGER NOT NULL DEFAULT 0,
    badge INTEGER NOT NULL DEFAULT 0,
    unread INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    muted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(agent_id, done_ts)
);
CREATE INDEX idx_user_notifications_session_created ON user_notifications(session, created_at DESC);
CREATE INDEX idx_user_notifications_agent_done ON user_notifications(agent_id, done_ts DESC);

CREATE TABLE backend_usage (
    backend TEXT PRIMARY KEY,
    used_percentage REAL,
    resets_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    raw TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_backend_usage_fetched ON backend_usage(fetched_at DESC);

CREATE TABLE dream_runs (
    run_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    local_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    timezone_source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    stage TEXT NOT NULL DEFAULT 'seed',
    min_directions INTEGER NOT NULL,
    planned_directions INTEGER NOT NULL,
    planned_rounds INTEGER NOT NULL,
    completed_rounds INTEGER NOT NULL DEFAULT 0,
    target_tokens INTEGER NOT NULL,
    target_minutes INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    finished_at INTEGER,
    last_error TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
);
CREATE INDEX idx_dream_runs_agent_status ON dream_runs(agent_id, status, started_at DESC);
CREATE INDEX idx_dream_runs_started ON dream_runs(started_at DESC);

CREATE TABLE dream_threads (
    thread_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thread_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    selected_for_iterate INTEGER NOT NULL DEFAULT 0,
    fanout_chars INTEGER NOT NULL DEFAULT 0,
    iterate_chars INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    evidence_status TEXT NOT NULL DEFAULT 'speculative',
    altitude TEXT NOT NULL DEFAULT 'idea',
    artifact_ref TEXT NOT NULL DEFAULT '',
    evidence_summary TEXT NOT NULL DEFAULT '',
    guardrail_refusals TEXT NOT NULL DEFAULT '[]',
    UNIQUE(run_id, thread_index),
    FOREIGN KEY (run_id) REFERENCES dream_runs(run_id)
);
CREATE INDEX idx_dream_threads_run ON dream_threads(run_id, thread_index);

CREATE TABLE dream_rounds (
    round_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thread_id TEXT,
    round_index INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    prompt TEXT NOT NULL,
    response TEXT,
    target_tokens INTEGER NOT NULL,
    sent_at INTEGER,
    completed_at INTEGER,
    output_chars INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(run_id, round_index),
    FOREIGN KEY (run_id) REFERENCES dream_runs(run_id),
    FOREIGN KEY (thread_id) REFERENCES dream_threads(thread_id)
);
CREATE INDEX idx_dream_rounds_run_status ON dream_rounds(run_id, status, round_index);
CREATE INDEX idx_dream_rounds_stage ON dream_rounds(stage, completed_at DESC);

CREATE TABLE heartbeat_accounting (
    accounting_key TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    counted_at INTEGER NOT NULL
);
CREATE INDEX idx_heartbeat_accounting_agent_counted ON heartbeat_accounting(agent_id, counted_at DESC);

CREATE TABLE heartbeat_state (
    agent_id TEXT PRIMARY KEY,
    last_started REAL NOT NULL DEFAULT 0,
    noop_streak INTEGER NOT NULL DEFAULT 0,
    dormant INTEGER NOT NULL DEFAULT 0,
    last_wake_signal_ms INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE background_jobs (
    job_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    cancelled_at INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    heartbeat_at INTEGER,
    heartbeat_timeout_ms INTEGER NOT NULL DEFAULT 600000,
    heartbeat_source TEXT NOT NULL DEFAULT 'unknown',
    worker_pid INTEGER,
    worker_start_token TEXT NOT NULL DEFAULT '',
    terminal_at INTEGER,
    terminal_reason TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 1,
    owner_kind TEXT NOT NULL DEFAULT 'agent',
    computer_id TEXT
);
CREATE INDEX idx_background_jobs_active ON background_jobs(status, updated_at DESC);
CREATE INDEX idx_background_jobs_session ON background_jobs(session, updated_at DESC);
CREATE INDEX idx_background_jobs_computer ON background_jobs(owner_kind, computer_id, updated_at DESC);

CREATE TABLE transcription_results (
    job_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_transcription_results_created ON transcription_results(created_at);

CREATE TABLE queued_turns (
    queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    text TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    client_msg_id TEXT NOT NULL DEFAULT '',
    synthesize_audio INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'user',
    sender_agent_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    enqueued_at INTEGER NOT NULL,
    started_at INTEGER,
    claimed_at INTEGER,
    prompt_admission_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_queued_turns_agent_time ON queued_turns(agent_id, enqueued_at);
CREATE INDEX idx_queued_turns_status_seq ON queued_turns(status, queue_seq);

CREATE TABLE queue_state_revisions (
    agent_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE personas (
    persona_id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    voice_id TEXT NOT NULL DEFAULT '',
    avatar_symbol TEXT NOT NULL DEFAULT '',
    avatar_path TEXT NOT NULL DEFAULT '',
    personality TEXT NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER
);
CREATE INDEX idx_personas_live_name ON personas(name) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX idx_personas_unique_live_name ON personas(name) WHERE deleted_at IS NULL;

CREATE TABLE task_plans (
    plan_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE INDEX idx_task_plans_agent_updated ON task_plans(agent_id, updated_at DESC);
CREATE UNIQUE INDEX idx_task_plans_one_active ON task_plans(agent_id) WHERE status = 'active';

CREATE TABLE task_items (
    item_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES task_plans(plan_id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES task_items(item_id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    active_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_task_items_plan_position ON task_items(plan_id, parent_id, position);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready',
    reference_id TEXT NOT NULL DEFAULT '',
    payload_version INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL DEFAULT '{}',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_trace_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    deleted_at INTEGER
);
CREATE INDEX idx_artifacts_agent_updated ON artifacts(agent_id, updated_at DESC);
CREATE INDEX idx_artifacts_session_updated ON artifacts(session, updated_at DESC);
CREATE UNIQUE INDEX idx_artifacts_reference ON artifacts(agent_id, type, reference_id) WHERE reference_id != '' AND deleted_at IS NULL;

CREATE TABLE artifact_decisions (
    decision_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(artifact_id),
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    yes_label TEXT NOT NULL DEFAULT 'Yes',
    no_label TEXT NOT NULL DEFAULT 'No',
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_choice TEXT NOT NULL DEFAULT '',
    resolved_at INTEGER,
    resolved_by TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    expires_at INTEGER
);
CREATE INDEX idx_artifact_decisions_status ON artifact_decisions(status, decision_id);

CREATE TABLE decision_deliveries (
    decision_id TEXT PRIMARY KEY REFERENCES artifact_decisions(decision_id),
    artifact_id TEXT NOT NULL,
    session TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    reference_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    choice TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    delivered_at INTEGER
);
CREATE INDEX idx_decision_deliveries_pending ON decision_deliveries(status, created_at);

CREATE TABLE prompt_admissions (
    admission_id TEXT PRIMARY KEY,
    admission_version INTEGER NOT NULL,
    authenticated_at_admission INTEGER NOT NULL,
    cooperative_principal TEXT NOT NULL,
    principal_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    client_admission_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    message_id TEXT NOT NULL,
    original_text TEXT NOT NULL,
    UNIQUE(agent_id, client_admission_id)
);
CREATE INDEX idx_prompt_admissions_history ON prompt_admissions( agent_id, cooperative_principal, observed_at DESC, admission_id );

CREATE TABLE background_job_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    observed_at INTEGER NOT NULL
);
CREATE INDEX idx_background_job_events_job ON background_job_events(job_id, event_id DESC);

CREATE TABLE agy_turn_authority (
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    backend_session_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    assistant_start_ordinal INTEGER NOT NULL,
    assistant_end_ordinal INTEGER,
    terminal_status TEXT NOT NULL DEFAULT 'pending',
    authoritative_message_id TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(agent_id, backend_session_id, trace_id)
);
CREATE INDEX idx_agy_turn_authority_import ON agy_turn_authority( agent_id, backend_session_id, assistant_start_ordinal, assistant_end_ordinal );

CREATE TABLE agent_portraits (
    portrait_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    media_asset_id TEXT REFERENCES media_assets(asset_id),
    storage_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER
);
CREATE UNIQUE INDEX idx_agent_portraits_live_content ON agent_portraits(agent_id, sha256) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX idx_agent_portraits_one_primary ON agent_portraits(agent_id) WHERE deleted_at IS NULL AND is_primary = 1;
CREATE INDEX idx_agent_portraits_agent_created ON agent_portraits(agent_id, created_at) WHERE deleted_at IS NULL;

CREATE TABLE provider_usage_windows (
    provider_instance_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    auth_generation_id TEXT NOT NULL,
    account_scope_ref TEXT,
    window_id TEXT NOT NULL,
    window_kind TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    unit TEXT NOT NULL,
    used_percentage REAL,
    resets_at TEXT,
    observed_at INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_detail TEXT NOT NULL,
    PRIMARY KEY(provider_instance_id, auth_generation_id, window_id)
);
CREATE INDEX idx_provider_usage_current ON provider_usage_windows( provider_instance_id, window_kind, observed_at DESC );

CREATE TABLE provider_limit_episodes (
    episode_id TEXT PRIMARY KEY,
    provider_instance_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    auth_generation_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    current_kind TEXT NOT NULL,
    threshold_id TEXT,
    opened_at INTEGER NOT NULL,
    resolved_at INTEGER,
    current_event_id TEXT
);
CREATE INDEX idx_provider_limit_episode_open ON provider_limit_episodes( provider_instance_id, auth_generation_id, window_id, status );

CREATE TABLE provider_limit_events (
    provider_limit_event_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES provider_limit_episodes(episode_id),
    provider_instance_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    auth_generation_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    threshold_id TEXT,
    used_percentage REAL,
    resets_at TEXT,
    observed_at INTEGER NOT NULL,
    freshness TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_provider_limit_events_instance ON provider_limit_events(provider_instance_id, observed_at DESC);

CREATE TABLE pairing_codes (
    code_hash TEXT PRIMARY KEY,
    device_name TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('full', 'limited')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER
);
CREATE INDEX idx_pairing_codes_expiry ON pairing_codes(expires_at);

CREATE TABLE paired_devices (
    device_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL CHECK(scope IN ('full', 'limited')),
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER,
    revoked_at INTEGER
);
CREATE INDEX idx_paired_devices_active ON paired_devices(revoked_at, created_at DESC);

CREATE TABLE oracle_delegations (
    delegation_id      TEXT PRIMARY KEY,
    owner_principal    TEXT NOT NULL,
    trace_id           TEXT NOT NULL UNIQUE,
    client_msg_id      TEXT NOT NULL UNIQUE,
    agent_id           TEXT NOT NULL REFERENCES agents(agent_id),
    session            TEXT NOT NULL,
    backend_session_id TEXT NOT NULL DEFAULT '',
    request_text       TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'accepted'
                       CHECK(status IN (
                           'accepted', 'queued', 'completed',
                           'failed', 'cancelled')),
    result_message_id  TEXT,
    result_text        TEXT NOT NULL DEFAULT '',
    error              TEXT NOT NULL DEFAULT '',
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    delivered_at       INTEGER
);
CREATE INDEX idx_oracle_delegations_delivery
    ON oracle_delegations(owner_principal, delivered_at, updated_at DESC);
CREATE INDEX idx_oracle_delegations_agent
    ON oracle_delegations(agent_id, created_at DESC);

CREATE VIEW clip_lifecycle AS
    SELECT
    c.clip_id,
    c.trace_id,
    c.agent_id,
    c.path AS clip_url,
    c.status,
    c.producer_status,
    c.created_at,
    c.broadcast_at,
    c.queued_at,
    c.play_started_at,
    c.played_at,
    c.completed_at,
    c.error
    FROM clips c;
"""


def _migrate(con: sqlite3.Connection) -> None:
    """Bring the DB up to the current schema version.

    Runs inside one write transaction so that the server and a hook
    subprocess opening a brand-new database at the same moment cannot both
    try to create it: the second waits on the write lock and then sees the
    finished version.
    """
    if _user_version(con) >= _SCHEMA_VERSION:
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        version = _user_version(con)
        if version >= _SCHEMA_VERSION:
            con.execute("COMMIT")
            return
        if version == 0:
            _create_schema(con)
        elif version < _MIN_UPGRADABLE_VERSION:
            raise RuntimeError(
                f"state database {DB_PATH} is at schema version {version}; "
                f"this release upgrades from version {_MIN_UPGRADABLE_VERSION} "
                "or newer. Move the file aside to start with a fresh database."
            )
        else:
            if version < 62:
                _migrate_to_v62(con)
            if version < 63:
                _migrate_to_v63(con)
        con.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _user_version(con: sqlite3.Connection) -> int:
    # Exhaust the cursor: a half-read SELECT keeps its read snapshot open, and
    # a BEGIN IMMEDIATE issued on top of it would then re-read a stale version.
    return int(con.execute("PRAGMA user_version").fetchall()[0][0])


def _create_schema(con: sqlite3.Connection) -> None:
    # executescript() would commit the surrounding transaction, so run the
    # statements one at a time.
    statement = ""
    for line in _SCHEMA_SQL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""
    assert not statement.strip(), "schema ends with an unterminated statement"
    con.execute(
        "INSERT OR IGNORE INTO message_clock (singleton, revision) VALUES (0, 0)")


def _migrate_to_v62(con: sqlite3.Connection) -> None:
    """Drop pre-release compatibility shapes.

    * Diagnostics moved to telemetry.sqlite; the retired `diagnostic_events`
      table and the views over it go away.
    * `tts_queue.mode` was a constant left over from removed audio modes.
    * Heartbeat timing is Computer-owned (`/heartbeat/settings`); the per-agent
      columns were unused.
    * `agents.mcp_servers` rows in the bare-list form become the explicit
      {"configured": true, "servers": [...]} form.
    * Avatar paths from the claude-pwa data root move to the clarp data root.
    * Settings rows for retired keys are removed.
    """
    for view in ("untraced_events", "events", "requests", "errors",
                 "trace_paths", "sse_delivery", "voice_latency"):
        con.execute(f"DROP VIEW IF EXISTS {view}")
    con.execute("DROP TABLE IF EXISTS diagnostic_events")
    con.execute("ALTER TABLE tts_queue DROP COLUMN mode")
    for column in ("heartbeat_interval_sec", "heartbeat_backoff_strategy",
                   "heartbeat_backoff_cap_sec", "heartbeat_dormant_after_noops"):
        con.execute(f"ALTER TABLE agents DROP COLUMN {column}")
    con.execute(
        """UPDATE agents
              SET mcp_servers = json_object('configured', json('true'),
                                            'servers', json(mcp_servers))
            WHERE json_valid(mcp_servers)
              AND json_type(mcp_servers) = 'array'
              AND json_array_length(mcp_servers) > 0"""
    )
    for table in ("agents", "personas"):
        con.execute(
            f"""UPDATE {table}
                   SET avatar_path = replace(avatar_path,
                                             '/claude-pwa/avatars/',
                                             '/clarp/avatars/')
                 WHERE avatar_path LIKE '%/claude-pwa/avatars/%'"""
        )
    con.execute(
        """DELETE FROM settings
            WHERE key IN ('transcription.guidance.mode',
                          'telemetry.state_events_retired.v1')"""
    )


def _migrate_to_v63(con: sqlite3.Connection) -> None:
    """Durable Oracle-to-agent delegation and result delivery.

    Result consumption is scoped to the authenticated device that owns the
    Oracle session, so the delivery index leads with `owner_principal`.
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS oracle_delegations (
            delegation_id      TEXT PRIMARY KEY,
            owner_principal    TEXT NOT NULL,
            trace_id           TEXT NOT NULL UNIQUE,
            client_msg_id      TEXT NOT NULL UNIQUE,
            agent_id           TEXT NOT NULL REFERENCES agents(agent_id),
            session            TEXT NOT NULL,
            backend_session_id TEXT NOT NULL DEFAULT '',
            request_text       TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'accepted'
                               CHECK(status IN (
                                   'accepted', 'queued', 'completed',
                                   'failed', 'cancelled')),
            result_message_id  TEXT,
            result_text        TEXT NOT NULL DEFAULT '',
            error              TEXT NOT NULL DEFAULT '',
            created_at         INTEGER NOT NULL,
            updated_at         INTEGER NOT NULL,
            delivered_at       INTEGER
        )
    """)
    # One statement per execute: executescript() commits the open migration
    # transaction before it runs.
    con.execute(
        """CREATE INDEX IF NOT EXISTS idx_oracle_delegations_delivery
             ON oracle_delegations(owner_principal, delivered_at,
                                   updated_at DESC)"""
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS idx_oracle_delegations_agent
             ON oracle_delegations(agent_id, created_at DESC)"""
    )



def now_ms() -> int:
    return int(time.time() * 1000)


def reset_for_tests(path: pathlib.Path | None = None) -> None:
    """Test helper: close cached connection so a new path takes effect."""
    global DB_PATH, _MIGRATED, _LAST_LOCK_REPORT_AT
    if path is not None:
        DB_PATH = path
    _MIGRATED = False
    close_local()
    with _TRANSACTION_LOCK:
        _TRANSACTION_OWNERS.clear()
        _LAST_LOCK_REPORT_AT = 0.0
