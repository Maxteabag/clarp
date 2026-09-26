"""Version-by-version upgrades of state.sqlite.

`_migrate` brings a database to `db_schema._SCHEMA_VERSION`: a fresh file gets
`_create_schema`, an old one runs every `_migrate_to_vN` above its version in
order. Each `_migrate_to_vN` is the single place that knows how to move a
v(N-1) database to vN and stays here until the version it upgrades from is no
longer supported (`db_schema._MIN_UPGRADABLE_VERSION`). See the module
docstring of `db.py` for the two collision pitfalls.

Schema strings are read from `db_schema` at call time and `DB_PATH` from `db`,
so importing either module first cannot bind a half-built value. Every name
here stays reachable as `db._migrate`, `db._migrate_to_vN` and so on.
"""
from __future__ import annotations

import sqlite3

from . import db, db_schema


def _migrate(con: sqlite3.Connection) -> None:
    """Bring the DB up to the current schema version.

    Runs inside one write transaction so that the server and a hook
    subprocess opening a brand-new database at the same moment cannot both
    try to create it: the second waits on the write lock and then sees the
    finished version.
    """
    if _user_version(con) >= db_schema._SCHEMA_VERSION:
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        version = _user_version(con)
        if version >= db_schema._SCHEMA_VERSION:
            con.execute("COMMIT")
            return
        if version == 0:
            _create_schema(con)
        elif version < db_schema._MIN_UPGRADABLE_VERSION:
            raise RuntimeError(
                f"state database {db.DB_PATH} is at schema version {version}; "
                f"this release upgrades from version {db_schema._MIN_UPGRADABLE_VERSION} "
                "or newer. Move the file aside to start with a fresh database."
            )
        else:
            if version < 62:
                _migrate_to_v62(con)
            if version < 63:
                _migrate_to_v63(con)
            if version < 64:
                _migrate_to_v64(con)
            if version < 65:
                _migrate_to_v65(con)
            if version < 66:
                _migrate_to_v66(con)
            if version < 70:
                _migrate_to_v70(con)
            if version < 71:
                _migrate_to_v71(con)
            if version < 72:
                for statement in db_schema._EXPLANATION_CACHE_SCHEMA.split(";"):
                    if statement.strip():
                        con.execute(statement)
            if version < 73:
                _migrate_to_v73(con)
            if version < 74:
                for statement in db_schema._HTML_FORMS_SCHEMA.split(";"):
                    if statement.strip(): con.execute(statement)
            if version < 75:
                columns = {row[1] for row in con.execute("PRAGMA table_info(oracle_delegations)")}
                if "completion_trace_id" not in columns:
                    con.execute("ALTER TABLE oracle_delegations ADD COLUMN completion_trace_id TEXT NOT NULL DEFAULT ''")
            if version < 76:
                _migrate_to_v76(con)
            if version < 77:
                con.execute(db_schema._ACTIVE_INTERVAL_TRIGGER)
            if version < 78:
                _migrate_to_v78(con)
            if version < 79:
                _migrate_to_v79(con)
            if version < 80:
                _migrate_to_v80(con)
            if version < 83:
                for statement in db_schema._PODCAST_HISTORY_SCHEMA.split(";"):
                    if statement.strip(): con.execute(statement)
        if version < 84:
            from .audio_bookkeeper import SCHEMA_STATEMENTS
            for statement in SCHEMA_STATEMENTS: con.execute(statement)
        if version < 85:
            from .janitor_autonomy import SCHEMA
            for statement in SCHEMA.split(";"):
                if statement.strip(): con.execute(statement)
        if version < 86:
            for statement in db_schema._ORACLE_MEMORY_SCHEMA.split(";"):
                if statement.strip(): con.execute(statement)
        if version < 87:
            for statement in db_schema._AGENT_GOALS_SCHEMA.split(";"):
                if statement.strip(): con.execute(statement)
        if version < 88:
            from .judgments import SCHEMA as judgments_schema
            for statement in judgments_schema.split(";"):
                if statement.strip(): con.execute(statement)
        if version < 89:
            _migrate_to_v89(con)
        if version < 90:
            _migrate_to_v90(con)
        if version < 91:
            _migrate_to_v91(con)
        if version < 92:
            _migrate_to_v92(con)
        if version < 93:
            _migrate_to_v93(con)
        if version < 94:
            _migrate_to_v94(con)
        if version < 95:
            _migrate_to_v95(con)

        con.execute(f"PRAGMA user_version = {db_schema._SCHEMA_VERSION}")
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
    for line in db_schema._SCHEMA_SQL.splitlines(keepends=True):
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


