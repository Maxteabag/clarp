"""Call-open claim lifecycle (P4) and bounded withdrawal authority (P3).

A listen-first initiative is claimed for spawn at most once. A generic reset,
release or recovery never makes an ambiguously dispatched claim spawnable
again: only the dispatcher's own definite pre-send failure returns it to the
queue; anything else becomes a durable explicit ``unknown`` (no replay). The
row is identified by its server-issued admission identity, never by a
client-suppliable prefix. Through the real dispatcher with a recording backend.
"""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from lib import agent_rings, agents as agents_db, db, group_calls, prompt_admissions, turn_queue
import lib.turn_dispatch as _td
from lib.turn_dispatch import TurnDispatchService


class _Backends:
    CLAUDE = "claude"

    def __init__(self):
        self.spawned, self.attempts = [], []
        self.live = False
        self.hold = None            # threading.Event: a spawn waits on it after it was received
        self.received = threading.Event()
        self.fail = None            # an exception raised after the backend received the turn

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        return 0

    def active_handles(self, backend, agent_id):
        return ["handle"] if self.live else []

    def spawn_turn(self, backend, **kwargs):
        self.attempts.append(kwargs["text"])           # the provider has it from here on
        self.received.set()
        if self.hold is not None:
            self.hold.wait(30)
        if self.fail is not None:
            raise self.fail
        self.spawned.append(kwargs)


def _service(tmp_path, backends):
    ctx = SimpleNamespace(default_session="mike", agents_path=tmp_path / "unused.json", stream=None)
    return TurnDispatchService(ctx, backend_registry=backends, home=tmp_path,
                               uuid_factory=lambda: "backend-session-1", now=lambda: 12.5,
                               retry_scheduler=lambda delay, fn: None)


@pytest.fixture
def paired_answerer():
    """Whether the answering phone is a paired device, so the answer's fence carries its auth
    generation. False everywhere except a test that parametrizes this name directly (L2 r4 R5):
    no other test gains an armed auth fence."""
    return False


@pytest.fixture
def env(tmp_path, monkeypatch, paired_answerer):
    """A listen_first ring answered, then a silence claim: the initiative is an
    admitted, queued call-open row the drain has not picked up yet."""
    _td.reset_for_tests()
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                      session="mike", backend="claude")
    agents_db.start_runtime(agent_id, "mike")
    backends = _Backends()
    for name, value in (("_kick_queue", lambda: None), ("_push", lambda ring, kind: {}),
                        ("_push_voip", lambda ring: {}), ("_schedule_expiry", lambda ring: None)):
        monkeypatch.setattr(agent_rings, name, value)
    ctx = SimpleNamespace(stream=None, tts=None)
    agent_rings.bind(ctx)
    group_calls.bind(ctx)
    monkeypatch.setattr(group_calls, "resolve_name", lambda name, participants=(): {
        "session": "mike", "persona": "Mike", "agent_id": agent_id})
    ring = agent_rings.ring(agent="mike", reason="Leak at pump 01", opening_line="Line",
                            opening_mode="listen_first")["ring"]
    if paired_answerer:
        # Before the answer: the fence captures the device's auth generation when it is answered.
        db.conn().execute("INSERT INTO paired_devices(device_id, name, token_hash, scope, created_at)"
                          " VALUES (?,?,?,?,?)", ("device_phone", "Phone", "h", "full", db.now_ms()))
    op = agent_rings.answer("device_phone", ring["ring_id"], opening_mode="listen_first")["opening"]
    out = SimpleNamespace(service=_service(tmp_path, backends), backends=backends, op=op,
                          agent_id=agent_id, ring_id=ring["ring_id"], tmp_path=tmp_path)
    out.silence = lambda: agent_rings.opening_claim("device_phone", {
        "token": op["token"], "epoch": 1, "claim": "silence", "request_id": "rq-silence",
        "capture_gen": 1, "at_ms": 1250, "watermark_ms": 1290})
    return out


def _admitted(env):
    queue_id = env.silence()["opening"]["intent"]["admission_id"]
    assert turn_queue.status(queue_id) == "queued"
    return queue_id


def _opening(env):
    return agent_rings.opening_state(env.ring_id)


def _dispatch_row(env, queue_id):
    """Exactly what queued recovery submits for the durable row."""
    row = turn_queue.get(queue_id)
    return env.service.dispatch(
        text=row["text"], requested_session=row["session"], trace_id=row["trace_id"],
        synthesize_audio=bool(row["synthesize_audio"]), forced_session=row["session"],
        client_msg_id=row["client_msg_id"], origin=row["origin"],
        sender_agent_id=row["sender_agent_id"], prompt_admission_id=row["prompt_admission_id"],
        queue_if_busy=True, skip_admission=True, durable_queue_id=queue_id)


def _held_first_dispatch(env, queue_id):
    """The first dispatcher claims the row and the provider receives the turn;
    it is then held before any start evidence (mark_started) is written."""
    env.backends.hold = threading.Event()
    worker = threading.Thread(target=lambda: _dispatch_row(env, queue_id), daemon=True)
    worker.start()
    assert env.backends.received.wait(10)
    assert turn_queue.status(queue_id) == "claimed"
    return worker


def _age_claim(queue_id, ms):
    db.conn().execute("UPDATE queued_turns SET claimed_at = claimed_at - ? WHERE queue_id = ?", (ms, queue_id))


def _sql_during(fn):
    seen = []
    con = db.conn()
    con.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        con.set_trace_callback(None)
    return seen


# ---- W2 across a process boundary ----------------------------------------------------------

def _new_process(monkeypatch):
    """What a restart leaves: no in-memory slots and no per-request locks
    (the held old worker keeps the old ones; the new process never sees them)."""
    _td.reset_for_tests()
    monkeypatch.setattr(_td, "_REQUEST_LOCKS", {})


def test_W2_a_claim_in_flight_across_a_restart_never_spawns_again(env, monkeypatch):
    """The previous process claimed, the provider received the turn, the
    process died before any start evidence: the new process must not run it
    again, whatever the claim's age."""
    queue_id = _admitted(env)
    worker = _held_first_dispatch(env, queue_id)
    _new_process(monkeypatch)                                 # the process is gone
    _age_claim(queue_id, 31_000)
    _service(env.tmp_path, env.backends).recover_queued()     # the new process recovers
    env.backends.hold.set()
    worker.join(10)
    assert len(env.backends.attempts) == 1                    # ONE external launch in total
    assert turn_queue.status(queue_id) == "unknown"
    assert _opening(env)["state"] == "unknown" and _opening(env)["intent"]["state"] == "unknown"


def test_W2_boot_resolves_a_previous_process_claim_even_when_the_clock_went_back(env):
    queue_id = _admitted(env)
    assert agent_rings.claim_initiative(queue_id) is True      # claimed by the previous process
    _age_claim(queue_id, -3_600_000)                            # its claim looks newer than now
    _td.reset_for_tests()
    _service(env.tmp_path, env.backends).recover_queued()
    assert env.backends.attempts == []
    assert turn_queue.status(queue_id) == "unknown" and turn_queue.claimed_count() == 0
    assert _opening(env)["state"] == "unknown"


# ---- in one process: age, a forward and a backward clock change -------------------------------

@pytest.mark.parametrize("change", ["age", "forward", "backward"])
def test_W2_a_live_held_first_launch_and_a_generic_reset_spawn_once(env, monkeypatch, change):
    queue_id = _admitted(env)
    worker = _held_first_dispatch(env, queue_id)
    if change == "age":
        _age_claim(queue_id, 31_000)
    else:
        now = db.now_ms()
        shift = 3_600_000 if change == "forward" else -3_600_000
        monkeypatch.setattr(db, "now_ms", lambda: now + shift)
    env.service.recover_queued()                               # the real _recover_queued_locked
    env.service.recover_queued()
    env.backends.hold.set()                                   # late evidence arrives after recovery
    worker.join(10)
    env.service.recover_queued()
    assert len(env.backends.attempts) == 1                    # one external launch, ever
    assert _opening(env)["state"] in ("initiative_started", "unknown")   # never a second opening


# ---- D2: the dispatcher's own definite pre-send failure ----------------------------------------

