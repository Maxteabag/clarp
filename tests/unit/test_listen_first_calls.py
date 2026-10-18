"""Listen-first agent calls (contract: listen-first v5 §11, then §10, §9, §9.1).

The real ``agent_rings`` module against a temporary database. A capable
phone answers a ``listen_first`` ring with an ACK only; the phone, not the
Host, decides when the opening resolves: the user's first captured utterance
(the greeting ``/send``) or one claim that nobody spoke (silence, no_mic,
not_ready, speech_abandoned), which admits ONE initiative turn for the
calling agent. Every opening transition is one SQLite transaction; the
initiative's admission is fenced by its intent so a greeting, a retire or a
crash never yields a second opening or a lost one.
"""
from __future__ import annotations

import itertools
import json
import pathlib
import re
import threading

import pytest

from lib import agent_rings as rings
from lib import db, group_calls as calls, settings_store, turn_queue

PHONE = "device_phone1"
SERVER = "srv-listen-first"
_IDS = itertools.count(1)


def _agent(agent_id, persona, session, *, voice="voice-1"):
    db.conn().execute(
        "INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, role)"
        " VALUES (?,?,?,?,?,?,?)", (agent_id, persona, voice, "/tmp", session, db.now_ms(), "agent"))
    db.conn().execute(
        "INSERT INTO runtimes(agent_id, session, backend_session_id, started_at) VALUES (?,?,?,?)",
        (agent_id, session, "bs-" + agent_id, db.now_ms()))


class Stream:
    def __init__(self):
        self.events, self.lock = [], threading.Lock()

    def broadcast(self, event):
        with self.lock:
            self.events.append(json.loads(json.dumps(event)))

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]


class FakeTTS:
    def __init__(self, tmp_path):
        self.calls, self.tmp = [], tmp_path

    def synthesize(self, text, voice_id, *, session=None):
        self.calls.append((text, voice_id, session))
        path = self.tmp / f"{len(self.calls)}__{session}.mp3"
        path.write_bytes(b"mp3")
        return path


class Clock:
    def __init__(self):
        self.now = 1_800_000_000_000

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path, monkeypatch):
    _agent("a-theo", "Theo", "theo-97e5")
    _agent("a-mike", "Mike", "mike-86db")
    stream, tts, clock, pushes, kicks = Stream(), FakeTTS(tmp_path), Clock(), [], []
    monkeypatch.setattr(rings, "now_ms", clock)
    monkeypatch.setattr(rings, "_push", lambda ring, kind: pushes.append((kind, dict(ring))) or {"sent": 1})
    monkeypatch.setattr(rings, "_schedule_expiry", lambda ring: None)
    monkeypatch.setattr(rings, "_push_voip", lambda ring: {"sent": 0})
    monkeypatch.setattr(rings, "_server_id", lambda: SERVER)
    monkeypatch.setattr(rings, "_kick_queue", lambda: kicks.append(1))
    ctx = type("Ctx", (), {"stream": stream, "tts": tts})()
    rings.bind(ctx)
    calls.bind(ctx)
    return type("Env", (), {"stream": stream, "tts": tts, "clock": clock, "pushes": pushes,
                            "kicks": kicks, "monkeypatch": monkeypatch})()


def _ring(agent="a-theo", **kw):
    body = {"reason": "Possible water leak at pump 01",
            "opening_line": "Camera 2 shows a possible water leak at pump 01. Shall I find a field worker?"}
    body.update(kw)
    return rings.ring(caller_agent_id=agent, **body)


def _lf(**kw):
    return _ring(opening_mode="listen_first", **kw)["ring"]


def _answered(principal=PHONE, **kw):
    ring = _lf(**kw)
    out = rings.answer(principal, ring["ring_id"], opening_mode="listen_first")
    return ring, out["opening"]


def _claim(op, claim, *, request_id=None, epoch=None, principal=PHONE, **extra):
    body = {"token": op["token"], "epoch": op["epoch"] if epoch is None else epoch, "claim": claim,
            "request_id": request_id or f"rq-{next(_IDS)}", "capture_gen": 1, "at_ms": 0}
    if claim == "silence":
        body.update(at_ms=1250, watermark_ms=1290)
    body.update(extra)
    return rings.opening_claim(principal, body)


def _greet(op, *, client_msg_id="u-hallo", epoch=None, trace_id="tr-greet", session="theo-97e5",
           call_id=None, principal=PHONE):
    return rings.greeting(principal, token=op["token"], epoch=op["epoch"] if epoch is None else epoch,
                          client_msg_id=client_msg_id, trace_id=trace_id, session=session,
                          call_id=call_id or op["call_id"])


def _state(ring_id):
    return rings.opening_state(ring_id)


def _queue(ring_id):
    return [dict(r) for r in db.conn().execute(
        "SELECT queue_id, status, claimed_at, client_msg_id, origin, text FROM queued_turns"
        " WHERE queue_id LIKE ? ORDER BY queue_seq", (f"call-open:{ring_id}:%",))]


def _admissions(ring_id):
    return [dict(r) for r in db.conn().execute(
        "SELECT admission_id, origin, client_admission_id FROM prompt_admissions"
        " WHERE client_admission_id LIKE ? ORDER BY observed_at", (f"call-open:{ring_id}:%",))]


def _aid(ring_id):
    """The current intent generation's admission id (§11.I: call-open:<ring_id>:<gen>)."""
    return _state(ring_id)["intent"]["admission_id"]


def _dispatch(ring_id):
    return rings.claim_initiative(_aid(ring_id))


def _user_rows():
    return db.conn().execute("SELECT count(*) FROM messages WHERE role = 'user'").fetchone()[0]


def _refused(call, status, error):
    with pytest.raises(calls.CallError) as err:
        call()
    assert (err.value.status, err.value.error) == (status, error), err.value.body()
    return err.value


def _no_crash_after_resolve(env):
    """The Host dies right after the resolve transaction: nobody admits."""
    env.monkeypatch.setattr(rings, "_admit_after_resolve", lambda ring_id: None)


# ---- capability -------------------------------------------------------------------

def test_an_unknown_ring_opening_mode_is_refused(env):
    for bad in ("listen", "LISTEN_FIRST", 1, ["listen_first"], {}):
        _refused(lambda: _ring(opening_mode=bad), 400, "unknown opening_mode")
    assert rings.current() is None


def test_listen_first_still_needs_an_opening_line(env):
    _refused(lambda: _ring(opening_mode="listen_first", opening_line="  "), 400, "opening_line required")


def test_a_listen_first_ring_is_voiced_and_marked(env):
    ring = _lf()
    assert ring["opening_mode"] == "listen_first"
    assert env.tts.calls == [(ring["opening_line"], "voice-1", "theo-97e5")]   # the legacy fallback line
    [event] = env.stream.of("agent-ring")
    assert event["opening_mode"] == "listen_first"


@pytest.mark.parametrize("mode", [None, "say", "nonsense", 7, ["listen_first"], ""])
def test_any_other_answer_capability_gets_the_legacy_answer(env, mode):
    ring = _lf()
    kwargs = {} if mode is None else {"opening_mode": mode}
    out = rings.answer(PHONE, ring["ring_id"], **kwargs)
    legacy = {"text": ring["opening_line"], "audio_url": ring["opening_audio_url"],
              "session": "theo-97e5", "persona": "Theo"}
    expected = legacy if mode is None else {**legacy, "mode": "say"}
    assert out["opening"] == expected
    assert _state(ring["ring_id"])["state"] == "fallback"          # no dispatch, ever
    _no_crash_after_resolve(env)
    assert rings.recover_openings() == 0 and _queue(ring["ring_id"]) == []


def test_a_capable_answer_on_a_say_ring_is_the_legacy_answer(env):
    ring = _ring()["ring"]
    out = rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    assert out["opening"] == {"text": ring["opening_line"], "audio_url": ring["opening_audio_url"],
                              "session": "theo-97e5", "persona": "Theo", "mode": "say"}
    assert rings.opening_state(ring["ring_id"]) is None


# ---- §9.1 identity: the say path ---------------------------------------------------

def _say_scenario(env, **ring_kw):
    env.stream.events.clear()
    env.pushes.clear()
    ring = _ring(**ring_kw)["ring"]
    out = rings.answer(PHONE, ring["ring_id"])
    notices = [dict(r) for r in db.conn().execute(
        "SELECT text, origin FROM messages WHERE origin = 'system' ORDER BY seq")]
    return ring, out, list(env.stream.events), list(env.pushes), notices


_TIMES = {"created_at", "expires_at", "ended_at", "server_now", "started_at", "joined_at", "changed_at",
          "updated_at"}


