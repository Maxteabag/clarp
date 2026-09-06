"""Short-lived, authenticated desktop activity leases shared by Host processes.

This only gates mobile message alerts. It never changes unread/read state or
queues deferred alerts. Generic settings storage avoids a schema migration.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from . import db

LEASE_MS = 45_000
TOMBSTONE_MS = 300_000
PREFIX = 'desktop-presence:'
MAX_INSTANCES = 256


def update(*, principal: str, instance_id: str, sequence: int, active: bool, sent_at_ms: int) -> dict:
    if not principal:
        raise ValueError('authenticated principal required')
    try:
        instance_id = str(uuid.UUID(instance_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('instance_id must be a UUID') from None
    if type(sequence) is not int or not 0 < sequence <= 2**53 - 1:
        raise ValueError('sequence must be a positive safe integer')
    if type(active) is not bool:
        raise ValueError('active must be boolean')
    key = PREFIX + hashlib.sha256(f'{principal}\0{instance_id}'.encode()).hexdigest()
    now = db.now_ms()
    if type(sent_at_ms) is not int or not now - LEASE_MS <= sent_at_ms <= now + 5_000:
        raise ValueError('presence report is stale or clock is out of sync')
    connection = db.conn()
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute('DELETE FROM settings WHERE key LIKE ? AND updated_at < ?',
                           (PREFIX + '%', now - TOMBSTONE_MS))
        row = connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        previous = json.loads(row['value']) if row else {}
        if sequence <= previous.get('sequence', 0):
            connection.execute('COMMIT')
            return {'accepted': False, 'lease_ms': LEASE_MS}
        if row is None and connection.execute(
                'SELECT COUNT(*) FROM settings WHERE key LIKE ?', (PREFIX + '%',)).fetchone()[0] >= MAX_INSTANCES:
            raise ValueError('too many desktop instances')
        value = json.dumps({'owner': principal, 'sequence': sequence,
                            'expires_at': min(now, sent_at_ms) + LEASE_MS if active else 0})
        connection.execute('''INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at''',
                           (key, value, now))
        connection.execute('COMMIT')
    except Exception:
        connection.execute('ROLLBACK')
        raise
    return {'accepted': True, 'lease_ms': LEASE_MS}


def active() -> bool:
    now = db.now_ms()
    rows = db.conn().execute('''SELECT value FROM settings
        WHERE key LIKE ? AND updated_at > ? AND updated_at <= ?''',
                            (PREFIX + '%', now - LEASE_MS, now)).fetchall()
    for row in rows:
        try:
            lease = json.loads(row['value'])
            if lease.get('expires_at', 0) <= now:
                continue
            owner = lease.get('owner', '')
            if owner == 'administrator':
                return True
            if db.conn().execute('''SELECT 1 FROM paired_devices
                WHERE device_id=? AND revoked_at IS NULL AND scope='full' ''', (owner,)).fetchone():
                return True
        except (ValueError, TypeError, AttributeError):
            continue
    return False
