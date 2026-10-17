"""Deterministic controls for two interleavings that main's push CI hit (run 39024846048).

Test helpers only (monkeypatched module attributes and threading Events); no production seam.
- R2: a poll that read "pending" before the worker published, and writes after, must not put a
  published key back in the queue (no extra job, run, cache or ledger mutation).
- R1: the crashed-worker recovery scenario with a slow first admission attempt. The synthetic clock
  may only advance once that blocked attempt is confirmed abandoned; the outcome is read from the
  owned job, run and cache rows, not inferred from a polling timeout.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

from lib import backends, db, janitors, tool_explanations

ITEM = {"id": "row", "demand_id": "view", "activity": {"command": "ls"}}


@pytest.fixture(autouse=True)
def no_agent_processes(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])


def translate(level, items):
    return {item["id"]: "List the files." for item in items}


def wait_for(check, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(.01)


def _one(sql, *args):
    return db.conn().execute(sql, args).fetchone()


# ---- R2: the poll that straddles publication ----------------------------------------------------

def test_a_poll_straddling_publication_does_not_requeue_the_published_key(monkeypatch):
    queue = tool_explanations.durable_queue
    key = "k" * 64
    prepared = [("row", key, {"activity": {"command": "ls"}, "janitor": None}, "view")]
    # Setup positives: the key is queued, then owned by the worker.
    assert queue.request(3, prepared, [], 0) == [{"id": "row", "status": "pending"}]
    [claimed] = queue.claim("worker")
    assert claimed[0] == key
    assert _one("SELECT status, owner FROM tool_explanation_jobs WHERE cache_key=?", key) == ("running", "worker")

    real_transaction = queue.transaction
    poll_threads, at_lock, proceed = set(), threading.Event(), threading.Event()

    @contextmanager
    def gated():
        # Only the poll's write transaction waits; its reads already happened.
        if threading.get_ident() in poll_threads:
            at_lock.set()
            assert proceed.wait(5), "the test never released the poll"
        with real_transaction() as connection:
            yield connection
    monkeypatch.setattr(queue, "transaction", gated)
    reply = {}

    def poll():
        poll_threads.add(threading.get_ident())
        reply["value"] = queue.request(3, prepared, [], 0)
    thread = threading.Thread(target=poll)
    thread.start()
    assert at_lock.wait(5), "the poll did not reach its write transaction"
    # The worker publishes while the poll holds its stale read.
    assert queue.complete("worker", [(key, {"status": "ready", "text": "List the files.", "source": "llm"})], 60)
    published = _one("SELECT explanation, created_at, expires_at FROM tool_explanation_cache WHERE cache_key=?", key)
    assert published is not None and _one("SELECT 1 FROM tool_explanation_jobs WHERE cache_key=?", key) is None
    proceed.set()
    thread.join(5)
    assert not thread.is_alive()

    # Reply semantics: the poll answers what it read before publication.
    assert reply["value"] == [{"id": "row", "status": "pending"}]
    # The invariant: no job came back for the published key, so nothing can run again ...
    assert _one("SELECT count(*) FROM tool_explanation_jobs WHERE cache_key=?", key)[0] == 0
    assert queue.claim("next-worker") == []
    # ... and the published answer is untouched; the viewer's demand bookkeeping is kept.
    assert _one("SELECT explanation, created_at, expires_at FROM tool_explanation_cache WHERE cache_key=?", key) == published
    assert _one("SELECT 1 FROM tool_explanation_demands WHERE cache_key=? AND demand_id='view'", key) is not None
    assert queue.request(3, prepared, [], 0)[0]["status"] == "ready"


# ---- R1: crashed-worker recovery with a slow first admission ------------------------------------

def _seed():
    return tool_explanations.janitor_builtins.ensure_builtins(cwd="/tmp", initial={"tool-explainer": {}})["tool-explainer"]


def test_recovery_survives_a_slow_first_admission_attempt(monkeypatch):
    config = _seed()
    builtin = tool_explanations.janitor_builtins
    queue = tool_explanations.durable_queue
    clock = [db.now_ms()]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    monkeypatch.setattr(queue.cache, "now_ms", lambda: clock[0])
    with tool_explanations.ToolExplanations(translate=translate, debounce=5) as stopped:
        stopped.request(3, [{"id": "row", "activity": ITEM["activity"]}])
    clock[0] += 5001
    assert queue.claim("dead-worker")
    orphan = builtin.begin_run("tool-explainer", "dead-invocation")
    assert builtin.claim_run(orphan["run_id"])

    real_begin = builtin.begin_run
    attempts, entered, release = [], threading.Event(), threading.Event()

    def slow_first_admission(*args, **kwargs):
        # A loaded runner: the replacement's first admission attempt is slow (bounded).
        if not attempts:
            attempts.append(1)
            entered.set()
            release.wait(5)
        return real_begin(*args, **kwargs)
    monkeypatch.setattr(builtin, "begin_run", slow_first_admission)
    calls = []

    def capture(level, items):
        calls.append(1)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=0) as replacement:
        clock[0] += 60001
        replacement.request(3, [ITEM])
        assert entered.wait(5), "the replacement never claimed the expired lease"
        # SCHEDULE UNDER TEST (the original test's order): advance the clock while that first
        # attempt is still in flight, then let it continue.
        clock[0] += builtin.DEMAND_RUN_TTL_MS
        release.set()

        def settled():
            published = _one("SELECT count(*) FROM tool_explanation_cache")[0]
            runs = [r for r in janitors.list_runs(config["session"]) if r["run_id"] != orphan["run_id"]]
            running_job = _one("SELECT 1 FROM tool_explanation_jobs WHERE status='running'")
            return (published, runs) if (published or (runs and not running_job)) else None
        published, runs = wait_for(settled, what="the replacement's batch to settle")
        assert published == 1, ("the batch was not published: its admission straddled the clock advance, so its "
                                f"queue lease had already expired (runs: {[(r['run_id'], r['status']) for r in runs]})")
        assert [r["status"] for r in runs] == ["completed"]
        assert _one("SELECT count(*) FROM tool_explanation_jobs WHERE status IN ('queued','running')")[0] == 0
    assert calls == [1]
    assert janitors.get_run(orphan["run_id"])["status"] == "cancelled"
