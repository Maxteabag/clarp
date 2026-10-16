"""Listen-first agent calls end to end on the QA Host (real HTTP, dispatch,
runtime, SQLite; the fake Codex provider). Contract: listen-first v5 §11.

A capable phone answers with an ACK only. Nothing runs until the phone says
how the opening resolved: the user's first utterance (the greeting /send,
forced to the calling agent with the call header) or a claim that nobody
spoke, which admits exactly one initiative turn and never a user row.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error

import pytest

from tests.integration.test_qa_turns import QAHost

REASON = "Possible water leak at pump 01"
LINE = "Camera 2 shows a possible water leak. Shall I find a field worker?"


@pytest.fixture
def host(tmp_path):
    h = QAHost(tmp_path / "lf-host")
    try:
        yield h.start()
    finally:
        h.stop()


def post(host, path, body):
    return host.request(path, body)


def refused(host, path, body):
    with pytest.raises(urllib.error.HTTPError) as err:
        host.request(path, body)
    return err.value.code, json.loads(err.value.read() or b"{}")


def provider(host, method):
    path = host.root / "provider" / "turn-requests.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line)["method"] == method]


def sql(host, query, *args):
    with sqlite3.connect(host.root / "state.sqlite") as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(query, args)]


def wait_for(predicate, timeout=15.0, what=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def wait_idle(host, timeout=20.0):
    wait_for(lambda: not host.request("/status?session=rachel").get("busy"), timeout, "idle")


def ring(host, **extra):
    return post(host, "/calls/ring", {"agent": "rachel", "reason": REASON, "opening_line": LINE, **extra})


def answered(host, mode="listen_first"):
    rid = ring(host, opening_mode="listen_first")["ring"]["ring_id"]
    body = {"ring_id": rid, **({"opening_mode": mode} if mode is not None else {})}
    return rid, post(host, "/calls/ring/answer", body)


def claim(host, op, claim_name, request_id, **extra):
    body = {"token": op["token"], "epoch": op["epoch"], "claim": claim_name, "request_id": request_id,
            "capture_gen": 1, "at_ms": 0, **extra}
    if claim_name == "silence":
        body.update(at_ms=1250, watermark_ms=1300)
    return post(host, "/calls/ring/opening", body)


def user_rows(host):
    return sql(host, "SELECT message_id, text FROM messages WHERE role='user'")


# ---- capability -----------------------------------------------------------------

def test_the_host_advertises_listen_first(host):
    info = host.request("/server-info")
    assert "agent_ring_listen_first" in info["capabilities"]["features"]
    assert info["contract"]["features"]["agent_ring_listen_first"] == 67
    assert info["contract"]["host"] >= 67


def test_an_unknown_ring_opening_mode_is_refused(host):
    code, body = refused(host, "/calls/ring", {"agent": "rachel", "reason": REASON, "opening_line": LINE,
                                                "opening_mode": "listen"})
    assert (code, body["error"]) == (400, "unknown opening_mode")


def test_a_capable_answer_runs_nothing(host):
    rid, out = answered(host)
    op = out["opening"]
    assert op["mode"] == "listen_first" and op["state"] == "pending" and op["epoch"] == 1
    assert op["silence_ms"] == 1250 and op["token"].startswith("opn_") and op["owner_nonce"].startswith("own_")
    time.sleep(0.5)
    assert provider(host, "turn/start") == [] and user_rows(host) == []


def test_an_old_client_gets_the_legacy_answer_and_no_initiative(host):
    rid, out = answered(host, mode=None)
    assert out["opening"] == {"text": LINE, "audio_url": "", "session": "rachel", "persona": "Rachel"}
    got = host.request(f"/calls/ring?ring_id={rid}")["ring"]
    assert got["opening"]["state"] == "fallback"
    time.sleep(0.5)
    assert provider(host, "turn/start") == []


# ---- the initiative ------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["silence", "no_mic", "not_ready"])
def test_a_silence_claim_runs_exactly_one_initiative_turn(host, kind):
    rid, out = answered(host)
    op = out["opening"]
    claim(host, op, "listening", "rq-1")
    res = claim(host, op, kind, "rq-2")
    assert res["opening"]["state"] in ("initiative_queued", "initiative_started")
    starts = wait_for(lambda: provider(host, "turn/start"), what="the initiative turn")
    wait_idle(host)
    starts = provider(host, "turn/start")
    assert len(starts) == 1
    sent = json.dumps(starts[0])
    assert REASON in sent and "Open the conversation" in sent
    assert user_rows(host) == []                                          # never a user row
    assert host.request(f"/calls/ring?ring_id={rid}")["ring"]["opening"]["state"] == "initiative_started"
    # An ambiguous ACK: the phone repeats the same request, nothing new runs.
    again = claim(host, op, kind, "rq-2")
    assert again["changed"] is False
    time.sleep(0.5)
    assert len(provider(host, "turn/start")) == 1


# ---- the greeting --------------------------------------------------------------------

def test_the_greeting_send_goes_to_the_caller_with_the_header(host):
    rid, out = answered(host)
    op = out["opening"]
    claim(host, op, "speech_started", "rq-1", at_ms=840)
    sent = post(host, "/send", {"session": "rachel", "text": "Hallo?", "client_msg_id": "u-hallo",
                                "call_id": op["call_id"], "call_opening": {"token": op["token"], "epoch": 1},
                                "synthesize_audio": False})
    assert sent["session"] == "rachel" and sent["opening"]["state"] == "greeting"
    host.wait_reply("rachel", "QA reply: Hallo?")
    [start] = provider(host, "turn/start")
    assert "Hallo?" in json.dumps(start["input"]) and "spoke first" in json.dumps(start)
    assert [r["message_id"] for r in user_rows(host)] == ["u-hallo"]
    # A retry of the same captured turn across a renewal attempt is deduped.
    again = post(host, "/send", {"session": "rachel", "text": "Hallo?", "client_msg_id": "u-hallo",
                                 "call_id": op["call_id"], "call_opening": {"token": op["token"], "epoch": 1},
                                 "synthesize_audio": False})
    assert again["opening"]["state"] == "greeting"
    wait_idle(host)
    assert len(provider(host, "turn/start")) == 1
    assert [r["message_id"] for r in user_rows(host)] == ["u-hallo"]


def test_a_greeting_refusal_dispatches_nothing(host):
    rid, out = answered(host)
    op = out["opening"]
    code, body = refused(host, "/send", {"session": "rachel", "text": "Hallo?", "client_msg_id": "u-x",
                                         "call_id": op["call_id"],
                                         "call_opening": {"token": op["token"], "epoch": 9},
                                         "synthesize_audio": False})
    assert (code, body["error"]) == (409, "stale epoch")
    code, body = refused(host, "/send", {"session": "rachel", "text": "Hallo?", "client_msg_id": "u-y",
                                         "call_id": op["call_id"], "call_opening": "opn_bad",
                                         "synthesize_audio": False})
    assert code == 400
    time.sleep(0.5)
    assert provider(host, "turn/start") == [] and user_rows(host) == []


def test_speech_after_a_started_initiative_is_an_ordinary_utterance(host):
    rid, out = answered(host)
    op = out["opening"]
    claim(host, op, "silence", "rq-1")
    wait_for(lambda: provider(host, "turn/start"), what="the initiative")
    wait_idle(host)
    sent = post(host, "/send", {"session": "rachel", "text": "Who is this?", "client_msg_id": "u-who",
                                "call_id": op["call_id"], "call_opening": {"token": op["token"], "epoch": 1},
                                "synthesize_audio": False})
    assert sent["opening"]["state"] == "initiative_started" and sent["opening"]["greeting"] == "ordinary"
    host.wait_reply("rachel", "QA reply: Who is this?")
    assert len(provider(host, "turn/start")) == 2                        # one initiative, one ordinary


def test_hanging_up_retires_the_opening(host):
    rid, out = answered(host)
    op = out["opening"]
    post(host, "/calls/end", {"call_id": op["call_id"]})
    code, body = refused(host, "/calls/ring/opening", {"token": op["token"], "epoch": 1, "claim": "silence",
                                                       "request_id": "rq-late", "capture_gen": 1,
                                                       "at_ms": 1250, "watermark_ms": 1300})
    assert (code, body["error"], body["reason"]) == (409, "opening retired", "call_ended")
    time.sleep(0.5)
    assert provider(host, "turn/start") == []