def _fail_open_turn_once(monkeypatch):
    """A Host-local failure after the gate, before the backend is invoked."""
    from lib import turn_lifecycle
    real = turn_lifecycle.open_turn
    calls = {"n": 0}

    def fails_once(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("host-local failure before the backend was invoked")
        return real(**kw)
    monkeypatch.setattr(turn_lifecycle, "open_turn", fails_once)
    return calls


def test_D2_a_failure_before_the_spawn_flag_runs_once_more_through_the_first_claim(env, monkeypatch):
    queue_id = _admitted(env)
    calls = _fail_open_turn_once(monkeypatch)
    env.service.recover_queued()
    assert calls["n"] == 1 and env.backends.attempts == []
    assert turn_queue.status(queue_id) == "queued"            # back in the queue, unclaimed
    assert _opening(env)["intent"]["state"] == "admitted"
    _td._RECOVERY_BACKOFF.clear(env.agent_id)
    env.service.recover_queued()
    assert len(env.backends.attempts) == 1 and turn_queue.status(queue_id) == "started"
    env.service.recover_queued()
    assert len(env.backends.attempts) == 1


def test_D2_a_stop_after_a_pre_send_failure_still_wins(env, monkeypatch):
    queue_id = _admitted(env)
    _fail_open_turn_once(monkeypatch)
    env.service.recover_queued()
    assert turn_queue.status(queue_id) == "queued"
    turn_queue.set_paused(env.agent_id, True)                  # a Stop after the failure
    _td._RECOVERY_BACKOFF.clear(env.agent_id)
    env.service.recover_queued()
    agent_rings.recover_openings()
    assert env.backends.attempts == []
    assert _opening(env)["retired"]["reason"] == "stopped_after_answer"


def test_D2_a_failure_after_the_spawn_flag_is_unknown_and_never_reruns(env):
    queue_id = _admitted(env)
    env.backends.fail = RuntimeError("the backend failed after it received the turn")
    env.service.recover_queued()
    env.backends.fail = None
    _td._RECOVERY_BACKOFF.clear(env.agent_id)
    turn_queue.reset_stale_claims(max_age_ms=0)
    env.service.recover_queued()
    env.service.recover_queued()
    assert len(env.backends.attempts) == 1
    assert _opening(env)["state"] == "unknown"


# ---- D1: the durable unknown counts as nothing and is never moved back ---------------------------

def test_D1_unknown_is_not_pending_claimed_settleable_or_success_and_never_moves_back(env):
    queue_id = _admitted(env)
    assert agent_rings.claim_initiative(queue_id) is True
    _td.reset_for_tests()
    env.service.recover_queued()                               # boot resolution
    assert turn_queue.status(queue_id) == "unknown"
    trace = db.conn().execute("SELECT trace_id FROM queued_turns WHERE queue_id = ?", (queue_id,)).fetchone()[0]
    assert turn_queue.pending(env.agent_id) == [] and turn_queue.pending_count(env.agent_id) == 0
    assert turn_queue.claimed_count() == 0 and turn_queue.state(env.agent_id)["count"] == 0
    assert not turn_queue.contains(queue_id) and not turn_queue.holds_client_message(queue_id)
    assert turn_queue.settle_receipt(env.agent_id, trace, "completed") == 0
    turn_queue.mark_started(queue_id)                          # a late start is not success
    assert turn_queue.terminalize_ended_receipts() == {}
    assert turn_queue.requeue_parked() == 0 and turn_queue.reset_stale_claims(max_age_ms=0) == 0
    turn_queue.set_paused(env.agent_id, True)
    turn_queue.set_paused(env.agent_id, False)                 # Resume
    assert turn_queue.claim(queue_id) is None and turn_queue.update_text(queue_id, "x") is False
    _td.reset_for_tests()
    env.service.recover_queued()                               # rehydrate and recovery again
    assert turn_queue.status(queue_id) == "unknown" and env.backends.attempts == []


# ---- D3 / D4: identity, not a client-suppliable prefix -------------------------------------------

def _user_send_row(env, queue_id, text, trace):
    """What /send leaves for a busy agent: the user's prompt admission and queue row."""
    admission = prompt_admissions.create(
        authenticated_at_admission=True, origin="user", sender_agent_id="", channel="chat",
        observed_at=db.now_ms(), client_admission_id=queue_id, trace_id=trace, original_text=text)
    admission_id = prompt_admissions.record(admission, agent_id=env.agent_id, session="mike")
    turn_queue.enqueue(queue_id=queue_id, agent_id=env.agent_id, session="mike", text=text,
                       trace_id=trace, client_msg_id=queue_id, synthesize_audio=True,
                       origin="user", sender_agent_id="", prompt_admission_id=admission_id)


def test_D3_a_user_row_with_the_call_open_prefix_keeps_ordinary_reset_and_runs_as_a_message(env):
    _user_send_row(env, "call-open:not-a-ring:1", "my own words", "tr-user")
    assert turn_queue.claim("call-open:not-a-ring:1") is not None
    _age_claim("call-open:not-a-ring:1", 31_000)
    env.service.recover_queued()
    assert len(env.backends.attempts) == 1 and "my own words" in env.backends.attempts[0]
    assert turn_queue.status("call-open:not-a-ring:1") == "started"


def test_D4_a_client_message_under_the_next_admission_id_never_becomes_the_opening(env):
    _user_send_row(env, f"call-open:{env.ring_id}:1", "client words", "tr-client")
    env.silence()
    env.service.recover_queued()
    assert _opening(env)["state"] == "unknown"                 # fenced: no initiative, never the client's text
    assert len(env.backends.attempts) == 1                      # the client's message, run once as itself
    assert "client words" in env.backends.attempts[0] and "Open the conversation" not in env.backends.attempts[0]
    assert db.conn().execute("SELECT count(*) FROM messages WHERE role='user' AND text = 'client words'"
                             ).fetchone()[0] == 1                # with its ordinary user row


# ---- P3 and R3: bounded facts only -----------------------------------------------------------

def test_P3_a_greeting_against_an_admitted_intent_reads_no_turns_history(env):
    queue_id = _admitted(env)
    trace = _opening(env)["intent"]["trace_id"]
    db.conn().execute("INSERT INTO turns(agent_id, source, trace_id, started_at) VALUES (?,?,?,?)",
                      (env.agent_id, "pwa", trace, db.now_ms()))   # P4-I: impossible without the gate
    sql = _sql_during(lambda: agent_rings.greeting(
        "device_phone", token=env.op["token"], epoch=1, client_msg_id="u-hallo", trace_id="tr-hallo",
        session="mike", call_id=env.op["call_id"]))
    assert not [s for s in sql if " turns" in s and "FROM turns" in s]
    assert not [s for s in sql if "state_log" in s]
    assert turn_queue.status(queue_id) == "withdrawn" and _opening(env)["intent"]["state"] == "cancelled"


def test_P3_the_row_read_is_a_primary_key_search():
    plan = db.conn().execute(
        "EXPLAIN QUERY PLAN SELECT status, claimed_at, trace_id FROM queued_turns WHERE queue_id = ?",
        ("call-open:x:1",)).fetchall()
    detail = " ".join(str(row["detail"]) for row in plan)
    assert "SEARCH queued_turns USING INDEX sqlite_autoindex_queued_turns_1 (queue_id=?)" in detail


def test_R3_no_state_log_query_runs_in_the_opening_transaction(env):
    queue_id = _admitted(env)
    assert agent_rings.claim_initiative(queue_id) is True
    turn_queue.reset_stale_claims(max_age_ms=0)
    sql = _sql_during(lambda: agent_rings.claim_initiative(queue_id))
    assert not [s for s in sql if "state_log" in s]


# ---- D1 amendment: an unknown row is never shown or acted on as queued work ------------------

@pytest.fixture(autouse=True)
def _no_real_backend(monkeypatch):
    """Nothing here may launch a real provider CLI: every route under test must
    use the recording dispatcher."""
    from lib import backends

    def refuse(*args, **kwargs):
        raise AssertionError("a real backend launch from a lifecycle test")
    monkeypatch.setattr(backends, "spawn_turn", refuse)


def _server():
    import importlib.util
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("clarp_server_lifecycle", root / "server/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _route(server, env, method, *args, body=None, query=None):
    """One real Handler route method, with the HTTP plumbing stubbed and the
    test's recording dispatcher behind it."""
    answer = {}
    server.TurnDispatchService = lambda ctx, **kw: env.service
    handler = server.Handler.__new__(server.Handler)
    handler.server = SimpleNamespace(ctx=SimpleNamespace(
        default_session="mike", stream=None, runtime_client=None, agents_path=env.tmp_path / "unused.json"))
    handler._read_json = lambda: body
    handler._query = lambda: query or {}
    handler._json_ok = lambda payload: answer.update(status=200, body=payload)
    handler._json = lambda status, payload: answer.update(status=status, body=payload)
    handler._json_error = lambda status, message: answer.update(status=status, body=message)
    getattr(server.Handler, method)(handler, *args)
    return answer


@pytest.fixture
def unknown_and_ordinary(env):
    queue_id = _admitted(env)
    assert agent_rings.claim_initiative(queue_id) is True
    _td.reset_for_tests()
    env.service.recover_queued()                               # the boot resolution: unknown
    assert turn_queue.status(queue_id) == "unknown"
    turn_queue.enqueue(queue_id="u-ordinary", agent_id=env.agent_id, session="mike", text="ordinary words",
                       trace_id="t-ordinary", client_msg_id="u-ordinary", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    turn_queue.set_paused(env.agent_id, True)                  # keep the ordinary row waiting
    return queue_id


def test_D1a_the_queue_list_and_every_count_show_only_the_ordinary_row(env, unknown_and_ordinary):
    server = _server()
    out = _route(server, env, "_handle_turn_queue", query={"session": ["mike"]})
    assert out["status"] == 200 and [i["id"] for i in out["body"]["items"]] == ["u-ordinary"]
    assert turn_queue.state(env.agent_id)["count"] == 1 and turn_queue.pending_count(env.agent_id) == 1
    assert turn_queue.states()[env.agent_id]["count"] == 1      # snapshot / stale-work counts
    assert turn_queue.pending_counts().get(env.agent_id) == 1
    assert [r["queue_id"] for r in turn_queue.pending()] == ["u-ordinary"]


@pytest.mark.parametrize("method,args,body", [
    ("_handle_turn_queue_update", None, {"text": "edited"}),
    ("_handle_turn_queue_delete", None, None),
    ("_handle_turn_queue_send", None, None),
])
def test_D1b_every_queue_mutation_refuses_an_unknown_row(env, unknown_and_ordinary, method, args, body):
    server = _server()
    queue_id = unknown_and_ordinary
    out = _route(server, env, method, queue_id, body=body)
    assert out == {"status": 409, "body": "opening launch unknown"}
    row = db.conn().execute("SELECT status, text FROM queued_turns WHERE queue_id = ?", (queue_id,)).fetchone()
    assert row is not None and row["status"] == "unknown"       # kept as the durable audit
    assert env.backends.attempts == []


def test_D1b_the_dispatcher_itself_refuses_to_send_an_unknown_row(env, unknown_and_ordinary):
    with pytest.raises(_td.DispatchError) as err:
        env.service.dispatch_queued(unknown_and_ordinary)
    assert (err.value.status, str(err.value)) == (409, "opening launch unknown")


def test_D1b_resume_leaves_the_unknown_row_and_runs_the_ordinary_one(env, unknown_and_ordinary):
    server = _server()
    out = _route(server, env, "_handle_turn_queue_resume", body={"session": "mike"})
    assert out["status"] == 200
    assert turn_queue.status(unknown_and_ordinary) == "unknown"
    assert len(env.backends.attempts) == 1 and "ordinary words" in env.backends.attempts[0]


def test_D1b_ordinary_rows_keep_every_mutation(env, unknown_and_ordinary):
    server = _server()
    assert _route(server, env, "_handle_turn_queue_update", "u-ordinary", body={"text": "edited"})["status"] == 200
    assert turn_queue.get("u-ordinary")["text"] == "edited"
    assert _route(server, env, "_handle_turn_queue_delete", "u-ordinary")["status"] == 200
    assert turn_queue.status("u-ordinary") == ""


# ---- F1: no in-dispatch redelivery of a launched opening -----------------------------------

OPENING_SUMMARY = "The call opening could not be confirmed and will not be sent again"


def _failure_rows(monkeypatch):
    rows = []
    monkeypatch.setattr(_td.TurnDispatchService, "_record_failure_row",
                        lambda self, spec, human, provider: rows.append(spec.queue_id))
    return rows


class _CallbackBackends(_Backends):
    """Keeps every launch's callbacks so a test can fail it after the flag."""

    def __init__(self):
        super().__init__()
        self.launches = []

    def spawn_turn(self, backend, **kwargs):
        self.attempts.append(kwargs["text"])
        self.launches.append(kwargs)


ERRORS = {
    "connection": "Error: connection reset by peer",
    "timeout": "turn timed out: no output for 600s",
    "account_plan": "your plan does not include access to this model",
    "usage_limit": "You hit your usage limit, which resets at 5pm",
}


def _immediate_service(env, backends):
    ctx = SimpleNamespace(default_session="mike", agents_path=env.tmp_path / "unused.json", stream=None)
    return TurnDispatchService(ctx, backend_registry=backends, home=env.tmp_path,
                               uuid_factory=lambda: "backend-session-1", now=lambda: 12.5,
                               retry_scheduler=lambda delay, fn: fn())


@pytest.mark.parametrize("failure", ["connection", "timeout", "account_plan", "usage_limit"])
def test_F1_a_launched_opening_that_fails_is_never_relaunched(env, failure):
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    queue_id = _admitted(env)
    service.recover_queued()
    assert len(backends.launches) == 1
    backends.launches[0]["on_error"](ERRORS[failure])          # after spawn_started
    for _ in range(3):
        if len(backends.launches) > 1:
            backends.launches[-1]["on_error"](ERRORS[failure])
    assert len(backends.attempts) == 1                          # one external launch, ever
    assert _opening(env)["state"] == "unknown" and _opening(env)["intent"]["state"] == "unknown"
    assert turn_queue.status(queue_id) == "unknown"


@pytest.mark.parametrize("failure", ["connection", "timeout"])
def test_F1_an_ordinary_turn_still_retries_as_before(env, failure):
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    turn_queue.enqueue(queue_id="u-plain", agent_id=env.agent_id, session="mike", text="plain words",
                       trace_id="t-plain", client_msg_id="u-plain", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    service.recover_queued()
    assert len(backends.launches) == 1
    backends.launches[0]["on_error"](ERRORS[failure])
    assert len(backends.launches) == 2                          # redelivered, as today
    assert "plain words" in backends.attempts[1]


def _account_recovery_takes_over(monkeypatch):
    """An account pool whose coordinator accepts the failure: the slot stays
    owned and the coordinator later resumes the turn (_resume_after_account_switch)."""
    monkeypatch.setattr(_td.account_failover("claude"), "request", lambda *a, **k: True)


def _captured_failure(monkeypatch):
    seen = {}
    real = _td.TurnDispatchService._handle_failure

    def capture(self, spec, attempt, state, category, message):
        seen.update(spec=spec, attempt=attempt, state=state)
        return real(self, spec, attempt, state, category, message)
    monkeypatch.setattr(_td.TurnDispatchService, "_handle_failure", capture)
    return seen


def test_F1_an_account_switch_resume_never_relaunches_a_launched_opening(env, monkeypatch):
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    seen = _captured_failure(monkeypatch)
    _account_recovery_takes_over(monkeypatch)
    _admitted(env)
    service.recover_queued()
    backends.launches[0]["on_error"](ERRORS["account_plan"])
    state = seen["state"]
    service._resume_after_account_switch(seen["spec"], seen["attempt"], state)   # what the coordinator does
    assert len(backends.attempts) == 1
    assert _opening(env)["state"] == "unknown"


def test_F1_an_account_switch_resume_of_an_ordinary_turn_is_unchanged(env, monkeypatch):
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    seen = _captured_failure(monkeypatch)
    _account_recovery_takes_over(monkeypatch)
    turn_queue.enqueue(queue_id="u-plain", agent_id=env.agent_id, session="mike", text="plain words",
                       trace_id="t-plain", client_msg_id="u-plain", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    service.recover_queued()
    backends.launches[0]["on_error"](ERRORS["account_plan"])
    state = seen["state"]
    service._resume_after_account_switch(seen["spec"], seen["attempt"], state)
    assert len(backends.attempts) == 2                          # resumed, as today


@pytest.mark.parametrize("failure", ["connection", "timeout", "account_plan", "usage_limit"])
def test_F1_a_failed_launched_opening_ends_visibly_interrupted_never_done(env, failure, monkeypatch):
    from lib.protocol import AgentState
    failure_rows = _failure_rows(monkeypatch)
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    queue_id = _admitted(env)
    service.recover_queued()
    backends.launches[0]["on_error"](ERRORS[failure])
    assert len(backends.attempts) == 1                          # no retry, for every category
    assert turn_queue.status(queue_id) == "unknown"
    assert _opening(env)["state"] == "unknown"                  # not started, not done
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["summary"] == OPENING_SUMMARY        # no "your message is saved"
    assert failure_rows == []                                   # nothing under the Host's initiative


def test_F1_a_retired_opening_stays_retired_when_its_launched_turn_fails(env):
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    _admitted(env)
    service.recover_queued()
    agent_rings.retire_for_call(_opening(env)["call_id"], "call_ended")    # the call ended first
    backends.launches[0]["on_error"](ERRORS["timeout"])
    assert len(backends.attempts) == 1
    assert _opening(env)["retired"]["reason"] == "call_ended"
    assert _opening(env)["state"] != "unknown"                  # the retire is not overwritten


def test_F1_after_an_unknown_opening_the_users_own_message_still_runs(env):
    """No SECOND AUTOMATIC opening, but a fresh legitimate user turn is never suppressed."""
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    _admitted(env)
    service.recover_queued()
    backends.launches[0]["on_error"](ERRORS["connection"])
    assert _opening(env)["state"] == "unknown"
    turn_queue.enqueue(queue_id="u-hello", agent_id=env.agent_id, session="mike", text="hei der",
                       trace_id="t-hello", client_msg_id="u-hello", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    _td._RECOVERY_BACKOFF.clear(env.agent_id)
    service.recover_queued()
    assert len(backends.attempts) == 2 and "hei der" in backends.attempts[1]


def test_F1_only_the_launched_call_open_row_becomes_unknown(env):
    """The call-open-specific row transition: an ordinary started row is never touched."""
    turn_queue.enqueue(queue_id="u-started", agent_id=env.agent_id, session="mike", text="x",
                       trace_id="t-started", client_msg_id="u-started", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    turn_queue.mark_started("u-started")
    assert turn_queue.mark_launched_call_open_unknown("u-started") is False
    assert turn_queue.status("u-started") == "started"


def test_F1_an_ordinary_turn_failure_keeps_its_summary_and_failure_row(env, monkeypatch):
    from lib.protocol import AgentState
    failure_rows = _failure_rows(monkeypatch)
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    turn_queue.enqueue(queue_id="u-plain", agent_id=env.agent_id, session="mike", text="plain words",
                       trace_id="t-plain", client_msg_id="u-plain", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    service.recover_queued()
    backends.launches[0]["on_error"](ERRORS["account_plan"])
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["summary"].endswith("Your message is saved.")
    assert failure_rows == ["u-plain"]


def test_F1_a_first_launch_that_raises_after_the_spawn_flag_ends_unknown_not_unlaunched(env, monkeypatch):
    """L1 (i): the backend had the turn, then the spawn raised: a durable unknown and a visible
    interrupted end, never recorded as an unlaunched turn, and never launched again."""
    from lib.protocol import AgentState
    unlaunched = []
    monkeypatch.setattr(_td.turn_lifecycle, "record_unlaunched",
                        lambda agent_id, trace_id: unlaunched.append(trace_id))
    queue_id = _admitted(env)
    env.backends.fail = RuntimeError("the backend failed after it received the turn")
    env.service.recover_queued()
    env.backends.fail = None
    _td._RECOVERY_BACKOFF.clear(env.agent_id)
    env.service.recover_queued()
    assert len(env.backends.attempts) == 1
    assert turn_queue.status(queue_id) == "unknown" and _opening(env)["state"] == "unknown"
    assert agents_db.latest_state(env.agent_id)["kind"] == AgentState.INTERRUPTED
    assert unlaunched == []


def test_F1_a_parked_opening_whose_resumed_launch_raises_after_the_flag_ends_unknown(env, monkeypatch):
    """L1 (ii): parked by the account coordinator before its spawn flag (a faithful register park,
    see _park_first_launch), then resumed: its one launch raises after the flag. It ends unknown
    and visibly interrupted, once."""
    from lib.protocol import AgentState
    parked = _park_first_launch(monkeypatch)
    queue_id = _admitted(env)
    env.service.recover_queued()
    assert env.backends.attempts == [] and len(parked) == 1     # parked before the flag
    _parked_unmarked(env, queue_id)
    env.backends.fail = RuntimeError("the backend failed after it received the turn")
    _resume_parked(env.service, parked)
    assert len(env.backends.attempts) == 1
    assert turn_queue.status(queue_id) == "unknown" and _opening(env)["state"] == "unknown"
    assert agents_db.latest_state(env.agent_id)["kind"] == AgentState.INTERRUPTED


class _OpeningLaunchRaises(_CallbackBackends):
    """The first launch (an ordinary turn) stays running; the second (the opening, drained
    behind it) reaches the backend and then raises."""

    def spawn_turn(self, backend, **kwargs):
        super().spawn_turn(backend, **kwargs)
        if len(self.attempts) == 2:
            raise RuntimeError("the backend failed after it received the opening")


def _opening_queued_behind_a_live_busy_turn(env, service, backends):
    """The supported route to the queue drain. An ordinary turn is positively live: its slot is
    owned, its backend reports an active handle, and its launch is recorded and unfinished. The
    opening's durable row is then submitted with exactly the DispatchCommand queued recovery builds
    for it (queue_if_busy, the durable queue id). The dispatcher's claim-or-queue decision holds it
    IN MEMORY behind the live turn; nothing is claimed or spawned for it yet.

    Queued recovery itself defers a row while the agent's handles are live, and submits this same
    command once they are gone; that later submit is a FIRST dispatch, not the drain. The drain
    (_resume_and_spawn_claimed) only ever consumes this memory queue."""
    turn_queue.enqueue(queue_id="u-busy", agent_id=env.agent_id, session="mike", text="busy words",
                       trace_id="t-busy", client_msg_id="u-busy", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    service.recover_queued()                                    # the busy turn launches
    assert len(backends.attempts) == 1 and "busy words" in backends.attempts[0]
    backends.live = True                                        # its process is up
    queue_id = _admitted(env)
    row = turn_queue.get(queue_id)
    result = service.submit(_td.DispatchCommand(
        text=row["text"], requested_session=row["session"], trace_id=row["trace_id"],
        synthesize_audio=bool(row["synthesize_audio"]), forced_session=row["session"],
        client_msg_id=row["client_msg_id"], origin=row["origin"],
        sender_agent_id=row["sender_agent_id"], prompt_admission_id=row["prompt_admission_id"],
        queue_if_busy=True, skip_admission=True, durable_queue_id=queue_id,
        allow_paused_queue=bool(row.get("allow_paused"))))
    # Before the busy turn ends: queued in memory behind it, and nothing premature.
    assert result.queued is True
    assert _td._INFLIGHT.get(env.agent_id) == "t-busy"           # the busy slot is owned
    assert backends.active_handles("claude", env.agent_id)       # and its process is live
    assert _td._SLOTS.has_queued(env.agent_id, lambda spec: (
        spec.queue_id == queue_id and spec.trace_id == row["trace_id"]))
    assert turn_queue.status(queue_id) == "queued"                # not claimed
    assert _opening(env)["intent"]["state"] == "admitted"         # the gate has not run
    assert len(backends.attempts) == 1                            # nothing spawned for it
    return queue_id


def _record_drain_entries(monkeypatch):
    """Observes (never replaces) the queue drain's entry: the real method still runs."""
    seen = []
    real = _td.TurnDispatchService._resume_and_spawn_claimed

    def observed(self, agent_id, next_spec, phase):
        seen.append(next_spec.queue_id)
        return real(self, agent_id, next_spec, phase)
    monkeypatch.setattr(_td.TurnDispatchService, "_resume_and_spawn_claimed", observed)
    return seen


def _the_live_busy_turn_ends(backends):
    """Its process exits (no live handle any more), then its own error callback fires: the real
    completion path, which finishes the busy turn and drains the memory queue behind it."""
    backends.live = False
    backends.launches[0]["on_error"]("turn_aborted: the user interrupted the previous turn")


def test_F1_an_opening_drained_behind_a_busy_turn_that_raises_after_the_flag_ends_unknown(
        env, monkeypatch):
    """The queue-drain first launch (_resume_and_spawn): the opening's one launch reached the
    backend and raised. The same fenced end as every post-launch failure: unknown, visibly
    interrupted with the opening's summary, never 'unlaunched', never launched again."""
    from lib.protocol import AgentState
    unlaunched = []
    monkeypatch.setattr(_td.turn_lifecycle, "record_unlaunched",
                        lambda agent_id, trace_id: unlaunched.append(trace_id))
    backends = _OpeningLaunchRaises()
    service = _immediate_service(env, backends)
    drained = _record_drain_entries(monkeypatch)
    queue_id = _opening_queued_behind_a_live_busy_turn(env, service, backends)
    _the_live_busy_turn_ends(backends)
    assert drained == [queue_id]                                # the real queue drain ran it
    assert len(backends.attempts) == 2                          # the drain launched the opening once
    assert turn_queue.status(queue_id) == "unknown" and _opening(env)["state"] == "unknown"
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["summary"] == OPENING_SUMMARY
    assert unlaunched == []
    _td._RECOVERY_BACKOFF.clear(env.agent_id)
    service.recover_queued()
    assert len(backends.attempts) == 2                          # never again


# ---- L2: a parked first launch is admitted again before it runs ----------------------------

def _park_first_launch(monkeypatch):
    """The account coordinator parks the opening's first launch before its spawn flag, as a real
    register park does:
    - the claimed spawn returns early;
    - the attempt stays registered with the coordinator, marked account_recovery, so the
      caller's _mark_spawned leaves it alone (the row stays claimed, no "started" broadcast,
      no THINKING);
    - the coordinator's pause is recorded.
    Resume only through _resume_parked, which clears the coordinator's attempts first, as the
    real Resume decision does."""
    from lib.account_failover import Attempt
    parked = []
    real = _td.TurnDispatchService._spawn_attempt_claimed
    monkeypatch.setattr(_td.account_failover("claude"), "attempts", {})     # restored after the test

    def park_first(self, spec, *, attempt):
        if not parked:
            parked.append((spec, attempt))
            _td.account_failover(spec.backend).attempts[spec.agent_id] = Attempt(
                agent_id=spec.agent_id, trace_id=spec.trace_id, model=spec.model,
                state={"account_recovery": True}, owned=lambda: True, pause=lambda: None,
                resume=lambda: None, work=(spec, attempt))
            self._pause_for_account(spec)           # what the coordinator's pause records
            return None
        return real(self, spec, attempt=attempt)
    monkeypatch.setattr(_td.TurnDispatchService, "_spawn_attempt_claimed", park_first)
    return parked


def _resume_parked(service, parked):
    """The coordinator's Resume: it clears its attempts, then resumes the parked work."""
    spec, attempt = parked[0]
    _td.account_failover(spec.backend).attempts.clear()
    service._resume_after_account_switch(spec, attempt, {})


def _parked_unmarked(env, queue_id):
    """After a faithful park: the row is still claimed and the agent shows the park's wait.
    It is not marked spawned. ACCOUNT_RECOVERY_WAIT is itself a THINKING kind, so the wait
    detail is what proves no SPAWN_STARTED replaced it."""
    assert turn_queue.status(queue_id) == "claimed"
    assert agents_db.latest_state(env.agent_id)["detail"].get("account_recovery") == "waiting"


OPENING_WITHDRAWN = "The call opening was withdrawn before it was sent"


def _withdrawn_receipt(env):
    """The truthful terminal state of a withdrawn, never-sent opening: the UNLAUNCHED event
    with the opening's own words (not "Message saved … Retry is safe"), no phantom wait."""
    from lib.protocol import AgentState
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.INTERRUPTED              # what UNLAUNCHED records
    assert state["detail"]["message"] == OPENING_WITHDRAWN
    assert state["detail"]["dispatch_not_started"] is True
    assert state["detail"].get("account_recovery") is None      # not the park's wait


def test_L2_a_retire_during_the_park_cancels_the_unsent_opening_instead_of_launching_it(env, monkeypatch):
    parked = _park_first_launch(monkeypatch)
    queue_id = _admitted(env)
    env.service.recover_queued()
    assert env.backends.attempts == [] and len(parked) == 1
    _parked_unmarked(env, queue_id)
    agent_rings.retire_for_call(_opening(env)["call_id"], "call_ended")     # the call ended meanwhile
    _resume_parked(env.service, parked)
    assert env.backends.attempts == []                          # the retire wins: never launched
    assert _opening(env)["intent"]["state"] == "cancelled"      # proven unsent, not unknown
    assert turn_queue.status(queue_id) == "withdrawn"
    assert _opening(env)["retired"]["reason"] == "call_ended"
    _withdrawn_receipt(env)
    _td._RECOVERY_BACKOFF.clear(env.agent_id)
    env.service.recover_queued()
    _resume_parked(env.service, parked)                         # a late duplicate resume
    assert env.backends.attempts == []                          # no automatic retry


def test_L2_a_parked_opening_with_its_fence_intact_launches_once_after_the_switch(env, monkeypatch):
    parked = _park_first_launch(monkeypatch)
    queue_id = _admitted(env)
    env.service.recover_queued()
    _parked_unmarked(env, queue_id)
    _resume_parked(env.service, parked)
    assert len(env.backends.attempts) == 1
    assert turn_queue.status(queue_id) == "started"             # the resumed launch is marked, once


def test_L2_a_restored_hand_over_resumes_through_the_same_admission(env, monkeypatch):
    """restore() (a failed hand-over) resumes the attempt through its resume callable, which
    turn_dispatch builds only as _resume_after_account_switch: the retire wins there too."""
    import threading as _threading
    from lib.account_failover import AccountFailover, Attempt
    parked = _park_first_launch(monkeypatch)
    queue_id = _admitted(env)
    env.service.recover_queued()
    _parked_unmarked(env, queue_id)
    spec, attempt = parked[0]
    agent_rings.retire_for_call(_opening(env)["call_id"], "call_ended")
    coordinator = AccountFailover(_threading.RLock())
    work = Attempt(agent_id=spec.agent_id, trace_id=spec.trace_id, model=spec.model, state={},
                   owned=lambda: True, pause=lambda: None,
                   resume=lambda: _resume_parked(env.service, parked),
                   work=(spec, attempt))
    assert coordinator.restore([work]) == []                   # not recovering: resumed now
    assert env.backends.attempts == []
    assert turn_queue.status(queue_id) == "withdrawn"
    _withdrawn_receipt(env)


def test_L2_a_parked_opening_whose_identity_cannot_be_proven_ends_unknown_never_launched(env, monkeypatch):
    from lib.protocol import AgentState
    parked = _park_first_launch(monkeypatch)
    queue_id = _admitted(env)
    env.service.recover_queued()
    _parked_unmarked(env, queue_id)
    db.conn().execute("DELETE FROM queued_turns WHERE queue_id = ?", (queue_id,))   # its row is gone
    _resume_parked(env.service, parked)
    assert env.backends.attempts == []                          # never launch on a False
    assert agents_db.latest_state(env.agent_id)["kind"] == AgentState.INTERRUPTED


# ---- L2 r3: the final fence re-check immediately before the spawn flag ---------------------
#
# Every launch path (dispatch, the queue drain, the account-switch resume, restore()) reaches
# _spawn_attempt_claimed, where _validate_janitor_target runs just before the call-open re-check
# and the spawn flag. The hook below commits a source write or retire at exactly that point:
# after every earlier admission, before the flag. The re-check must then refuse the launch:
# proven unsent, so withdrawn and cancelled.
#
# Documented limitation (R2), NOT tested as acceptance: a write committed AFTER the re-check and
# before the provider handoff (claude's stdin write, the argv backends' Popen) still launches the
# one opening. The spawn flag is possibly-sent bookkeeping, not provider delivery. Closing that
# needs a launch-commit mechanism that is not implemented (see the design request).

def _write_just_before_the_flag(env, monkeypatch, write):
    """Commit ``write`` at the exact boundary before the final call-open check: the SECOND
    _validate_janitor_target call made from _spawn_attempt_claimed for the opening's trace. The
    first is that function's entry check; the second immediately precedes the re-check and the
    spawn flag. Both exist at 1dd9e30a as well, where nothing re-checks after the second.

    Counting per trace from that one frame aims every launch path at the same point. The drain's
    own earlier call (_resume_and_spawn_claimed, BEFORE the gate) and agy's run_if_owned call
    (after the flag) come from other frames and are not counted. What the gate had admitted at
    that moment is recorded, so a test can show the write landed after admission."""
    real = _td._validate_janitor_target
    calls, done = {}, []

    def hook(agent, janitor_run_id, trace_id):
        if (str(trace_id).startswith("callopen-")
                and sys._getframe(1).f_code.co_name == "_spawn_attempt_claimed"):
            calls[trace_id] = calls.get(trace_id, 0) + 1
            if calls[trace_id] == 2 and not done:
                intent = _opening(env)["intent"]
                done.append({"intent": intent["state"],
                             "row": turn_queue.status(intent["admission_id"])})
                write()
        return real(agent, janitor_run_id, trace_id)
    monkeypatch.setattr(_td, "_validate_janitor_target", hook)
    return done


def _after_admission(done):
    """The write landed once, after the gate had claimed the launch (intent started, row
    claimed): only the final check can refuse it, and without that check it would launch."""
    assert done == [{"intent": "started", "row": "claimed"}]


def _refused_unsent(env, queue_id):
    assert env.backends.attempts == []                          # the provider never had it
    assert _opening(env)["intent"]["state"] == "cancelled"
    assert turn_queue.status(queue_id) == "withdrawn"
    _withdrawn_receipt(env)


def test_L2r3_R1_a_retire_after_resume_admission_is_refused_at_the_flag(env, monkeypatch):
    parked = _park_first_launch(monkeypatch)
    queue_id = _admitted(env)
    env.service.recover_queued()
    _parked_unmarked(env, queue_id)
    done = _write_just_before_the_flag(
        env, monkeypatch, lambda: agent_rings.retire_for_call(_opening(env)["call_id"], "call_ended"))
    _resume_parked(env.service, parked)                         # resume admission passes first
    _after_admission(done)
    _refused_unsent(env, queue_id)


def test_L2r3_R3_a_restored_park_is_refused_at_the_flag_too(env, monkeypatch):
    import threading as _threading
    from lib.account_failover import AccountFailover, Attempt
    parked = _park_first_launch(monkeypatch)
    queue_id = _admitted(env)
    env.service.recover_queued()
    _parked_unmarked(env, queue_id)
    spec, attempt = parked[0]
    done = _write_just_before_the_flag(
        env, monkeypatch, lambda: agent_rings.retire_for_call(_opening(env)["call_id"], "call_ended"))
    work = Attempt(agent_id=spec.agent_id, trace_id=spec.trace_id, model=spec.model, state={},
                   owned=lambda: True, pause=lambda: None,
                   resume=lambda: _resume_parked(env.service, parked),
                   work=(spec, attempt))
    assert AccountFailover(_threading.RLock()).restore([work]) == []
    _after_admission(done)
    _refused_unsent(env, queue_id)


def test_L2r3_R4_a_retire_after_the_gate_on_the_first_dispatch_is_refused_at_the_flag(env, monkeypatch):
    queue_id = _admitted(env)
    done = _write_just_before_the_flag(
        env, monkeypatch, lambda: agent_rings.retire_for_call(_opening(env)["call_id"], "call_ended"))
    env.service.recover_queued()                                # gate claimed it, then the write
    _after_admission(done)
    _refused_unsent(env, queue_id)


def test_L2r3_R4_a_retire_after_the_gate_on_the_queue_drain_is_refused_at_the_flag(env, monkeypatch):
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    drained = _record_drain_entries(monkeypatch)
    queue_id = _opening_queued_behind_a_live_busy_turn(env, service, backends)
    done = _write_just_before_the_flag(
        env, monkeypatch, lambda: agent_rings.retire_for_call(_opening(env)["call_id"], "call_ended"))
    _the_live_busy_turn_ends(backends)
    assert drained == [queue_id]                                # the real queue drain reached it
    _after_admission(done)                                      # after the drain's gate, not before
    assert len(backends.attempts) == 1                          # only the busy turn ever launched
    assert _opening(env)["intent"]["state"] == "cancelled"
    assert turn_queue.status(queue_id) == "withdrawn"


_FENCE_SOURCES = {
    "stop": lambda env, call_id: turn_queue.set_paused(env.agent_id, True),
    "call_end": lambda env, call_id: group_calls.end("device_phone", call_id=call_id, by="device_phone"),
    # The device's revoke, committed BEFORE any retire_for_principal: device_pairing.revoke runs
    # this UPDATE and only then retires, so only the UPDATE is committed here.
    "revoke": lambda env, call_id: db.conn().execute(
        "UPDATE paired_devices SET revoked_at = ? WHERE device_id = 'device_phone' AND revoked_at IS NULL",
        (db.now_ms(),)),
    # A token re-issue: a fence-only source that no writer ever retires on its own.
    "reauth": lambda env, call_id: db.conn().execute(
        "UPDATE paired_devices SET token_hash = 'h2' WHERE device_id = 'device_phone'"),
}


@pytest.mark.parametrize("source, paired_answerer", [
    ("stop", False), ("call_end", False), ("revoke", True), ("reauth", True),
], ids=["stop", "call_end", "revoke", "reauth"])
def test_L2r3_R5_a_source_write_after_the_gate_is_refused_at_the_flag(env, monkeypatch, source):
    """The fence reads its sources live: a source written after the gate and before the flag
    refuses the launch, with no retire committed beforehand. That covers a Stop
    (stop_generation), the device's revoke (revoked_at) and its token re-issue (token_hash).
    call_end is the retire-first case (group_calls.end retires before it saves)."""
    queue_id = _admitted(env)
    call_id = _opening(env)["call_id"]
    assert _opening(env).get("retired") is None
    done = _write_just_before_the_flag(env, monkeypatch, lambda: _FENCE_SOURCES[source](env, call_id))
    env.service.recover_queued()
    _after_admission(done)
    assert env.backends.attempts == []
    assert _opening(env)["intent"]["state"] == "cancelled"
    assert turn_queue.status(queue_id) == "withdrawn"
    if source in ("revoke", "reauth"):
        # Retired by the final check itself (_enforce_fence), not by a revoke writer.
        assert _opening(env)["retired"]["reason"] == {"revoke": "auth_revoked",
                                                      "reauth": "auth_changed"}[source]


# ---- L2 r4: a refusal at the flag is the turn's end, never a launch ------------------------
#
# The final check settles the turn itself: "cancelled" writes the withdrawn receipt and finishes
# the turn; "unknown" ends it visibly interrupted. Each caller (the first dispatch, the queue
# drain, the account-switch resume) must then treat it as handled. That means not marking it
# spawned (no "started" queue broadcast, no row flipped to started, no THINKING with no
# process), and not reading it as superseded (dispatch's 409, D2 release or unlaunched receipt,
# which would overwrite the truthful one).

def _unprovable(env):
    """The opening no longer names this launch's trace: its identity cannot be proven, so the
    final check answers unknown. The row stays claimed, unresolved by the refusal itself."""
    with agent_rings._opening_txn(env.ring_id) as box:
        box["op"]["intent"]["trace_id"] = "tr-not-this-launch"
        box["dirty"] = True


_REFUSALS = {
    "cancelled": lambda env: agent_rings.retire_for_call(_opening(env)["call_id"], "call_ended"),
    "unknown": _unprovable,
}


def _refused_at_the_flag(env, monkeypatch, path, verdict):
    """Run the opening's first launch on ``path`` with the final check refusing it. Returns the
    service, its backends, the queue id, the opening's trace and the queue broadcasts made."""
    if path == "resume":
        parked = _park_first_launch(monkeypatch)
    if path == "drain":
        backends = _CallbackBackends()
        service = _immediate_service(env, backends)
        drained = _record_drain_entries(monkeypatch)
        queue_id = _opening_queued_behind_a_live_busy_turn(env, service, backends)
    else:
        backends, service = env.backends, env.service
        queue_id = _admitted(env)
    trace_id = turn_queue.get(queue_id)["trace_id"]
    if path == "resume":
        service.recover_queued()                    # parked by the account coordinator
    if path == "resume":
        _parked_unmarked(env, queue_id)
    # Recorded from here on only: the refusal's own phase, not the setup's turns.
    broadcasts = []
    monkeypatch.setattr(_td.TurnDispatchService, "_broadcast_queue_state",
                        lambda self, spec, *, started: broadcasts.append((spec.queue_id, started)))
    done = _write_just_before_the_flag(env, monkeypatch, lambda: _REFUSALS[verdict](env))
    if path == "dispatch":
        service.recover_queued()
    elif path == "drain":
        _the_live_busy_turn_ends(backends)
        assert drained == [queue_id]                # the real queue drain reached it
    else:
        _resume_parked(service, parked)
    _after_admission(done)
    return service, backends, queue_id, trace_id, broadcasts


@pytest.mark.parametrize("verdict", ["cancelled", "unknown"])
@pytest.mark.parametrize("path", ["dispatch", "drain", "resume"])
def test_L2r4_a_refusal_at_the_flag_is_never_marked_spawned(env, monkeypatch, path, verdict):
    from lib.protocol import AgentState
    service, backends, queue_id, trace_id, broadcasts = _refused_at_the_flag(
        env, monkeypatch, path, verdict)
    assert not any("callopen-" in text or "Leak at pump 01" in text
                   for text in backends.attempts)             # no process for the opening
    assert (queue_id, True) not in broadcasts                   # no "started" queue SSE
    assert turn_queue.status(queue_id) != "started"
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] != AgentState.THINKING                 # no phantom thinking agent
    assert state["detail"].get("account_recovery") is None      # no park wait left behind
    if verdict == "cancelled":
        assert turn_queue.status(queue_id) == "withdrawn"
        assert _opening(env)["intent"]["state"] == "cancelled"
        _withdrawn_receipt(env)                                 # the truthful receipt, kept
    else:
        assert turn_queue.status(queue_id) == "claimed"         # unresolved; the stale pass settles it
        assert state["kind"] == AgentState.INTERRUPTED
        assert state["detail"]["summary"] == OPENING_SUMMARY    # never "unlaunched"/"retry is safe"
    assert _td._INFLIGHT.get(env.agent_id) != trace_id          # no phantom slot


@pytest.mark.parametrize("verdict", ["cancelled", "unknown"])
@pytest.mark.parametrize("path", ["dispatch", "drain", "resume"])
def test_L2r4_after_a_refusal_at_the_flag_the_users_next_turn_runs(env, monkeypatch, path, verdict):
    from lib.protocol import AgentState
    service, backends, _queue_id, _trace_id, _broadcasts = _refused_at_the_flag(
        env, monkeypatch, path, verdict)
    before = len(backends.attempts)
    turn_queue.enqueue(queue_id="u-later", agent_id=env.agent_id, session="mike", text="hei igjen",
                       trace_id="t-later", client_msg_id="u-later", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    _td._RECOVERY_BACKOFF.clear(env.agent_id)
    service.recover_queued()
    assert len(backends.attempts) == before + 1 and "hei igjen" in backends.attempts[-1]
    assert turn_queue.status("u-later") == "started"
    assert _td._INFLIGHT.get(env.agent_id) == "t-later"
    assert agents_db.latest_state(env.agent_id)["kind"] == AgentState.THINKING


# ---- an unknown call-open end shows interrupted, but only for the opening that still owns it ---

def _unknown_at_the_flag_after(env, monkeypatch, takeover):
    """The first dispatch of the opening on a never-run agent (state SPAWNED); at the final check
    its identity is unprovable (unknown), after `takeover` ran."""
    queue_id = _admitted(env)
    trace = turn_queue.get(queue_id)["trace_id"]
    done = _write_just_before_the_flag(env, monkeypatch, lambda: (takeover(trace), _unprovable(env)))
    env.service.recover_queued()
    _after_admission(done)
    assert env.backends.attempts == []
    return trace


def test_an_unknown_opening_end_never_overwrites_a_successor_turn(env, monkeypatch):
    """A newer turn took the agent's slot and live trace before the opening's end landed: the
    opening's interrupted end must not overwrite the successor's state."""
    from lib.protocol import AgentState

    def successor(_trace):
        with _td._TURN_LOCK:
            _td._SLOTS.claim(env.agent_id, "t-successor")
        agents_db.set_trace_for_session("mike", "t-successor")
        _td.turn_lifecycle.transition(env.agent_id, _td.TurnEvent.SPAWN_STARTED, {"trace_id": "t-successor"})
    _unknown_at_the_flag_after(env, monkeypatch, successor)
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.THINKING
    assert state["detail"]["trace_id"] == "t-successor"


def test_an_unknown_opening_end_never_overwrites_a_stop(env, monkeypatch):
    """The user stopped the agent (its slot cleared, the Stop recorded) before the opening's end
    landed: the Stop's interrupted state stands, not the opening's summary."""
    from lib.protocol import AgentState

    def stop(trace):
        _td.clear_for_agent(env.agent_id, preserve_queue=True)
        _td.turn_lifecycle.transition(env.agent_id, _td.TurnEvent.STOP_REQUESTED,
                                      {"trace_id": trace, "reason": "stopped by the user"})
    _unknown_at_the_flag_after(env, monkeypatch, stop)
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"].get("reason") == "stopped by the user"
    assert state["detail"].get("summary") != OPENING_SUMMARY


# ---- L2 r6: the opening's end is written only while nothing newer touched the agent -----------
# Write-time conditions (turn_lifecycle's conditional transition): the agent's newest state row is
# still the one read before the ownership check (A0), its live trace is the opening's, its turn is
# unsettled, and no hook prompt admission landed after the opening's admission (S_open). The real
# Claude Code and agy hooks run at their own boundary (stdin JSON, or the agy hook's process).

import dataclasses as _dataclasses
import io as _io
import json as _json
import pathlib as _pathlib
import subprocess as _subprocess
import time as _time

_HOOKS_DIR = _pathlib.Path(__file__).resolve().parents[2] / "plugin" / "hooks"


def _opening_summary_rows(env):
    rows = db.conn().execute("SELECT detail FROM state_log WHERE agent_id = ?",
                             (env.agent_id,)).fetchall()
    return [r for r in rows if r["detail"] and OPENING_SUMMARY in r["detail"]]


def _latest_row(agent_id):
    return db.conn().execute("SELECT state_id, kind, detail FROM state_log WHERE agent_id = ?"
                             " ORDER BY state_id DESC LIMIT 1", (agent_id,)).fetchone()


def _turn_outcome(env, trace):
    row = db.conn().execute("SELECT outcome FROM turns WHERE agent_id = ? AND trace_id = ?"
                            " ORDER BY turn_id LIMIT 1", (env.agent_id, trace)).fetchone()
    return row["outcome"] if row else None


def _at_the_end_after_ownership(monkeypatch, act):
    """Runs ``act`` once inside the opening's end, after its ownership check and before its
    write: at the current_kind read that picks the end's event."""
    real, fired = _td.turn_lifecycle.current_kind, []

    def hooked(agent_id):
        if not fired and sys._getframe(1).f_code.co_name == "_end_opening_state":
            fired.append(agent_id)
            act()
        return real(agent_id)
    monkeypatch.setattr(_td.turn_lifecycle, "current_kind", hooked)
    return fired


def _unknown_end_with(env, monkeypatch, act):
    """The opening's first dispatch ends unknown at the final check; ``act(trace)`` runs after
    the end's ownership check and before its write."""
    queue_id = _admitted(env)
    trace = turn_queue.get(queue_id)["trace_id"]
    done = _write_just_before_the_flag(env, monkeypatch, lambda: _unprovable(env))
    fired = _at_the_end_after_ownership(monkeypatch, lambda: act(trace))
    env.service.recover_queued()
    _after_admission(done)
    assert fired == [env.agent_id]                              # the window was exercised once
    return trace


def _user_stop(agent_id):
    """Exactly what /stop records: no trace in the detail."""
    _td.clear_for_agent(agent_id, preserve_queue=True)
    return _td.turn_lifecycle.try_transition(agent_id, _td.TurnEvent.STOP_REQUESTED, {
        "source": "user_stop", "message": "Turn stopped", "stop_actor": "user",
        "stop_actor_verified": False, "stop_reason": ""})


def test_r6_an_unknown_end_never_interrupts_a_successor_admitted_after_its_ownership_check(
        env, monkeypatch):
    from lib.protocol import AgentState

    def successor(_trace):
        with _td._TURN_LOCK:
            _td._SLOTS.claim(env.agent_id, "t-successor")
        agents_db.set_trace_for_session("mike", "t-successor")
        _td.turn_lifecycle.open_turn(agent_id=env.agent_id, source="pwa", trace_id="t-successor")
        _td.turn_lifecycle.transition(env.agent_id, _td.TurnEvent.SPAWN_STARTED, {
            "trace_id": "t-successor", "source": "pwa", "dispatch": "claude"})
    _unknown_end_with(env, monkeypatch, successor)
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.THINKING
    assert state["detail"]["trace_id"] == "t-successor"
    assert _opening_summary_rows(env) == []


def test_r6_an_unknown_end_never_replaces_a_stop_completed_after_its_ownership_check(
        env, monkeypatch):
    from lib.protocol import AgentState
    trace = _unknown_end_with(env, monkeypatch, lambda _trace: _user_stop(env.agent_id))
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"].get("stop_actor") == "user"
    assert _opening_summary_rows(env) == []
    assert _turn_outcome(env, trace) == "interrupted"


def test_r6_an_unknown_end_never_lands_between_a_stops_row_and_its_settlement(env, monkeypatch):
    """The Stop's state row commits on its own, then its turn settlement runs. The opening's end
    lands in exactly that gap: the trace still matches and the turn is still unsettled."""
    from lib.protocol import AgentState
    inserted, go, errors = threading.Event(), threading.Event(), []
    real_settle = _td.turn_lifecycle._settle_turn
    stopper = threading.Thread(target=lambda: errors.append(None) if _user_stop(env.agent_id) is None
                               else None, name="r6-stopper")

    def gated(agent_id, event, target, detail, stamp):
        if event == _td.TurnEvent.STOP_REQUESTED and threading.current_thread() is stopper:
            inserted.set()
            if not go.wait(10):
                errors.append("the opening's end never finished")
        return real_settle(agent_id, event, target, detail, stamp)
    monkeypatch.setattr(_td.turn_lifecycle, "_settle_turn", gated)

    def stop_row_without_settlement(_trace):
        stopper.start()
        assert inserted.wait(10), "the Stop never inserted its row"
    trace = _unknown_end_with(env, monkeypatch, stop_row_without_settlement)
    go.set()
    stopper.join(10)
    assert not stopper.is_alive() and errors == []
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"].get("stop_actor") == "user"
    assert _opening_summary_rows(env) == []
    assert _turn_outcome(env, trace) == "interrupted"


def test_r6_control_a_stop_then_a_stray_row_after_the_check_is_never_overwritten(env, monkeypatch):
    def stop_then_stray(trace):
        _user_stop(env.agent_id)
        _td.turn_lifecycle.transition(env.agent_id, _td.TurnEvent.TEXT_STREAMED,
                                      {"trace_id": trace, "phase": "responding"})
    _unknown_end_with(env, monkeypatch, stop_then_stray)
    assert _opening_summary_rows(env) == []


def test_r6_control_the_openings_own_late_row_after_the_anchor_leaves_its_end_invisible(
        env, monkeypatch):
    """Fail-closed, by design: any row after the anchor, even the opening's own, refuses."""
    from lib.protocol import AgentState
    _unknown_end_with(env, monkeypatch, lambda trace: _td.turn_lifecycle.transition(
        env.agent_id, _td.TurnEvent.TEXT_STREAMED, {"trace_id": trace, "phase": "thinking"}))
    assert agents_db.latest_state(env.agent_id)["kind"] == AgentState.THINKING
    assert _opening_summary_rows(env) == []


def test_r6_control_a_predecessors_second_end_report_before_the_anchor_keeps_the_end_visible(
        env, monkeypatch):
    from lib.protocol import AgentState
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    real = _td.TurnDispatchService._resume_and_spawn_claimed

    def at_the_drain(self, agent_id, next_spec, phase):
        # The busy turn's second end report (Stop hook and dispatcher both report an end).
        _td.turn_lifecycle.transition(agent_id, _td.TurnEvent.HOOK_STOP,
                                      {"source": "stop_hook", "trace_id": "t-busy"})
        return real(self, agent_id, next_spec, phase)
    monkeypatch.setattr(_td.TurnDispatchService, "_resume_and_spawn_claimed", at_the_drain)
    _opening_queued_behind_a_live_busy_turn(env, service, backends)
    done = _write_just_before_the_flag(env, monkeypatch, lambda: _unprovable(env))
    _the_live_busy_turn_ends(backends)
    _after_admission(done)
    state = agents_db.latest_state(env.agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["summary"] == OPENING_SUMMARY


def test_r6_control_an_end_without_an_admission_mark_is_refused(env, monkeypatch):
    real = _td.TurnDispatchService._end_launched_opening

    def unmarked(self, spec, *args, **kwargs):
        return real(self, _dataclasses.replace(spec, s_open=None), *args, **kwargs)
    monkeypatch.setattr(_td.TurnDispatchService, "_end_launched_opening", unmarked)
    _admitted(env)
    done = _write_just_before_the_flag(env, monkeypatch, lambda: _unprovable(env))
    env.service.recover_queued()
    _after_admission(done)
    assert _opening_summary_rows(env) == []


# ---- P4s: any hook prompt admission after the opening's admission refuses its visible end ----

def _end_refusal_reasons(monkeypatch):
    """The reasons the opening's end logs when its visible write is refused (callOpenEnd*)."""
    import re
    real, reasons = _td.log, []

    def recording(event, detail=""):
        if event.startswith("callOpenEnd"):
            found = re.search(r"reason=(\S+)", detail)
            reasons.append(found.group(1) if found else event)
        return real(event, detail)
    monkeypatch.setattr(_td, "log", recording)
    return reasons


def _bound_uuid(env):
    uuid = agents_db.live_backend_session(env.agent_id)
    if not uuid:
        uuid = "backend-session-1"
        agents_db.bind_backend_session(env.agent_id, uuid)
    return uuid


def _run_claude_hook(monkeypatch, env, name, payload, *, app_session=None):
    """The REAL Claude Code hook ``name`` from plugin/hooks, at its boundary: stdin JSON. Without
    ``app_session`` there is no CLAUDE_PWA_SESSION, so the hook resolves the agent from the
    payload's session_id (a terminal or a resumed shell session)."""
    import importlib
    if str(_HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOKS_DIR))
    module = importlib.import_module(name)
    if app_session is None:
        monkeypatch.delenv("CLAUDE_PWA_SESSION", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_PWA_SESSION", app_session)
    if hasattr(module, "PATHS"):
        from lib.paths import RuntimePaths
        monkeypatch.setattr(module, "PATHS", RuntimePaths.from_home(env.tmp_path))
    monkeypatch.setattr(sys, "stdin", _io.StringIO(_json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", _io.StringIO())
    module.main()


def test_P4s_the_openings_own_hook_prompt_then_an_unknown_end_stays_invisible(env, monkeypatch):
    """Accepted trade-off: the opening's own Claude prompt is a hook admission too. Its unknown
    end is refused (no OPENING_NOT_CONFIRMED); the durable unknown and no retry remain."""
    from lib.protocol import AgentState
    env.service.now = _time.time                  # the marker is armed fresh for the hook
    queue_id = _admitted(env)
    real_spawn = env.backends.spawn_turn

    def prompted_then_failed(backend, **kwargs):
        _run_claude_hook(monkeypatch, env, "pwa_source_flag",
                         {"session_id": _bound_uuid(env), "prompt": "opening"}, app_session="mike")
        return real_spawn(backend, **kwargs)
    monkeypatch.setattr(env.backends, "spawn_turn", prompted_then_failed)
    env.backends.fail = RuntimeError("the backend failed after its own prompt")
    env.service.recover_queued()
    latest = _latest_row(env.agent_id)
    assert latest["kind"] == AgentState.THINKING
    assert _json.loads(latest["detail"])["source"] == "pwa"   # the opening's own admission
    assert _opening_summary_rows(env) == []
    assert turn_queue.status(queue_id) == "unknown" and _opening(env)["state"] == "unknown"
    assert len(env.backends.attempts) == 1                    # no retry


def test_P4s_a_shell_that_steals_the_openings_fresh_marker_is_never_overwritten(env, monkeypatch):
    """`claude --resume <the agent's uuid>` from a plain shell: no CLAUDE_PWA_SESSION, so the hook
    reverse-resolves the session from the bound uuid and consumes the opening's fresh marker,
    recording 'pwa' with the opening's trace."""
    env.service.now = _time.time
    queue_id = _admitted(env)
    trace = turn_queue.get(queue_id)["trace_id"]

    def steal():
        _run_claude_hook(monkeypatch, env, "pwa_source_flag",
                         {"session_id": _bound_uuid(env), "prompt": "hi from a shell"})
        _unprovable(env)
    done = _write_just_before_the_flag(env, monkeypatch, steal)
    reasons = _end_refusal_reasons(monkeypatch)
    env.service.recover_queued()
    _after_admission(done)
    stolen = db.conn().execute(
        "SELECT detail FROM state_log WHERE agent_id = ? AND detail LIKE '%backend_session_id%'"
        " AND detail NOT LIKE '%dispatch%' ORDER BY state_id DESC LIMIT 1",
        (env.agent_id,)).fetchone()
    assert _json.loads(stolen["detail"])["source"] == "pwa"
    assert agents_db.get_trace(env.agent_id, max_age_ms=None) == trace
    assert _opening_summary_rows(env) == []
    assert reasons == ["p4s_prompt_admitted"]          # refused by the hook admission, nothing else


def test_P4s_an_interactive_prompt_after_the_admission_is_never_overwritten(env, monkeypatch):
    queue_id = _admitted(env)
    trace = turn_queue.get(queue_id)["trace_id"]

    def typed():                                   # no fresh marker: the hook records 'local'
        _run_claude_hook(monkeypatch, env, "pwa_source_flag",
                         {"session_id": _bound_uuid(env), "prompt": "typed at the terminal"})
        _unprovable(env)
    done = _write_just_before_the_flag(env, monkeypatch, typed)
    reasons = _end_refusal_reasons(monkeypatch)
    env.service.recover_queued()
    _after_admission(done)
    local = db.conn().execute(
        "SELECT detail FROM state_log WHERE agent_id = ? AND detail LIKE '%backend_session_id%'"
        " AND detail NOT LIKE '%dispatch%' ORDER BY state_id DESC LIMIT 1",
        (env.agent_id,)).fetchone()
    assert _json.loads(local["detail"])["source"] == "local"
    assert agents_db.get_trace(env.agent_id, max_age_ms=None) == trace
    assert _opening_summary_rows(env) == []
    assert reasons == ["p4s_prompt_admitted"]


def test_P4s_recognizer_inventory_from_the_real_writers(env, monkeypatch):
    """A source-shape heuristic, not event identity: every real THINKING writer is classified.
    Matches: hook prompt admissions (pwa, local, agy_hook) and the slot repair's audit note (an
    accepted false positive: the repair already released the slot). No match: the dispatcher's
    SPAWN_STARTED, the tool_finished hook's THINKING row, and the non-thinking hook rows."""
    from lib import backends as real_backends, turn_lifecycle

    def latest_matches(agent_id):
        row = _latest_row(agent_id)
        hit = db.conn().execute(
            f"SELECT EXISTS(SELECT 1 FROM state_log WHERE state_id = ? AND "
            f"{turn_lifecycle.PROMPT_ADMISSION_SQL})", (row["state_id"],)).fetchone()[0]
        return row["kind"], bool(hit)

    turn_queue.enqueue(queue_id="u-inv", agent_id=env.agent_id, session="mike", text="hello",
                       trace_id="t-inv", client_msg_id="u-inv", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    env.service.recover_queued()                                  # the real dispatcher
    assert latest_matches(env.agent_id) == ("thinking", False)    # SPAWN_STARTED (dispatch)
    uuid = _bound_uuid(env)

    def hook(name, payload, **kwargs):
        _run_claude_hook(monkeypatch, env, name, {"session_id": uuid, **payload}, **kwargs)
    hook("tool_activity", {"tool_name": "Bash", "tool_use_id": "u1", "tool_input": {"command": "ls"}})
    assert latest_matches(env.agent_id) == ("tool", False)
    hook("tool_finished", {"tool_name": "Bash", "tool_use_id": "u1",
                           "tool_input": {"command": "ls"}, "tool_response": {"stdout": ""}})
    assert latest_matches(env.agent_id) == ("thinking", False)    # the closest boundary
    env.service.repair_slot(env.agent_id, actor="test", reason="inventory probe")
    assert latest_matches(env.agent_id) == ("thinking", True)     # accepted false positive (I-1)
    hook("pwa_source_flag", {"prompt": "typed"})
    assert latest_matches(env.agent_id) == ("thinking", True)     # 'local'
    real_backends.by_id("claude").arm_source_marker("mike", "t-pwa", True, home=env.tmp_path,
                                                    now=_time.time)
    hook("pwa_source_flag", {"prompt": "from the app"})
    assert _json.loads(_latest_row(env.agent_id)["detail"])["source"] == "pwa"
    assert latest_matches(env.agent_id) == ("thinking", True)
    hook("pre_compact", {"trigger": "auto"})
    assert latest_matches(env.agent_id) == ("compacting", False)
    hook("notification_state", {"message": "needs you", "title": "Claude"})
    assert latest_matches(env.agent_id) == ("waiting", False)
    hook("stop_state", {})
    assert latest_matches(env.agent_id) == ("done", False)
    # agy run from a terminal Clarp did not start: the real hook process, no managed turn.
    monkeypatch.delenv("CLARP_AGY_MANAGED_TURN", raising=False)
    conversation = "11111111-2222-4333-8444-555555555555"
    agy_agent = agents_db.create_agent(persona="Ada", voice_id="", cwd=str(env.tmp_path),
                                       session="ada", backend="agy")
    agents_db.start_runtime(agy_agent, "ada")
    agents_db.bind_backend_session(agy_agent, conversation)
    import os
    result = _subprocess.run(
        [sys.executable, str(_HOOKS_DIR / "agy_state.py"), "PreInvocation"],
        input=_json.dumps({"conversationId": conversation}), capture_output=True, text=True,
        env={**os.environ, "CLAUDE_PWA_DB": str(db.DB_PATH)}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert _json.loads(_latest_row(agy_agent)["detail"])["source"] == "agy_hook"
    assert latest_matches(agy_agent) == ("thinking", True)


# ---- the admission mark (s_open) is a call opening's alone ------------------------------------

def _generation_reads(monkeypatch):
    """Which dispatcher frames read the agent's state generation (the real read still runs)."""
    real, reads = _td.turn_lifecycle.state_generation, []

    def recording(agent_id):
        reads.append(sys._getframe(1).f_code.co_name)
        return real(agent_id)
    monkeypatch.setattr(_td.turn_lifecycle, "state_generation", recording)
    return reads


def test_r6_ordinary_turns_read_no_state_generation_on_dispatch_or_drain(env, monkeypatch):
    """An ordinary first dispatch and an ordinary turn drained behind a live busy turn never
    read the state generation (their spec keeps s_open None)."""
    reads = _generation_reads(monkeypatch)
    backends = _CallbackBackends()
    service = _immediate_service(env, backends)
    turn_queue.enqueue(queue_id="u-first", agent_id=env.agent_id, session="mike", text="first words",
                       trace_id="t-first", client_msg_id="u-first", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    service.recover_queued()                                    # the ordinary first dispatch
    assert len(backends.attempts) == 1 and "first words" in backends.attempts[0]
    backends.live = True
    turn_queue.enqueue(queue_id="u-second", agent_id=env.agent_id, session="mike",
                       text="queued words", trace_id="t-second", client_msg_id="u-second",
                       synthesize_audio=False, origin="user", sender_agent_id="")
    row = turn_queue.get("u-second")
    result = service.submit(_td.DispatchCommand(
        text=row["text"], requested_session=row["session"], trace_id=row["trace_id"],
        synthesize_audio=bool(row["synthesize_audio"]), forced_session=row["session"],
        client_msg_id=row["client_msg_id"], origin=row["origin"],
        sender_agent_id=row["sender_agent_id"], prompt_admission_id=row["prompt_admission_id"],
        queue_if_busy=True, skip_admission=True, durable_queue_id="u-second",
        allow_paused_queue=bool(row.get("allow_paused"))))
    assert result.queued is True                                # held behind the live turn
    drained = _record_drain_entries(monkeypatch)
    _the_live_busy_turn_ends(backends)                          # the real drain spawns it
    assert drained == ["u-second"]
    assert len(backends.attempts) == 2 and "queued words" in backends.attempts[1]
    assert reads == []                                          # no generation read at all


def test_r6_a_call_opening_reads_its_admission_mark_before_setting_its_trace(env, monkeypatch):
    events = []
    real_generation = _td.turn_lifecycle.state_generation
    real_trace = _td.agents_db.set_trace_for_session

    def generation(agent_id):
        events.append(("generation", sys._getframe(1).f_code.co_name))
        return real_generation(agent_id)

    def set_trace(session, trace_id):
        events.append(("trace", trace_id))
        return real_trace(session, trace_id)
    monkeypatch.setattr(_td.turn_lifecycle, "state_generation", generation)
    monkeypatch.setattr(_td.agents_db, "set_trace_for_session", set_trace)
    queue_id = _admitted(env)
    trace = turn_queue.get(queue_id)["trace_id"]
    env.service.recover_queued()                                # the opening's first dispatch
    assert env.backends.attempts                                # it launched
    first_trace = events.index(("trace", trace))
    assert events[:first_trace].count(("generation", "_dispatch_owned")) == 1