def _scrub(value):
    """Ids, the clock and the TTS file counter differ between two runs; nothing else may."""
    text = json.dumps(value, sort_keys=True)
    text = re.sub(r"(rng|gcl)_[0-9a-f]{32}", r"\1_X", text)
    text = re.sub(r"/audio/\d+__", "/audio/N__", text)

    def drop(node):
        if isinstance(node, dict):
            return {k: (None if k in _TIMES else drop(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [drop(v) for v in node]
        return node
    return drop(json.loads(text))


def test_a_say_ring_is_identical_to_a_ring_without_opening_mode(env, tmp_path):
    first = _say_scenario(env)
    calls.end(PHONE)
    db.conn().execute("DELETE FROM messages")
    env.clock.now += rings.AGENT_INTERVAL_MS
    second = _say_scenario(env, opening_mode="say")
    assert _scrub(first[0]) == _scrub(second[0])                     # record
    assert "opening_mode" not in second[0]
    assert _scrub(first[1]) == _scrub(second[1])                     # answer
    assert _scrub([e for e in first[2] if e["type"] == "agent-ring"]) == \
        _scrub([e for e in second[2] if e["type"] == "agent-ring"])  # events
    assert _scrub(first[3]) == _scrub(second[3])                     # pushes
    assert first[4] == second[4]                                      # notices
    assert all("opening" not in e for e in second[2] if e["type"] == "agent-ring")


def test_the_say_answer_carries_no_new_field_and_the_limits_are_unchanged(env):
    assert (rings.AGENT_INTERVAL_MS, rings.HOURLY_LIMIT, rings.RING_TIMEOUT_MS) == (60_000, 10, 45_000)
    ring = _ring()["ring"]
    out = rings.answer(PHONE, ring["ring_id"])
    assert set(out) == {"ok", "ring", "call", "changed", "opening"}
    assert set(out["opening"]) == {"text", "audio_url", "session", "persona"}
    assert "opening_mode" not in out["ring"] and "opening" not in out["ring"]


# ---- the capable answer is an ACK -----------------------------------------------------

def test_a_capable_answer_is_an_ack_with_no_speech_row_or_turn(env):
    ring = _lf()
    tts_before, rows_before = len(env.tts.calls), _user_rows()
    out = rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    op = out["opening"]
    assert set(op) == {"mode", "ring_id", "call_id", "token", "owner_nonce", "epoch", "state",
                       "silence_ms", "session", "persona", "server_id", "intent"}
    assert op["mode"] == "listen_first" and op["state"] == "pending" and op["epoch"] == 1
    assert re.fullmatch(r"opn_[0-9a-f]{32}", op["token"])
    assert re.fullmatch(r"own_[0-9a-f]{32}", op["owner_nonce"])
    assert op["silence_ms"] == 1250 and op["server_id"] == SERVER and op["intent"] is None
    assert (op["ring_id"], op["call_id"]) == (ring["ring_id"], out["call"]["call_id"])
    assert (op["session"], op["persona"]) == ("theo-97e5", "Theo")
    assert "text" not in op and "audio_url" not in op
    assert len(env.tts.calls) == tts_before and _user_rows() == rows_before
    assert env.kicks == [] and _queue(ring["ring_id"]) == []
    again = rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    assert again["changed"] is False and again["opening"]["token"] == op["token"]


def test_the_event_and_get_show_the_opening_state_but_never_its_secrets(env):
    ring, op = _answered()
    got = rings.get(ring["ring_id"])
    assert got["opening"]["state"] == "pending" and got["opening"]["epoch"] == 1
    assert "token" not in got["opening"] and "owner_nonce" not in got["opening"]
    last = env.stream.of("agent-ring")[-1]
    assert last["opening"]["state"] == "pending" and "token" not in last["opening"]


# ---- claims -----------------------------------------------------------------------

def test_listening_records_readiness_without_resolving(env):
    ring, op = _answered()
    out = _claim(op, "listening", capture_gen=3, at_ms=0)
    assert out["changed"] is True and out["opening"]["state"] == "listening"
    assert _claim(op, "listening", capture_gen=4)["opening"]["state"] == "listening"   # a route change
    assert _queue(ring["ring_id"]) == []


def test_speech_started_latches_and_blocks_every_silence_kind(env):
    ring, op = _answered()
    _claim(op, "listening")
    assert _claim(op, "speech_started", at_ms=840)["opening"]["state"] == "speech_latched"
    for claim in ("silence", "no_mic", "not_ready"):
        err = _refused(lambda: _claim(op, claim), 409, "speech latched")
        assert err.extra["opening"]["state"] == "speech_latched"
    assert _claim(op, "speech_started", at_ms=900)["opening"]["state"] == "speech_latched"
    assert _queue(ring["ring_id"]) == [] and env.kicks == []


@pytest.mark.parametrize("reason", ["empty_capture", "capture_aborted"])
def test_a_definitive_speech_abandonment_queues_the_initiative(env, reason):
    ring, op = _answered()
    _claim(op, "speech_started")
    out = _claim(op, "speech_abandoned", reason=reason)
    assert out["opening"]["state"] == "initiative_queued"
    assert out["opening"]["intent"]["state"] == "admitted"
    [row] = _queue(ring["ring_id"])
    assert "started to speak" in row["text"]


@pytest.mark.parametrize("reason", ["transcription_error", "network", "", None])
def test_only_definitive_abandonment_reasons_are_accepted(env, reason):
    ring, op = _answered()
    _claim(op, "speech_started")
    _refused(lambda: _claim(op, "speech_abandoned", reason=reason), 400, "bad claim")
    assert _state(ring["ring_id"])["state"] == "speech_latched"


def test_abandonment_without_a_latch_is_a_bad_claim(env):
    ring, op = _answered()
    _refused(lambda: _claim(op, "speech_abandoned", reason="empty_capture"), 400, "bad claim")


def test_silence_needs_the_deadline_and_a_watermark_past_it(env):
    ring, op = _answered()
    _refused(lambda: _claim(op, "silence", at_ms=900, watermark_ms=1300), 400, "bad claim")
    _refused(lambda: _claim(op, "silence", at_ms=1250, watermark_ms=1100), 400, "bad claim")
    _refused(lambda: _claim(op, "silence", at_ms=1250, watermark_ms=None), 400, "bad claim")
    out = _claim(op, "silence")
    assert out["opening"]["state"] == "initiative_queued"


@pytest.mark.parametrize("claim,needle", [("silence", "1.25 seconds"), ("no_mic", "microphone is unavailable"),
                                          ("not_ready", "microphone could not start")])
def test_each_silence_kind_admits_one_initiative_with_its_header(env, claim, needle):
    ring, op = _answered()
    rows_before = _user_rows()
    out = _claim(op, claim)
    assert out["opening"]["state"] == "initiative_queued" and out["opening"]["intent"]["state"] == "admitted"
    [row] = _queue(ring["ring_id"])
    assert row["status"] == "queued" and row["origin"] == "automation"
    assert row["client_msg_id"] == f"call-open:{ring['ring_id']}:1" == _aid(ring["ring_id"])
    assert needle in row["text"] and "Possible water leak at pump 01" in row["text"]
    assert len(_admissions(ring["ring_id"])) == 1
    assert _user_rows() == rows_before                                # never a user row
    assert env.kicks == [1]


def test_a_repeated_request_is_answered_from_the_record(env):
    ring, op = _answered()
    first = _claim(op, "no_mic", request_id="rq-same")
    again = _claim(op, "no_mic", request_id="rq-same")
    assert again["changed"] is False and again["opening"]["state"] == first["opening"]["state"]
    assert len(_queue(ring["ring_id"])) == 1 and env.kicks == [1]


def test_claim_refusals(env, monkeypatch):
    ring, op = _answered()
    _refused(lambda: rings.opening_claim(PHONE, {**_body(op), "token": "opn_" + "0" * 32}), 404, "unknown opening")
    _refused(lambda: _claim(op, "listening", principal="device_other"), 403, "not the answering device")
    _refused(lambda: _claim(op, "listening", epoch=2), 409, "stale epoch")
    for bad in ({"claim": "greeting"}, {"claim": "nonsense"}, {"epoch": "1"}, {"request_id": ""}):
        _refused(lambda bad=bad: rings.opening_claim(PHONE, {**_body(op), **bad}), 400, "bad claim")
    _claim(op, "no_mic")
    err = _refused(lambda: _claim(op, "silence"), 409, "already resolved")
    assert err.extra["outcome"] == "initiative_queued"
    calls.end(PHONE)
    err = _refused(lambda: _claim(op, "listening"), 409, "opening retired")
    assert err.extra["reason"] == "call_ended"


def _body(op, **kw):
    return {"token": op["token"], "epoch": op["epoch"], "claim": "listening",
            "request_id": f"rq-{next(_IDS)}", "capture_gen": 1, "at_ms": 0, **kw}


# ---- the greeting ------------------------------------------------------------------

def test_the_greeting_resolves_the_opening_for_the_exact_caller(env):
    ring, op = _answered()
    out = _greet(op)
    assert out["route"] == "greeting" and out["session"] == "theo-97e5"
    assert out["opening"]["state"] == "greeting" and out["opening"]["intent"] is None
    assert _queue(ring["ring_id"]) == []
    # The phone retries the same captured turn (an ambiguous ACK): same answer, no new resolution.
    again = _greet(op)
    assert again["route"] == "greeting" and again["changed"] is False
    err = _refused(lambda: _greet(op, client_msg_id="u-other"), 409, "already resolved")
    assert err.extra["outcome"] == "greeting"


def test_a_greeting_for_another_session_or_call_is_the_wrong_call(env):
    ring, op = _answered()
    _refused(lambda: _greet(op, session="mike-86db"), 409, "wrong call")
    _refused(lambda: _greet(op, call_id="gcl_" + "0" * 32), 409, "wrong call")
    _refused(lambda: _greet(op, principal="device_other"), 403, "not the answering device")
    assert _state(ring["ring_id"])["state"] == "pending"


def test_the_listen_first_call_header_says_the_user_spoke_first(env):
    ring, op = _answered()
    _greet(op)
    calls.route(PHONE, op["call_id"], "theo-97e5")
    header = calls.prompt_header("theo-97e5")
    assert "You called the user about: Possible water leak at pump 01" in header
    assert "spoke first" in header and "opening line" not in header


def test_a_greeting_after_renewal_retrying_the_old_epoch_is_deduped(env):
    ring, op = _answered()
    _claim(op, "speech_started")
    _greet(op, client_msg_id="u-first")
    # The ACK was lost; the phone relaunches, renewal is refused (committed); the retry of
    # the same captured turn with the old epoch is still that ring's greeting.
    _refused(lambda: _claim(op, "renew", expected_epoch=1, owner_nonce=op["owner_nonce"]),
             409, "already resolved")
    assert _greet(op, client_msg_id="u-first")["route"] == "greeting"


def test_renewal_keeps_the_latch_and_the_greeting_carries_the_new_epoch(env):
    ring, op = _answered()
    _claim(op, "speech_started")
    renewed = _claim(op, "renew", expected_epoch=1, owner_nonce=op["owner_nonce"])["opening"]
    assert renewed["state"] == "speech_latched" and renewed["epoch"] == 2
    _refused(lambda: _greet(op, epoch=1, client_msg_id="u-new"), 409, "stale epoch")
    assert _greet({**op, "epoch": 2})["route"] == "greeting"


# ---- §11.A: intent and admission, every order ------------------------------------------

def _queued(env, claim="silence"):
    """Resolve to initiative_queued with the admitter held back (intent intended)."""
    _no_crash_after_resolve(env)
    ring, op = _answered()
    out = _claim(op, claim)
    assert out["opening"]["intent"]["state"] == "intended"
    assert _queue(ring["ring_id"]) == [] and _admissions(ring["ring_id"]) == []
    return ring, op


def test_A_greeting_before_admission_cancels_the_intent_with_no_row(env):
    ring, op = _queued(env)
    out = _greet(op)
    assert out["route"] == "greeting" and out["opening"]["intent"]["state"] == "cancelled"
    assert rings.admit_initiative(ring["ring_id"]) == "fenced"
    assert rings.recover_openings() == 0
    assert _queue(ring["ring_id"]) == [] and _admissions(ring["ring_id"]) == []


def test_A_speech_started_before_admission_cancels_and_latches(env):
    ring, op = _queued(env)
    out = _claim(op, "speech_started")
    assert out["opening"]["state"] == "speech_latched" and out["opening"]["intent"]["state"] == "cancelled"
    assert rings.admit_initiative(ring["ring_id"]) == "fenced" and _queue(ring["ring_id"]) == []
    # The latch is fresh: a definitive abandonment queues a new intent generation.
    out = _claim(op, "speech_abandoned", reason="empty_capture")
    assert out["opening"]["intent"]["state"] == "intended" and out["opening"]["intent"]["gen"] == 2
    assert rings.admit_initiative(ring["ring_id"]) == "admitted"
    assert len(_queue(ring["ring_id"])) == 1


def test_A_greeting_after_admission_and_before_the_claim_withdraws_it(env):
    ring, op = _queued(env)
    assert rings.admit_initiative(ring["ring_id"]) == "admitted"
    out = _greet(op)
    assert out["route"] == "greeting" and out["opening"]["intent"]["state"] == "cancelled"
    [row] = _queue(ring["ring_id"])
    assert row["status"] == "withdrawn"
    assert _dispatch(ring["ring_id"]) is False      # the dispatcher loses
    assert rings.recover_openings() == 0


def test_A_greeting_after_the_dispatcher_claim_is_ordinary(env):
    ring, op = _queued(env)
    rings.admit_initiative(ring["ring_id"])
    assert _dispatch(ring["ring_id"]) is True
    assert _state(ring["ring_id"])["state"] == "initiative_started"
    out = _greet(op)
    assert out["route"] == "ordinary" and out["opening"]["state"] == "initiative_started"
    assert out["opening"]["greeting"] == "ordinary" and out["opening"]["intent"]["state"] == "started"
    [row] = _queue(ring["ring_id"])
    assert row["status"] == "claimed"
    turn_queue.mark_started(_aid(ring["ring_id"]))                            # it ran
    assert _dispatch(ring["ring_id"]) is False                                # never a second run


def test_A_the_dispatcher_claim_before_admission_finds_nothing(env):
    ring, op = _queued(env)
    assert _dispatch(ring["ring_id"]) is False
    assert _state(ring["ring_id"])["intent"]["state"] == "intended"


def test_A_crash_after_the_resolve_txn_recovery_admits_exactly_once(env):
    ring, op = _queued(env)
    assert rings.recover_openings() == 1
    assert rings.recover_openings() == 0
    assert len(_queue(ring["ring_id"])) == 1 and len(_admissions(ring["ring_id"])) == 1
    assert _state(ring["ring_id"])["intent"]["state"] == "admitted"


def test_A_a_lazy_read_recovers_too(env):
    ring, op = _queued(env)
    rings.get(ring["ring_id"])
    assert len(_queue(ring["ring_id"])) == 1


def test_A_crash_after_the_admitter_txn_never_admits_twice(env):
    ring, op = _queued(env)
    assert rings.admit_initiative(ring["ring_id"]) == "admitted"
    assert rings.admit_initiative(ring["ring_id"]) == "fenced"
    assert rings.recover_openings() == 0
    assert len(_queue(ring["ring_id"])) == 1 and len(_admissions(ring["ring_id"])) == 1


def test_A_crash_mid_admitter_txn_rolls_back_and_recovery_admits_once(env, monkeypatch):
    ring, op = _queued(env)
    enqueue = turn_queue.enqueue

    def dies(**kwargs):
        enqueue(**kwargs)
        raise RuntimeError("the Host died inside the admitter transaction")
    monkeypatch.setattr(turn_queue, "enqueue", dies)
    with pytest.raises(RuntimeError):
        rings.admit_initiative(ring["ring_id"])
    assert _queue(ring["ring_id"]) == [] and _admissions(ring["ring_id"]) == []
    assert _state(ring["ring_id"])["intent"]["state"] == "intended"
    monkeypatch.setattr(turn_queue, "enqueue", enqueue)
    assert rings.recover_openings() == 1
    assert len(_queue(ring["ring_id"])) == 1 and len(_admissions(ring["ring_id"])) == 1


@pytest.mark.parametrize("trace,result,intent", [
    ("own", "adopted", "admitted"),      # this intent's own admission, left by a crash
    ("t", "fenced", "unknown"),          # D4: a call admission under another trace is not this initiative
])
def test_A_a_stray_admission_row_is_adopted_only_when_it_is_this_intents(env, trace, result, intent):
    ring, op = _queued(env)
    from lib import prompt_admissions
    admission = prompt_admissions.create(
        authenticated_at_admission=True, origin="automation", sender_agent_id="", channel="call",
        observed_at=db.now_ms(), client_admission_id=_aid(ring["ring_id"]),
        trace_id=_state(ring["ring_id"])["intent"]["trace_id"] if trace == "own" else trace,
        original_text="x")
    prompt_admissions.record(admission, agent_id="a-theo", session="theo-97e5")
    assert rings.admit_initiative(ring["ring_id"]) == result
    assert len(_admissions(ring["ring_id"])) == 1 and _queue(ring["ring_id"]) == []
    assert _state(ring["ring_id"])["intent"]["state"] == intent


@pytest.mark.parametrize("state", ["cancelled", "started", "greeting", "retired"])
def test_A_recovery_acts_only_on_intended(env, state):
    ring, op = _queued(env)
    if state == "cancelled":
        _greet(op)
    elif state == "started":
        rings.admit_initiative(ring["ring_id"])
        _dispatch(ring["ring_id"])
    elif state == "greeting":
        _greet(op)
    else:
        calls.end(PHONE)
    before = (len(_queue(ring["ring_id"])), len(_admissions(ring["ring_id"])))
    assert rings.recover_openings() == 0
    assert (len(_queue(ring["ring_id"])), len(_admissions(ring["ring_id"]))) == before


ORDERS = list(__import__("itertools").permutations(["greeting", "admitter", "dispatcher"]))


@pytest.mark.parametrize("order", ORDERS, ids=["-".join(o) for o in ORDERS])
def test_A_greeting_admitter_and_dispatcher_in_every_order(env, order):
    ring, op = _queued(env)
    results = {}
    for step in order:
        if step == "greeting":
            results[step] = _greet(op)["route"]
        elif step == "admitter":
            results[step] = rings.admit_initiative(ring["ring_id"])
        else:
            results[step] = _dispatch(ring["ring_id"])
    final = _state(ring["ring_id"])
    rows = _queue(ring["ring_id"])
    ran = results["dispatcher"] is True
    # Exactly one committed outcome, and the initiative runs at most once.
    assert final["state"] in ("greeting", "initiative_started")
    assert sum(1 for r in rows if r["status"] == "claimed") == (1 if ran else 0)
    if ran:
        assert final["state"] == "initiative_started" and results["greeting"] == "ordinary"
        assert final["intent"]["state"] == "started"
    else:
        assert final["state"] == "greeting" and results["greeting"] == "greeting"
        assert final["intent"]["state"] == "cancelled"
        assert all(r["status"] == "withdrawn" for r in rows)
    assert len(_admissions(ring["ring_id"])) <= 1
    assert rings.recover_openings() == 0


def test_A_concurrent_greeting_admitter_and_dispatcher_threads(env):
    for _ in range(5):
        env.clock.now += rings.AGENT_INTERVAL_MS
        calls_live = calls.snapshot(PHONE)["call"]
        if calls_live:
            calls.end(PHONE)
        ring, op = _queued(env)
        barrier = threading.Barrier(3)
        out, errors = {}, []

        def run(name, fn):
            try:
                barrier.wait(5)
                out[name] = fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close_local()
        threads = [threading.Thread(target=run, args=a) for a in (
            ("greeting", lambda: _greet(op)["route"]),
            ("admitter", lambda: rings.admit_initiative(ring["ring_id"])),
            ("dispatcher", lambda: _dispatch(ring["ring_id"])))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert errors == []
        final = _state(ring["ring_id"])
        assert final["state"] in ("greeting", "initiative_started")
        assert (final["state"] == "initiative_started") == (out["dispatcher"] is True)


# ---- retire ------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["pending", "listening", "speech_latched", "intended", "admitted",
                                   "initiative_started", "greeting"])
def test_retire_in_each_state(env, state):
    if state in ("intended", "admitted", "initiative_started"):
        ring, op = _queued(env)
        if state != "intended":
            rings.admit_initiative(ring["ring_id"])
        if state == "initiative_started":
            _dispatch(ring["ring_id"])
    else:
        ring, op = _answered()
        if state == "listening":
            _claim(op, "listening")
        elif state == "speech_latched":
            _claim(op, "speech_started")
        elif state == "greeting":
            _greet(op)
    calls.end(PHONE)
    final = _state(ring["ring_id"])
    assert final["retired"]["reason"] == "call_ended"
    if state in ("initiative_started", "greeting"):
        assert final["state"] == state                     # committed outcome kept, output fenced
    else:
        assert final["state"] == "retired"
    if state == "admitted":
        assert _queue(ring["ring_id"])[0]["status"] == "withdrawn"
    if state in ("intended", "admitted"):
        assert final["intent"]["state"] == "cancelled"
    assert rings.recover_openings() == 0
    _refused(lambda: _claim(op, "listening"), 409, "opening retired")
    _refused(lambda: _greet(op, client_msg_id="u-late"), 409, "opening retired")


def test_a_superseding_call_retires_and_a_new_ring_gets_a_new_token(env):
    ring, op = _answered()
    calls.start(PHONE, ["mike-86db"])
    assert _state(ring["ring_id"])["retired"]["reason"] == "call_superseded"
    env.clock.now += rings.AGENT_INTERVAL_MS
    ring2, op2 = _answered()
    assert op2["token"] != op["token"] and op2["owner_nonce"] != op["owner_nonce"]
    _refused(lambda: _claim(op, "listening"), 409, "opening retired")
    assert _claim(op2, "listening")["opening"]["state"] == "listening"


def test_revoking_the_device_retires_its_opening(env):
    from lib import device_pairing
    db.conn().execute("INSERT INTO paired_devices(device_id, name, token_hash, scope, created_at)"
                      " VALUES (?,?,?,?,?)", (PHONE, "Phone", "h", "full", db.now_ms()))
    ring, op = _answered()
    assert device_pairing.revoke(PHONE) is True
    assert _state(ring["ring_id"])["retired"]["reason"] == "auth_revoked"
    _refused(lambda: _claim(op, "listening"), 409, "opening retired")


def test_a_host_instance_change_retires_the_opening(env, monkeypatch):
    ring, op = _answered()
    monkeypatch.setattr(rings, "_server_id", lambda: "srv-another-host")
    err = _refused(lambda: _claim(op, "silence"), 409, "opening retired")
    assert err.extra["reason"] == "host_changed"
    assert _queue(ring["ring_id"]) == []


# ---- §11.E renewal ----------------------------------------------------------------------

def test_renewal_is_a_cas_that_rotates_the_nonce(env):
    ring, op = _answered()
    out = _claim(op, "renew", request_id="rq-renew", expected_epoch=1, owner_nonce=op["owner_nonce"])
    renewed = out["opening"]
    assert out["changed"] is True and renewed["epoch"] == 2
    assert renewed["owner_nonce"] != op["owner_nonce"] and renewed["owner_nonce"].startswith("own_")
    again = _claim(op, "renew", request_id="rq-renew", expected_epoch=1, owner_nonce=op["owner_nonce"])
    assert again["changed"] is False and again["opening"]["owner_nonce"] == renewed["owner_nonce"]
    _refused(lambda: _claim(op, "renew", expected_epoch=1, owner_nonce=renewed["owner_nonce"]),
             409, "stale epoch")
    _refused(lambda: _claim(op, "renew", expected_epoch=2, owner_nonce=op["owner_nonce"]),
             409, "owner changed")
    _refused(lambda: _claim(op, "renew", expected_epoch=2, owner_nonce=renewed["owner_nonce"],
                            principal="device_other"), 403, "not the answering device")
    _refused(lambda: _claim(op, "listening", epoch=1), 409, "stale epoch")
    assert _claim({**op, "epoch": 2}, "listening")["opening"]["epoch"] == 2


def test_two_instances_race_the_renewal_and_exactly_one_wins(env):
    ring, op = _answered()
    out, errors = [], []
    barrier = threading.Barrier(2)

    def renew(request_id):
        try:
            barrier.wait(5)
            out.append(_claim(op, "renew", request_id=request_id, expected_epoch=1,
                              owner_nonce=op["owner_nonce"]))
        except calls.CallError as exc:
            errors.append(exc.error)
        finally:
            db.close_local()
    threads = [threading.Thread(target=renew, args=(f"rq-race-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(out) == 1 and errors in (["stale epoch"], ["owner changed"])
    assert _state(ring["ring_id"])["epoch"] == 2


def test_no_renewal_after_commit(env):
    ring, op = _answered()
    _greet(op)
    err = _refused(lambda: _claim(op, "renew", expected_epoch=1, owner_nonce=op["owner_nonce"]),
                   409, "already resolved")
    assert err.extra["outcome"] == "greeting"


# ---- §11.B audio identity -------------------------------------------------------------

def test_opening_audio_is_tagged_and_ordinary_follow_ups_are_not(env):
    ring, op = _queued(env)
    rings.admit_initiative(ring["ring_id"])
    _dispatch(ring["ring_id"])
    initiative_trace = _state(ring["ring_id"])["intent"]["trace_id"]
    tag = rings.opening_audio(initiative_trace)
    assert tag == {"call_id": op["call_id"], "opening": {
        "ring_id": ring["ring_id"], "token": op["token"], "epoch": 1, "kind": "initiative",
        "admission_id": f"call-open:{ring['ring_id']}:1"}}
    assert rings.opening_audio("tr-ordinary") is None
    assert rings.skip_opening_synthesis(initiative_trace) is False


def test_a_greeting_reply_is_tagged_with_its_message_id(env):
    ring, op = _answered()
    _greet(op, client_msg_id="u-hallo", trace_id="tr-greet")
    tag = rings.opening_audio("tr-greet")
    assert tag["opening"]["kind"] == "greeting_reply" and tag["opening"]["admission_id"] == "u-hallo"
    assert rings.skip_opening_synthesis("tr-greet") is False


def test_initiative_synthesis_is_skipped_once_superseded_or_retired(env):
    ring, op = _queued(env)
    rings.admit_initiative(ring["ring_id"])
    _dispatch(ring["ring_id"])
    trace = _state(ring["ring_id"])["intent"]["trace_id"]
    _greet(op, trace_id="tr-after")                         # a greeting arrived after start
    assert rings.skip_opening_synthesis(trace) is True
    assert rings.skip_opening_synthesis("tr-after") is False   # ordinary utterance, not an opening
    calls.end(PHONE)                                         # retire after commit still fences
    assert rings.skip_opening_synthesis(trace) is True


def test_a_retired_greeting_reply_is_skipped_too(env):
    ring, op = _answered()
    _greet(op, trace_id="tr-greet")
    calls.end(PHONE)
    assert rings.skip_opening_synthesis("tr-greet") is True


# ---- the CLI ------------------------------------------------------------------------

def test_the_cli_rings_listen_first_and_the_demo_ring_is_unchanged(monkeypatch, capsys):
    import importlib.util
    root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("clarp_admin_listen_first", root / "bin/clarp-admin.py")
    admin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin)
    sent = []
    monkeypatch.setattr(admin, "api_request", lambda method, path, body=None, **kw: sent.append(
        (method, path, body)) or {"ok": True, "ring": {"ring_id": "rng_1", "state": "ringing"}})
    run = lambda argv: admin.parser().parse_args(argv).func(admin.parser().parse_args(argv))  # noqa: E731
    assert run(["call", "ring", "--agent", "rk-sander", "--reason", "Leak", "--say", "Sander here."]) == 0
    assert sent[-1] == ("POST", "/calls/ring", {"agent": "rk-sander", "reason": "Leak", "opening_line": "Sander here."})
    assert run(["call", "ring", "--reason", "Leak", "--say", "Fallback.", "--listen-first"]) == 0
    assert sent[-1] == ("POST", "/calls/ring", {"reason": "Leak", "opening_line": "Fallback.",
                                                "opening_mode": "listen_first"})


# ---- audio events carry the tag; legacy clips do not -------------------------------------

def test_the_tts_worker_tags_opening_clips_and_skips_fenced_ones(env, tmp_path, monkeypatch):
    from lib import tts_worker
    ring, op = _answered()
    _greet(op, trace_id="tr-greet")
    stream = Stream()
    session = type("S", (), {"target_path": tmp_path / "1__theo.mp3", "clip_id": 7, "sse_fields": {}})()
    row = {"session": "theo-97e5", "voice_id": "v", "source": "pwa", "text": "hi"}
    agent = {"agent_id": "a-theo", "persona": "Theo"}
    tts_worker._publish_clip_event(stream=stream, herald=None, session=session, row=row, agent=agent,
                                   trace_id="tr-greet")
    tts_worker._publish_clip_event(stream=stream, herald=None, session=session, row=row, agent=agent,
                                   trace_id="tr-other")
    tagged, plain = stream.of("audio")
    assert tagged["call_id"] == op["call_id"]
    assert tagged["opening"] == {"ring_id": ring["ring_id"], "token": op["token"], "epoch": 1,
                                 "kind": "greeting_reply", "admission_id": "u-hallo"}
    assert "opening" not in plain and "call_id" not in plain
    calls.end(PHONE)
    assert tts_worker._opening_fenced("tr-greet") is True and tts_worker._opening_fenced("tr-other") is False


# ---- §11.H: a withdrawal that affects no row proves nothing -----------------------------

def _admitted(env):
    ring, op = _queued(env)
    assert rings.admit_initiative(ring["ring_id"]) == "admitted"
    return ring, op


def _greeted_without_start(ring, op):
    out = _greet(op)
    final = _state(ring["ring_id"])
    assert out["route"] == "greeting" and final["state"] == "greeting"
    assert final["intent"]["state"] == "cancelled"
    assert rings.recover_openings() == 0 and _dispatch(ring["ring_id"]) is False   # never resurrected
    return final


def test_H_a_stop_between_admit_and_greeting_withdraws_the_held_row(env):
    ring, op = _admitted(env)
    turn_queue.set_paused("a-theo", True)                    # a Stop: the row is held, unclaimed
    assert _queue(ring["ring_id"])[0]["status"] == "queued"
    _greeted_without_start(ring, op)
    assert _queue(ring["ring_id"])[0]["status"] == "withdrawn"
    turn_queue.set_paused("a-theo", False)
    assert turn_queue.pending("a-theo") == []                # Resume runs nothing


def test_H_a_stop_parked_row_is_withdrawn_too(env):
    ring, op = _admitted(env)
    db.conn().execute("UPDATE queued_turns SET status='parked' WHERE queue_id=?", (_aid(ring["ring_id"]),))
    _greeted_without_start(ring, op)
    assert _queue(ring["ring_id"])[0]["status"] == "withdrawn"


def test_H_an_owner_cancelled_row_cancels_the_intent(env):
    ring, op = _admitted(env)
    assert turn_queue.remove(_aid(ring["ring_id"])) is True   # DELETE /turn-queue/<id>
    _greeted_without_start(ring, op)
    assert _queue(ring["ring_id"]) == []


def test_H_a_recovery_cancelled_row_cancels_the_intent(env):
    ring, op = _admitted(env)
    assert turn_queue.cancel(_aid(ring["ring_id"])) is True   # what recovery retires with
    _greeted_without_start(ring, op)
    assert _queue(ring["ring_id"])[0]["status"] == "cancelled"


def test_H_a_deleted_row_cancels_the_intent(env):
    ring, op = _admitted(env)
    db.conn().execute("DELETE FROM queued_turns WHERE queue_id=?", (_aid(ring["ring_id"]),))
    _greeted_without_start(ring, op)


@pytest.mark.parametrize("status", ["interrupted", "done", "failed", "ended", "started"])
def test_H_a_started_row_under_an_admitted_intent_is_unknown_never_not_started(env, status):
    # P3: only the dispatcher's claim moves the intent to started with the row
    # (P4-I), so a started or settled row under an admitted intent contradicts
    # it: unknown, never "not started" and never "started".
    ring, op = _admitted(env)
    db.conn().execute("UPDATE queued_turns SET status=?, claimed_at=1 WHERE queue_id=?",
                      (status, _aid(ring["ring_id"])))
    out = _greet(op)
    assert out["route"] == "ordinary" and out["opening"]["state"] == "unknown"
    assert _state(ring["ring_id"])["intent"]["state"] == "unknown"


def test_H_interrupted_after_start_stays_committed(env):
    ring, op = _admitted(env)
    assert _dispatch(ring["ring_id"]) is True
    turn_queue.mark_started(_aid(ring["ring_id"]))
    turn_queue.settle_receipt("a-theo", _state(ring["ring_id"])["intent"]["trace_id"], "interrupted")
    assert _queue(ring["ring_id"])[0]["status"] == "interrupted"
    out = _greet(op)
    assert out["route"] == "ordinary" and out["opening"]["state"] == "initiative_started"


def test_H_a_row_claimed_by_a_manual_send_never_passed_the_gate_so_it_cancels(env):
    # P3 by P4-I: a manual send's claim is not the dispatcher's claim of the
    # opening, so an admitted intent proves nothing ran: the greeting cancels it.
    ring, op = _admitted(env)
    assert turn_queue.claim(_aid(ring["ring_id"])) is not None   # a manual send claim, not the opening's
    _greeted_without_start(ring, op)
    # No initiative, ever: not after the stale claim resets, not on recovery.
    turn_queue.reset_stale_claims(max_age_ms=0)
    assert _dispatch(ring["ring_id"]) is False
    assert _queue(ring["ring_id"])[0]["status"] == "withdrawn"
    assert rings.recover_openings() == 0


def test_H_an_admitted_intent_is_decided_from_its_row_never_from_turns_history(env):
    # P3: a turns row under an admitted intent is impossible by P4-I; the
    # verdict comes from the exact row (waiting, unclaimed: withdrawn).
    ring, op = _admitted(env)
    db.conn().execute("INSERT INTO turns(agent_id, source, trace_id, started_at) VALUES (?,?,?,?)",
                      ("a-theo", "pwa", _state(ring["ring_id"])["intent"]["trace_id"], db.now_ms()))
    _greeted_without_start(ring, op)


def test_H_the_dispatcher_never_runs_an_intent_that_is_not_admitted(env):
    ring, op = _admitted(env)
    rings.retire_for_principal(PHONE, "auth_revoked")
    assert _queue(ring["ring_id"])[0]["status"] == "withdrawn"
    db.conn().execute("UPDATE queued_turns SET status='queued' WHERE queue_id=?", (_aid(ring["ring_id"]),))
    assert _dispatch(ring["ring_id"]) is False                 # the intent, not the row, authorizes
    assert _queue(ring["ring_id"])[0]["status"] == "withdrawn"


# ---- §11.I: one admission id per intent generation --------------------------------------

def test_I_speech_withdraws_gen_1_and_abandonment_admits_gen_2(env):
    ring, op = _admitted(env)
    first = _aid(ring["ring_id"])
    assert first == f"call-open:{ring['ring_id']}:1"
    out = _claim(op, "speech_started")
    assert out["opening"]["state"] == "speech_latched"
    out = _claim(op, "speech_abandoned", reason="capture_aborted")
    second = _aid(ring["ring_id"])
    assert second == f"call-open:{ring['ring_id']}:2" and out["opening"]["intent"]["gen"] == 2
    assert rings.admit_initiative(ring["ring_id"]) == "admitted"      # no UNIQUE collision with gen 1
    assert [r["status"] for r in _queue(ring["ring_id"])] == ["withdrawn", "queued"]
    assert [a["client_admission_id"] for a in _admissions(ring["ring_id"])] == [first, second]
    assert rings.claim_initiative(first) is False and rings.claim_initiative(second) is True
    assert _state(ring["ring_id"])["state"] == "initiative_started"


def test_I_a_committed_gen_gets_no_successor(env):
    ring, op = _admitted(env)
    assert _dispatch(ring["ring_id"]) is True
    _refused(lambda: _claim(op, "speech_started"), 409, "already resolved")
    _refused(lambda: _claim(op, "speech_abandoned", reason="empty_capture"), 409, "already resolved")
    assert len(_queue(ring["ring_id"])) == 1 and _state(ring["ring_id"])["intent"]["gen"] == 1


def test_I_recovery_admits_only_the_current_gen(env):
    ring, op = _admitted(env)
    _claim(op, "speech_started")                              # gen 1 withdrawn
    _claim(op, "speech_abandoned", reason="empty_capture")    # gen 2 intended (admitter held back)
    assert _state(ring["ring_id"])["intent"]["state"] == "intended"
    assert rings.recover_openings() == 1
    statuses = {r["queue_id"]: r["status"] for r in _queue(ring["ring_id"])}
    assert statuses == {f"call-open:{ring['ring_id']}:1": "withdrawn", f"call-open:{ring['ring_id']}:2": "queued"}
    assert rings.recover_openings() == 0


@pytest.mark.parametrize("gen1", ["intended", "admitted"])
@pytest.mark.parametrize("crash", ["after_latch", "after_resolve", "mid_admit", "after_admit"])
def test_I_crash_and_recovery_at_each_step_admits_exactly_one_gen_2(env, monkeypatch, gen1, crash):
    """§11.I: gen 1 cancelled (intended) or withdrawn (admitted) by speech; a
    definitive abandonment makes gen 2; a crash at any step and recovery give
    exactly one gen-2 initiative and never touch gen 1."""
    ring, op = _queued(env)                                   # gen 1 intended, admitter held back
    rid = ring["ring_id"]
    if gen1 == "admitted":
        assert rings.admit_initiative(rid) == "admitted"
    _claim(op, "speech_started")
    assert _state(rid)["intent"]["state"] == "cancelled" and _state(rid)["state"] == "speech_latched"
    gen1_rows = [r["status"] for r in _queue(rid)]
    assert gen1_rows == ([] if gen1 == "intended" else ["withdrawn"])
    if crash == "after_latch":
        assert rings.recover_openings() == 0 and [r["status"] for r in _queue(rid)] == gen1_rows
        return
    _claim(op, "speech_abandoned", reason="empty_capture")    # gen 2 intended; the admitter "crashed"
    assert _state(rid)["intent"]["gen"] == 2 and _state(rid)["intent"]["state"] == "intended"
    if crash == "mid_admit":
        enqueue = turn_queue.enqueue

        def dies(**kwargs):
            enqueue(**kwargs)
            raise RuntimeError("the Host died inside the gen-2 admitter transaction")
        monkeypatch.setattr(turn_queue, "enqueue", dies)
        with pytest.raises(RuntimeError):
            rings.admit_initiative(rid)
        monkeypatch.setattr(turn_queue, "enqueue", enqueue)
        assert _state(rid)["intent"]["state"] == "intended"
        assert [r["status"] for r in _queue(rid)] == gen1_rows
    elif crash == "after_admit":
        assert rings.admit_initiative(rid) == "admitted"
    assert rings.recover_openings() == (0 if crash == "after_admit" else 1)
    assert rings.recover_openings() == 0
    gen2 = f"call-open:{rid}:2"
    rows = {r["queue_id"]: r["status"] for r in _queue(rid)}
    assert rows[gen2] == "queued" and list(rows.values()).count("queued") == 1
    assert [a["client_admission_id"] for a in _admissions(rid)].count(gen2) == 1
    assert rings.claim_initiative(f"call-open:{rid}:1") is False
    assert rings.claim_initiative(gen2) is True
    turn_queue.mark_started(gen2)                                             # it ran
    assert rings.claim_initiative(gen2) is False


def test_I_a_committed_gen_1_then_speech_and_abandonment_make_no_gen_2(env):
    ring, op = _queued(env)
    rid = ring["ring_id"]
    rings.admit_initiative(rid)
    assert _dispatch(rid) is True                             # gen 1 committed
    err = _refused(lambda: _claim(op, "speech_started"), 409, "already resolved")
    assert err.extra["outcome"] == "initiative_started"
    _refused(lambda: _claim(op, "speech_abandoned", reason="capture_aborted"), 409, "already resolved")
    assert rings.recover_openings() == 0
    assert [r["queue_id"] for r in _queue(rid)] == [f"call-open:{rid}:1"]
    assert _state(rid)["intent"] == {**_state(rid)["intent"], "gen": 1, "state": "started"}


# ---- §11.L scoped pause authority: the answer passes an EXISTING pause, nothing newer -------

class _Backends:
    CLAUDE = CODEX = "codex"

    def __init__(self):
        self.spawned = []

    def normalize(self, backend):
        return backend or "codex"

    def interrupt(self, backend, agent_id):
        return 0

    def active_handles(self, backend, agent_id):
        return []

    def spawn_turn(self, backend, **kwargs):
        self.spawned.append(kwargs)


def _service(tmp_path):
    from types import SimpleNamespace
    import lib.turn_dispatch as td
    td.reset_for_tests()
    backends = _Backends()
    ctx = SimpleNamespace(default_session="theo-97e5", agents_path=tmp_path / "unused.json", stream=None)
    return td.TurnDispatchService(ctx, backend_registry=backends, home=tmp_path,
                                  uuid_factory=lambda: "backend-session-1", now=lambda: 12.5,
                                  retry_scheduler=lambda delay, fn: None), backends


def _stop(agent_id="a-theo"):
    """What every Stop does to the queue (turn_dispatch.clear_for_agent / begin_stop)."""
    turn_queue.set_paused(agent_id, True)


def _held(queue_id, *, origin="user", agent_id="a-theo"):
    turn_queue.enqueue(queue_id=queue_id, agent_id=agent_id, session="theo-97e5", text=queue_id,
                       trace_id=f"t-{queue_id}", client_msg_id=queue_id, synthesize_audio=False,
                       origin=origin, sender_agent_id="")


def _silence(op, rid="rq-silence"):
    return _claim(op, "silence", request_id=rid)


def _L_retired(ring_id):
    final = _state(ring_id)
    assert final["retired"]["reason"] == "stopped_after_answer", final["retired"]
    assert final["state"] == "retired"
    assert (final.get("intent") or {}).get("state") in (None, "cancelled")


def test_L1_a_pause_before_the_answer_passes_exactly_one_opening_and_nothing_else(env, tmp_path):
    service, backends = _service(tmp_path)
    _held("u-held-1")
    _stop()                                                     # paused BEFORE the answer
    _held("u-held-2")
    ring, op = _answered()
    out = _silence(op)
    assert out["opening"]["intent"]["state"] == "admitted"
    service.recover_queued()
    assert [s["text"].count("Open the conversation") for s in backends.spawned] == [1]   # exactly one
    assert turn_queue.status("u-held-1") == "queued" and turn_queue.status("u-held-2") == "queued"
    assert db.conn().execute("SELECT count(*) FROM queued_turns WHERE queue_id LIKE 'u-held-%'"
                             " AND claimed_at IS NOT NULL").fetchone()[0] == 0
    assert turn_queue.is_paused("a-theo") is True               # the queue stays paused
    service.recover_queued()
    assert len(backends.spawned) == 1


def test_L1_the_opening_starting_never_ends_the_pause_even_with_nothing_else_held(env, tmp_path):
    service, backends = _service(tmp_path)
    _stop()
    ring, op = _answered()
    _silence(op)
    service.recover_queued()
    assert len(backends.spawned) == 1
    assert turn_queue.is_paused("a-theo") is True


def test_L2_a_stop_after_the_answer_before_admission_admits_nothing(env, tmp_path):
    service, backends = _service(tmp_path)
    ring, op = _answered()
    _stop()                                                     # newer than the answer
    err = _refused(lambda: _silence(op), 409, "opening retired")
    assert err.extra["reason"] == "stopped_after_answer"
    _L_retired(ring["ring_id"])
    assert _queue(ring["ring_id"]) == [] and _admissions(ring["ring_id"]) == []
    service.recover_queued()
    assert backends.spawned == [] and rings.recover_openings() == 0


def test_L2_the_admitter_rechecks_the_fence_in_its_own_transaction(env, tmp_path):
    ring, op = _queued(env)                                     # resolved; the admitter has not run
    _stop()
    assert rings.admit_initiative(ring["ring_id"]) == "fenced"
    _L_retired(ring["ring_id"])
    assert _queue(ring["ring_id"]) == [] and _admissions(ring["ring_id"]) == []


def test_L3_a_stop_after_admission_before_the_claim_means_no_spawn(env, tmp_path):
    service, backends = _service(tmp_path)
    ring, op = _queued(env)
    assert rings.admit_initiative(ring["ring_id"]) == "admitted"
    _stop()
    service.recover_queued()
    assert backends.spawned == []
    assert rings.claim_initiative(_aid(ring["ring_id"])) is False
    _L_retired(ring["ring_id"])
    assert _queue(ring["ring_id"])[0]["status"] == "withdrawn"
    assert rings.recover_openings() == 0


def test_L4_a_stop_after_the_claim_keeps_existing_stop_semantics_and_no_resurrection(env, tmp_path):
    ring, op = _queued(env)
    rings.admit_initiative(ring["ring_id"])
    assert _dispatch(ring["ring_id"]) is True                  # claimed: the initiative is running
    _stop()                                                     # Stop semantics as today (interrupt/hold)
    final = _state(ring["ring_id"])
    assert final["state"] == "initiative_started" and final["intent"]["state"] == "started"
    assert rings.recover_openings() == 0 and _dispatch(ring["ring_id"]) is False
    assert len(_queue(ring["ring_id"])) == 1


@pytest.mark.parametrize("case", ["late_answer", "other_principal", "other_auth_generation", "duplicate_answer"])
def test_L5_no_extra_admission_for_late_foreign_reauthenticated_or_duplicate_answers(env, case):
    db.conn().execute("INSERT INTO paired_devices(device_id, name, token_hash, scope, created_at)"
                      " VALUES (?,?,?,?,?)", (PHONE, "Phone", "hash-1", "full", db.now_ms()))
    ring = _lf()
    if case == "late_answer":
        rings.decline(PHONE, ring["ring_id"])
        _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"), 409, "the call is over")
        assert rings.opening_state(ring["ring_id"]) is None
        return
    op = rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")["opening"]
    if case == "other_principal":
        _refused(lambda: rings.opening_claim(
            "device_other", {"token": op["token"], "epoch": 1, "claim": "silence", "request_id": "rq-x",
                             "capture_gen": 1, "at_ms": 1250, "watermark_ms": 1290}), 403, "not the answering device")
        assert _queue(ring["ring_id"]) == []
        return
    if case == "duplicate_answer":
        again = rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
        assert again["changed"] is False and again["opening"]["token"] == op["token"]
        _silence(op)
        _refused(lambda: _silence(op, rid="rq-again"), 409, "already resolved")
        assert len(_queue(ring["ring_id"])) == 1 and len(_admissions(ring["ring_id"])) == 1
        return
    # The phone re-paired: a new token for the same device id is another auth generation.
    db.conn().execute("UPDATE paired_devices SET token_hash='hash-2' WHERE device_id=?", (PHONE,))
    err = _refused(lambda: _silence(op), 409, "opening retired")
    assert err.extra["reason"] == "auth_changed"
    assert _queue(ring["ring_id"]) == [] and _admissions(ring["ring_id"]) == []


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("crash", ["before_admit", "before_claim"])
def test_L6_recovery_rechecks_the_fence_and_admits_at_most_once(env, tmp_path, stopped, crash):
    service, backends = _service(tmp_path)
    ring, op = _queued(env)                                     # crash right after the resolve txn
    if crash == "before_claim":
        assert rings.admit_initiative(ring["ring_id"]) == "admitted"
    if stopped:
        _stop()
    admitted = rings.recover_openings()
    service.recover_queued()
    service.recover_queued()
    if stopped:
        assert admitted == 0 and backends.spawned == []
        _L_retired(ring["ring_id"])
        assert all(r["status"] == "withdrawn" for r in _queue(ring["ring_id"]))
    else:
        assert admitted == (1 if crash == "before_admit" else 0)
        assert len(backends.spawned) == 1
        assert len(_admissions(ring["ring_id"])) == 1


def test_L7_other_automation_for_the_agent_stays_held(env, tmp_path):
    service, backends = _service(tmp_path)
    _stop()
    _held("hb-1", origin="heartbeat")
    ring, op = _answered()
    _held("auto-2", origin="automation")
    _silence(op)
    service.recover_queued()
    assert len(backends.spawned) == 1 and "Open the conversation" in backends.spawned[0]["text"]
    assert turn_queue.status("hb-1") == "queued" and turn_queue.status("auto-2") == "queued"
    assert turn_queue.is_paused("a-theo") is True


# ---- review L2: the greeting's commit and its route are one decision -----------------------

def _route_greet(op, *, client_msg_id="u-hallo", principal=PHONE):
    return rings.greeting_routed(principal, token=op["token"], epoch=op["epoch"], client_msg_id=client_msg_id,
                                 trace_id="tr-greet", session=op["session"], call_id=op["call_id"])


def test_L2_a_caller_on_hold_refuses_the_greeting_and_leaves_the_opening_untouched(env):
    ring, op = _queued(env)
    rings.admit_initiative(ring["ring_id"])
    before = _state(ring["ring_id"])
    calls.change(PHONE, "hold", "theo-97e5")
    err = _refused(lambda: _route_greet(op), 409, "wrong call")
    assert err.extra["reason"] == "not_reachable"
    after = _state(ring["ring_id"])
    assert (after["state"], after["intent"]) == (before["state"], before["intent"])
    assert _queue(ring["ring_id"])[0]["status"] == "queued"


def test_L2_an_ended_call_refuses_before_the_opening_is_touched(env, monkeypatch):
    ring, op = _answered()
    monkeypatch.setattr(rings, "retire_for_call", lambda call_id, reason: 0)   # the retire hook lags
    calls.end(PHONE)
    _refused(lambda: _route_greet(op), 409, "call ended")
    assert _state(ring["ring_id"])["state"] == "pending"


def test_L2_a_greeting_then_a_hold_was_routed_to_the_caller(env):
    ring, op = _answered()
    out = _route_greet(op)
    assert out["route"] == "greeting" and out["session"] == "theo-97e5"
    assert calls.snapshot(PHONE)["call"]["floor"] == "theo-97e5"
    calls.change(PHONE, "hold", "theo-97e5")                  # afterwards: it was already delivered
    assert _state(ring["ring_id"])["state"] == "greeting"


# ---- review L3 / Q3: the stop generation moves with the pause, even for a failed Stop ------

def test_L3_the_stop_generation_and_the_pause_are_one_transaction(env, monkeypatch):
    before = turn_queue.stop_generation("a-theo")

    def broken(agent_id):
        raise RuntimeError("the pause write failed")
    monkeypatch.setattr(turn_queue, "is_paused", broken)
    with pytest.raises(RuntimeError):
        turn_queue.set_paused("a-theo", True)
    monkeypatch.undo()
    assert turn_queue.stop_generation("a-theo") == before and turn_queue.is_paused("a-theo") is False


@pytest.mark.parametrize("paused_before", [True, False])
def test_Q3_a_failed_stop_retires_the_opening_and_restores_the_pause(env, tmp_path, monkeypatch, paused_before):
    from lib import backends, runtime_bridge
    service, spawner = _service(tmp_path)
    _held("u-held-q3")
    if paused_before:
        _stop()
    ring, op = _answered()
    monkeypatch.setattr(backends, "interrupt", lambda backend, agent_id: (_ for _ in ()).throw(
        RuntimeError("the backend did not answer")))
    server = object.__new__(runtime_bridge.RuntimeRPCServer)
    out = server._dispatch_request("begin_stop", {"agent_id": "a-theo", "backend": "codex", "strict": True})
    assert out["ok"] is False                                  # the Stop failed and was rolled back
    assert turn_queue.is_paused("a-theo") is paused_before    # the pause is what it was
    assert turn_queue.status("u-held-q3") == "queued"
    err = _refused(lambda: _silence(op), 409, "opening retired")
    assert err.extra["reason"] == "stopped_after_answer"
    service.recover_queued()
    assert not any("Open the conversation" in s["text"] for s in spawner.spawned)   # no opening, ever
    assert _admissions(ring["ring_id"]) == []
    # Held work stays held under the restored pause; with no pause it is ordinary queued work.
    assert turn_queue.status("u-held-q3") == ("queued" if paused_before else "started")
    assert rings.recover_openings() == 0
    _refused(lambda: _greet(op), 409, "opening retired")       # the opening path is closed
    assert calls.route(PHONE, op["call_id"], "theo-97e5")["session"] == "theo-97e5"   # ordinary speech works


# ---- review L4: no transcription view on the say path, even with an agent STT override -----

def test_L4_the_say_path_carries_no_transcription_view_with_an_agent_override(env):
    plain = _say_scenario(env)
    calls.end(PHONE)
    db.conn().execute("DELETE FROM messages")
    env.clock.now += rings.AGENT_INTERVAL_MS
    settings_store.set_text("transcription.agent_languages", json.dumps({"theo": "no"}))
    settings_store.set_text("transcription.agent_engines", json.dumps({"theo": "google:chirp_3"}))
    override = _say_scenario(env)
    assert "transcription" not in override[1]["call"]
    assert "transcription" not in calls.snapshot(PHONE)["call"]
    assert _scrub(plain[1]) == _scrub(override[1])
    assert _scrub([e for e in plain[2] if e["type"] in ("agent-ring", "group-call")]) == \
        _scrub([e for e in override[2] if e["type"] in ("agent-ring", "group-call")])
    assert plain[4] == override[4]


def test_L4_a_listen_first_call_shows_the_view_when_an_override_applies(env):
    settings_store.set_text("transcription.agent_languages", json.dumps({"theo": "no"}))
    ring, op = _answered()
    view = calls.snapshot(PHONE)["call"]["transcription"]
    assert view["theo-97e5"]["language"] == {"value": "no", "source": "agent"}


# ---- review L5: the answer and its fence are one transaction -------------------------------

def test_L5_a_failure_while_opening_rolls_the_answer_back(env, monkeypatch):
    ring = _lf()
    real, broken = rings._auth_proof, {"on": True}

    def dies(principal):
        if broken["on"]:
            raise RuntimeError("the Host died while recording the opening")
        return real(principal)
    monkeypatch.setattr(rings, "_auth_proof", dies)
    with pytest.raises(RuntimeError):
        rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    broken["on"] = False
    assert rings.get(ring["ring_id"])["state"] == "ringing" and _state(ring["ring_id"]) is None
    op = rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")["opening"]
    assert op["state"] == "pending" and op["token"].startswith("opn_")
    assert _state(ring["ring_id"])["fence"]["stop_generation"] == turn_queue.stop_generation("a-theo")


# ---- C2-S2b: host identity at every opening site (WIRING-DESIGN §2.2, ADD-1, ADD-2 A2', R-a, R-b) ----

from lib import host_generation                      # noqa: E402

_PIN = ("boot-synthetic-test", "00000000-0000-4000-8000-000000000001")     # tests/conftest.py
REBOOTED = host_generation.GenerationRead(("boot-after-reboot", _PIN[1]))
RESTORED = host_generation.GenerationRead((_PIN[0], "00000000-0000-4000-8000-000000000002"))
UNPROVABLE = host_generation.GenerationRead(None, "token absent")
BREACHES = [(REBOOTED, "host_rebooted"), (RESTORED, "db_restored"), (UNPROVABLE, "host_identity_unprovable")]


def _host(env, read):
    env.monkeypatch.setattr(rings, "_generation", lambda: read)


def _stored_ring(ring_id):
    return next(r for r in rings._load() if r["ring_id"] == ring_id)


def test_the_real_identity_seam_refuses_a_missed_injection(_pinned_host_generation):
    """P-b11: agent_rings reads the host identity only through host_generation's
    default seams, so an unpinned read in a test fails instead of reading the
    kernel boot id or the live data root's token."""
    assert _pinned_host_generation is not None, "agent_rings has no _generation seam"
    with pytest.raises(pytest.fail.Exception):
        _pinned_host_generation()


def test_listen_first_ring_is_refused_while_host_identity_is_unprovable(env):
    """P-b5: no listen-first ring without a provable identity; a say ring still rings."""
    _host(env, UNPROVABLE)
    _refused(lambda: _lf(), 409,
             "listen-first opening is unavailable: host identity unprovable; ring with opening_mode say")
    assert _ring()["ring"]["state"] == "ringing"


def test_a_listen_first_ring_keeps_its_host_identity_off_the_wire(env):
    ring = _lf()
    assert _stored_ring(ring["ring_id"])["_host_gen"] == {"boot": _PIN[0], "instance": _PIN[1]}
    assert "_host_gen" not in ring and "host_gen" not in json.dumps(ring)


@pytest.mark.parametrize("read, cause", BREACHES)
def test_answer_after_a_reboot_or_restore_ends_the_ring_missed(env, read, cause):
    """P-b4: a listen-first ring answered on another boot or instance is over:
    missed, internal end reason, no call, no opening record."""
    ring = _lf()
    _host(env, read)
    err = _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
                   409, "the call is over")
    stored = _stored_ring(ring["ring_id"])
    assert stored["state"] == "missed" and stored["_end_reason"] == cause
    assert stored["call_id"] == "" and _state(ring["ring_id"]) is None
    assert "_end_reason" not in json.dumps(err.body())


def test_a_say_ring_is_answered_whatever_the_host_identity(env):
    ring = _ring()["ring"]
    _host(env, UNPROVABLE)
    out = rings.answer(PHONE, ring["ring_id"])
    assert out["opening"]["text"] == ring["opening_line"] and "token" not in out["opening"]


def test_answered_ring_replay_withholds_the_secret_after_a_breach(env):
    """R-a / P-b10c: only the replay that would return the token is guarded, and
    the proof is the payload actually returned."""
    ring, op = _answered()
    replay = rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    assert replay["opening"]["token"] == op["token"]                    # control: no breach
    _host(env, RESTORED)
    _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
             409, "the call is over")
    assert _stored_ring(ring["ring_id"])["state"] == "answered"         # kept as stored
    legacy = rings.answer(PHONE, ring["ring_id"])                       # say-shaped replay
    assert legacy["opening"]["text"] == ring["opening_line"]
    assert "token" not in json.dumps(legacy) and "owner_nonce" not in json.dumps(legacy)


@pytest.mark.parametrize("read, cause", BREACHES)
def test_a_live_opening_goes_unknown_on_a_breach_and_refuses_the_claim(env, read, cause):
    """P-b2 at opening_claim: settle unknown before any replay or claim."""
    ring, op = _answered()
    _host(env, read)
    _refused(lambda: _claim(op, "listening"), 409, "opening retired")
    final = _state(ring["ring_id"])
    assert final["state"] == "unknown" and final["retired"]["reason"] == cause
    assert final["identity_breach"]["cause"] == cause and final["requests"] == {}


def test_a_queued_initiative_goes_unknown_on_a_breach_never_withdrawn(env):
    """P-b2 at claim_initiative: the row is unknown (never withdrawn), no spawn."""
    ring, op = _admitted(env)
    _host(env, REBOOTED)
    assert _dispatch(ring["ring_id"]) is False
    final = _state(ring["ring_id"])
    assert final["intent"]["state"] == "unknown" and final["state"] == "unknown"
    assert [r["status"] for r in _queue(ring["ring_id"])] == ["unknown"]


def test_a_completed_opening_is_kept_as_stored_and_still_denied(env):
    """A2' / P-b10a: terminal history is never rewritten by a breach, and the
    breach is never permission: the claim is refused, the bytes are unchanged."""
    ring, op = _answered()
    _greet(op)
    before = settings_store.get(rings.OPENING_KEY + ring["ring_id"])
    _host(env, RESTORED)
    _refused(lambda: _claim(op, "listening"), 409, "opening retired")
    _refused(lambda: _greet(op), 409, "opening retired")
    assert rings.retire_for_call(op["call_id"], "call_ended") == 0
    assert settings_store.get(rings.OPENING_KEY + ring["ring_id"]) == before


def test_an_identity_change_inside_the_transaction_is_unprovable(env):
    """P-b7 (N2): the read before BEGIN and the read inside differ → unprovable."""
    ring, op = _answered()
    reads = itertools.chain([host_generation.GenerationRead(_PIN)], itertools.repeat(RESTORED))
    env.monkeypatch.setattr(rings, "_generation", lambda: next(reads))
    _refused(lambda: _claim(op, "listening"), 409, "opening retired")
    assert _state(ring["ring_id"])["identity_breach"]["cause"] == "host_identity_unprovable"


def test_an_earlier_retire_keeps_its_reason_after_a_breach(env):
    """P-b8: a retired opening is history: the breach refuses and rewrites nothing."""
    ring, op = _answered()
    assert rings.retire_for_call(op["call_id"], "call_ended") == 1
    _host(env, RESTORED)
    _refused(lambda: _claim(op, "listening"), 409, "opening retired")
    final = _state(ring["ring_id"])
    assert final["retired"]["reason"] == "call_ended" and "identity_breach" not in final


def test_after_a_breach_retire_and_supersede_write_nothing_more(env):
    """P-b8: a settled opening cannot be rewritten by a later branch."""
    ring, op = _queued(env)
    _host(env, RESTORED)
    assert rings.admit_initiative(ring["ring_id"]) == "fenced"
    final = _state(ring["ring_id"])
    assert final["retired"]["reason"] == "db_restored" and final["intent"]["state"] == "unknown"
    # Probes that only the identity_breach mark stops: unretired, and a live intent.
    unretired = json.loads(json.dumps(final))
    unretired["retired"] = None
    assert rings._retire(unretired, "call_ended") is False and unretired["retired"] is None
    live = json.loads(json.dumps(final))
    live["intent"]["state"] = "intended"
    assert rings._supersede(live) == "unknown" and live["intent"]["state"] == "intended"


def test_identity_reads_inside_a_site_are_read_only(env, tmp_path):
    """P-b1/P-b6 on a real tmp root: the site reads the boot id and the token,
    creates nothing; a deleted token makes the opening unknown."""
    import os
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    boot = tmp_path / "boot_id"
    boot.write_text("11111111-1111-4111-8111-111111111111\n")
    host_generation.ensure_instance_generation(backups_dir=backups)
    env.monkeypatch.setattr(rings, "_generation", lambda: host_generation.read_host_generation(
        backups_dir=backups, boot_id_path=boot))
    ring, op = _answered()
    writes = ("link", "replace", "fsync", "mkdir", "unlink", "rename")
    originals = {name: getattr(os, name) for name in writes}
    for name in writes:
        env.monkeypatch.setattr(os, name, lambda *a, _n=name, **k: pytest.fail(f"os.{_n} at a site"))
    out = _claim(op, "listening")
    assert out["opening"]["state"] == "listening"
    for name, original in originals.items():
        env.monkeypatch.setattr(os, name, original)
    (backups / host_generation.TOKEN_NAME).unlink()
    _refused(lambda: _claim(op, "speech_started"), 409, "opening retired")
    assert _state(ring["ring_id"])["identity_breach"]["cause"] == "host_identity_unprovable"


def test_legacy_records_without_a_host_identity_fail_closed(env):
    """P-b3 (N3): a listen-first ring or opening stored before this change has
    no host identity: the ring cannot be answered, the opening goes unknown."""
    ring = _lf()
    records = rings._load()
    next(r for r in records if r["ring_id"] == ring["ring_id"]).pop("_host_gen")
    rings._save(records)
    _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
             409, "the call is over")
    assert _stored_ring(ring["ring_id"])["_end_reason"] == "host_identity_unprovable"

    ring, op = _answered(agent="a-mike")
    stored = _state(ring["ring_id"])
    stored["fence"].pop("host_gen")
    settings_store.set_text(rings.OPENING_KEY + ring["ring_id"], json.dumps(stored))
    _refused(lambda: _claim(op, "listening"), 409, "opening retired")
    assert _state(ring["ring_id"])["identity_breach"]["cause"] == "host_identity_unprovable"


def test_a_breach_miss_tells_every_device_but_not_that_the_user_ignored_it(env):
    """D-5: a ring that ends missed on a breach is announced like any missed ring
    (agent-ring event and the replacing push, so other devices and CallKit stop
    ringing), but the agent is not told the user did not answer: they did."""
    notices: list[str] = []
    env.monkeypatch.setattr(rings, "_notice", lambda ring, text: notices.append(text))
    ring = _lf()
    _host(env, RESTORED)
    _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
             409, "the call is over")
    missed = [e for e in env.stream.of("agent-ring")
              if e["ring_id"] == ring["ring_id"] and e["state"] == "missed"]
    assert len(missed) == 1 and "_end_reason" not in json.dumps(missed[0])
    assert [kind for kind, r in env.pushes if r["ring_id"] == ring["ring_id"]][-1] == "missed"
    assert notices == []


