"""Immutable HTML form contracts and durable, idempotent answer delivery."""
from __future__ import annotations
import html
import ipaddress
import hashlib
import json
import re
# Defined before `db`: db_schema imports SCHEMA while `db` is still loading.
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
 delivered_at INTEGER,
 synthesize_audio INTEGER NOT NULL DEFAULT 0 CHECK (synthesize_audio IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_form_submission_pending ON form_submissions(status,created_at);'''
from . import db


# A read-only form is an HTML report: the same sandboxed renderer, no answers.
READ_ONLY_SCHEMA = {'type': 'object', 'properties': {}, 'additionalProperties': False}


class ReadOnlyForm(ValueError):
    """A submission addressed a read-only report."""


def normalize(payload):
    """Fill in the empty answer schema a read-only report may omit."""
    if isinstance(payload, dict) and payload.get('read_only') is True and 'answer_schema' not in payload:
        payload = {**payload, 'answer_schema': dict(READ_ONLY_SCHEMA)}
    if isinstance(payload, dict) and 'connect_origins' in payload:
        return {**payload, 'connect_origins': connect_origins(payload['connect_origins'])}
    return payload


def connect_origins(value) -> list[str]:
    """Bounded exact HTTPS origins; never interpret URLs or CSP fragments as policy."""
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError('connect_origins must be an array of at most 8 exact HTTPS origins')
    result = []
    for origin in value:
        if not isinstance(origin, str) or len(origin) > 256:
            raise ValueError('invalid connect_origins origin')
        match = re.fullmatch(r'https://([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?', origin, flags=re.ASCII)
        if not match:
            raise ValueError('connect_origins requires exact HTTPS origins without paths or credentials')
        host, port = match.groups()
        host = host.lower()
        labels = host.split('.')
        if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels):
            raise ValueError('invalid connect_origins hostname')
        # Browsers rewrite abbreviated/decimal/hex IPv4 names; reject those aliases.
        if labels[-1].isdigit() or labels[-1].startswith('0x'):
            try:
                if str(ipaddress.IPv4Address(host)) != host:
                    raise ValueError()
            except ValueError:
                raise ValueError('invalid connect_origins IPv4 address') from None
        number = int(port) if port else 443
        if not 1 <= number <= 65535:
            raise ValueError('invalid connect_origins port')
        canonical = 'https://' + host + (':' + str(number) if number != 443 else '')
        if canonical not in result:
            result.append(canonical)
    return result


def content_security_policy(payload: dict) -> str:
    """A corrupt stored policy fails closed; page content cannot widen the header."""
    try:
        origins = connect_origins(payload.get('connect_origins', []))
    except ValueError:
        origins = []
    connect = ' '.join(origins) or "'none'"
    return ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; font-src data:; media-src data:; connect-src " + connect +
            "; frame-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'")


def render_html(payload: dict) -> tuple[bytes, str]:
    """Read-only browser rendering, with an opaque origin and no Host answer bridge."""
    policy = content_security_policy(payload)
    try:
        origins = connect_origins(payload.get('connect_origins', []))
    except ValueError:
        origins = []
    capabilities = json.dumps({'network': origins}, separators=(',', ':'))
    disclosure = ('<details style="position:fixed;top:8px;right:8px;background:white;color:black;padding:6px;font:14px system-ui">'
                  '<summary>Network access</summary>' + ('<br>'.join(origins) or 'External connections blocked') + '</details>')
    page = ('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">'
            '<script>window.clarpForm={capabilities:' + capabilities + '};</script>' + payload['content'] + disclosure)
    return page.encode(), 'sandbox allow-scripts; ' + policy


def validate_contract(payload: dict) -> None:
    from jsonschema import Draft202012Validator
    if not isinstance(payload.get('content'), str) or not payload['content'].strip():
        raise ValueError('HTML form requires content')
    if not isinstance(payload.get('version'), str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', payload['version']):
        raise ValueError('HTML form requires a stable version string')
    if 'connect_origins' in payload:
        connect_origins(payload['connect_origins'])
    read_only = payload.get('read_only', False)
    if not isinstance(read_only, bool):
        raise ValueError('read_only must be a boolean')
    schema = payload.get('answer_schema')
    if read_only and schema != READ_ONLY_SCHEMA:
        raise ValueError('a read-only report cannot declare answers; omit answer_schema')
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
    synthesize_audio = data.get('synthesize_audio', True)
    if not isinstance(synthesize_audio, bool): raise ValueError('synthesize_audio must be a boolean')
    version = data.get('version')
    digest = hashlib.sha256(json.dumps([artifact_id, version, encoded]).encode()).hexdigest()
    con = db.conn()
    con.execute('BEGIN IMMEDIATE')
    try:
        previous = con.execute('SELECT * FROM form_submissions WHERE submission_id=?', (submission_id,)).fetchone()
        if previous:
            if previous['payload_hash'] != digest: raise ValueError('submission ID already used for different answers')
            if 'synthesize_audio' in data and bool(previous['synthesize_audio']) != synthesize_audio:
                raise ValueError('submission ID already used for a different audio preference')
            result = receipt(previous)
        else:
            form = artifacts.get(artifact_id)
            if not form or form['type'] != 'html_form': raise ValueError('HTML form not found')
            if form['payload'].get('read_only') is True:
                raise ReadOnlyForm('read-only report; it does not accept answers')
            # Archiving only hides the inbox entry; it does not close the form.
            if form['status'] not in {'ready', 'active'}:
                raise ValueError('HTML form no longer accepts answers')
            if version != form['payload']['version']: raise ValueError('form version mismatch; reopen the form')
            try: Draft202012Validator(form['payload']['answer_schema']).validate(answers)
            except Exception as exc: raise ValueError('answers do not match the form schema: '+str(exc).split('\n')[0][:300]) from exc
            prompt = (f"The user submitted answers to HTML form {form['title']!r}.\n"
                      f"Artifact ID: {artifact_id}\nVersion: {version}\nSubmission ID: {submission_id}\n"
                      "These are user preferences for review, not approval to perform protected actions. "
                      "Interpret the answers in the context of the originating plan.\nAnswers:\n"+encoded)
            con.execute('''INSERT INTO form_submissions(submission_id,artifact_id,version,session,
                answers_json,payload_hash,prompt,created_at,synthesize_audio) VALUES(?,?,?,?,?,?,?,?,?)''',
                (submission_id, artifact_id, version, form['session'], encoded, digest, prompt, db.now_ms(), int(synthesize_audio)))
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