def _migrate_to_v66(con: sqlite3.Connection) -> None:
    """Agent scheduled jobs for recurring autonomous session turns."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS agent_schedules (
            schedule_id     TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL REFERENCES agents(agent_id),
            session         TEXT NOT NULL,
            name            TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            prompt          TEXT NOT NULL,
            enabled         INTEGER NOT NULL DEFAULT 1,
            last_run_at     INTEGER,
            next_run_at     INTEGER,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL
        )
    """)
    con.execute(
        """CREATE INDEX IF NOT EXISTS idx_agent_schedules_session
             ON agent_schedules(session)"""
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS idx_agent_schedules_next
             ON agent_schedules(enabled, next_run_at)"""
    )


def _migrate_to_v70(con: sqlite3.Connection) -> None:
    """Reconcile attention columns across the historical v67-v69 schema overlap.

    Some Hosts reached v69 through unrelated experimental migrations before
    attention shipped as v67. Reconcile the actual columns rather than assuming
    any of those version stamps proves attention exists. All changes are
    additive; unrelated columns, tables and existing answer snapshots stay put.
    """
    additions = {
        "artifacts": ("archived_at INTEGER",),
        "artifact_decisions": (
            "response_type TEXT NOT NULL DEFAULT 'approval'",
            "options_json TEXT NOT NULL DEFAULT '[]'",
            "allow_custom_text INTEGER NOT NULL DEFAULT 0",
            "recommended_option_id TEXT",
            "blocks_progress INTEGER NOT NULL DEFAULT 0",
            "priority_reason TEXT NOT NULL DEFAULT ''",
            "urgency TEXT NOT NULL DEFAULT 'normal'",
            "response_effort TEXT NOT NULL DEFAULT 'review'",
            "deadline_at INTEGER",
            "answer_json TEXT",
        ),
        "decision_deliveries": (
            "response_type TEXT NOT NULL DEFAULT 'approval'",
            "answer_json TEXT",
        ),
    }
    for table, definitions in additions.items():
        columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        for definition in definitions:
            if definition.split()[0] not in columns:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
    con.execute("""UPDATE artifact_decisions SET answer_json=json_object('choice',resolved_choice)
                    WHERE response_type='approval' AND answer_json IS NULL
                      AND status IN ('accepted','rejected')""")
    con.execute("""UPDATE decision_deliveries SET answer_json=json_object('choice',choice)
                    WHERE response_type='approval' AND answer_json IS NULL
                      AND choice IN ('accepted','rejected')""")


def _migrate_to_v80(con: sqlite3.Connection) -> None:
    """Permanent voice timeline (lib/voice_events.py).

    One row per moment of a voice exchange, on a server-corrected clock.
    Lives here rather than in telemetry.sqlite because it is never pruned.
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS voice_events (
            event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              INTEGER NOT NULL,
            client_ts       INTEGER,
            mono_ms         INTEGER,
            received_at     INTEGER NOT NULL,
            clock_offset_ms INTEGER,
            source          TEXT NOT NULL,
            client_id       TEXT,
            session         TEXT,
            utterance_id    TEXT,
            trace_id        TEXT,
            event           TEXT NOT NULL,
            duration_ms     REAL,
            level_db        REAL,
            peak_db         REAL,
            text            TEXT,
            detail          TEXT NOT NULL DEFAULT '{}'
        )
    """)
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_voice_events_ts ON voice_events(ts)",
        "CREATE INDEX IF NOT EXISTS idx_voice_events_session_ts"
        " ON voice_events(session, ts)",
        "CREATE INDEX IF NOT EXISTS idx_voice_events_utterance"
        " ON voice_events(utterance_id, ts)",
        "CREATE INDEX IF NOT EXISTS idx_voice_events_trace"
        " ON voice_events(trace_id, ts)",
    ):
        con.execute(statement)


def _migrate_to_v64(con: sqlite3.Connection) -> None:
    """Transcription context packs: terms, packs, profiles, runs.

    Packs are ranked sources rather than fixed lists, so nothing here stores a
    length - how deep a pack is drawn is decided at compile time against the
    active model's budget. `vocab_runs` records every compile so a transcript
    can always be traced back to the exact prompt that produced it.
    """
    # Comments are stripped before splitting because prose in a `--` line may
    # itself contain a semicolon, which would otherwise cut a statement in two.
    # `executescript` is not an option - it issues its own COMMIT, which would
    # break the transaction `_migrate` opened around all upgrades.
    stripped = "\n".join(
        line.split("--", 1)[0] for line in _V64_SQL.splitlines())
    for statement in stripped.split(";"):
        text = statement.strip()
        if text:
            con.execute(text)


def _migrate_to_v65(con: sqlite3.Connection) -> None:
    """A user message remembers the trace of the turn that carried it.

    That is the link from a transcript bubble back to the vocabulary run and
    the retained audio behind it - the "what was sent" the app can now show
    for one message rather than for the agent as a whole.
    """
    con.execute("ALTER TABLE messages ADD COLUMN trace_id TEXT")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_trace ON messages(trace_id)"
        " WHERE trace_id IS NOT NULL")


def _migrate_to_v79(con: sqlite3.Connection) -> None:
    """Per-agent fallback models and their once-only invocation receipts.

    Purely additive: every statement is CREATE TABLE IF NOT EXISTS, so a host
    that already ran this branch re-applies it without touching stored rows.
    """
    for statement in db_schema._MODEL_FALLBACK_SCHEMA.split(";"):
        if statement.strip():
            con.execute(statement)


def _migrate_to_v91(con: sqlite3.Connection) -> None:
    """Expression index behind the dashboard message previews.

    message_store.dashboard_messages ranks every agent's messages by their
    semantic activity time (_message_activity_sql) on each snapshot. Without
    this index SQLite evaluated julianday() over the whole table twice per
    snapshot; with it the window walks the index. The expression here must
    stay textually identical to _message_activity_sql and the ORDER BY.
    """
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_dashboard_activity
            ON messages(agent_id,
                        COALESCE(CAST((julianday(timestamp) - 2440587.5) * 86400000 AS INTEGER), updated_at) DESC,
                        seq DESC, updated_at DESC)
            WHERE COALESCE(text, '') != '' AND COALESCE(tool_name, '') = ''
    """)


