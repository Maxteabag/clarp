"""The periodic receipt pass does not read the same undecidable receipt again every minute.

laptopstudio, 15/16 Nov 2026 (issue 363): one Host thread read about 250 MB
every two minutes, all day. The background-job watcher's pass looked at every
queued receipt each time; deciding one calls ``agents._receipt_attempt_status``,
whose two ``state_log`` statements walk the agent's whole state history when
nothing matches. 56 rows queued between 3 Oct and 11 Nov cost 171.7 MB a pass;
six of them ended ``unknown`` every pass, the same six for days.

A receipt the pass could not decide is now looked at again after two minutes,
then four, eight, up to an hour. What a pass decides about the receipts it
does examine is unchanged.
"""
from __future__ import annotations

import pytest

from lib import db, launch_receipts, reconcile, stale_work, turn_lifecycle, turn_queue
from lib.background_job_watcher import BackgroundJobWatcher
from tests.unit.test_turn_dispatch import _local_dispatch_runtime, _make_service  # noqa: F401

FIRST = BackgroundJobWatcher.RECEIPT_RECHECK_SEC
CAP = BackgroundJobWatcher.RECEIPT_RECHECK_MAX_SEC


def _undecidable_receipt(tmp_path, queue_id="q", trace="t"):
    """A queued message whose turn was opened and attempted, with nothing that
    proves it ran or did not: the pass defers it as unknown."""
    _service, _backend, agent = _make_service(tmp_path)
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id=trace, synthesize_audio=False)
    turn_queue.enqueue(queue_id=queue_id, agent_id=agent, session="mike", text="synthetic",
                       trace_id=trace, client_msg_id=queue_id, synthesize_audio=False,
                       origin="user", sender_agent_id="")
    launch_receipts.record_pending(agent_id=agent, trace_id=trace, client_msg_id=queue_id,
                                   origin="user")
    assert launch_receipts.mark_attempted(trace) == "attempted"
    return agent


def _another(agent, queue_id, trace):
    """One more undecidable receipt of the same agent, behind the ones before it."""
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id=trace, synthesize_audio=False)
    turn_queue.enqueue(queue_id=queue_id, agent_id=agent, session="mike", text="synthetic",
                       trace_id=trace, client_msg_id=queue_id, synthesize_audio=False,
                       origin="user", sender_agent_id="")
    launch_receipts.record_pending(agent_id=agent, trace_id=trace, client_msg_id=queue_id,
                                   origin="user")
    assert launch_receipts.mark_attempted(trace) == "attempted"


def _its_turn_completed(trace):
    """The receipt's turn ran to its end: the pass can now settle the receipt."""
    db.conn().execute("UPDATE turns SET settled_at=?, outcome='completed' WHERE trace_id=?",
                      (db.now_ms(), trace))
    db.conn().commit()


def _status(queue_id):
    return _row(queue_id)["status"]


def _look(**kwargs):
    """One look at every queued receipt. The pass keeps a cursor: a pass that
    reaches the end of the rows is followed by one that only wraps it round,
    so with a handful of rows every other pass examines them."""
    turn_queue.reconcile_receipts(**kwargs)
    turn_queue.reconcile_receipts(**kwargs)


def _row(queue_id="q"):
    return dict(db.conn().execute("SELECT * FROM queued_turns WHERE queue_id=?", (queue_id,)).fetchone())


def _counted(monkeypatch):
    calls = []
    real = turn_queue._launch_status

    def launch_status(row):
        calls.append(str(row["queue_id"]))
        return real(row)
    monkeypatch.setattr(turn_queue, "_launch_status", launch_status)
    return calls


# --- the pass itself ----------------------------------------------------------

def test_the_pass_tells_which_receipts_it_could_not_decide(tmp_path, monkeypatch):
    _undecidable_receipt(tmp_path)
    examined, deferred = _counted(monkeypatch), []
    before = _row()

    turn_queue.reconcile_receipts(on_deferred=lambda *told: deferred.append(told))

    assert examined == ["q"] and deferred == [("q", before["queue_seq"], "unknown")]
    assert _row() == before


def test_a_receipt_that_is_skipped_is_not_examined_and_not_changed(tmp_path, monkeypatch):
    _undecidable_receipt(tmp_path)
    examined, deferred = _counted(monkeypatch), []
    before = _row()

    turn_queue.reconcile_receipts(skip=lambda queue_id, queue_seq: (queue_id, queue_seq) == ("q", before["queue_seq"]),
                                  on_deferred=lambda *args: deferred.append(args))

    assert examined == [] and deferred == []
    assert _row() == before


