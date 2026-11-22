"""The apps' client log and crash events are always kept on the Host, bounded and readable by
agents (lib.client_events, feature client_events_kept)."""
from __future__ import annotations

import importlib.util
import io
import json
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from lib import client_events, device_pairing
from tests.integration.test_attention_questions_http import TOKEN, _request, host, server_module  # noqa: F401

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def diagnostics_off(monkeypatch):
    from lib import diagnostics_settings
    monkeypatch.setattr(diagnostics_settings, "accepts_client_uploads", lambda: False)


def _post(host, body, token=TOKEN, client="ios/2990 contract=80"):
    request = urllib.request.Request(host.base + "/clog", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token,
                                              "X-Clarp-Client": client})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def test_kept_with_diagnostics_off_and_the_app_is_told_so(host, diagnostics_off):
    phone = device_pairing.exchange(device_pairing.issue(device_name="Phone", scope="limited")["code"])
    answer = _post(host, {"events": [
        {"event": "appLaunch", "detail": "cold", "trace_id": "t1"},
        {"event": "fatalError", "level": "fault", "detail": {"where": "ChatView"}},
    ]}, token=phone["token"])
    assert answer == {"ok": True, "captured": True, "kept": 2, "full_capture": False}
    status, body = _request(host, "/client-log?since=30m")
    assert status == 200 and body["count"] == 2
    first, second = body["events"]
    assert first["event"] == "appLaunch" and first["detail"] == "cold"
    assert first["device"] == phone["device_id"] and first["client"] == "ios"
    assert first["client_build"] == "2990" and first["client_contract"] == 80 and first["trace_id"] == "t1"
    assert first["server_id"] and isinstance(first["received_at"], int)
    assert second["level"] == "fault" and second["kept"] is True
    # Filters.
    assert [e["event"] for e in _request(host, "/client-log?level=error")[1]["events"]] == ["fatalError"]
    assert [e["event"] for e in _request(host, "/client-log?event=appLaunch")[1]["events"]] == ["appLaunch"]
    # Full scope only: the phone that posted them cannot read them back.
    limited = urllib.request.Request(host.base + "/client-log", headers={"Authorization": "Bearer " + phone["token"]})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(limited, timeout=10)
    assert refused.value.code == 403


def test_secrets_never_reach_the_store(host, diagnostics_off):
    _post(host, {"event": "requestFailed", "level": "error", "detail": {
        "token": "cld_abcdefghijklmnopqrstuvwxyz0123456789",
        "headers": {"Authorization": "Bearer abcdefghijklmnop1234567890"},
        "password": "hunter22", "api_key": "k-123",
        "url": "https://h.example/x?token=supersecretvalue&a=1",
        "note": "sent Bearer zzzzzzzzzzzzzzzzzzzz with cld_qqqqqqqqqqqqqqqqqqqqqqqq and clp_pppppppppppppppppppppp",
    }})
    [row] = _request(host, "/client-log")[1]["events"]
    text = json.dumps(row)
    for secret in ("cld_abcdefghijklmnop", "abcdefghijklmnop1234567890", "hunter22", "k-123", "supersecretvalue",
                   "zzzzzzzzzzzzzzzzzzzz", "cld_qqqq", "clp_pppp"):
        assert secret not in text, secret
    assert row["detail"]["token"] == "[redacted]" and row["detail"]["headers"]["Authorization"] == "[redacted]"
    assert "a=1" in row["detail"]["url"]


def test_a_detail_is_capped():
    client_events.record([{"event": "big", "detail": "x" * 50_000}], device="d", now_ms=1_000_000)
    [row] = client_events.read()
    assert len(json.dumps(row["detail"]).encode()) <= client_events.DETAIL_BYTES + 32
    assert str(row["detail"]).endswith("…[truncated]")