def _calls_of(principal=PHONE):
    raw = settings_store.get(calls.KEY + principal)
    return json.loads(raw)["calls"] if raw else []


def _reads(env, *sequence):
    """The identity seam returns ``sequence`` in order, then its last value."""
    reads = itertools.chain(sequence, itertools.repeat(sequence[-1]))
    env.monkeypatch.setattr(rings, "_generation", lambda: next(reads))


def test_identity_moving_before_the_call_starts_starts_no_call(env):
    """Fresh answer: the read before the ring lock matches, but the proof under
    the write lock (taken before group_calls.start) does not. No call, no
    opening, no token: the ring is over."""
    ring = _lf()
    pinned = host_generation.GenerationRead(_PIN)
    _reads(env, pinned, RESTORED)
    _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
             409, "the call is over")
    stored = _stored_ring(ring["ring_id"])
    assert stored["state"] == "missed" and stored["_end_reason"] == "db_restored"
    assert _calls_of() == [] and _state(ring["ring_id"]) is None


def test_identity_moving_after_the_call_started_ends_it_and_mints_nothing(env):
    """The residual window: proven before the call starts, moved before the
    opening is recorded. The call is ended for real, the opening is born
    unknown without authority, and no ACK or token is returned."""
    ring = _lf()
    pinned = host_generation.GenerationRead(_PIN)
    _reads(env, pinned, pinned, pinned, RESTORED)
    err = _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
                   409, "the call is over")
    assert "token" not in json.dumps(err.body())
    [call] = _calls_of()
    assert call["state"] == "ended"
    op = _state(ring["ring_id"])
    assert op["state"] == "unknown" and op["identity_breach"]["cause"] == "db_restored"
    assert op["retired"]["reason"] == "db_restored"
    _refused(lambda: _claim({"token": op["token"], "epoch": op["epoch"]}, "listening"),
             409, "opening retired")


