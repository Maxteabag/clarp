"""Queue lane recovery: the Pebble wedge of 2026-10-10.

07:23:49 Pebble (claude) started trace f6974a14; at 07:24:16 a peer message
queued behind it (trace d6e87cdc). While the Host's SQLite writer was held
("database is locked" from 07:24 to 07:40 across the runtime), the turn finished
at 07:26:13. The runtime handed the slot to the queued message and opened its
turn row: INSERT INTO turns failed with "database is locked" (07:26:19
queuedSpawnFail). The handoff swallowed the error and left the slot claimed
and "spawning" with no process. Spawning slots are exempt from every leak
check, so 18 more messages queued silently behind it until Theo pressed Stop
at 08:07, which also paused the queue and the goal.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import db, turn_lifecycle, turn_queue
from lib.protocol import AgentState
from lib.turn_dispatch import TurnDispatchService
import lib.turn_dispatch as _td


@pytest.fixture(autouse=True)
def _local_dispatch_runtime(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()


class _Backends:
    CLAUDE = "claude"

    def __init__(self):
        self.spawned = []
        self.interrupted = []
        self.live = True

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        self.interrupted.append((backend, agent_id))
        return 0

    def active_handles(self, backend, agent_id):
        return ["handle"] if self.live else []

    def spawn_turn(self, backend, **kwargs):
        self.spawned.append((backend, kwargs))


def _service(tmp_path, *, retries=None):
    agent_id = agents_db.create_agent(
        persona="Pebble", voice_id="V", cwd=str(tmp_path),
        session="pebble", backend="claude")
    agents_db.start_runtime(agent_id, "pebble")
    backends = _Backends()
    ctx = SimpleNamespace(default_session="pebble",
                          agents_path=tmp_path / "unused.json",
                          stream=SimpleNamespace(broadcast=lambda _e: None))
    scheduled = retries if retries is not None else []
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: "backend-session-1", now=lambda: 12.5,
        retry_scheduler=lambda _delay, fn: scheduled.append(fn))
    return service, backends, agent_id, scheduled


def _queue(service, trace, text=None, *, origin="agent", client_msg_id=None):
    return service.dispatch(
        text=text or f"message {trace}", requested_session="pebble",
        trace_id=trace, client_msg_id=client_msg_id or f"q-{trace}",
        synthesize_audio=False, queue_if_busy=True, origin=origin)


def _texts(backends):
    return [kw["text"] for _, kw in backends.spawned]


def _lock_once(monkeypatch, target, name):
    real = getattr(target, name)
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, flaky)
    return calls


def _drain(scheduled):
    while scheduled:
        scheduled.pop(0)()


def _finish(backends, index, ok=True):
    call = backends.spawned[index][1]
    if ok:
        call["on_result"]({"duration_ms": 5})
    else:
        call["on_error"]("Turn interrupted")


# --- (a) a failed queued handoff never strands its slot ----------------------

@pytest.mark.parametrize("failing", ["open_turn", "record_user_message"])
def test_locked_database_during_queued_handoff_does_not_wedge_the_agent(
        tmp_path, monkeypatch, failing):
    service, backends, agent_id, scheduled = _service(tmp_path)
    service.dispatch(text="first", requested_session="pebble", trace_id="tA",
                     synthesize_audio=False)
    _queue(service, "tB")
    _queue(service, "tC")
    if failing == "open_turn":
        _lock_once(monkeypatch, _td.turn_lifecycle, "open_turn")
    else:
        _lock_once(monkeypatch, TurnDispatchService, "_record_user_message")

    _finish(backends, 0)

    # The failed handoff gave the slot back: nothing claims to be spawning.
    assert agent_id not in _td.runtime_status()["spawning"]
    assert _td.runtime_status()["active"].get(agent_id) != "tB"
    assert turn_queue.status("q-tB") == "queued"
    _drain(scheduled)
    assert _texts(backends) == ["first", "message tB"]
    _finish(backends, 1)
    assert _texts(backends) == ["first", "message tB", "message tC"]
    assert turn_queue.pending_count(agent_id) == 0


def test_messages_arriving_after_a_failed_handoff_run_in_order(tmp_path, monkeypatch):
    service, backends, agent_id, scheduled = _service(tmp_path)
    service.dispatch(text="first", requested_session="pebble", trace_id="tA",
                     synthesize_audio=False)
    _queue(service, "tB")
    _lock_once(monkeypatch, _td.turn_lifecycle, "open_turn")
    _finish(backends, 0)
    backends.live = False
    # Before the retry fires, more peers write: they must not queue behind a
    # phantom (Pebble collected 18 this way) and must not overtake tB.
    _queue(service, "tC")
    _drain(scheduled)
    backends.live = True
    assert _texts(backends)[:2] == ["first", "message tB"]
    _finish(backends, len(backends.spawned) - 1)
    _drain(scheduled)
    assert _texts(backends) == ["first", "message tB", "message tC"]


def test_memory_only_send_survives_a_failed_handoff(tmp_path, monkeypatch):
    # A plain send admitted during Stop waits only in memory; a failed launch
    # must retry it, not drop it.
    service, backends, agent_id, scheduled = _service(tmp_path)
    backends.live = False
    _td._INFLIGHT[agent_id] = _td._STOPPING_SENTINEL
    service.dispatch(text="new work after stop", requested_session="pebble",
                     trace_id="t-new", client_msg_id="u-new",
                     synthesize_audio=False)
    _lock_once(monkeypatch, _td.turn_lifecycle, "open_turn")
    # finish_stop builds its own service, which retries on the default timer.
    monkeypatch.setattr(_td, "_default_retry_scheduler",
                        lambda _delay, fn: scheduled.append(fn))

    _td.finish_stop(service.ctx, agent_id, backend_registry=backends)

    assert agent_id not in _td.runtime_status()["spawning"]
    # Until it launches, its park row keeps it across a runtime restart.
    assert turn_queue.status("stop-park-t-new") == "parked"
    _drain(scheduled)
    assert _texts(backends) == ["new work after stop"]
    assert turn_queue.status("stop-park-t-new") == ""


# --- (a) defence in depth: an orphaned spawning slot is released ------------

def _orphan_spawning_slot(service, backends, agent_id, *, claimed_s_ago=600):
    service.dispatch(text="first", requested_session="pebble", trace_id="tA",
                     synthesize_audio=False)
    turn_lifecycle.try_transition(
        agent_id, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {"trace_id": "tA"})
    db.conn().execute("UPDATE state_log SET ts = ts - 600000 WHERE agent_id = ?",
                      (agent_id,))
    # What the 07:26 handoff left: the next trace owns a slot that is still
    # "spawning", and no launch is running for it.
    _td._SLOTS.claim(agent_id, "tB")
    _td._SLOTS.claimed_at[agent_id] -= claimed_s_ago
    backends.live = False


def test_orphaned_spawning_slot_is_released(tmp_path):
    service, backends, agent_id, _ = _service(tmp_path)
    _orphan_spawning_slot(service, backends, agent_id)

    assert service.release_leaked_slots(agent_id=agent_id) == {agent_id: "tB"}
    assert agent_id not in _td.runtime_status()["active"]


@pytest.mark.parametrize("owner", ["launching", "account_recovery", "live_process",
                                   "young_claim", "stop_barrier", "terminal"])
def test_spawning_slot_with_an_owner_is_never_released(tmp_path, monkeypatch, owner):
    service, backends, agent_id, _ = _service(tmp_path)
    _orphan_spawning_slot(service, backends, agent_id,
                          claimed_s_ago=5 if owner == "young_claim" else 600)
    expected = "tB"
    if owner == "launching":
        _td._SLOTS.launches[agent_id] = "tB"
    elif owner == "account_recovery":
        failover = _td.account_failover("claude")
        from lib.account_failover import Attempt
        failover.attempts[agent_id] = Attempt(
            agent_id=agent_id, trace_id="tB", model="", state={"account_recovery": True},
            owned=lambda: True, pause=lambda: None, resume=lambda: None)
    elif owner == "live_process":
        backends.live = True
    elif owner == "stop_barrier":
        _td._INFLIGHT[agent_id] = expected = _td._STOPPING_SENTINEL
    elif owner == "terminal":
        _td._INFLIGHT[agent_id] = expected = _td._TERMINAL_SENTINEL
    try:
        assert service.release_leaked_slots(agent_id=agent_id) == {}
        assert _td._INFLIGHT[agent_id] == expected
    finally:
        _td._SLOTS.launches.pop(agent_id, None)
        _td.account_failover("claude").attempts.pop(agent_id, None)


def test_queued_work_without_an_owner_is_handed_the_free_slot(tmp_path, monkeypatch):
    # Nadia and psa-billing, 2026-10-10 09:24: the older runtime freed a failed
    # spawn's slot but kept the messages queued behind it, so nothing started
    # them and the runtime's idle drain (which refuses while anything is
    # queued) could never hand over to a new release. The leak check that the
    # drain and the Host run first now starts the head of such a queue.
    service, backends, agent_id, scheduled = _service(tmp_path)
    service.dispatch(text="first", requested_session="pebble", trace_id="tA",
                     synthesize_audio=False)
    _queue(service, "tB")
    _queue(service, "tC")
    _lock_once(monkeypatch, _td.turn_lifecycle, "open_turn")
    _finish(backends, 0)
    scheduled.clear()  # the retry timer was lost (the older runtime had none)
    assert _td.runtime_status()["queued"] == {agent_id: 2}
    assert agent_id not in _td.runtime_status()["active"]

    service.release_leaked_slots()

    assert _texts(backends) == ["first", "message tB"]
    assert _td.runtime_status()["active"][agent_id] == "tB"
    _finish(backends, 1)
    assert _texts(backends)[-1] == "message tC"


# --- (b) queue receipts end with their turn -----------------------------------

@pytest.mark.parametrize("ok,status", [(True, "done"), (False, "failed")])
def test_queue_receipt_is_terminal_once_its_turn_settles(tmp_path, ok, status):
    service, backends, agent_id, _ = _service(tmp_path)
    service.dispatch(text="first", requested_session="pebble", trace_id="tA",
                     synthesize_audio=False)
    _queue(service, "tB")
    _finish(backends, 0)
    assert turn_queue.status("q-tB") == "started"

    _finish(backends, 1, ok=ok)

    assert turn_queue.status("q-tB") == status


def test_restart_settles_the_receipt_of_the_turn_it_killed(tmp_path):
    from lib import interrupted_turns
    service, backends, agent_id, _ = _service(tmp_path)
    backends.live = False
    _queue(service, "tB")
    assert turn_queue.status("q-tB") == "started"
    turn_lifecycle.try_transition(
        agent_id, turn_lifecycle.TurnEvent.SPAWN_STARTED, {"trace_id": "tB"})

    interrupted_turns.recover_after_restart()

    assert turn_queue.status("q-tB") == "interrupted"


def test_startup_repair_terminalizes_only_rows_whose_turn_ended(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(turn_queue, "log", lambda e, m="": logged.append((e, m)))
    agent_id = agents_db.create_agent(persona="P", voice_id="V", cwd=str(tmp_path),
                                      session="pebble", backend="claude")
    con = db.conn()

    def row(queue_id, trace, status, started_at):
        con.execute(
            """INSERT INTO queued_turns (queue_id, agent_id, session, text,
                   trace_id, client_msg_id, origin, status, enqueued_at, started_at)
               VALUES (?, ?, 'pebble', '', ?, ?, 'agent', ?, ?, ?)""",
            (queue_id, agent_id, trace, queue_id, status, started_at, started_at))

    def turn(trace, started_at, outcome=None):
        con.execute(
            """INSERT INTO turns (agent_id, source, trace_id, started_at,
                   settled_at, outcome) VALUES (?, 'pwa', ?, ?, ?, ?)""",
            (agent_id, trace, started_at,
             started_at + 5 if outcome else None, outcome))

    row("settled", "t1", "started", 1000); turn("t1", 1000); turn("t1", 1001, "completed")
    row("failed", "t2", "started", 2000); turn("t2", 2000, "failed")
    row("later", "t3", "started", 3000); turn("t3", 3000)          # ledger never settled
    row("steered", "t4", "started", 3500)                          # no turn of its own
    row("current", "t5", "started", 4000); turn("t5", 4000)        # may still run
    row("waiting", "t6", "queued", 4500)                           # user work: untouched

    counts = turn_queue.terminalize_ended_receipts()

    assert {q: turn_queue.status(q) for q in
            ("settled", "failed", "later", "steered", "current", "waiting")} == {
        "settled": "done", "failed": "failed", "later": "ended",
        "steered": "ended", "current": "started", "waiting": "queued"}
    assert counts == {"done": 1, "failed": 1, "ended": 2}
    assert [e for e, _ in logged] == ["queueReceiptsTerminalized"]
    assert turn_queue.terminalize_ended_receipts() == {}
    assert turn_queue.get("waiting")["text"] == ""


def test_terminal_receipts_still_deduplicate_retries(tmp_path):
    service, backends, agent_id, _ = _service(tmp_path)
    backends.live = False
    _queue(service, "tB")
    _finish(backends, 0)
    assert turn_queue.status("q-tB") == "done"

    again = _queue(service, "tB-retry", "message tB", client_msg_id="q-tB")

    assert again.queued is False
    assert len(backends.spawned) == 1


# --- (c) dedupe automation by identity, never user or peer requests ----------

def test_duplicate_automation_wake_runs_once(tmp_path):
    service, backends, agent_id, _ = _service(tmp_path)
    service.dispatch(text="first", requested_session="pebble", trace_id="tA",
                     synthesize_audio=False)
    _queue(service, "w1", "wake up", origin="automation", client_msg_id="bookkeeping-1")
    _queue(service, "w2", "wake up", origin="automation", client_msg_id="bookkeeping-1")
    assert turn_queue.pending_count(agent_id) == 1
    _finish(backends, 0)
    _finish(backends, 1)
    assert _texts(backends) == ["first", "wake up"]


@pytest.mark.parametrize("origin", ["user", "agent"])
def test_distinct_requests_with_the_same_text_all_run(tmp_path, origin):
    service, backends, agent_id, _ = _service(tmp_path)
    service.dispatch(text="first", requested_session="pebble", trace_id="tA",
                     synthesize_audio=False)
    for n in range(3):
        _queue(service, f"t{n}", "status?", origin=origin)
    assert turn_queue.pending_count(agent_id) == 3
    for index in range(3):
        _finish(backends, index)
    assert _texts(backends) == ["first", "status?", "status?", "status?"]


# --- (d) a repair is not a Stop ------------------------------------------------

def test_repair_releases_a_stranded_slot_without_pausing_queue_or_goals(tmp_path):
    from lib import task_plans
    service, backends, agent_id, scheduled = _service(tmp_path)
    _orphan_spawning_slot(service, backends, agent_id)
    turn_queue.enqueue(queue_id="q-held", agent_id=agent_id, session="pebble",
                       text="held", trace_id="t-held", client_msg_id="q-held",
                       synthesize_audio=False, origin="agent", sender_agent_id="")
    paused_goals = []
    original = task_plans.pause_recovery_for_agent
    task_plans.pause_recovery_for_agent = lambda *a, **k: paused_goals.append(a)
    try:
        result = service.repair_slot(agent_id, actor="agent:theo-97e5",
                                     reason="Pebble busy with no process")
    finally:
        task_plans.pause_recovery_for_agent = original

    assert result["released_trace"] == "tB"
    assert turn_queue.is_paused(agent_id) is False
    assert paused_goals == []
    assert backends.interrupted == []
    import json
    audit = db.conn().execute(
        """SELECT kind, detail FROM state_log WHERE agent_id = ?
              AND json_extract(detail, '$.source') = 'slot_repair'""",
        (agent_id,)).fetchall()
    assert [row["kind"] for row in audit] == [AgentState.DONE]
    detail = json.loads(audit[0]["detail"])
    assert detail["repair_actor"] == "agent:theo-97e5"
    assert detail["repair_reason"] == "Pebble busy with no process"
    _drain(scheduled)
    assert _texts(backends)[-1] == "held"
    assert result["queue_recovered"] == 1


def test_repair_never_touches_a_live_turn(tmp_path):
    service, backends, agent_id, _ = _service(tmp_path)
    service.dispatch(text="first", requested_session="pebble", trace_id="tA",
                     synthesize_audio=False)

    result = service.repair_slot(agent_id, actor="user", reason="looks stuck")

    assert result["released_trace"] == ""
    assert _td.runtime_status()["active"][agent_id] == "tA"
    assert backends.interrupted == []


def test_repair_requires_a_reason(tmp_path):
    service, _backends, agent_id, _ = _service(tmp_path)
    with pytest.raises(ValueError):
        service.repair_slot(agent_id, actor="user", reason="  ")
