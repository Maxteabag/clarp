"""Call-scoped transcription on the QA Host (contract §12.1/§12.2): a present
X-Clarp-Call-Id is validated before any STT and refuses explicitly; an
absent one is today's transcription, byte for byte."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.error
import urllib.request

import pytest

from tests.integration.test_qa_turns import QAHost

PHONE = "cld_qa_call_transcription_token_0001"


@pytest.fixture
def host(tmp_path):
    h = QAHost(tmp_path / "ct-host")
    try:
        h.start()
        with sqlite3.connect(h.root / "state.sqlite") as con:
            con.execute("INSERT INTO paired_devices(device_id, name, token_hash, scope, created_at)"
                        " VALUES ('device_qa_phone', 'QA phone', ?, 'full', 1)",
                        (hashlib.sha256(PHONE.encode()).hexdigest(),))
        yield h
    finally:
        h.stop()


def call(host, method, path, body=None, headers=None, data=None, token=PHONE):
    hdrs = {"Authorization": "Bearer " + token, **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(host.url + path, method=method, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def transcribe(host, tid, headers=None, token=PHONE):
    return call(host, "POST", "/transcribe", data=b"RIFF" + tid.encode() * 64, token=token,
                headers={"Content-Type": "audio/wav", "X-Transcription-ID": tid, **(headers or {})})


def test_no_header_is_unchanged_and_carries_no_stt(host):
    status, body = transcribe(host, "tx-plain", {"X-Transcription-Session": "rachel"})
    assert status == 200 and body["text"] == "QA voice prompt" and "stt" not in body


def test_a_present_call_header_is_validated_before_any_stt(host):
    status, started = call(host, "POST", "/calls", {"agents": ["rachel"]})
    assert status == 200, started
    call_id = started["call"]["call_id"]
    cases = [
        ({"X-Clarp-Call-Id": "gcl_nope", "X-Transcription-Session": "rachel"}, PHONE, 400, "bad call id"),
        ({"X-Clarp-Call-Id": "gcl_" + "0" * 32, "X-Transcription-Session": "rachel"}, PHONE, 404, "unknown call"),
        ({"X-Clarp-Call-Id": call_id, "X-Transcription-Session": "rachel"}, "qa-host-test", 403, "not your call"),
        ({"X-Clarp-Call-Id": call_id, "X-Transcription-Session": "mike"}, PHONE, 409, "not a call participant"),
    ]
    for n, (headers, token, code, error) in enumerate(cases):
        status, body = transcribe(host, f"tx-refused-{n}", headers, token=token)
        assert (status, body.get("error")) == (code, error), (headers, body)
        assert "text" not in body
    status, body = transcribe(host, "tx-in-call", {"X-Clarp-Call-Id": call_id, "X-Transcription-Session": "rachel"})
    assert status == 200 and body["text"] == "QA voice prompt"
    assert body["stt"]["language"] == {"value": "en", "source": "global"}
    assert body["stt"]["engines"]["source"] == "global" and body["stt"]["attempts"][0]["outcome"] == "ok"
    call(host, "POST", "/calls/end", {"call_id": call_id})
    # A retry with the same identity after the call ended is refused, cached result or not.
    status, body = transcribe(host, "tx-in-call", {"X-Clarp-Call-Id": call_id, "X-Transcription-Session": "rachel"})
    assert (status, body["error"], body["reason"]) == (409, "call ended", "ended")


def test_the_settings_read_back_unset_by_default(host):
    status, body = call(host, "GET", "/server-info", token="qa-host-test")
    assert "call_transcription_policy" in body["capabilities"]["features"]
    assert body["contract"]["features"]["call_transcription_policy"] == 67