def test_the_store_is_bounded_and_keeps_what_led_to_a_crash(monkeypatch):
    minute = 60_000
    t0 = 10_000 * minute
    # Ordinary events: kept for the context window, then dropped.
    client_events.record([{"event": "tap"}], device="phone", now_ms=t0)
    client_events.record([{"event": "scroll"}], device="phone", now_ms=t0 + 20 * minute)
    # A crash 25 minutes in pins the 30 minutes before it on that device, not another device's.
    client_events.record([{"event": "tap"}], device="other", now_ms=t0 + 24 * minute)
    client_events.record([{"event": "appCrash", "detail": {"signal": "SIGSEGV"}}], device="phone", now_ms=t0 + 25 * minute)
    client_events.prune(now_ms=t0 + 120 * minute)
    assert [(e["device"], e["event"]) for e in client_events.read()] == [
        ("phone", "tap"), ("phone", "scroll"), ("phone", "appCrash")]
    # Age: everything goes after 7 days.
    client_events.prune(now_ms=t0 + client_events.MAX_AGE_MS + 26 * minute)
    assert client_events.read() == []
    # Count and bytes, oldest first, in total and per device.
    monkeypatch.setattr(client_events, "MAX_EVENTS", 10)
    monkeypatch.setattr(client_events, "DEVICE_MAX_EVENTS", 6)
    now = t0 + 30 * client_events.MAX_AGE_MS
    for i in range(8):
        client_events.record([{"event": f"error{i}"}], device="a", now_ms=now + i)
    for i in range(8):
        client_events.record([{"event": f"error{i}"}], device="b", now_ms=now + 100 + i)
    client_events.prune(now_ms=now + 200)
    rows = client_events.read(limit=100)
    assert len(rows) <= 10
    per_device = {d: [e["event"] for e in rows if e["device"] == d] for d in ("a", "b")}
    assert len(per_device["a"]) <= 6 and len(per_device["b"]) <= 6
    assert per_device["b"][-1] == "error7"   # the newest stay
    monkeypatch.setattr(client_events, "MAX_BYTES", 400)
    client_events.prune(now_ms=now + 300)
    assert sum(len(json.dumps(r)) for r in client_events.read(limit=100)) < 2000


def test_a_crash_report_is_kept_too(host, diagnostics_off):
    payload = {"crashDiagnostics": [{"callStackTree": {}}], "hangDiagnostics": []}
    status, _ = _request(host, "/crash", payload)
    assert status == 200
    events = _request(host, "/client-log?event=metrickit_report")[1]["events"]
    assert len(events) == 1 and events[0]["kept"] is True and events[0]["detail"]["file"].startswith("ios-diagnostic-")


def test_the_cli_prints_json_lines(host, diagnostics_off, monkeypatch):
    _post(host, {"events": [{"event": "launch"}, {"event": "hangDetected", "level": "warning"}]})
    spec = importlib.util.spec_from_file_location("clarp_admin_cli", _ROOT / "bin/clarp-admin.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    def api_request(method, path, body=None, **_):
        status, answer = _request(host, path)
        assert status == 200
        return answer

    monkeypatch.setattr(cli, "api_request", api_request)
    out = io.StringIO()
    import argparse
    args = argparse.Namespace(device=None, since="1h", level="warning", event=None, limit=200)
    with redirect_stdout(out):
        assert cli.cmd_client_log(args) == 0
    # (the in-process test server writes its access lines to the same stdout)
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
    assert [line["event"] for line in lines] == ["hangDetected"]


def test_since_takes_durations_and_epoch_ms():
    assert client_events.parse_since("30m", now_ms=10_000_000) == 10_000_000 - 30 * 60_000
    assert client_events.parse_since("2h", now_ms=10_000_000) == 10_000_000 - 7_200_000
    assert client_events.parse_since("1700000000000") == 1_700_000_000_000
    assert client_events.parse_since("") is None
    with pytest.raises(ValueError):
        client_events.parse_since("yesterday")
