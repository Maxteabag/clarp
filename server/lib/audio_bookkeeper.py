"""Deterministic Janitor-owned audio lifecycle observations.

SQLite triggers enqueue bounded facts atomically with producer/client status changes.
No primary agent prompt, tool or model executes bookkeeping. Observations describe
reported lifecycle stages, never acoustic output. Clip identity is Host-local.
"""


ROLE = 'audio-bookkeeper'
# Statements kept separate so migration remains inside its enclosing transaction.
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

def drain(limit=32):
    """Complete a bounded batch under current Janitor authority, never call a model.

    A paused/ambiguous owner leaves events pending. Each event is a first-observed
    stage, not a playback-attempt counter; repeated plays cannot invent new facts.
    Gate, run, result and outbox completion commit atomically under one write lock.
    """
    from . import db, janitors, janitor_builtins
    done = 0
    with janitors._write() as c:
        # Filter paused/out-of-scope targets before LIMIT: their older pending
        # observations must not starve an eligible target's bookkeeping.
        rows = c.execute("""SELECT e.* FROM audio_bookkeeping_events e
            JOIN agents a ON a.agent_id=e.agent_id
            WHERE e.completed_at IS NULL AND a.deleted_at IS NULL AND a.archived_at IS NULL
              AND a.is_janitor=0 AND EXISTS (
                SELECT 1 FROM janitor_configs jc WHERE jc.template_id=? AND jc.enabled=1
                AND (COALESCE(json_array_length(jc.scope_json,'$.agent_ids'),0)=0 OR
                     e.agent_id IN (SELECT value FROM json_each(jc.scope_json,'$.agent_ids')))
                AND e.agent_id NOT IN (SELECT value FROM json_each(jc.scope_json,'$.exclude_agent_ids')))
            ORDER BY e.event_id LIMIT ?""", (ROLE,max(1,min(int(limit),128)))).fetchall()
        for row in rows:
            target = c.execute('SELECT deleted_at,archived_at FROM agents WHERE agent_id=?',(row['agent_id'],)).fetchone()
            if not target or target['deleted_at'] or target['archived_at']:
                continue
            config = janitor_builtins.resolve(ROLE, target_agent_id=row['agent_id'])
            if not config:
                continue
            # Config read and write share the transaction: pause/generation changes
            # cannot race the committed result. No provider claim exists to expire.
            run_id = 'audio-bookkeeping-' + str(row['event_id'])
            now = db.now_ms()
            attachment = next(a for a in config['attachments'] if a['enabled'] and a['trigger_id']=='audio-lifecycle-observed')
            frozen = {'template_id':ROLE,'executor':'deterministic','generation':config['generation'],
                      'context':{'event_id':row['event_id'],'clip_id':row['clip_id'],'stage':row['stage'],
                                 'target_agent_id':row['agent_id'],'turn_id':row['turn_id'],'runtime_id':row['runtime_id']}}
            result = {'summary':'Audio lifecycle fact recorded','status':row['stage'],'item_count':1,
                      'target_agent_id':row['agent_id']}
            c.execute("""INSERT OR IGNORE INTO janitor_runs(run_id,agent_id,session,attachment_id,generation,trace_id,status,
                candidates_json,configuration_json,created_at,started_at,finished_at,outcome)
                VALUES(?,?,?,?,?,?,'completed','[]',?,?,?,?,'completed')""",
                (run_id,config['agent_id'],config['session'],attachment['attachment_id'],config['generation'],run_id,
                 janitors._json(frozen),now,now,now))
            c.execute('INSERT OR IGNORE INTO janitor_demand_results(run_id,result_json,created_at) VALUES(?,?,?)',
                      (run_id,janitors._json(result),now))
            c.execute('UPDATE audio_bookkeeping_events SET run_id=?,completed_at=? WHERE event_id=? AND completed_at IS NULL',
                      (run_id,now,row['event_id']))
            c.execute('UPDATE janitor_configs SET last_run_at=?,last_error=\'\' WHERE agent_id=?',(now,config['agent_id']))
            done += 1
    return done
