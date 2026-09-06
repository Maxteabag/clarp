"""Authenticated foreground/input leases for maintenance, independent of APNs."""
import hashlib
import json
import uuid
from . import db

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
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("DELETE FROM settings WHERE key LIKE ? AND updated_at<?", (PREFIX + "%", now - 300_000))
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row and sequence <= json.loads(row[0])["sequence"]:
            c.execute("COMMIT")
            return {"accepted": False}
        if not row and c.execute("SELECT COUNT(*) FROM settings WHERE key LIKE ?", (PREFIX + "%",)).fetchone()[0] >= 256:
            raise ValueError("too many activity instances")
        value = json.dumps({"owner": principal, "sequence": sequence, "foreground": foreground,
            "expires_at": min(now, sent_at_ms) + 45_000, "input_at": min(now, sent_at_ms) - input_age_ms})
        c.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key,value,now))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return {"accepted": True}


def active(idle_timeout_seconds):
    now = db.now_ms()
    for row in db.conn().execute("SELECT value FROM settings WHERE key LIKE ? AND updated_at BETWEEN ? AND ?", (PREFIX + "%", now - 45_000, now)):
        try:
            value = json.loads(row[0])
            if not value["foreground"] or value["expires_at"] <= now or not 0 <= now - value["input_at"] < idle_timeout_seconds * 1000:
                continue
            if value["owner"] == "administrator" or db.conn().execute("SELECT 1 FROM paired_devices WHERE device_id=? AND revoked_at IS NULL AND scope='full'", (value["owner"],)).fetchone():
                return True
        except (ValueError, KeyError, TypeError):
            continue
    return False
