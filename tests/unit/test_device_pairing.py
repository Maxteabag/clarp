from __future__ import annotations

import hashlib

import pytest

from lib import db, device_pairing


def test_pairing_code_is_single_use_and_stores_only_hashes():
    issued = device_pairing.issue(
        device_name="Peter's iPhone", scope="full", ttl_seconds=600)
    assert issued["code"].startswith("clp_")
    row = db.conn().execute("SELECT * FROM pairing_codes").fetchone()
    assert row["code_hash"] == hashlib.sha256(
        issued["code"].encode()).hexdigest()
    assert issued["code"] not in tuple(str(value) for value in row)

    paired = device_pairing.exchange(
        issued["code"], device_name="My iPhone")
    assert paired["token"].startswith("cld_")
    assert paired["scope"] == "full"
    stored = db.conn().execute("SELECT * FROM paired_devices").fetchone()
    assert stored["token_hash"] == hashlib.sha256(
        paired["token"].encode()).hexdigest()
    assert paired["token"] not in tuple(str(value) for value in stored)
    assert device_pairing.authenticate(paired["token"])["device_id"] == paired[
        "device_id"]

    with pytest.raises(device_pairing.PairingError, match="already used"):
        device_pairing.exchange(issued["code"])


def test_expired_pairing_code_is_rejected():
    issued = device_pairing.issue(ttl_seconds=30)
    db.conn().execute(
        "UPDATE pairing_codes SET expires_at = ?", (db.now_ms() - 1,))
    with pytest.raises(device_pairing.PairingError, match="expired"):
        device_pairing.exchange(issued["code"])


def test_limited_device_can_be_revoked():
    issued = device_pairing.issue(scope="limited")
    paired = device_pairing.exchange(issued["code"])
    assert device_pairing.authenticate(paired["token"])["scope"] == "limited"
    assert device_pairing.revoke(paired["device_id"]) is True
    assert device_pairing.authenticate(paired["token"]) is None
    assert device_pairing.revoke(paired["device_id"]) is False


def test_last_seen_is_written_at_most_once_a_minute(monkeypatch):
    # Every authenticated request used to UPDATE paired_devices, taking the
    # write lock behind long imports (2026-09-20). Reads stay lock-free now.
    issued = device_pairing.issue(ttl_seconds=600)
    paired = device_pairing.exchange(issued["code"], device_name="iPhone")
    db.conn().execute("UPDATE paired_devices SET last_seen_at=0")
    base = db.now_ms()
    clock = {"now": base}
    monkeypatch.setattr(db, "now_ms", lambda: clock["now"])

    device_pairing.authenticate(paired["token"])
    first = db.conn().execute(
        "SELECT last_seen_at FROM paired_devices").fetchone()["last_seen_at"]
    assert first == base

    clock["now"] = base + 30_000
    assert device_pairing.authenticate(paired["token"])["last_seen_at"] == base
    assert db.conn().execute(
        "SELECT last_seen_at FROM paired_devices").fetchone()["last_seen_at"] == base

    clock["now"] = base + 61_000
    assert device_pairing.authenticate(paired["token"])["last_seen_at"] == base + 61_000
    assert db.conn().execute(
        "SELECT last_seen_at FROM paired_devices").fetchone()["last_seen_at"] == base + 61_000


def test_last_seen_contention_does_not_break_valid_authentication():
    import sqlite3
    issued = device_pairing.issue()
    paired = device_pairing.exchange(issued['code'])
    db.conn().execute('UPDATE paired_devices SET last_seen_at=0')
    with sqlite3.connect(str(db.DB_PATH), timeout=1) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with db.busy_timeout(20):
            result = device_pairing.authenticate(paired['token'])
        assert result['device_id'] == paired['device_id']
        assert result['last_seen_at'] == 0
        writer.rollback()
    device_pairing.revoke(paired['device_id'])
    assert device_pairing.authenticate(paired['token']) is None
