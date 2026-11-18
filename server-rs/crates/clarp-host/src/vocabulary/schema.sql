CREATE TABLE IF NOT EXISTS pack (
    pack_id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
    generator TEXT NOT NULL, priority REAL NOT NULL, floor INTEGER NOT NULL, enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS term (
    term_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id TEXT NOT NULL REFERENCES pack ON DELETE CASCADE,
    text TEXT NOT NULL COLLATE NOCASE, say_as TEXT NOT NULL, often_heard_as TEXT NOT NULL,
    rarity REAL NOT NULL, source TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(pack_id,text)
);
CREATE TABLE IF NOT EXISTS profile (
    profile_id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, harvests INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS member (
    profile_id TEXT NOT NULL REFERENCES profile ON DELETE CASCADE,
    pack_id TEXT NOT NULL REFERENCES pack ON DELETE CASCADE,
    position INTEGER NOT NULL, PRIMARY KEY(profile_id,pack_id)
);
CREATE TABLE IF NOT EXISTS assignment (
    profile_id TEXT NOT NULL REFERENCES profile ON DELETE CASCADE,
    agent_id TEXT UNIQUE, team_id TEXT UNIQUE, created_at INTEGER NOT NULL,
    CHECK((agent_id IS NULL) != (team_id IS NULL))
);
CREATE TABLE IF NOT EXISTS run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
    trace_id TEXT NOT NULL, agent_id TEXT NOT NULL, profile_id TEXT,
    provider TEXT NOT NULL, model TEXT NOT NULL, audit TEXT NOT NULL,
    transcript TEXT NOT NULL, latency_ms INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS run_session ON run(session,run_id DESC);
CREATE INDEX IF NOT EXISTS run_trace ON run(trace_id,run_id DESC);