def test_say_rings_never_read_the_host_identity(env):
    """D-4: a say ring, its answer and a say-shaped replay of a listen-first
    ring need no identity, so the seam is never called."""
    env.monkeypatch.setattr(rings, "_generation",
                            lambda: pytest.fail("a say path read the host identity"))
    ring = _ring()["ring"]
    rings.answer(PHONE, ring["ring_id"])
    rings.answer(PHONE, ring["ring_id"])                                  # say replay


def test_a_say_shaped_replay_of_a_listen_first_ring_reads_no_identity(env):
    ring, _op = _answered()
    env.monkeypatch.setattr(rings, "_generation",
                            lambda: pytest.fail("a say-shaped replay read the host identity"))
    out = rings.answer(PHONE, ring["ring_id"])
    assert out["opening"]["text"] == ring["opening_line"]


def test_a_replay_after_a_transient_unprovable_record_returns_no_secret(env):
    """Solu #1: the record-time read is unprovable for a moment, so the opening
    is born unknown and retired while the ring keeps its original identity.
    When reads recover, a listen-first replay must honour the OPENING's own
    authority: no ACK, no token, no say-shaped fallback, no history rewrite."""
    ring = _lf()
    pinned = host_generation.GenerationRead(_PIN)
    _reads(env, pinned, pinned, pinned, UNPROVABLE, UNPROVABLE, pinned)
    _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
             409, "the call is over")
    before = settings_store.get(rings.OPENING_KEY + ring["ring_id"])
    assert json.loads(before)["state"] == "unknown"
    _host(env, pinned)                                                  # reads recovered
    err = _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
                   409, "the call is over")
    assert "token" not in json.dumps(err.body()) and "owner_nonce" not in json.dumps(err.body())
    assert settings_store.get(rings.OPENING_KEY + ring["ring_id"]) == before