def _migrate_to_v94(con: sqlite3.Connection) -> None:
    """Index release expiry: pruning and the 4096-row cap scanned the table."""
    con.execute("CREATE INDEX IF NOT EXISTS tool_explanation_releases_expiry "
                "ON tool_explanation_releases(expires_at)")


def _migrate_to_v95(con: sqlite3.Connection) -> None:
    """Inspectable background processes.

    A job carries the worker's last progress line, the log file it writes
    (and the directory it ran in, which bounds what the Host will read), and
    the trace of the turn that registered it. Each change-feed event records
    the status it left and an optional note, so a job has a timeline.
    """
    for table, columns in (
        ("background_jobs", (("progress_text", "TEXT NOT NULL DEFAULT ''"),
                             ("progress_at", "INTEGER"),
                             ("log_path", "TEXT NOT NULL DEFAULT ''"),
                             ("worker_cwd", "TEXT NOT NULL DEFAULT ''"),
                             ("started_trace_id", "TEXT NOT NULL DEFAULT ''"))),
        ("background_job_events", (("status", "TEXT NOT NULL DEFAULT ''"),
                                   ("note", "TEXT NOT NULL DEFAULT ''"))),
    ):
        existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_to_v93(con: sqlite3.Connection) -> None:
    """Helper agents: a parent, a role and a helper lifecycle on agents.

    ``parent_agent_id`` names the agent that created this one. It is not a
    foreign key with a cascade on purpose: deleting a parent flags its
    helpers ``abandoned`` instead of removing them. ``role`` absorbs
    ``is_janitor``, which stays until its readers move over.
    """
    columns = {row[1] for row in con.execute("PRAGMA table_info(agents)")}
    for name, definition in (("parent_agent_id", "TEXT"),
                             ("role", "TEXT NOT NULL DEFAULT 'agent'"),
                             ("helper_state", "TEXT"),
                             ("helper_completed_at", "INTEGER")):
        if name not in columns:
            con.execute(f"ALTER TABLE agents ADD COLUMN {name} {definition}")
    con.execute("UPDATE agents SET role = 'janitor' WHERE is_janitor = 1")
    con.execute("CREATE INDEX IF NOT EXISTS idx_agents_parent ON agents(parent_agent_id) "
                "WHERE parent_agent_id IS NOT NULL")


