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
    # What is advertised has a contract number; live features appear only
    # while live events flow (test_live_capability).
    assert set(contract["features"]) == set(info["capabilities"]["features"])
    assert set(server_identity.FEATURE_CONTRACTS) == set(server_identity.FEATURES)
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


def test_agent_rings_arrive_with_host_contract_64():
    """Agent rings ship after controller actions (contract 63), as 64."""
    assert server_identity.FEATURE_CONTRACTS["agent_rings"] == 64
    assert server_identity.HOST_CONTRACT >= 64
    text = (ROOT / "docs" / "agent-rings.md").read_text()
    assert "contract 64" in text


def test_agent_ring_live_is_advertised_with_the_number_of_its_row():
    """The flag's map value is its introduction number (booked at the landing), the
    row that documents it; clients gate on the flag and live.protocol, never on it."""
    assert "agent_ring_live" in server_identity.FEATURES
    text = (ROOT / "docs" / "compatibility.md").read_text()
    [row] = re.findall(r"^\|\s*(\d+)\s*\|\s*\d+\s*\|[^|]*\|\s*Agent-live rings[^\n]*feature `agent_ring_live`", text, re.M)
    assert server_identity.FEATURE_CONTRACTS["agent_ring_live"] == int(row) <= server_identity.HOST_CONTRACT


def test_artifact_summary_v1_is_advertised_with_the_number_of_its_row():
    """The map value is the introduction number its row documents, never the dict's default
    1: a client gating on ``== 1`` would never enable against a real Host."""
    assert "artifact_summary_v1" in server_identity.FEATURES
    text = (ROOT / "docs" / "compatibility.md").read_text()
    [row] = re.findall(r"^\|\s*(\d+)\s*\|\s*\d+\s*\|[^|]*\|\s*Opt-in artifact list summaries[^\n]*`artifact_summary_v1`", text, re.M)
    number = server_identity.FEATURE_CONTRACTS["artifact_summary_v1"]
    assert number == int(row) <= server_identity.HOST_CONTRACT
    assert number > 1
