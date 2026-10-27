"""G-H rollback residue (d98 -> 17979 -> d98): the bounded pointer reconcile pass.

The 17979 writer is the EXACT 17979f7b server/lib/launch_receipts.py (git blob
4b0b07bf, tests/fixtures/legacy_17979_launch_receipts.py), loaded against this
tree's settings_store, whose functions it calls are byte-identical in both
versions. Isolated DB per test (conftest); a fake millisecond clock so every
write has its own stamp. Predeclared cases (GH-REPAIR-EXPECTATIONS.md):
A valid pointers kept + concurrent writer ordering, B legacy full-rewrite
orphan, C legacy pruned orphan, D stale-rev state change -> cursor reset,
E watermark monotonic + cursor reset + bounded work, F round trip without
retry or state erasure, G maintenance wiring."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import threading
import time

import pytest

from lib import db, maintenance, settings_store, turn_queue
from lib import launch_receipts as lr

LEGACY_PATH = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "legacy_17979_launch_receipts.py"
DAY = 24 * 3600 * 1000


def _load_legacy():
    spec = importlib.util.spec_from_file_location("lib._legacy_17979_launch_receipts", LEGACY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Clock:
    def __init__(self, start=1_800_000_000_000):
        self.t = start

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    for module in (db, settings_store, lr):
        monkeypatch.setattr(module, "now_ms", c)
    return c


@pytest.fixture
def legacy(clock, monkeypatch):
    module = _load_legacy()
    monkeypatch.setattr(module, "now_ms", clock)
    module.reset_for_tests()
    module._LAST_PRUNE = time.monotonic()   # 17979's own prune interval: off unless a test resets it to 0
    return module


def _pending(trace, agent="ag", cid=None):
    lr.record_pending(agent_id=agent, trace_id=trace, client_msg_id=cid or "c-" + trace, origin="user",
                      user_stop_generation=turn_queue.user_stop_generation(agent))


def _legacy_pending(legacy, trace, agent="ag", cid=None):
    legacy.record_pending(agent_id=agent, trace_id=trace, client_msg_id=cid or "c-" + trace, origin="user",
                          user_stop_generation=turn_queue.user_stop_generation(agent))


def _rows(low, high):
    """(key, value, updated_at) by direct SQL: the baseline tree has no stamped helper (RED run)."""
    return [(r["key"], r["value"], r["updated_at"]) for r in db.conn().execute(
        "SELECT key, value, updated_at FROM settings WHERE key >= ? AND key < ? ORDER BY key", (low, high))]


def _pointers(agent="ag"):
    return _rows(lr.POINTER_PREFIX + agent + ":", lr.POINTER_PREFIX + agent + ";")


def _receipt_rows():
    return _rows(lr.PREFIX, lr.PREFIX[:-1] + chr(ord(lr.PREFIX[-1]) + 1))


def _gen(agent="ag"):
    return turn_queue.user_stop_generation(agent)


def _normal(since, agent="ag"):
    return lr.delivery_page(agent, since=since, stop_generation=_gen(agent))


def _bootstrap(agent="ag"):
    page = lr.delivery_page(agent, since=0, bootstrap=True)
    shown = {u["delivery"]["for"]["trace_id"]: u["delivery"]["state"] for u in page["updates"]}
    while page["more"]:
        page = lr.delivery_page(agent, since=page["cursor"], until=page["until"],
                                stop_generation=page["stop_generation"], bootstrap=True)
        shown.update({u["delivery"]["for"]["trace_id"]: u["delivery"]["state"] for u in page["updates"]})
    return shown, page["cursor"]


def _counters():
    return settings_store.get_int(lr.SEQ_KEY), settings_store.get_int(lr.PRUNE_SEQ_KEY)


def _status_all(agent="ag"):
    return {key[len(lr.PREFIX):]: lr.status(agent, key[len(lr.PREFIX):]) for key, _v, _a in _receipt_rows()}


def _maintenance(now):
    return maintenance.prune_database(now_ms=now)


# --- A: valid pointers are never touched; concurrent writer ordering -------------------------

def test_a1_pure_d98_history_is_left_exactly_as_it_was(clock):
    for trace in ("t1", "t2", "t3", "t4"):
        _pending(trace)
    lr.mark_attempted("t1")
    lr.fence_agent("ag")                                  # t2, t3, t4 -> fenced
    lr.record_pending(agent_id="ag", trace_id="t5", client_msg_id="c-t5", origin="user",
                      supersedes="t4", user_stop_generation=_gen())
    before = (_pointers(), _receipt_rows(), _counters())
    done = lr.reconcile()
    assert (done["orphans"], done["stale"], done["reset_to"]) == (0, 0, 0)
    assert done["read"] == len(before[0]) == 5
    assert (_pointers(), _receipt_rows(), _counters()) == before


def test_a2_reconcile_waits_for_a_concurrent_admission_and_keeps_its_pointer(clock):
    """The admission holds the writer lock (its own thread = its own connection);
    the pass cannot interleave, and afterwards the committed pointer is valid."""
    _pending("t0")
    inside, release, finished = threading.Event(), threading.Event(), {}

    def admission():
        with settings_store.transaction():
            _pending("t1")
            inside.set()
            release.wait(10)
        db.close_local()

    def reconcile():
        finished["done"] = lr.reconcile()
        finished["pointers"] = [k for k, _t, _a in _pointers()]
        db.close_local()

    a = threading.Thread(target=admission)
    a.start()
    assert inside.wait(10)
    r = threading.Thread(target=reconcile)
    r.start()
    r.join(0.5)
    assert r.is_alive(), "the pass must wait for the admission's writer lock"
    release.set()
    a.join(10)
    r.join(10)
    assert not r.is_alive()
    assert (finished["done"]["orphans"], finished["done"]["stale"]) == (0, 0)
    assert finished["pointers"] == [lr.pointer_key("ag", 1), lr.pointer_key("ag", 2)]


def test_a3_an_admission_that_waits_for_the_pass_is_committed_whole_and_kept(clock, monkeypatch):
    _pending("t0")
    inside, release = threading.Event(), threading.Event()
    real = settings_store.get_many_stamped

    def paused(keys):
        inside.set()
        release.wait(10)
        return real(keys)
    monkeypatch.setattr(settings_store, "get_many_stamped", paused)
    result = {}

    def reconcile():
        result["done"] = lr.reconcile()
        db.close_local()

    r = threading.Thread(target=reconcile)
    r.start()
    assert inside.wait(10)
    admitted = {}

    def admission():
        _pending("t1")
        admitted["ok"] = True
        db.close_local()
    a = threading.Thread(target=admission)
    a.start()
    a.join(0.5)
    assert a.is_alive(), "the admission must wait for the pass's writer lock"
    release.set()
    r.join(10)
    a.join(10)
    monkeypatch.setattr(settings_store, "get_many_stamped", real)
    assert admitted == {"ok": True}
    assert (result["done"]["orphans"], result["done"]["stale"]) == (0, 0)
    done = lr.reconcile()
    assert (done["orphans"], done["stale"]) == (0, 0)
    assert [k for k, _t, _a in _pointers()] == [lr.pointer_key("ag", 1), lr.pointer_key("ag", 2)]


# --- B: legacy full rewrite (17979 same-trace re-admission) -----------------------------------

def test_b_legacy_full_rewrite_orphan_is_removed_and_every_cursor_expires(clock, legacy):
    _pending("t1")
    _pending("t2")
    shown, cursor = _bootstrap()
    assert shown == {"t1": "pending_launch", "t2": "pending_launch"} and cursor == 2
    _legacy_pending(legacy, "t1")                         # 17979: legacy shape, rev/seq dropped
    body = lr.get("t1")
    assert "seq" not in body and "rev" not in body
    assert _normal(cursor)["expired"] is False, "baseline: nothing expires the client's cursor"
    _maintenance(clock.t)
    assert [k for k, _t, _a in _pointers()] == [lr.pointer_key("ag", 2)], "the orphan pointer is gone"
    assert lr.get("t1") == body, "the receipt body is untouched"
    seq, watermark = _counters()
    assert (seq, watermark) == (3, 3)
    assert _normal(cursor)["expired"] is True, "a cursor that could hold t1's old state expires"
    shown, cursor = _bootstrap()
    assert shown == {"t2": "pending_launch"}, "t1 is unknown now, never a guessed state"
    assert _normal(cursor)["expired"] is False


# --- C: legacy prune of a d98 receipt -----------------------------------------------------

def test_c_legacy_pruned_orphan_is_removed_and_the_watermark_moves(clock, legacy):
    _pending("old")
    _, cursor = _bootstrap()
    clock.t += 8 * DAY
    legacy._LAST_PRUNE = 0.0
    _legacy_pending(legacy, "new")                        # 17979 record_pending runs its own prune
    assert lr.get("old") is None and lr.get("new") is not None
    assert [k for k, _t, _a in _pointers()] == [lr.pointer_key("ag", 1)], "17979 left the pointer"
    assert _counters() == (1, 0), "17979 moved no counter"
    assert _normal(cursor)["expired"] is False
    _maintenance(clock.t)
    assert _pointers() == []
    assert _counters() == (2, 2)
    assert _normal(cursor)["expired"] is True
    assert lr.get("new") is not None and "seq" not in lr.get("new"), "the 17979 receipt is kept as it is"


# --- D: stale rev (17979 state-only change) -------------------------------------------------

def test_d_stale_rev_change_is_a_lost_update_until_the_pass_then_the_cursor_resets(clock, legacy):
    _pending("t1")
    shown, cursor = _bootstrap()
    assert shown == {"t1": "pending_launch"}
    assert legacy.mark_attempted("t1") == "attempted"     # 17979: json_set $.state, updated_at moves
    body = lr.get("t1")
    assert (body["state"], body["rev"], body["seq"]) == ("attempted", 1, 1), "rev / seq stale"
    page = _normal(cursor)
    assert page["expired"] is False and page["updates"] == [], "baseline: the update is lost to this cursor"
    _maintenance(clock.t)
    assert [k for k, _t, _a in _pointers()] == [lr.pointer_key("ag", 1)], "a stale-rev pointer is kept"
    assert lr.get("t1") == body, "the receipt body is untouched"
    assert _normal(cursor)["expired"] is True
    shown, cursor = _bootstrap()
    assert shown == {"t1": "handed_over"}, "re-projection shows the true current state"
    again = lr.reconcile()
    assert (again["orphans"], again["stale"], again["reset_to"]) == (0, 0, 0), "reported once"
    assert _normal(cursor)["expired"] is False


# --- E: watermark monotonic, cursor resumes, bounded work ------------------------------------

def test_e1_bounded_passes_cover_everything_with_a_resumable_cursor(clock, legacy):
    for i in range(1203):
        _pending(f"t{i:05d}")
    orphaned = [f"t{i:05d}" for i in (0, 499, 500, 777, 1000, 1202)]
    for trace in orphaned:
        _legacy_pending(legacy, trace)
    seen, passes, last = 0, 0, _counters()
    while True:
        statements: list[str] = []
        db.conn().set_trace_callback(statements.append)
        try:
            done = lr.reconcile()
        finally:
            db.conn().set_trace_callback(None)
        passes += 1
        assert done["read"] <= lr.RECONCILE_LIMIT
        assert sum(1 for s in statements if s.lstrip().startswith("SELECT key, value, updated_at FROM settings WHERE key >=")) == 1, \
            "exactly one pointer range read per pass"
        assert len(statements) <= 2 * lr.RECONCILE_LIMIT + 16, "bounded statements per pass"
        now = _counters()
        assert now[0] >= last[0] and now[1] >= last[1], "no counter goes back"
        last = now
        seen += done["orphans"]
        if done["wrapped"]:
            break
        assert passes < 10
    assert passes == 3 and seen == len(orphaned)
    assert len(_pointers()) == 1203 - len(orphaned)


def test_e2_the_watermark_never_moves_down(clock, legacy):
    _pending("t1")
    _legacy_pending(legacy, "t1")
    settings_store.set_int(lr.PRUNE_SEQ_KEY, 10**9)
    done = lr.reconcile()
    assert done["orphans"] == 1
    assert settings_store.get_int(lr.PRUNE_SEQ_KEY) == 10**9
    assert settings_store.get_int(lr.SEQ_KEY) == 2


def test_e3_a_reset_expires_old_cursors_and_a_fresh_bootstrap_cursor_stays_valid(clock, legacy):
    _pending("t1")
    _pending("t2")
    _, old = _bootstrap()
    legacy.mark_attempted("t2")
    lr.reconcile()
    assert _normal(old)["expired"] is True
    _, fresh = _bootstrap()
    assert fresh == settings_store.get_int(lr.SEQ_KEY) == settings_store.get_int(lr.PRUNE_SEQ_KEY)
    assert _normal(fresh)["expired"] is False
    _pending("t3")
    page = _normal(fresh)
    assert [u["delivery"]["for"]["trace_id"] for u in page["updates"]] == ["t3"]


# --- F: the round trip: no retry, no state erasure ----------------------------------------------

def test_f_round_trip_keeps_every_receipt_state_and_retry_answer(clock, legacy, monkeypatch):
    _pending("old")
    clock.t += 8 * DAY
    for trace, agent in (("p1", "ag"), ("p2", "ag"), ("p3", "ag"), ("q1", "bg")):
        _pending(trace, agent=agent)
    lr.mark_attempted("p3")
    # 17979 period: its prune (record_pending), state-only writes, a full rewrite, a new admission
    legacy._LAST_PRUNE = 0.0
    _legacy_pending(legacy, "n1")                         # 17979 prune deletes "old", keeps its pointer
    legacy.mark_attempted("p1")                           # stale rev
    legacy.fence_agent("bg")                              # stale rev (another agent)
    _legacy_pending(legacy, "p2")                         # full rewrite -> orphan
    assert lr.get("old") is None
    after_legacy = {k: v for k, v, _a in _receipt_rows()}
    answers = {agent: _status_all(agent) for agent in ("ag", "bg")}
    gens = (_gen("ag"), _gen("bg"))
    calls = []
    monkeypatch.setattr(lr, "mark_attempted", lambda *a, **k: calls.append(a))
    for _ in range(3):
        _maintenance(clock.t)
    assert calls == [], "the pass never retries or hands anything over"
    rows = {k: v for k, v, _a in _receipt_rows()}
    assert rows == after_legacy, "no receipt deleted or changed: every body exactly as 17979 left it"
    assert {agent: _status_all(agent) for agent in ("ag", "bg")} == answers, "retry answers unchanged"
    assert (_gen("ag"), _gen("bg")) == gens, "Stop generations untouched"
    remaining = []
    for agent in ("ag", "bg"):
        for key, trace, at in _pointers(agent):
            receipt_at = {k: a for k, _v, a in _receipt_rows()}.get(lr.PREFIX + trace)
            receipt = lr.get(trace)
            assert receipt is not None and receipt["seq"] == lr._pointer_seq(key) and receipt_at == at, \
                f"pointer {key} -> {trace} is not valid (orphan or stale)"
            remaining.append(trace)
    assert sorted(remaining) == ["p1", "p3", "q1"], "valid pointers kept, orphans (old, p2) gone"


# --- G: maintenance wiring ----------------------------------------------------------------

def test_g_a_reconcile_failure_is_logged_and_never_fatal(clock, monkeypatch):
    _pending("t1")
    logged = []
    monkeypatch.setattr(lr, "reconcile", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    import lib.log as log_module
    monkeypatch.setattr(log_module, "log_exception", lambda name, e: logged.append(name))
    counts = _maintenance(clock.t)
    assert counts["launch_receipt_pointers"] == 0 and logged == ["launchReceiptReconcileFail"]
    assert lr.get("t1") is not None