def _migrate_to_v92(con: sqlite3.Connection) -> None:
    """Covering index behind the agent-pair conversation list.

    agent_conversations.list_conversations aggregates every agent-to-agent
    message (count, newest revision, newest activity, whether the pair was
    delivered). Before this index that meant reading ~100k full rows, text
    included, on every call: 0.4 s warm and 18-37 s on a cold page cache
    after a reboot, with every other request queued behind it. The index
    carries exactly the columns the aggregate needs, so it never touches the
    table, and the newest row per pair is one indexed lookup.
    """
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_pair_summary
            ON messages(agent_id, sender_agent_id, role, revision, timestamp, seq)
            WHERE COALESCE(origin, 'user') = 'agent'
      AND COALESCE(sender_agent_id, '') != ''
      AND sender_agent_id != agent_id
      AND COALESCE(text, '') != ''
      AND COALESCE(tool_name, '') = ''
      AND role IN ('user', 'assistant')
    """)


def _migrate_to_v90(con: sqlite3.Connection) -> None:
    """Explanation producer provenance and learned template mappings.

    Rows cached before this version were all written by the language model.
    """
    columns = {row[1] for row in con.execute("PRAGMA table_info(tool_explanation_cache)")}
    for name, definition in (("source", "TEXT NOT NULL DEFAULT 'llm'"),
                             ("provenance_json", "TEXT NOT NULL DEFAULT '{}'"),
                             ("signature", "TEXT NOT NULL DEFAULT ''")):
        if name not in columns:
            con.execute(f"ALTER TABLE tool_explanation_cache ADD COLUMN {name} {definition}")
    con.execute("CREATE INDEX IF NOT EXISTS tool_explanation_cache_signature ON tool_explanation_cache(signature)")
    for statement in db_schema._EXPLANATION_MAPPINGS_SCHEMA.split(";"):
        if statement.strip():
            con.execute(statement)


def _migrate_to_v89(con: sqlite3.Connection) -> None:
    """Durable owner/thread deduplication for Oracle context notifications."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS oracle_context_notifications (
            thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id),
            owner_principal TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            reference_ts INTEGER NOT NULL,
            stale INTEGER NOT NULL DEFAULT 0,
            sent_at INTEGER NOT NULL,
            PRIMARY KEY(thread_id, source_kind, source_id)
        )
    """)
    con.execute(
        """CREATE INDEX IF NOT EXISTS oracle_context_notifications_owner
             ON oracle_context_notifications(owner_principal,thread_id,sent_at)"""
    )


def _migrate_to_v78(con: sqlite3.Connection) -> None:
    """Dreaming seed strategies, thread kill notes, and agent voice verbosity.

    Guarded: a host can reach this point with the columns already present.
    """
    runs = {row[1] for row in con.execute("PRAGMA table_info(dream_runs)")}
    for name, definition in (
        ("seed_strategy", "TEXT NOT NULL DEFAULT 'control'"),
        ("context_dose", "TEXT NOT NULL DEFAULT 'full'"),
        ("seed_material", "TEXT NOT NULL DEFAULT ''"),
        ("artifact_branch", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in runs:
            con.execute(f"ALTER TABLE dream_runs ADD COLUMN {name} {definition}")
    threads = {row[1] for row in con.execute("PRAGMA table_info(dream_threads)")}
    for name in ("killed_reason", "origin_note"):
        if name not in threads:
            con.execute(f"ALTER TABLE dream_threads ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    agent_cols = {row[1] for row in con.execute("PRAGMA table_info(agents)")}
    if "voice_verbosity" not in agent_cols:
        con.execute("ALTER TABLE agents ADD COLUMN voice_verbosity INTEGER NOT NULL DEFAULT 0")


_V64_SQL = """
CREATE TABLE vocab_packs (
    pack_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'static'
               CHECK(kind IN ('static', 'dynamic')),
    generator  TEXT NOT NULL DEFAULT '',
    priority   REAL NOT NULL DEFAULT 1.0,
    floor      INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX vocab_packs_name ON vocab_packs(name);

CREATE TABLE vocab_terms (
    term_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id         TEXT NOT NULL REFERENCES vocab_packs(pack_id)
                    ON DELETE CASCADE,
    text            TEXT NOT NULL,
    say_as          TEXT NOT NULL DEFAULT '',
    often_heard_as  TEXT NOT NULL DEFAULT '',
    rarity          REAL NOT NULL DEFAULT 0.5,
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      INTEGER NOT NULL
);
CREATE UNIQUE INDEX vocab_terms_pack_text
    ON vocab_terms(pack_id, text COLLATE NOCASE);

CREATE TABLE vocab_profiles (
    profile_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX vocab_profiles_name ON vocab_profiles(name);

CREATE TABLE vocab_profile_packs (
    profile_id TEXT NOT NULL REFERENCES vocab_profiles(profile_id)
               ON DELETE CASCADE,
    pack_id    TEXT NOT NULL REFERENCES vocab_packs(pack_id)
               ON DELETE CASCADE,
    position   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (profile_id, pack_id)
);

-- A profile binds to an agent or a team, never both; the CHECK keeps the
-- "exactly one owner" rule in the database rather than in every caller.
CREATE TABLE vocab_assignments (
    assignment_id TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL REFERENCES vocab_profiles(profile_id)
                  ON DELETE CASCADE,
    agent_id      TEXT REFERENCES agents(agent_id) ON DELETE CASCADE,
    team_id       TEXT,
    created_at    INTEGER NOT NULL,
    CHECK ((agent_id IS NULL) <> (team_id IS NULL))
);
CREATE UNIQUE INDEX vocab_assignments_agent
    ON vocab_assignments(agent_id) WHERE agent_id IS NOT NULL;
CREATE UNIQUE INDEX vocab_assignments_team
    ON vocab_assignments(team_id) WHERE team_id IS NOT NULL;

-- One row per compile. This is the transparency contract: it must always be
-- possible to answer "what exactly did we send to the model, and what did we
-- leave out?" for any transcript the user is looking at.
CREATE TABLE vocab_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT REFERENCES agents(agent_id) ON DELETE SET NULL,
    session       TEXT NOT NULL DEFAULT '',
    trace_id      TEXT NOT NULL DEFAULT '',
    profile_id    TEXT,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    unit          TEXT NOT NULL,
    capacity      INTEGER NOT NULL,
    used          INTEGER NOT NULL,
    form          TEXT NOT NULL,
    rarity_floor  REAL NOT NULL DEFAULT 0,
    payload       TEXT NOT NULL DEFAULT '',
    included_json TEXT NOT NULL DEFAULT '[]',
    dropped_json  TEXT NOT NULL DEFAULT '[]',
    transcript    TEXT NOT NULL DEFAULT '',
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);
CREATE INDEX vocab_runs_recent ON vocab_runs(created_at DESC);
CREATE INDEX vocab_runs_trace ON vocab_runs(trace_id)
    WHERE trace_id <> '';
"""


def _migrate_to_v71(con: sqlite3.Connection) -> None:
    """Preserve existing coordination; newly created teams are passive groups."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(teams)")}
    for definition in ("parent_team_id TEXT", "leader_enabled INTEGER NOT NULL DEFAULT 0",
                       "communication_enabled INTEGER NOT NULL DEFAULT 0"):
        if definition.split()[0] not in columns:
            con.execute(f"ALTER TABLE teams ADD COLUMN {definition}")
    if "communication_enabled" not in columns:
        con.execute("UPDATE teams SET communication_enabled = 1")
    if "leader_enabled" not in columns:
        con.execute("UPDATE teams SET leader_enabled = CASE WHEN COALESCE(leader_agent_id, '') != '' THEN 1 ELSE 0 END")


def _migrate_to_v73(con: sqlite3.Connection) -> None:
    """Add optional maintenance without rewriting existing identities/history."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(agents)")}
    if "is_janitor" not in columns:
        con.execute("ALTER TABLE agents ADD COLUMN is_janitor INTEGER NOT NULL "
                    "DEFAULT 0 CHECK (is_janitor IN (0, 1))")
    for statement in db_schema._JANITOR_SCHEMA.split(";"):
        if statement.strip():
            con.execute(statement)


def _migrate_to_v76(con: sqlite3.Connection) -> None:
    """Add demand execution contracts without enabling or converting agents."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(janitor_configs)")}
    if "execution_json" not in columns:
        con.execute("ALTER TABLE janitor_configs ADD COLUMN execution_json TEXT NOT NULL DEFAULT '{}'")
    if "options_json" not in columns:
        con.execute("ALTER TABLE janitor_configs ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'")
    statement = ""
    for line in db_schema._BUILTIN_JANITOR_SCHEMA.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""
    assert not statement.strip()
