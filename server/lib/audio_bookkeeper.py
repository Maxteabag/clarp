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

_PENDING_SQL = """SELECT e.* FROM audio_bookkeeping_events e
    JOIN agents a ON a.agent_id=e.agent_id
    WHERE e.completed_at IS NULL AND a.deleted_at IS NULL AND a.archived_at IS NULL
      AND a.is_janitor=0 AND EXISTS (
        SELECT 1 FROM janitor_configs jc WHERE jc.template_id=? AND jc.enabled=1
        AND (COALESCE(json_array_length(jc.scope_json,'$.agent_ids'),0)=0 OR
             e.agent_id IN (SELECT value FROM json_each(jc.scope_json,'$.agent_ids')))
        AND e.agent_id NOT IN (SELECT value FROM json_each(jc.scope_json,'$.exclude_agent_ids')))
    ORDER BY e.event_id LIMIT ?"""


def drain(limit=32):
    """Complete a bounded batch under current Janitor authority, never call a model.

    A paused/ambiguous owner leaves events pending. Each event is a first-observed
    stage, not a playback-attempt counter; repeated plays cannot invent new facts.
    Pending rows are read and the owner resolved without the write lock, so an
    idle tick never takes it. Inside the lock only a configuration fingerprint is
    compared; a change since the resolve leaves the event for the next tick.
    """
    from . import db, janitor_builtins, janitor_store
    # Filter paused/out-of-scope targets before LIMIT: their older pending
    # observations must not starve an eligible target's bookkeeping.
    rows = db.conn().execute(_PENDING_SQL, (ROLE, max(1, min(int(limit), 128)))).fetchall()
    if not rows:
        return 0
    owners = {}
    for row in rows:
        target = row['agent_id']
        if target not in owners:
            # Fingerprint first: a change during resolve() makes the check below fail.
            revision = janitor_store.role_revision(ROLE, target)
            owners[target] = (revision, janitor_builtins.resolve(ROLE, target_agent_id=target))
    done = 0
    with janitor_store.write() as c:
        current = {}
        for row in rows:
            revision, config = owners[row['agent_id']]
            if not config:
                continue
            if row['agent_id'] not in current:
                current[row['agent_id']] = janitor_store.role_revision(ROLE, row['agent_id'], c)
            if current[row['agent_id']] != revision:
                continue
            run_id = 'audio-bookkeeping-' + str(row['event_id'])
            now = db.now_ms()
            # A concurrent drain may have completed this event since the read.
            if not c.execute('UPDATE audio_bookkeeping_events SET run_id=?,completed_at=? WHERE event_id=? AND completed_at IS NULL',
                             (run_id, now, row['event_id'])).rowcount:
                continue
            attachment = next(a for a in config['attachments'] if a['enabled'] and a['trigger_id']=='audio-lifecycle-observed')
            frozen = {'template_id':ROLE,'executor':'deterministic','generation':config['generation'],
                      'context':{'event_id':row['event_id'],'clip_id':row['clip_id'],'stage':row['stage'],
                                 'target_agent_id':row['agent_id'],'turn_id':row['turn_id'],'runtime_id':row['runtime_id']}}
            result = {'summary':'Audio lifecycle fact recorded','status':row['stage'],'item_count':1,
                      'target_agent_id':row['agent_id']}
            janitor_store.insert_run(c,run_id=run_id,agent_id=config['agent_id'],session=config['session'],
                attachment_id=attachment['attachment_id'],generation=config['generation'],trace_id=run_id,
                status='completed',outcome='completed',candidates=[],configuration=frozen,
                created_at=now,started_at=now,finished_at=now,ignore_existing=True)
            janitor_store.insert_demand_result(c,run_id,result,now,ignore_existing=True)
            janitor_store.record_config_run(c,config['agent_id'],now,touch_updated=False)
            done += 1
    return done
