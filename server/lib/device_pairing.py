"""One-time bootstrap pairing and revocable device-token authentication."""
from __future__ import annotations

import hashlib
import secrets
import uuid

from . import db

DEFAULT_TTL_SECONDS = 10 * 60
MAX_TTL_SECONDS = 60 * 60
VALID_SCOPES = frozenset({"full", "limited"})


class PairingError(ValueError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue(
    *, device_name: str = "iPhone", scope: str = "full",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict:
    name = " ".join(device_name.strip().split())[:80] or "iPhone"
    scope = scope.strip().lower()
    if scope not in VALID_SCOPES:
        raise PairingError("scope must be full or limited")
    ttl_seconds = int(ttl_seconds)
    if ttl_seconds < 30 or ttl_seconds > MAX_TTL_SECONDS:
        raise PairingError("pairing expiry must be between 30 and 3600 seconds")
    code = "clp_" + secrets.token_urlsafe(32)
    now = db.now_ms()
    expires = now + ttl_seconds * 1000
    connection = db.conn()
    connection.execute(
        "DELETE FROM pairing_codes WHERE expires_at < ? OR used_at IS NOT NULL",
        (now,),
    )
    connection.execute(
        """INSERT INTO pairing_codes(
               code_hash,device_name,scope,created_at,expires_at,used_at
           ) VALUES(?,?,?,?,?,NULL)""",
        (_digest(code), name, scope, now, expires),
    )
    return {
        "code": code, "device_name": name, "scope": scope,
        "created_at": now, "expires_at": expires,
    }


def exchange(code: str, *, device_name: str = "") -> dict:
    code = code.strip()
    if not code.startswith("clp_") or len(code) < 30:
        raise PairingError("invalid pairing code")
    connection = db.conn()
    now = db.now_ms()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """SELECT device_name,scope,expires_at,used_at
                 FROM pairing_codes WHERE code_hash = ?""",
            (_digest(code),),
        ).fetchone()
        if row is None or row["used_at"] is not None:
            raise PairingError("pairing code is invalid or already used")
        if int(row["expires_at"]) < now:
            raise PairingError("pairing code has expired")
        token = "cld_" + secrets.token_urlsafe(36)
        device_id = "device_" + uuid.uuid4().hex
        name = " ".join((device_name or row["device_name"]).strip().split())[:80]
        connection.execute(
            """INSERT INTO paired_devices(
                   device_id,name,token_hash,scope,created_at,last_seen_at,revoked_at
               ) VALUES(?,?,?,?,?,?,NULL)""",
            (device_id, name or "iPhone", _digest(token), row["scope"], now, now),
        )
        connection.execute(
            "UPDATE pairing_codes SET used_at = ? WHERE code_hash = ?",
            (now, _digest(code)),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return {
        "device_id": device_id, "device_name": name or "iPhone",
        "scope": row["scope"], "token": token, "created_at": now,
    }


def authenticate(token: str) -> dict | None:
    token = token.strip()
    if not token.startswith("cld_") or len(token) < 30:
        return None
    row = db.conn().execute(
        """SELECT device_id,name,scope,created_at,last_seen_at
             FROM paired_devices
            WHERE token_hash = ? AND revoked_at IS NULL""",
        (_digest(token),),
    ).fetchone()
    if row is None:
        return None
    now = db.now_ms()
    db.conn().execute(
        "UPDATE paired_devices SET last_seen_at = ? WHERE device_id = ?",
        (now, row["device_id"]),
    )
    return dict(row) | {"last_seen_at": now}


def list_devices(*, include_revoked: bool = False) -> list[dict]:
    where = "" if include_revoked else "WHERE revoked_at IS NULL"
    rows = db.conn().execute(
        f"""SELECT device_id,name,scope,created_at,last_seen_at,revoked_at
               FROM paired_devices {where}
              ORDER BY created_at DESC"""
    ).fetchall()
    return [dict(row) for row in rows]


def revoke(device_id: str) -> bool:
    result = db.conn().execute(
        """UPDATE paired_devices SET revoked_at = ?
            WHERE device_id = ? AND revoked_at IS NULL""",
        (db.now_ms(), device_id.strip()),
    )
    return result.rowcount == 1
