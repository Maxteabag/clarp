"""Authenticated foreground/input leases for maintenance, independent of APNs.

Leases live under one `settings` key prefix and are written only through
`settings_store`; this module owns the lease semantics, not the table.
"""
import hashlib
import json
import uuid
from . import db, settings_store

PREFIX = "application-activity:"


def report(principal, instance_id, sequence, foreground, input_age_ms, sent_at_ms):
    try:
        instance_id = str(uuid.UUID(instance_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("instance_id must be a UUID") from None
    now = db.now_ms()
    if not principal or type(sequence) is not int or not 0 < sequence < 2**53:
        raise ValueError("invalid principal or sequence")
    if type(foreground) is not bool or type(input_age_ms) is not int or not 0 <= input_age_ms <= 86_400_000:
        raise ValueError("invalid foreground or input age")
    if type(sent_at_ms) is not int or not now - 45_000 <= sent_at_ms <= now + 5_000:
        raise ValueError("stale activity report")
    key = PREFIX + hashlib.sha256(f"{principal}\0{instance_id}".encode()).hexdigest()
    with settings_store.transaction():
        settings_store.prune_prefix_due(PREFIX, now=now, updated_before=now - 300_000)
        stored = settings_store.get(key)
        if stored is not None and sequence <= json.loads(stored)["sequence"]:
            return {"accepted": False}
        # Pruning runs on a timer; at the cap, prune now before refusing.
        if stored is None and settings_store.count_prefix(PREFIX) >= 256 and (
                settings_store.prune_prefix(PREFIX, updated_before=now - 300_000) == 0
                or settings_store.count_prefix(PREFIX) >= 256):
            raise ValueError("too many activity instances")
        value = json.dumps({"owner": principal, "sequence": sequence, "foreground": foreground,
            "expires_at": min(now, sent_at_ms) + 45_000, "input_at": min(now, sent_at_ms) - input_age_ms})
        settings_store.set_text(key, value, updated_at=now)
    return {"accepted": True}


def active(idle_timeout_seconds):
    now = db.now_ms()
    for raw in settings_store.values_with_prefix(PREFIX, updated_after=now - 45_001, updated_through=now):
        try:
            value = json.loads(raw)
            if not value["foreground"] or value["expires_at"] <= now or not 0 <= now - value["input_at"] < idle_timeout_seconds * 1000:
                continue
            if value["owner"] == "administrator" or db.conn().execute("SELECT 1 FROM paired_devices WHERE device_id=? AND revoked_at IS NULL AND scope='full'", (value["owner"],)).fetchone():
                return True
        except (ValueError, KeyError, TypeError):
            continue
    return False
