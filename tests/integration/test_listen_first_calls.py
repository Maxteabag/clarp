"""Listen-first agent calls end to end on the QA Host (real HTTP, dispatch,
runtime, SQLite; the fake Codex provider). Contract: listen-first v5 §11.

A capable phone answers with an ACK only. Nothing runs until the phone says
how the opening resolved: the user's first utterance (the greeting /send,
forced to the calling agent with the call header) or a claim that nobody
spoke, which admits exactly one initiative turn and never a user row.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request

import pytest

from tests.integration.test_qa_turns import QAHost

REASON = "Possible water leak at pump 01"
LINE = "Camera 2 shows a possible water leak. Shall I find a field worker?"


PHONE_TOKEN = "cld_qa_listen_first_phone_token_0001"


def _started(h):
    h.start()
    # The answering phone: a paired full-scope device (calls belong to a device).
    with sqlite3.connect(h.root / "state.sqlite") as con:
        con.execute("INSERT INTO paired_devices(device_id, name, token_hash, scope, created_at)"
                    " VALUES ('device_qa_phone', 'QA phone', ?, 'full', 1)",
                    (hashlib.sha256(PHONE_TOKEN.encode()).hexdigest(),))
    return h


@pytest.fixture
def host(tmp_path):
    """The QA Host as production starts it: with its instance generation."""
    h = QAHost(tmp_path / "lf-host")
    try:                                    # stop() also reaps a host that never became ready
        yield _started(h)
    finally:
        h.stop()


@pytest.fixture
def host_without_identity(tmp_path):
    """Negative control: a fresh disposable root started without the instance generation."""
    h = QAHost(tmp_path / "lf-host-no-identity", ("--no-instance-generation",))
    try:
        yield _started(h)
    finally:
        h.stop()


def post(host, path, body):
    """As the paired phone."""
    request = urllib.request.Request(
        host.url + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + PHONE_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def refused(host, path, body):
    with pytest.raises(urllib.error.HTTPError) as err:
        post(host, path, body)
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
    return host.request("/calls/ring", {"agent": "rachel", "reason": REASON, "opening_line": LINE, **extra})


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


def test_the_qa_host_provisions_its_instance_generation_inside_its_root(host):
    """The production creator ran in the disposable root (and nowhere else)."""
    token = host.root / "share" / "clarp" / "backups" / "instance.generation"
    assert token.is_file() and len(token.read_text().strip()) == 36


def test_without_an_instance_generation_listen_first_is_refused_and_nothing_is_minted(host_without_identity):
    """Fail closed end to end: no token, so a listen-first ring is refused 409 and no
    ring, opening, call or token is created; an ordinary ring still rings."""
    h = host_without_identity
    with pytest.raises(urllib.error.HTTPError) as err:
        ring(h, opening_mode="listen_first")
    assert err.value.code == 409
    assert json.loads(err.value.read())["error"].startswith(
        "listen-first opening is unavailable: host identity unprovable")
    assert not (h.root / "share" / "clarp" / "backups" / "instance.generation").exists()
    rings = sql(h, "SELECT value FROM settings WHERE key = 'agent_ring.records'")
    assert all(json.loads(r["value"]) == [] for r in rings)          # no ring was recorded
    assert sql(h, "SELECT key FROM settings WHERE key LIKE 'agent_ring.opening.%'") == []
    assert sql(h, "SELECT key FROM settings WHERE key LIKE 'group_call.%'") == []
    said = ring(h)
    assert said["ring"]["state"] == "ringing" and said["ring"].get("opening_mode", "say") == "say"


def test_an_unknown_ring_opening_mode_is_refused(host):
    with pytest.raises(urllib.error.HTTPError) as err:
        ring(host, opening_mode="listen")
    assert (err.value.code, json.loads(err.value.read())["error"]) == (400, "unknown opening_mode")


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
    wait_for(lambda: provider(host, "turn/start"), what="the initiative turn")
    log = host.wait_reply("rachel", "QA reply:")                          # the agent spoke first
    wait_idle(host)
    time.sleep(0.5)                                                      # the transcript import ran
    starts = provider(host, "turn/start")
    assert len(starts) == 1
    sent = json.dumps(starts[0])
    assert REASON in sent and "Open the conversation" in sent
    assert user_rows(host) == []                                          # never a user row
    assert [t for t in host.request("/log?session=rachel")["turns"] if t["role"] == "user"] == []
    assert log["turns"][-1]["role"] == "assistant"
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
    host.wait_reply("rachel", "QA reply:")
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


# ---- §11.H on the real Host: a Stop between admission and the greeting ------------------

def test_a_stop_between_admit_and_greeting_withdraws_the_initiative(host):
    rid, out = answered(host)
    op = out["opening"]
    post(host, "/send", {"session": "rachel", "text": "[qa-slow] earlier work", "client_msg_id": "u-busy",
                         "synthesize_audio": False})
    wait_for(lambda: provider(host, "turn/start"), what="the busy turn")
    res = claim(host, op, "silence", "rq-1")
    assert res["opening"]["state"] == "initiative_queued" and res["opening"]["intent"]["state"] == "admitted"
    post(host, "/stop", {"session": "rachel", "reason": "the user pressed Stop"})
    queued = sql(host, "SELECT status FROM queued_turns WHERE queue_id LIKE 'call-open:%'")
    assert [r["status"] for r in queued] == ["queued"]                 # held by the Stop, unclaimed
    sent = post(host, "/send", {"session": "rachel", "text": "Hallo?", "client_msg_id": "u-hallo",
                                "call_id": op["call_id"], "call_opening": {"token": op["token"], "epoch": 1},
                                "synthesize_audio": False})
    assert sent["opening"]["state"] == "greeting" and sent["opening"]["intent"]["state"] == "cancelled"
    host.wait_reply("rachel", "QA reply: Hallo?")
    wait_idle(host)
    assert [r["status"] for r in sql(host, "SELECT status FROM queued_turns WHERE queue_id LIKE 'call-open:%'")] \
        == ["withdrawn"]
    host.request("/turn-queue/resume", {"session": "rachel"})
    time.sleep(1.0)
    starts = provider(host, "turn/start")
    assert not any("clarp-call-opening" in json.dumps(s) for s in starts)   # the initiative never ran


def test_a_real_stop_after_the_answer_retires_the_opening_stopped_after_answer(host):
    """§11.L: the answer passes only a pause that existed when it arrived."""
    rid, out = answered(host)
    op = out["opening"]
    post(host, "/stop", {"session": "rachel", "reason": "the user pressed Stop"})
    code, body = refused(host, "/calls/ring/opening", {"token": op["token"], "epoch": 1, "claim": "silence",
                                                       "request_id": "rq-after-stop", "capture_gen": 1,
                                                       "at_ms": 1250, "watermark_ms": 1300})
    assert (code, body["error"], body["reason"]) == (409, "opening retired", "stopped_after_answer")
    time.sleep(1.0)
    assert provider(host, "turn/start") == []
    assert host.request(f"/calls/ring?ring_id={rid}")["ring"]["opening"]["retired"] == "stopped_after_answer"


def test_a_stop_before_the_answer_still_lets_exactly_one_opening_run(host):
    post(host, "/stop", {"session": "rachel", "reason": "earlier Stop"})
    rid, out = answered(host)
    claim(host, out["opening"], "silence", "rq-1")
    wait_for(lambda: provider(host, "turn/start"), what="the opening")
    host.wait_reply("rachel", "QA reply:")
    wait_idle(host)
    assert len(provider(host, "turn/start")) == 1
    assert host.request("/turn-queue?session=rachel")["paused"] is True    # the pause stands