# ---- ADD-7 replay authority, ADD-8 post-start compensation ----

def test_a_replay_of_an_opening_settled_unknown_by_a_site_returns_no_secret(env):
    """ADD-7: identity_breach set by a site while the ring's identity is fine;
    after reads recover, an LF replay is refused without a token, bytes unchanged."""
    ring, op = _answered()
    _host(env, RESTORED)
    _refused(lambda: _claim(op, "listening"), 409, "opening retired")
    before = settings_store.get(rings.OPENING_KEY + ring["ring_id"])
    assert json.loads(before)["identity_breach"]["cause"] == "db_restored"
    _host(env, host_generation.GenerationRead(_PIN))
    err = _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
                   409, "the call is over")
    assert "token" not in json.dumps(err.body())
    assert settings_store.get(rings.OPENING_KEY + ring["ring_id"]) == before


def test_a_replay_of_a_retired_opening_returns_no_secret(env):
    """ADD-7 (disclosed delta, DRAFT row): an LF replay of an opening retired for
    any reason is 409, not an ACK carrying `retired`."""
    ring, op = _answered()
    assert rings.retire_for_call(op["call_id"], "call_ended") == 1
    err = _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
                   409, "the call is over")
    assert "token" not in json.dumps(err.body())


def _record_ends(env, *, fail=False):
    ends: list[str] = []
    original = calls.end

    def end(caller, *, call_id="", principal="", by=""):
        ends.append(call_id)
        if fail:
            raise RuntimeError("injected end failure (before any state change)")
        return original(caller, call_id=call_id, principal=principal, by=by)

    env.monkeypatch.setattr(calls, "end", end)
    return ends


