"""The client contract: constants, header parsing, the verdict, and the
compatibility table that documents them (docs/compatibility.md)."""
import pathlib
import re

from lib import server_identity

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_contract_block_is_advertised_and_monotonic():
    info = server_identity.get_server_info()
    contract = info["contract"]
    assert contract["host"] == server_identity.HOST_CONTRACT >= 1
    assert contract["min_ios"] == server_identity.MIN_IOS_CONTRACT >= 1
    assert contract["min_ios"] <= contract["host"]
    assert set(contract["features"]) == set(server_identity.FEATURES)
    assert all(1 <= value <= contract["host"] for value in contract["features"].values())


def test_client_header_parsing():
    parse = server_identity.parse_client_header
    assert parse("ios/2620 contract=1") == {"platform": "ios", "build": "2620", "contract": 1}
    assert parse("IOS/2620 contract=7 extra=x") == {"platform": "ios", "build": "2620", "contract": 7}
    assert parse("ios/2620") == {"platform": "ios", "build": "2620", "contract": 0}
    assert parse("ios/2620 contract=abc") == {"platform": "ios", "build": "2620", "contract": 0}
    assert parse("web") == {"platform": "web", "build": "", "contract": 0}
    assert parse("") is None
    assert parse(None) is None
    assert parse("/2620 contract=1") is None


def test_client_verdict(monkeypatch):
    monkeypatch.setattr(server_identity, "MIN_IOS_CONTRACT", 3)
    ok = server_identity.evaluate_client("ios/2700 contract=3")
    assert ok["status"] == "compatible" and ok["reason"] == ""
    old = server_identity.evaluate_client("ios/2600 contract=2")
    assert old["status"] == "client_outdated"
    assert "contract 3" in old["reason"] and "sent 2" in old["reason"]
    # An app from before the handshake sends no header: contract 0, outdated.
    assert server_identity.evaluate_client(None)["status"] == "unknown"
    assert server_identity.evaluate_client("web/1.2") == {
        "platform": "web", "build": "", "contract": 0, "status": "unknown", "reason": ""}


def test_compatibility_table_matches_constants():
    """docs/compatibility.md is the human record of every contract bump. Its
    newest row must describe the constants that ship, or the table is a lie."""
    text = (ROOT / "docs" / "compatibility.md").read_text()
    rows = re.findall(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|([^|]*)\|([^|]*)\|", text, re.M)
    assert rows, "docs/compatibility.md has no contract rows"
    hosts = [int(host) for host, _, _, _ in rows]
    assert hosts == sorted(hosts, reverse=True), "newest contract row must come first"
    newest_host, newest_min_ios, _, _ = rows[0]
    assert int(newest_host) == server_identity.HOST_CONTRACT, (
        f"HOST_CONTRACT is {server_identity.HOST_CONTRACT} but the newest table row says {newest_host}; "
        "add a row to docs/compatibility.md")
    assert int(newest_min_ios) == server_identity.MIN_IOS_CONTRACT
    for host, min_ios, _, _ in rows:
        assert int(min_ios) <= int(host)
