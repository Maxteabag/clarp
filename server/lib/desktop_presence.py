"""Short-lived, authenticated desktop activity leases shared by Host processes.

This only gates mobile message alerts. It never changes unread/read state or
queues deferred alerts. Leases live under one `settings` key prefix written
only through `settings_store`, which avoids a schema migration.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from . import db, settings_store

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
    with settings_store.transaction():
        settings_store.prune_prefix_due(PREFIX, now=now, updated_before=now - TOMBSTONE_MS)
        stored = settings_store.get(key)
        previous = json.loads(stored) if stored is not None else {}
        if sequence <= previous.get('sequence', 0):
            return {'accepted': False, 'lease_ms': LEASE_MS}
        # Pruning runs on a timer; at the cap, prune now before refusing.
        if stored is None and settings_store.count_prefix(PREFIX) >= MAX_INSTANCES and (
                settings_store.prune_prefix(PREFIX, updated_before=now - TOMBSTONE_MS) == 0
                or settings_store.count_prefix(PREFIX) >= MAX_INSTANCES):
            raise ValueError('too many desktop instances')
        value = json.dumps({'owner': principal, 'sequence': sequence,
                            'expires_at': min(now, sent_at_ms) + LEASE_MS if active else 0})
        settings_store.set_text(key, value, updated_at=now)
    return {'accepted': True, 'lease_ms': LEASE_MS}


def active() -> bool:
    now = db.now_ms()
    for raw in settings_store.values_with_prefix(PREFIX, updated_after=now - LEASE_MS, updated_through=now):
        try:
            lease = json.loads(raw)
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
