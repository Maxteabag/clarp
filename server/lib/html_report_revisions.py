"""Mutable read-only reports with immutable publication receipts."""
from __future__ import annotations

SCHEMA = '''CREATE TABLE IF NOT EXISTS html_report_revisions (
 artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 version TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 title TEXT NOT NULL,
 summary TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(artifact_id, version)
);'''

import json
import uuid
from . import db


def is_report(row):
    from .html_forms import READ_ONLY_SCHEMA
    return row['type'] == 'html_form' and row['payload'].get('read_only') is True and row['payload'].get('answer_schema') == READ_ONLY_SCHEMA


def _remember(row):
    db.conn().execute('''INSERT OR IGNORE INTO html_report_revisions
        (artifact_id,version,payload_json,title,summary,created_at) VALUES(?,?,?,?,?,?)''',
        (row['artifact_id'], row['version'], json.dumps(row['payload'], ensure_ascii=False, sort_keys=True),
         row['title'], row['summary'], row['updated_at']))


def publish(agent, artifact_id, title, summary, status, reference_id, payload, expected_version=None):
    from . import artifacts
    con = db.conn()
    con.execute('BEGIN IMMEDIATE')
    try:
        current = artifacts.get(artifact_id)
        if current:
            if current['agent_id'] != agent['agent_id']:
                raise ValueError('report artifact belongs to another agent')
            row = _revise(current, {'payload': payload, 'title': title, 'summary': summary,
                'expected_version': expected_version}, require_base=True)
        else:
            row = artifacts._insert_artifact(agent, artifact_id, 'html_form', title, summary, status, reference_id, payload)
            _remember(row)
        con.execute('COMMIT')
        return row
    except BaseException:
        con.execute('ROLLBACK')
        raise


def update(artifact_id, data):
    from . import artifacts
    con = db.conn()
    con.execute('BEGIN IMMEDIATE')
    try:
        current = artifacts.get(artifact_id)
        if not current:
            raise ValueError('artifact not found')
        row = _revise(current, data)
        con.execute('COMMIT')
        return row
    except BaseException:
        con.execute('ROLLBACK')
        raise


def _revise(current, data, *, require_base=False):
    from . import artifacts, html_forms
    if not is_report(current):
        raise ValueError('HTML forms are immutable; publish a new artifact/version')
    if 'payload' in data and 'payload_patch' in data:
        raise ValueError('provide payload or payload_patch, not both')
    incoming = data.get('payload', data.get('payload_patch', {}))
    if not isinstance(incoming, dict):
        raise ValueError('payload must be an object')
    value = {**current['payload'], **incoming} if 'payload_patch' in data else incoming
    payload = artifacts._payload(html_forms.normalize(value), preserve_html=True)
    if payload.get('read_only') is not True:
        raise ValueError('a report revision must remain read-only')
    if 'version' not in incoming and payload != current['payload']:
        payload['version'] = 'revision-' + uuid.uuid4().hex
    artifacts._require_payload('html_form', payload, current['artifact_id'], current['session'])
    _remember(current)
    previous = db.conn().execute('SELECT * FROM html_report_revisions WHERE artifact_id=? AND version=?',
        (current['artifact_id'], payload['version'])).fetchone()
    if previous:
        if json.loads(previous['payload_json']) != payload:
            raise ValueError('report version already used for different content; choose a new version')
        # A lost response can be retried after another revision has landed.
        # Return the latest link target rather than rolling it back.
        if payload['version'] != current['version'] or (payload == current['payload'] and require_base):
            return current
    base = data.get('expected_version')
    if (base is not None and base != current['version']) or (require_base and previous is None and base is None):
        raise ValueError('expected_version must match the current report version')
    change = {key: value for key, value in data.items() if key != 'payload_patch'}
    change['payload'] = payload
    row = artifacts._apply_update(current, change, report_payload=True)
    _remember(row)
    return row


def revisions(artifact_id, version=None, *, limit=50, offset=0):
    from . import artifacts
    current = artifacts.get(artifact_id)
    if not current or not is_report(current):
        raise ValueError('read-only report not found')
    if version is not None:
        row = db.conn().execute('SELECT * FROM html_report_revisions WHERE artifact_id=? AND version=?',
            (artifact_id, version)).fetchone()
        if not row:
            if version == current['version']:
                return {'version': version, 'payload': current['payload'], 'title': current['title'],
                        'summary': current['summary'], 'created_at': current['updated_at']}
            raise ValueError('report revision not found')
        value = dict(row)
        value['payload'] = json.loads(value.pop('payload_json'))
        return value
    rows = db.conn().execute('''SELECT version,title,summary,created_at FROM html_report_revisions
        WHERE artifact_id=? ORDER BY created_at DESC,version DESC LIMIT ? OFFSET ?''',
        (artifact_id, max(1, min(int(limit), 100)), max(0, int(offset)))).fetchall()
    if rows:
        return [dict(row) for row in rows]
    if offset or db.conn().execute('SELECT 1 FROM html_report_revisions WHERE artifact_id=? LIMIT 1', (artifact_id,)).fetchone():
        return []
    return [{'version': current['version'], 'title': current['title'],
             'summary': current['summary'], 'created_at': current['updated_at']}]
