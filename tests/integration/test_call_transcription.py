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
    # Its introduction number (booked at the landing, shared with listen-first), never a fixed one.
    introduced = body["contract"]["features"]["call_transcription_policy"]
    assert 1 < introduced <= body["contract"]["host"]
    assert introduced == body["contract"]["features"]["agent_ring_listen_first"]


# ---- L2 on a real Host: the language handed at the engine boundary (recording engines) -----------

@pytest.fixture
def recording_host(tmp_path, monkeypatch):
    monkeypatch.setenv("CLARP_QA_RECORDING_STT", "1")          # no paid provider: recorders behind the handoff
    h = QAHost(tmp_path / "ct-recording-host")
    try:
        h.start()
        with sqlite3.connect(h.root / "state.sqlite") as con:
            con.execute("INSERT INTO paired_devices(device_id, name, token_hash, scope, created_at)"
                        " VALUES ('device_qa_phone', 'QA phone', ?, 'full', 1)",
                        (hashlib.sha256(PHONE.encode()).hexdigest(),))
        yield h
    finally:
        h.stop()


def _handoffs(host):
    path = host.root / "provider" / "stt-handoffs.jsonl"
    return [] if not path.is_file() else [json.loads(line) for line in path.read_text().splitlines()]


def _case(host, tid, settings, control=None):
    (host.root / "provider").mkdir(exist_ok=True)
    (host.root / "provider" / "stt-control.json").write_text(json.dumps(control or {}))
    reset = {"call_languages": "", "call_engines": "", "agent_languages": {"rachel": ""},
             "agent_engines": {"rachel": ""}}
    status, out = call(host, "POST", "/transcription-providers", {**reset, **settings}, token="qa-host-test")
    assert status == 200, out
    before = len(_handoffs(host))
    status, started = call(host, "POST", "/calls", {"agents": ["rachel"]})
    status, body = transcribe(host, tid, {"X-Clarp-Call-Id": started["call"]["call_id"],
                                          "X-Transcription-Session": "rachel"})
    return status, body, _handoffs(host)[before:]


def _attempts(body):
    return [(a["engine"], a["outcome"], a.get("language", "<absent>")) for a in body["stt"]["attempts"]]


def test_L2_on_the_host_null_auto_observed_and_no_leakage(recording_host):
    host = recording_host
    one = {"agent_languages": {"rachel": "no,en"}, "agent_engines": {"rachel": "google:chirp_3"}}
    status, body, handed = _case(host, "tx-pre", one, {"no_key": ["google"]})
    assert status == 500 and handed == [] and _attempts(body) == [("google:chirp_3", "error", None)]
    status, body, handed = _case(host, "tx-post", one, {"fail_after": ["google"]})
    assert status == 500 and [h["language"] for h in handed] == ["no,en"]
    assert _attempts(body) == [("google:chirp_3", "error", "no,en")]
    scribe = {"agent_languages": {"rachel": "no,en"}, "agent_engines": {"rachel": "elevenlabs:scribe_v2"}}
    status, body, handed = _case(host, "tx-scribe-set", scribe)
    assert status == 200 and handed == [{"provider": "elevenlabs", "model": "scribe_v2", "language": None}]
    assert _attempts(body) == [("elevenlabs:scribe_v2", "ok", "auto")]
    assert body["stt"]["language"] == {"value": "no,en", "source": "agent"}
    chain = {"agent_languages": {"rachel": "no,en"}, "agent_engines": {"rachel": "google:chirp_3,elevenlabs:scribe_v2"}}
    status, body, handed = _case(host, "tx-fallback", chain, {"fail_after": ["google"]})
    assert status == 200 and [h["language"] for h in handed] == ["no,en", None]
    assert _attempts(body) == [("google:chirp_3", "error", "no,en"), ("elevenlabs:scribe_v2", "ok", "auto")]


def test_C3_on_the_host_a_set_skips_scribe_and_nothing_runnable_is_409(recording_host):
    host = recording_host
    status, body, handed = _case(host, "tx-c3-skip", {"call_languages": "no,en",
                                                      "call_engines": "elevenlabs:scribe_v2,google:chirp_3"})
    assert status == 200 and [(h["provider"], h["language"]) for h in handed] == [("google", "no,en")]
    assert _attempts(body) == [("elevenlabs:scribe_v2", "skipped", "<absent>"), ("google:chirp_3", "ok", "no,en")]
    assert body["stt"]["attempts"][0]["reason"] == "language_incompatible"
    status, body, handed = _case(host, "tx-c3-none", {"call_languages": "no,en", "call_engines": "google:chirp_3",
                                                      "agent_engines": {"rachel": "elevenlabs:scribe_v2"}})
    assert status == 409 and body["error"] == "no compatible engine" and handed == []
    assert body["attempts"] == [{"engine": "elevenlabs:scribe_v2", "outcome": "skipped",
                                 "reason": "language_incompatible"}]


def test_C3_on_the_host_single_code_scribe_runs_and_a_stored_impossible_policy_is_invalid(recording_host):
    host = recording_host
    status, body, handed = _case(host, "tx-scribe-single", {"call_languages": "no",
                                                            "call_engines": "elevenlabs:scribe_v2"})
    assert status == 200 and handed == [{"provider": "elevenlabs", "model": "scribe_v2", "language": "no"}]
    assert _attempts(body) == [("elevenlabs:scribe_v2", "ok", "no")]
    with sqlite3.connect(host.root / "state.sqlite") as con:      # stored around the settings check
        con.execute("UPDATE settings SET value = 'no,en' WHERE key = 'transcription.call_languages'")
        con.execute("UPDATE settings SET value = 'elevenlabs:scribe_v2' WHERE key = 'transcription.call_engines'")
    before = len(_handoffs(host))
    status, started = call(host, "POST", "/calls", {"agents": ["rachel"]})
    status, body = transcribe(host, "tx-invalid", {"X-Clarp-Call-Id": started["call"]["call_id"],
                                                   "X-Transcription-Session": "rachel"})
    assert status == 409 and body["error"] == "call policy invalid" and "can never work" in body["call_policy_error"]
    assert body["attempts"] == [] and _handoffs(host)[before:] == []
