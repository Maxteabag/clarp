"""Complete schema of state.sqlite plus the version it describes.

`_SCHEMA_SQL` is applied in one step to a fresh database (see
`db_migrations._create_schema`). To change the schema: edit `_SCHEMA_SQL`
here, bump `_SCHEMA_VERSION`, and add one `_migrate_to_vN` in
`db_migrations.py` that upgrades an existing database. Feature modules that own
their own tables contribute a `SCHEMA` string that is appended below; the
append order is the order tables are created in.

Everything here stays reachable as `db._SCHEMA_SQL`, `db._SCHEMA_VERSION` and
`db._MIN_UPGRADABLE_VERSION` through `db.__getattr__`.
"""
from __future__ import annotations


# Versions 81 and 82 also exist on installed Hosts with additive indexing
# migrations. History must run when upgrading those Hosts, not only main's v80.
_SCHEMA_VERSION = 95


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
    archived_at INTEGER,
    is_janitor INTEGER NOT NULL DEFAULT 0 CHECK (is_janitor IN (0, 1)),
    voice_verbosity INTEGER NOT NULL DEFAULT 0,
    parent_agent_id TEXT,
    role TEXT NOT NULL DEFAULT 'agent',
    helper_state TEXT,
    helper_completed_at INTEGER
);
CREATE INDEX idx_agents_parent ON agents(parent_agent_id) WHERE parent_agent_id IS NOT NULL;

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
    trace_id TEXT,
    UNIQUE(agent_id, backend_session_id, seq)
);
CREATE INDEX idx_messages_agent_seq ON messages(agent_id, backend_session_id, seq);
CREATE INDEX idx_messages_trace ON messages(trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX idx_messages_agent_timestamp ON messages(agent_id, timestamp);
CREATE INDEX idx_messages_agent_revision ON messages(agent_id, backend_session_id, revision);
CREATE INDEX idx_messages_dashboard_activity
    ON messages(agent_id,
                COALESCE(CAST((julianday(timestamp) - 2440587.5) * 86400000 AS INTEGER), updated_at) DESC,
                seq DESC, updated_at DESC)
    WHERE COALESCE(text, '') != '' AND COALESCE(tool_name, '') = '';
CREATE INDEX idx_messages_pair_summary
    ON messages(agent_id, sender_agent_id, role, revision, timestamp, seq)
    WHERE COALESCE(origin, 'user') = 'agent'
      AND COALESCE(sender_agent_id, '') != ''
      AND sender_agent_id != agent_id
      AND COALESCE(text, '') != ''
      AND COALESCE(tool_name, '') = ''
      AND role IN ('user', 'assistant');

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
    leader_agent_id TEXT,
    parent_team_id TEXT,
    leader_enabled INTEGER NOT NULL DEFAULT 0,
    communication_enabled INTEGER NOT NULL DEFAULT 0
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
    seed_strategy TEXT NOT NULL DEFAULT 'control',
    context_dose TEXT NOT NULL DEFAULT 'full',
    seed_material TEXT NOT NULL DEFAULT '',
    artifact_branch TEXT NOT NULL DEFAULT '',
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
    killed_reason TEXT NOT NULL DEFAULT '',
    origin_note TEXT NOT NULL DEFAULT '',
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
    computer_id TEXT,
    progress_text TEXT NOT NULL DEFAULT '',
    progress_at INTEGER,
    log_path TEXT NOT NULL DEFAULT '',
    worker_cwd TEXT NOT NULL DEFAULT '',
    started_trace_id TEXT NOT NULL DEFAULT ''
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
    deleted_at INTEGER,
    archived_at INTEGER
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
    expires_at INTEGER,
    response_type TEXT NOT NULL DEFAULT 'approval',
    options_json TEXT NOT NULL DEFAULT '[]',
    allow_custom_text INTEGER NOT NULL DEFAULT 0,
    recommended_option_id TEXT,
    blocks_progress INTEGER NOT NULL DEFAULT 0,
    priority_reason TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT 'normal',
    response_effort TEXT NOT NULL DEFAULT 'review',
    deadline_at INTEGER,
    answer_json TEXT
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
    delivered_at INTEGER,
    response_type TEXT NOT NULL DEFAULT 'approval',
    answer_json TEXT
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
    observed_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
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
    completion_trace_id TEXT NOT NULL DEFAULT '',
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

CREATE TABLE agent_schedules (
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
);
CREATE INDEX idx_agent_schedules_session
    ON agent_schedules(session);
CREATE INDEX idx_agent_schedules_next
    ON agent_schedules(enabled, next_run_at);

CREATE TABLE voice_events (
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
);
CREATE INDEX idx_voice_events_ts ON voice_events(ts);
CREATE INDEX idx_voice_events_session_ts ON voice_events(session, ts);
CREATE INDEX idx_voice_events_utterance ON voice_events(utterance_id, ts);
CREATE INDEX idx_voice_events_trace ON voice_events(trace_id, ts);

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


_EXPLANATION_CACHE_SCHEMA = """
CREATE TABLE tool_explanation_cache (
    cache_key TEXT PRIMARY KEY,
    explanation TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'llm',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    signature TEXT NOT NULL DEFAULT ''
);
CREATE INDEX tool_explanation_cache_expiry ON tool_explanation_cache(expires_at);
CREATE INDEX tool_explanation_cache_signature ON tool_explanation_cache(signature);
CREATE TABLE tool_explanation_jobs (
    cache_key TEXT PRIMARY KEY,
    detail_level INTEGER NOT NULL,
    activity_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    available_at INTEGER NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    lease_until INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX tool_explanation_jobs_queue ON tool_explanation_jobs(status, available_at, created_at);
CREATE TABLE tool_explanation_demands (
    cache_key TEXT NOT NULL REFERENCES tool_explanation_jobs(cache_key) ON DELETE CASCADE,
    demand_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY(cache_key, demand_id)
);
CREATE INDEX tool_explanation_demands_owner ON tool_explanation_demands(demand_id);
CREATE TABLE tool_explanation_releases (
    demand_id TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
CREATE INDEX tool_explanation_releases_expiry ON tool_explanation_releases(expires_at);
"""
_SCHEMA_SQL += _EXPLANATION_CACHE_SCHEMA
from .tool_explanation_mappings import SCHEMA as _EXPLANATION_MAPPINGS_SCHEMA
_SCHEMA_SQL += _EXPLANATION_MAPPINGS_SCHEMA
from .html_forms import SCHEMA as _HTML_FORMS_SCHEMA
_SCHEMA_SQL += _HTML_FORMS_SCHEMA
from .model_fallbacks import SCHEMA as _MODEL_FALLBACK_SCHEMA
_SCHEMA_SQL += _MODEL_FALLBACK_SCHEMA
from .podcast_history import SCHEMA as _PODCAST_HISTORY_SCHEMA
_SCHEMA_SQL += _PODCAST_HISTORY_SCHEMA
from .agent_goals import SCHEMA as _AGENT_GOALS_SCHEMA
from .oracle_memory import SCHEMA as _ORACLE_MEMORY_SCHEMA
_SCHEMA_SQL += _ORACLE_MEMORY_SCHEMA
_SCHEMA_SQL += _AGENT_GOALS_SCHEMA


_JANITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS janitor_configs (
    agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
    template_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    generation INTEGER NOT NULL DEFAULT 1,
    scope_json TEXT NOT NULL DEFAULT '{}',
    execution_json TEXT NOT NULL DEFAULT '{}',
    options_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT NOT NULL DEFAULT '',
    last_run_at INTEGER,
    last_change_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS janitor_trigger_definitions (
    trigger_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    defaults_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (trigger_id, version)
);
CREATE TABLE IF NOT EXISTS janitor_attachments (
    attachment_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES janitor_configs(agent_id),
    trigger_id TEXT NOT NULL,
    trigger_version INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    next_run_at INTEGER,
    retired_at INTEGER,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(trigger_id, trigger_version)
        REFERENCES janitor_trigger_definitions(trigger_id, version)
);
CREATE INDEX IF NOT EXISTS idx_janitor_attachments_agent
    ON janitor_attachments(agent_id, retired_at);
CREATE TABLE IF NOT EXISTS janitor_progress (
    attachment_id TEXT PRIMARY KEY REFERENCES janitor_attachments(attachment_id),
    generation INTEGER NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS janitor_runs (
    run_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES janitor_configs(agent_id),
    session TEXT NOT NULL,
    attachment_id TEXT NOT NULL REFERENCES janitor_attachments(attachment_id),
    generation INTEGER NOT NULL,
    trace_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued',
    outcome TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL,
    configuration_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_janitor_runs_agent
    ON janitor_runs(agent_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_janitor_runs_one_active
    ON janitor_runs(agent_id) WHERE status IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS janitor_effects (
    run_id TEXT NOT NULL REFERENCES janitor_runs(run_id),
    target_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    target_session TEXT NOT NULL,
    observed_state_id INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    before_label TEXT NOT NULL,
    after_label TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY(run_id, target_agent_id)
);
CREATE TABLE IF NOT EXISTS janitor_label_ownership (
    target_agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
    owner_agent_id TEXT NOT NULL REFERENCES janitor_configs(agent_id),
    run_id TEXT NOT NULL REFERENCES janitor_runs(run_id),
    label TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    task_signature TEXT NOT NULL,
    valid_until INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS janitor_creation_requests (
    request_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    session TEXT NOT NULL UNIQUE,
    agent_id TEXT REFERENCES agents(agent_id),
    response_json TEXT,
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS janitor_pilot_imports (
    import_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES janitor_configs(agent_id),
    imported_at INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL
);
INSERT OR IGNORE INTO janitor_trigger_definitions
    (trigger_id, version, name, kind, defaults_json) VALUES
    ('agent-work-completed', 1, 'After an agent finishes a turn', 'event',
     '{"coalesce_seconds":8,"max_targets":3}'),
    ('schedule', 1, 'On a schedule', 'schedule',
     '{"cron":"0 9 * * *","timezone":"UTC","max_targets":3}');
"""
_SCHEMA_SQL += _JANITOR_SCHEMA

_ACTIVE_INTERVAL_TRIGGER = """INSERT OR IGNORE INTO janitor_trigger_definitions
    (trigger_id,version,name,kind,defaults_json) VALUES
    ('active-interval',1,'Periodically while using the app','interval',
     '{"interval_seconds":900,"idle_timeout_seconds":300,"run_on_resume":true,"max_targets":3,"coalesce_seconds":0}')"""
_SCHEMA_SQL += _ACTIVE_INTERVAL_TRIGGER + ";"


_BUILTIN_JANITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS janitor_builtins (
    role TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL UNIQUE REFERENCES agents(agent_id),
    seed_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS janitor_demand_results (
    run_id TEXT PRIMARY KEY REFERENCES janitor_runs(run_id),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS janitor_demand_claims (
    run_id TEXT PRIMARY KEY REFERENCES janitor_runs(run_id),
    claimed_at INTEGER NOT NULL,
    finished_at INTEGER
);
INSERT OR IGNORE INTO janitor_trigger_definitions
    (trigger_id,version,name,kind,defaults_json) VALUES
    ('routing-requested',1,'When a message needs a recipient','demand','{}'),
    ('tool-explanation-requested',1,'When a tool explanation is requested','demand','{}');
CREATE TRIGGER IF NOT EXISTS janitor_demand_trigger_no_update
BEFORE UPDATE ON janitor_trigger_definitions
WHEN OLD.kind='demand'
BEGIN SELECT RAISE(ABORT,'Demand trigger versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS janitor_demand_trigger_no_delete
BEFORE DELETE ON janitor_trigger_definitions
WHEN OLD.kind='demand'
BEGIN SELECT RAISE(ABORT,'Demand trigger versions are immutable'); END;
"""
_SCHEMA_SQL += _BUILTIN_JANITOR_SCHEMA


from .audio_bookkeeper import SCHEMA_STATEMENTS as _AUDIO_BOOKKEEPING_SCHEMA
_SCHEMA_SQL += ";\n".join(_AUDIO_BOOKKEEPING_SCHEMA) + ";"