def _audit_raising(env, outcome):
    original = rings._audit

    def audit(entry):
        if entry.get("outcome") == outcome:
            raise RuntimeError(f"audit {outcome} failed")
        return original(entry)

    env.monkeypatch.setattr(rings, "_audit", audit)


def test_compensation_lf_failure_before_commit_ends_the_call_and_keeps_the_ring_ringing(env):
    """ADD-8 A: the record txn rolls back; the started call is ended once; the
    original exception propagates; the ring is still ringing, no opening."""
    ring = _lf()
    ends = _record_ends(env)

    def broken(*a, **k):
        raise RuntimeError("open record failed")

    env.monkeypatch.setattr(rings, "_open_record", broken)
    with pytest.raises(RuntimeError, match="open record failed"):
        rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    [call] = _calls_of()
    assert ends == [call["call_id"]] and call["state"] == "ended"
    assert _stored_ring(ring["ring_id"])["state"] == "ringing"
    assert _state(ring["ring_id"]) is None


@pytest.mark.parametrize("outcome", ["opening", "answered"])
def test_compensation_lf_audit_failure_after_commit_retires_the_opening(env, outcome):
    """ADD-8 B: the ring and opening are committed, then an audit raises. The
    started call is ended once, which retires the opening; the next LF replay
    is refused; the original exception propagates."""
    ring = _lf()
    ends = _record_ends(env)
    _audit_raising(env, outcome)
    with pytest.raises(RuntimeError, match=f"audit {outcome} failed"):
        rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    [call] = _calls_of()
    assert ends == [call["call_id"]] and call["state"] == "ended"
    assert _stored_ring(ring["ring_id"])["state"] == "answered"
    assert _state(ring["ring_id"])["retired"]["reason"].startswith("call_")
    env.monkeypatch.setattr(rings, "_audit", lambda entry: None)
    _refused(lambda: rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first"),
             409, "the call is over")


