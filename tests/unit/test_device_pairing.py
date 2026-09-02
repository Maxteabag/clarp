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
