"""Append-only form telemetry. It never dispatches a turn or submits answers."""
from __future__ import annotations
import hashlib
import json
import re

SCHEMA = '''CREATE TABLE IF NOT EXISTS html_form_event_config (
 artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id),
 draft_key TEXT
);
CREATE TABLE IF NOT EXISTS html_form_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 version TEXT NOT NULL,
 event_id TEXT NOT NULL,
 client_seq INTEGER NOT NULL,
 client_at INTEGER NOT NULL,
 received_at INTEGER NOT NULL,
 event_json TEXT NOT NULL,
 payload_hash TEXT NOT NULL,
 UNIQUE(artifact_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_html_form_events_artifact_seq ON html_form_events(artifact_id,seq);
CREATE TRIGGER IF NOT EXISTS html_form_events_no_update BEFORE UPDATE ON html_form_events
 BEGIN SELECT RAISE(ABORT,'Form events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS html_form_events_no_delete BEFORE DELETE ON html_form_events
 BEGIN SELECT RAISE(ABORT,'Form events are append-only'); END;'''
from . import db


def _form(artifact_id):
    from . import artifacts
    form = artifacts.get(artifact_id)
    if not form or form['type'] != 'html_form' or form['payload'].get('read_only') is True:
        raise ValueError('interactive form not found')
    return form


def append(artifact_id: str, data: dict) -> dict:
    if not isinstance(data, dict): raise ValueError('JSON object required')
    version = data.get('version')
    rows = data.get('events')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError('events must contain 1 to 32 records')
    normalized = []
    for row in rows:
        if not isinstance(row, dict): raise ValueError('event record must be an object')
        event_id = row.get('event_id')
        if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{16,80}', event_id):
            raise ValueError('invalid event_id')
        for key in ('client_seq', 'client_at'):
            if type(row.get(key)) is not int or not 0 <= row[key] <= 9007199254740991:
                raise ValueError(key + ' must be a safe nonnegative integer')
        event = row.get('event')
        if not isinstance(event, dict): raise ValueError('event must be an object')
        try: encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc: raise ValueError('event must be finite JSON') from exc
        if len(encoded.encode()) > 16384: raise ValueError('event exceeds 16 KiB')
        digest = hashlib.sha256(json.dumps([version,encoded]).encode()).hexdigest()
        normalized.append((event_id,row['client_seq'],row['client_at'],encoded,digest))
    con = db.conn(); con.execute('BEGIN IMMEDIATE')
    try:
        form = _form(artifact_id)
        if version != form['payload']['version']: raise ValueError('form version mismatch')
        accepted = []
        for event_id, client_seq, client_at, encoded, digest in normalized:
            old = con.execute('SELECT seq,payload_hash FROM html_form_events WHERE artifact_id=? AND event_id=?', (artifact_id,event_id)).fetchone()
            if old:
                if old['payload_hash'] != digest: raise ValueError('event ID already used for different data')
                seq = old['seq']
            else:
                seq = con.execute('''INSERT INTO html_form_events
                    (artifact_id,version,event_id,client_seq,client_at,received_at,event_json,payload_hash)
                    VALUES(?,?,?,?,?,?,?,?)''', (artifact_id,version,event_id,client_seq,client_at,db.now_ms(),encoded,digest)).lastrowid
            accepted.append({'event_id':event_id,'seq':seq})
        con.execute('COMMIT')
        return {'artifact_id':artifact_id,'version':version,'accepted':True,'events':accepted}
    except BaseException:
        con.execute('ROLLBACK'); raise


def read(artifact_id: str, *, after: int = 0, limit: int = 100, through: int | None = None) -> dict:
    _form(artifact_id)
    if type(after) is not int or not 0 <= after <= 9223372036854775807 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid event cursor or limit')
    con = db.conn()
    if through is None:
        through = con.execute('SELECT COALESCE(MAX(seq),0) FROM html_form_events WHERE artifact_id=?',(artifact_id,)).fetchone()[0]
    if type(through) is not int or not after <= through <= 9223372036854775807: raise ValueError('invalid snapshot cursor')
    rows = con.execute('SELECT * FROM html_form_events WHERE artifact_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?',
                       (artifact_id,after,through,limit+1)).fetchall()
    events = []
    for row in rows[:limit]:
        event = dict(row); event['event'] = json.loads(event.pop('event_json')); event.pop('payload_hash')
        events.append(event)
    return {'artifact_id':artifact_id,'events':events,'next_seq':events[-1]['seq'] if events else after,
            'snapshot_seq':through,'has_more':len(rows)>limit}


def configuration(artifact_id: str) -> dict:
    form = _form(artifact_id)
    row = db.conn().execute('SELECT draft_key FROM html_form_event_config WHERE artifact_id=?', (artifact_id,)).fetchone()
    return {'artifact_id':artifact_id,'version':form['payload']['version'], 'draft_key':row['draft_key'] if row else None}


def configure(artifact_id: str, data: dict) -> dict:
    if not isinstance(data, dict) or 'draft_key' not in data: raise ValueError('draft_key required; use null to disable')
    key = data['draft_key']
    if key is not None and (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', key)):
        raise ValueError('invalid draft_key')
    _form(artifact_id)
    db.conn().execute('INSERT INTO html_form_event_config(artifact_id,draft_key) VALUES(?,?) ON CONFLICT(artifact_id) DO UPDATE SET draft_key=excluded.draft_key', (artifact_id,key))
    return configuration(artifact_id)