def test_compensation_end_failure_after_commit_is_the_reported_residual(env):
    """ADD-8 B-fail (accepted residual): the end attempt fails at the injected
    boundary (before any state change), so the call stays live and the committed,
    identity-proven opening stays usable: an LF replay returns the ACK. One end
    attempt only; the original exception propagates."""
    ring = _lf()
    ends = _record_ends(env, fail=True)
    _audit_raising(env, "opening")
    with pytest.raises(RuntimeError, match="audit opening failed"):
        rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    [call] = _calls_of()
    assert len(ends) == 1 and ends[0] == call["call_id"]
    assert call["state"] == "live"                                   # observed: end never ran
    op = _state(ring["ring_id"])
    assert op["state"] == "pending" and not op.get("retired")
    env.monkeypatch.setattr(rings, "_audit", lambda entry: None)
    replay = rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    assert replay["opening"]["token"] == op["token"]


def test_compensation_say_save_failure_ends_the_call(env):
    """ADD-8 C: a say ring's save fails after the call started: one end, the
    original exception, the ring still ringing."""
    ring = _ring()["ring"]
    ends = _record_ends(env)
    original = rings._save

    def save(records):
        if any(r["ring_id"] == ring["ring_id"] and r["state"] == "answered" for r in records):
            raise RuntimeError("save failed")
        return original(records)

    env.monkeypatch.setattr(rings, "_save", save)
    with pytest.raises(RuntimeError, match="save failed"):
        rings.answer(PHONE, ring["ring_id"])
    [call] = _calls_of()
    assert ends == [call["call_id"]] and call["state"] == "ended"
    assert _stored_ring(ring["ring_id"])["state"] == "ringing"


