"""Schema of lib.audio_bookkeeper, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""

SCHEMA_STATEMENTS = [
"""CREATE TABLE IF NOT EXISTS audio_bookkeeping_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,
 clip_id INTEGER NOT NULL, agent_id TEXT NOT NULL,
 turn_id INTEGER, runtime_id INTEGER, trace_id TEXT,
 stage TEXT NOT NULL, created_at INTEGER NOT NULL,
 run_id TEXT, completed_at INTEGER,
 UNIQUE(clip_id,stage))""",
"""INSERT OR IGNORE INTO janitor_trigger_definitions
 (trigger_id,version,name,kind,defaults_json) VALUES
 ('audio-lifecycle-observed',1,'When audio lifecycle facts are observed','demand','{}')""",
"""CREATE TRIGGER IF NOT EXISTS audio_bookkeeping_created AFTER INSERT ON clips
 BEGIN
 INSERT OR IGNORE INTO audio_bookkeeping_events(clip_id,agent_id,turn_id,runtime_id,trace_id,stage,created_at)
 VALUES(NEW.clip_id,NEW.agent_id,NEW.turn_id,NEW.runtime_id,NEW.trace_id,'producer:'||COALESCE(NEW.producer_status,'complete'),NEW.created_at);
 INSERT OR IGNORE INTO audio_bookkeeping_events(clip_id,agent_id,turn_id,runtime_id,trace_id,stage,created_at)
 VALUES(NEW.clip_id,NEW.agent_id,NEW.turn_id,NEW.runtime_id,NEW.trace_id,'client:'||NEW.status,NEW.created_at);
 END""",
"""CREATE TRIGGER IF NOT EXISTS audio_bookkeeping_producer AFTER UPDATE OF producer_status ON clips
 WHEN NEW.producer_status IS NOT OLD.producer_status
 BEGIN
 INSERT OR IGNORE INTO audio_bookkeeping_events(clip_id,agent_id,turn_id,runtime_id,trace_id,stage,created_at)
 VALUES(NEW.clip_id,NEW.agent_id,NEW.turn_id,NEW.runtime_id,NEW.trace_id,'producer:'||NEW.producer_status,CAST(strftime('%s','now') AS INTEGER)*1000);
 END""",
"""CREATE TRIGGER IF NOT EXISTS audio_bookkeeping_client AFTER UPDATE OF status ON clips
 WHEN NEW.status IS NOT OLD.status
 BEGIN
 INSERT OR IGNORE INTO audio_bookkeeping_events(clip_id,agent_id,turn_id,runtime_id,trace_id,stage,created_at)
 VALUES(NEW.clip_id,NEW.agent_id,NEW.turn_id,NEW.runtime_id,NEW.trace_id,'client:'||NEW.status,CAST(strftime('%s','now') AS INTEGER)*1000);
 END""",
]