def test_only_the_skipped_receipt_is_left_out(tmp_path, monkeypatch):
    agent = _undecidable_receipt(tmp_path)
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="t2", synthesize_audio=False)
    turn_queue.enqueue(queue_id="q2", agent_id=agent, session="mike", text="synthetic",
                       trace_id="t2", client_msg_id="q2", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    examined = _counted(monkeypatch)

    _look(skip=lambda queue_id, _queue_seq: queue_id == "q")
    assert examined == ["q2"]

    del examined[:]
    _look()                                            # no caller, no skipping: as before
    assert sorted(examined) == ["q", "q2"]


def test_the_pass_without_the_new_arguments_is_what_it_was(tmp_path, monkeypatch):
    _undecidable_receipt(tmp_path)
    examined = _counted(monkeypatch)
    before = _row()

    assert turn_queue.reconcile_receipts() == {}       # examines
    assert turn_queue.reconcile_receipts() == {}       # wraps the cursor round
    assert reconcile.end_idle_agent_receipts({"provider_turns": ["<unknown>"]}) == {}

    assert examined == ["q", "q"] and _row() == before


def test_a_receipt_behind_waiting_ones_is_decided_in_the_same_pass(tmp_path, monkeypatch):
    """"Skip" means "leave this one", not "stop here": the reviewer of PR 364
    made a skipped receipt end the page and every test passed; a new receipt
    behind 60 waiting ones then took 61 passes instead of one."""
    agent = _undecidable_receipt(tmp_path, "waiting-1", "t1")
    _another(agent, "waiting-2", "t2")
    _another(agent, "waiting-3", "t3")
    _another(agent, "fresh", "t-fresh")
    _its_turn_completed("t-fresh")
    examined = _counted(monkeypatch)

    turn_queue.reconcile_receipts(skip=lambda queue_id, _queue_seq: queue_id.startswith("waiting"))

    assert examined == ["fresh"]
    assert _status("fresh") == "done"
    assert [_status(f"waiting-{n}") for n in (1, 2, 3)] == ["queued"] * 3


def test_a_page_of_waiting_receipts_is_passed_not_stood_behind(tmp_path, monkeypatch):
    """The cursor moves past a receipt that is skipped. A hundred waiting
    receipts fill a page; were the cursor left where it was, every pass would
    read the same page and the receipt behind it would never be reached."""
    agent = _undecidable_receipt(tmp_path, "waiting-000", "t000")
    for n in range(1, 100):
        _another(agent, f"waiting-{n:03d}", f"t{n:03d}")
    _another(agent, "fresh", "t-fresh")
    _its_turn_completed("t-fresh")
    examined = _counted(monkeypatch)
    waiting = lambda queue_id, _queue_seq: queue_id.startswith("waiting")  # noqa: E731
    # The pass gives itself 200 ms; a loaded machine must not end the page early.
    monkeypatch.setattr(turn_queue.time, "monotonic", lambda: 1000.0)

    turn_queue.reconcile_receipts(skip=waiting)        # the first page: a hundred, all waiting
    assert examined == [] and _status("fresh") == "queued"
    turn_queue.reconcile_receipts(skip=waiting)        # the next page begins behind them

    assert examined == ["fresh"] and _status("fresh") == "done"


def test_a_receipt_that_waits_for_the_queues_own_retry_is_not_told(tmp_path, monkeypatch):
    """``retryable`` is the queue's to retry, not the caller's to wait for: it
    is examined at every pass and never handed to ``on_deferred``."""
    _undecidable_receipt(tmp_path)
    examined, deferred = [], []
    monkeypatch.setattr(turn_queue, "_launch_status",
                        lambda row: examined.append(str(row["queue_id"])) or "retryable")

    for _ in range(3):
        _look(on_deferred=lambda *told: deferred.append(told))

    assert examined == ["q"] * 3 and deferred == []
    assert _status("q") == "queued"


# --- the watcher's waits ------------------------------------------------------

@pytest.fixture
def watcher():
    return BackgroundJobWatcher(stream=None)


def test_a_deferred_receipt_waits_two_minutes_then_twice_as_long_each_time(watcher):
    assert not watcher._receipt_waiting("q", 1, now=0.0)          # never deferred: examined

    due, waits = 0.0, []
    for _ in range(8):
        watcher._receipt_deferred("q", 1, "unknown", now=due)
        wait = watcher._receipt_waits["q", 1][1]
        waits.append(wait)
        assert watcher._receipt_waiting("q", 1, now=due + wait - 0.001)
        assert not watcher._receipt_waiting("q", 1, now=due + wait)
        due += wait

    assert waits == [FIRST, 2 * FIRST, 4 * FIRST, 8 * FIRST, 16 * FIRST, CAP, CAP, CAP]


def test_each_receipt_waits_by_itself(watcher):
    watcher._receipt_deferred("old", 1, "unknown", now=0.0)
    watcher._receipt_deferred("old", 1, "unknown", now=FIRST)
    watcher._receipt_deferred("new", 1, "unknown", now=FIRST)

    assert watcher._receipt_waits["old", 1][1] == 2 * FIRST
    assert watcher._receipt_waits["new", 1][1] == FIRST
    assert not watcher._receipt_waiting("other", 1, now=FIRST)


def test_two_receipts_deferred_in_the_same_passes_both_wait_twice_as_long(watcher):
    """Deferring one receipt does not forget another whose time has just
    passed: it is about to be deferred in this same pass, and would start
    again from two minutes every time."""
    for now in (0.0, FIRST + 1):
        watcher._receipt_deferred("a", 1, "unknown", now=now)
        watcher._receipt_deferred("b", 2, "unknown", now=now)

    assert watcher._receipt_waits["a", 1][1] == 2 * FIRST
    assert watcher._receipt_waits["b", 2][1] == 2 * FIRST


def test_a_new_receipt_under_an_old_queue_id_does_not_inherit_its_wait(watcher):
    """Goal wakes and peer requests have queue ids that come again. The wait
    belongs to the row (its ``queue_seq``), not to the id."""
    due = 0.0
    for _ in range(5):                                 # the old one: waits half an hour by now
        watcher._receipt_deferred("q", 7, "unknown", now=due)
        due += watcher._receipt_waits["q", 7][1]
    assert watcher._receipt_waiting("q", 7, now=due - 1)

    assert not watcher._receipt_waiting("q", 8, now=due - 1)
    watcher._receipt_deferred("q", 8, "unknown", now=due - 1)
    assert watcher._receipt_waits["q", 8][1] == FIRST
    assert watcher._receipt_waits["q", 7][1] == 16 * FIRST


def test_a_receipt_that_is_no_longer_deferred_is_forgotten(watcher):
    watcher._receipt_deferred("settled", 1, "unknown", now=0.0)
    watcher._receipt_deferred("stuck", 1, "unknown", now=0.0)
    late = FIRST + 2 * CAP + 1                         # well past the settled one's time

    watcher._receipt_deferred("stuck", 1, "unknown", now=late)

    assert set(watcher._receipt_waits) == {("stuck", 1)}
    assert not watcher._receipt_waiting("settled", 1, now=late)


def test_the_periodic_pass_hands_its_waits_to_the_reconciliation(watcher, monkeypatch):
    seen = {}
    rounds = iter([False, True])                      # one round, then stop
    monkeypatch.setattr(watcher._stop, "wait", lambda _interval: next(rounds))
    monkeypatch.setattr(watcher, "_tick", lambda: None)
    monkeypatch.setattr(stale_work, "sweep", lambda stream: {})
    monkeypatch.setattr(reconcile, "end_idle_agent_receipts_now",
                        lambda **kwargs: seen.update(kwargs) or {})

    watcher._loop()

    assert seen["skip"] == watcher._receipt_waiting
    assert seen["on_deferred"] == watcher._receipt_deferred


def test_end_to_end_the_stuck_receipt_is_not_read_again_before_its_time(tmp_path, monkeypatch, watcher):
    _undecidable_receipt(tmp_path)
    examined = _counted(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr("lib.background_job_watcher.time.monotonic", lambda: clock[0])

    def one_pass():
        return reconcile.end_idle_agent_receipts(
            {"provider_turns": ["<unknown>"]},
            skip=watcher._receipt_waiting, on_deferred=watcher._receipt_deferred)

    one_pass()                                         # second 0: examined, deferred for two minutes
    assert examined == ["q"]
    for _ in range(3):                                 # seconds 30, 60, 90: wrap, wait, wrap
        clock[0] += 30
        one_pass()
    assert examined == ["q"]
    clock[0] += 29                                     # second 119: not yet
    one_pass()
    one_pass()
    assert examined == ["q"]
    clock[0] += 1                                      # second 120: its time
    one_pass()
    one_pass()
    assert examined == ["q", "q"]
    assert watcher._receipt_waits["q", _row()["queue_seq"]][1] == 2 * FIRST


def test_without_the_waits_the_same_passes_read_it_every_other_time(tmp_path, monkeypatch):
    """What the Host did: the baseline the waits are measured against."""
    _undecidable_receipt(tmp_path)
    examined = _counted(monkeypatch)

    for _ in range(8):
        reconcile.end_idle_agent_receipts({"provider_turns": ["<unknown>"]})

    assert examined == ["q"] * 4


def test_end_to_end_a_new_receipt_under_the_same_queue_id_is_settled_at_the_next_look(
        tmp_path, monkeypatch, watcher):
    """The reviewer's case: a receipt deferred for half an hour is replaced by
    a new one under the same queue id whose turn has ended. Keyed by the id
    alone the new one inherited the wait and was settled 33 minutes late."""
    agent = _undecidable_receipt(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr("lib.background_job_watcher.time.monotonic", lambda: clock[0])

    def look():
        _look(skip=watcher._receipt_waiting, on_deferred=watcher._receipt_deferred)

    for _ in range(31):                                # half an hour of passes a minute apart
        look()
        clock[0] += 60
    old = _row()["queue_seq"]
    assert watcher._receipt_waits["q", old][1] > 15 * 60 and watcher._receipt_waiting("q", old)

    db.conn().execute("DELETE FROM queued_turns WHERE queue_id='q'")
    db.conn().commit()
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="t-again", synthesize_audio=False)
    turn_queue.enqueue(queue_id="q", agent_id=agent, session="mike", text="again",
                       trace_id="t-again", client_msg_id="q", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    _its_turn_completed("t-again")
    assert _row()["queue_seq"] != old

    look()

    assert _status("q") == "done"
