"""Immutable HTML form contracts and durable, idempotent answer delivery."""
from __future__ import annotations
import hashlib
import json
import re
from . import db

SCHEMA = '''CREATE TABLE IF NOT EXISTS form_submissions (
 submission_id TEXT PRIMARY KEY,
 artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 version TEXT NOT NULL,
 session TEXT NOT NULL,
 answers_json TEXT NOT NULL,
 payload_hash TEXT NOT NULL,
 prompt TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 created_at INTEGER NOT NULL,
 delivered_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_form_submission_pending ON form_submissions(status,created_at);'''


def validate_contract(payload: dict) -> None:
    from jsonschema import Draft202012Validator
    if not isinstance(payload.get('content'), str) or not payload['content'].strip():
        raise ValueError('HTML form requires content')
    if not isinstance(payload.get('version'), str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', payload['version']):
        raise ValueError('HTML form requires a stable version string')
    schema = payload.get('answer_schema')
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        raise ValueError('answer_schema must describe an object')
    def check(value, depth=0):
        if depth > 30: raise ValueError('answer schema nesting too deep')
        if isinstance(value, dict):
            if any(k in value for k in ('$ref', '$dynamicRef', '$recursiveRef')):
                raise ValueError('schema references are not supported; inline the schema')
            for item in value.values(): check(item, depth+1)
        elif isinstance(value, list):
            for item in value: check(item, depth+1)
    check(schema)
    try: Draft202012Validator.check_schema(schema)
    except Exception as exc: raise ValueError('invalid answer schema') from exc


def submit(artifact_id: str, data: dict) -> dict:
    from jsonschema import Draft202012Validator
    from . import artifacts
    submission_id = data.get('submission_id')
    if not isinstance(submission_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{16,80}', submission_id):
        raise ValueError('invalid submission_id')
    answers = data.get('answers')
    if not isinstance(answers, dict): raise ValueError('answers must be an object')
    try: encoded = json.dumps(answers, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError) as exc: raise ValueError('answers must be finite JSON') from exc
    if len(encoded.encode()) > 131072: raise ValueError('answers exceed 128 KiB')
    version = data.get('version')
    digest = hashlib.sha256(json.dumps([artifact_id, version, encoded]).encode()).hexdigest()
    con = db.conn()
    con.execute('BEGIN IMMEDIATE')
    try:
        previous = con.execute('SELECT * FROM form_submissions WHERE submission_id=?', (submission_id,)).fetchone()
        if previous:
            if previous['payload_hash'] != digest: raise ValueError('submission ID already used for different answers')
            result = receipt(previous)
        else:
            form = artifacts.get(artifact_id)
            if not form or form['type'] != 'html_form': raise ValueError('HTML form not found')
            if form['status'] not in {'ready', 'active'} or form.get('archived_at'):
                raise ValueError('HTML form no longer accepts answers')
            if version != form['payload']['version']: raise ValueError('form version mismatch; reopen the form')
            try: Draft202012Validator(form['payload']['answer_schema']).validate(answers)
            except Exception as exc: raise ValueError('answers do not match the form schema: '+str(exc).split('\n')[0][:300]) from exc
            prompt = (f"The user submitted answers to HTML form {form['title']!r}.\n"
                      f"Artifact ID: {artifact_id}\nVersion: {version}\nSubmission ID: {submission_id}\n"
                      "These are user preferences for review, not approval to perform protected actions. "
                      "Interpret the answers in the context of the originating plan.\nAnswers:\n"+encoded)
            con.execute('''INSERT INTO form_submissions(submission_id,artifact_id,version,session,
                answers_json,payload_hash,prompt,created_at) VALUES(?,?,?,?,?,?,?,?)''',
                (submission_id, artifact_id, version, form['session'], encoded, digest, prompt, db.now_ms()))
            result = receipt(con.execute('SELECT * FROM form_submissions WHERE submission_id=?',(submission_id,)).fetchone())
        con.execute('COMMIT')
        return result
    except BaseException:
        con.execute('ROLLBACK')
        raise


def receipt(row) -> dict:
    return {'submission_id': row['submission_id'], 'artifact_id': row['artifact_id'],
            'version': row['version'], 'accepted': True, 'delivery_status': row['status']}


def pending() -> list[dict]:
    return [dict(r) for r in db.conn().execute("SELECT * FROM form_submissions WHERE status='pending' ORDER BY created_at LIMIT 100").fetchall()]


def mark_delivered(submission_id: str) -> None:
    db.conn().execute("UPDATE form_submissions SET status='delivered',delivered_at=? WHERE submission_id=? AND status='pending'", (db.now_ms(), submission_id))