def test_compensation_say_audit_failure_after_save_ends_the_call(env):
    """ADD-8 D: the say ring is saved answered, then its audit raises: one end,
    the original exception; the ring stays answered (with an ended call)."""
    ring = _ring()["ring"]
    ends = _record_ends(env)
    _audit_raising(env, "answered")
    with pytest.raises(RuntimeError, match="audit answered failed"):
        rings.answer(PHONE, ring["ring_id"])
    [call] = _calls_of()
    assert ends == [call["call_id"]] and call["state"] == "ended"
    assert _stored_ring(ring["ring_id"])["state"] == "answered"


def test_compensation_never_replaces_the_original_exception(env):
    """ADD-8: the end fails AND every logging path raises (including the stderr
    last resort): the original exception is still the one raised."""
    import builtins
    ring = _lf()
    ends = _record_ends(env, fail=True)
    original = rings._audit

    def broken_log(*a, **k):
        raise RuntimeError("logging failed")

    def audit(entry):
        # Logging breaks only once the call is up and the opening audit fails:
        # earlier, the call start's own log line would raise first and no
        # compensation would run at all.
        if entry.get("outcome") == "opening":
            env.monkeypatch.setattr(rings, "log_exception", broken_log)
            env.monkeypatch.setattr(builtins, "print", broken_log)
            raise RuntimeError("audit opening failed")
        return original(entry)

    env.monkeypatch.setattr(rings, "_audit", audit)
    with pytest.raises(RuntimeError, match="audit opening failed"):
        rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    assert len(ends) == 1                                           # exactly one attempt


@pytest.mark.parametrize("listen", [True, False])
def test_a_successful_answer_ends_no_call(env, listen):
    ring = _lf() if listen else _ring()["ring"]
    ends = _record_ends(env)
    rings.answer(PHONE, ring["ring_id"], **({"opening_mode": "listen_first"} if listen else {}))
    assert ends == [] and _calls_of()[0]["state"] == "live"


def test_a_reanswer_after_a_failed_compensation_end_adopts_the_live_call(env):
    """ADD-8 A-fail: the record txn rolled back (the ring still ringing) and the
    compensation end failed, so the started call is still live. A re-answer
    before expiry adopts that call (group_calls.start is idempotent for a live
    call with request_id = ring_id) instead of starting a second one."""
    ring = _lf()
    _record_ends(env, fail=True)
    original_open = rings._open_record
    attempts = {"n": 0}

    def flaky(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("open record failed")
        return original_open(*a, **k)

    env.monkeypatch.setattr(rings, "_open_record", flaky)
    with pytest.raises(RuntimeError, match="open record failed"):
        rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    [first] = _calls_of()
    assert first["state"] == "live" and _stored_ring(ring["ring_id"])["state"] == "ringing"
    out = rings.answer(PHONE, ring["ring_id"], opening_mode="listen_first")
    assert out["call"]["call_id"] == first["call_id"] and len(_calls_of()) == 1
