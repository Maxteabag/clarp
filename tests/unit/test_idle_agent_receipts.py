"""A ``started`` receipt never outlives its turn.

After the 2026-10-11 06:40 runtime restart the boot repair left 60 receipts
``started``; 46 belonged to agents idle for days. Their trace never settled
in ``turns`` (two rows per trace, one never settles) and no later turn of the
agent proved they ended, so nothing would ever end them. An agent with no
slot, no process and a terminal newest state that came after the receipt
started has finished that turn.
"""
import pytest

from lib import agents as agents_db
from lib import db, reconcile, turn_queue
import lib.turn_dispatch as _td

HOUR = 3_600_000
NOW = 1_000 * HOUR


@pytest.fixture(autouse=True)
def _local_dispatch_runtime(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()


def _agent(tmp_path, name):
    agent_id = agents_db.create_agent(persona=name, voice_id="V", cwd=str(tmp_path),
                                      session=name, backend="claude")
    db.conn().execute("DELETE FROM state_log WHERE agent_id = ?", (agent_id,))
    return agent_id


def _row(agent_id, queue_id, status, started_at):
    db.conn().execute(
        """INSERT INTO queued_turns (queue_id, agent_id, session, text,
               trace_id, client_msg_id, origin, status, enqueued_at, started_at)
           VALUES (?, ?, 's', '', ?, ?, 'user', ?, ?, ?)""",
        (queue_id, agent_id, f"t-{queue_id}", queue_id, status, started_at,
         started_at if status == "started" else None))


def _state(agent_id, kind, ts):
    db.conn().execute(
        "INSERT INTO state_log (agent_id, ts, kind) VALUES (?, ?, ?)",
        (agent_id, ts, kind))


def _statuses(*queue_ids):
    return {q: turn_queue.status(q) for q in queue_ids}


def test_receipt_of_an_agent_that_settled_after_it_started_ends(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(turn_queue, "log", lambda e, m="": logged.append((e, m)))
    a = _agent(tmp_path, "avana")
    _row(a, "old", "started", NOW - 30 * 24 * HOUR)
    _state(a, "done", NOW - 29 * 24 * HOUR)

    counts = turn_queue.terminalize_idle_agent_receipts(lambda _a: False, now_ms=NOW)

    assert counts == {"ended": 1}
    assert _statuses("old") == {"old": "ended"}
    assert logged == [("queueReceiptsTerminalized", "reason=idle_agent ended=1")]


def test_live_slot_keeps_its_started_receipt(tmp_path):
    a = _agent(tmp_path, "busy")
    _row(a, "live", "started", NOW - 48 * HOUR)
    _state(a, "done", NOW - 47 * HOUR)

    assert turn_queue.terminalize_idle_agent_receipts(
        lambda agent: agent == a, now_ms=NOW) == {}
    assert _statuses("live") == {"live": "started"}


def test_unknown_liveness_keeps_the_receipt(tmp_path):
    a = _agent(tmp_path, "unsure")
    _row(a, "r", "started", NOW - 48 * HOUR)
    _state(a, "done", NOW - 47 * HOUR)

    def broken(_agent):
        raise OSError("runtime socket gone")

    assert turn_queue.terminalize_idle_agent_receipts(broken, now_ms=NOW) == {}
    assert _statuses("r") == {"r": "started"}


@pytest.mark.parametrize("kind", ["thinking", "tool", "compacting", "background",
                                  "spawned"])
def test_non_terminal_newest_state_keeps_even_an_old_receipt(tmp_path, kind):
    a = _agent(tmp_path, "worker")
    _row(a, "r", "started", NOW - 48 * HOUR)
    _state(a, kind, NOW - HOUR)

    assert turn_queue.terminalize_idle_agent_receipts(lambda _a: False, now_ms=NOW) == {}
    assert _statuses("r") == {"r": "started"}


def test_fresh_receipt_whose_turn_has_not_recorded_an_end_is_untouched(tmp_path):
    # The newest terminal state is the previous turn's: it predates the receipt.
    a = _agent(tmp_path, "fresh")
    _state(a, "done", NOW - 10 * 60_000)
    _row(a, "fresh", "started", NOW - 5 * 60_000)

    assert turn_queue.terminalize_idle_agent_receipts(lambda _a: False, now_ms=NOW) == {}
    assert _statuses("fresh") == {"fresh": "started"}


def test_a_just_settled_turn_gets_a_grace_period(tmp_path):
    a = _agent(tmp_path, "finishing")
    _row(a, "r", "started", NOW - 5 * 60_000)
    _state(a, "done", NOW - 1_000)

    assert turn_queue.terminalize_idle_agent_receipts(lambda _a: False, now_ms=NOW) == {}
    assert turn_queue.terminalize_idle_agent_receipts(
        lambda _a: False, now_ms=NOW + 60_000) == {"ended": 1}


def test_old_receipt_of_an_idle_agent_ends_even_without_a_newer_state(tmp_path):
    a = _agent(tmp_path, "quiet")
    _state(a, "idle", NOW - 72 * HOUR)
    _row(a, "old", "started", NOW - 48 * HOUR)
    b = _agent(tmp_path, "stateless")
    _row(b, "nostate", "started", NOW - 48 * HOUR)
    c = _agent(tmp_path, "recent")
    _state(c, "idle", NOW - 72 * HOUR)
    _row(c, "young", "started", NOW - 2 * HOUR)

    counts = turn_queue.terminalize_idle_agent_receipts(lambda _a: False, now_ms=NOW)

    assert counts == {"ended": 2}
    assert _statuses("old", "nostate", "young") == {
        "old": "ended", "nostate": "ended", "young": "started"}


def test_open_and_terminal_rows_are_never_touched(tmp_path):
    a = _agent(tmp_path, "mixed")
    for status in ("queued", "claimed", "parked", "done", "failed", "interrupted"):
        _row(a, status, status, NOW - 48 * HOUR)
    _state(a, "done", NOW - HOUR)

    assert turn_queue.terminalize_idle_agent_receipts(lambda _a: False, now_ms=NOW) == {}
    assert _statuses("queued", "claimed", "parked", "done", "failed", "interrupted") == {
        s: s for s in ("queued", "claimed", "parked", "done", "failed", "interrupted")}


def test_rerun_is_idempotent_and_silent(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(turn_queue, "log", lambda e, m="": logged.append((e, m)))
    a = _agent(tmp_path, "again")
    _row(a, "r", "started", NOW - 48 * HOUR)
    _state(a, "done", NOW - HOUR)

    assert turn_queue.terminalize_idle_agent_receipts(lambda _a: False, now_ms=NOW) == {"ended": 1}
    assert turn_queue.terminalize_idle_agent_receipts(lambda _a: False, now_ms=NOW) == {}
    assert len(logged) == 1


def test_ended_receipt_still_deduplicates_a_client_retry(tmp_path):
    a = _agent(tmp_path, "dedupe")
    _row(a, "q-retry", "started", NOW - 48 * HOUR)
    _state(a, "done", NOW - HOUR)
    turn_queue.terminalize_idle_agent_receipts(lambda _a: False, now_ms=NOW)

    again = turn_queue.enqueue(
        queue_id="q-retry", agent_id=a, session="s", text="retry", trace_id="t2",
        client_msg_id="q-retry", synthesize_audio=False, origin="user",
        sender_agent_id="")

    assert again is False
    assert _statuses("q-retry") == {"q-retry": "ended"}


def test_reconcile_pass_skips_agents_the_runtime_holds(tmp_path, monkeypatch):
    held = {
        "active": _agent(tmp_path, "slot"), "spawning": _agent(tmp_path, "spawn"),
        "terminals": _agent(tmp_path, "term"), "queued": _agent(tmp_path, "queue"),
        "provider_turns": _agent(tmp_path, "codex"),
    }
    free = _agent(tmp_path, "free")
    for agent_id in (*held.values(), free):
        _row(agent_id, f"r-{agent_id}", "started", 1_000)
        _state(agent_id, "done", 2_000)
    status = {"active": {held["active"]: "t"}, "spawning": [held["spawning"]],
              "terminals": [held["terminals"]], "queued": {held["queued"]: 1},
              "provider_turns": [held["provider_turns"]]}
    monkeypatch.setattr(reconcile, "has_live_work", lambda _a, _b: False)

    assert reconcile.end_idle_agent_receipts(status) == {"ended": 1}
    assert turn_queue.status(f"r-{free}") == "ended"
    assert all(turn_queue.status(f"r-{a}") == "started" for a in held.values())


def test_reconcile_pass_skips_an_agent_with_a_live_process(tmp_path, monkeypatch):
    a = _agent(tmp_path, "proc")
    _row(a, "r", "started", 1_000)
    _state(a, "done", 2_000)
    monkeypatch.setattr(reconcile, "has_live_work", lambda agent, _b: agent == a)

    assert reconcile.end_idle_agent_receipts({}) == {}
    assert _statuses("r") == {"r": "started"}


def test_reconcile_pass_does_nothing_when_provider_turns_are_unknown(tmp_path, monkeypatch):
    a = _agent(tmp_path, "unknown")
    _row(a, "r", "started", 1_000)
    _state(a, "done", 2_000)
    monkeypatch.setattr(reconcile, "has_live_work", lambda _a, _b: False)

    assert reconcile.end_idle_agent_receipts({"provider_turns": ["<unknown>"]}) == {}
    assert _statuses("r") == {"r": "started"}


def test_boot_repair_ends_idle_agent_receipts_and_keeps_a_rehydrated_slot(
        tmp_path, monkeypatch):
    from lib import runtime_startup
    idle, held = _agent(tmp_path, "idle"), _agent(tmp_path, "held")
    for agent_id in (idle, held):
        _row(agent_id, f"r-{agent_id}", "started", 1_000)
        _state(agent_id, "done", 2_000)
    monkeypatch.setattr(_td, "runtime_status", lambda: {
        "active": {held: f"t-r-{held}"}, "provider_turns": []})
    monkeypatch.setattr(reconcile, "has_live_work", lambda _a, _b: False)

    assert runtime_startup._terminalize_ended_receipts() == {"idle_ended": 1}
    assert turn_queue.status(f"r-{idle}") == "ended"
    assert turn_queue.status(f"r-{held}") == "started"
