"""G0 for the queue receipt conversion (docs/architecture/lifecycle-conversion-plan.md,
section 1): today's transitions of queued_turns.status, written against the old
code before any conversion.

Every writer in lib/turn_queue.py is called on a target row q1 (agent a-1,
trace t-1) in each status, at a fixed clock, next to three rows that pin its
scope: q2 (same agent, other trace, same status), q3 (other agent, same trace,
same status) and q4 (same agent, other trace, queued: the order neighbour);
q2 and q3 carry allow_paused = 1. The matrix records what it returned, every
row afterwards (order, agent, trace, status, text, started_at, claimed_at,
allow_paused), and per agent the queue revision, the paused flag and both
stop generations. The
sweeps (stale claims, ended and idle receipts, settlement) have their own
cases. The conversion must reproduce the matrix byte for byte; a change is a
registered intended difference. Regenerate only for such a change, with
UPDATE_LIFECYCLE_GOLDENS=1, and review the diff.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from lib import db, turn_queue

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "lifecycles" / "queue_receipt_matrix.json"
NOW = 50_000_000
STATUSES = ("queued", "claimed", "parked", "started", "done", "failed", "interrupted",
            "ended", "unknown", "cancelled", "withdrawn")
AGENT, TRACE, OTHER_AGENT, OTHER_TRACE = "a-1", "t-1", "a-2", "t-2"
NEW = dict(agent_id=AGENT, session="s", text="new", trace_id="t-9", client_msg_id="c-9",
           synthesize_audio=True, origin="user", sender_agent_id="")

WRITERS = {
    "enqueue[new]": lambda: turn_queue.enqueue(queue_id="q9", **NEW),
    "enqueue[same id]": lambda: turn_queue.enqueue(queue_id="q1", **NEW),
    "park[new]": lambda: turn_queue.park(queue_id="q9", **NEW),
    "park[same id]": lambda: turn_queue.park(queue_id="q1", **NEW),
    "park_at_head[new]": lambda: turn_queue.park_at_head(queue_id="q9", **NEW),
    "park_at_head[same id]": lambda: turn_queue.park_at_head(queue_id="q1", **NEW),
    "allow_paused": lambda: turn_queue.allow_paused("q1"),
    "set_paused[stop]": lambda: turn_queue.set_paused(AGENT, True),
    "set_paused[user stop]": lambda: turn_queue.set_paused(AGENT, True, user_stop=True),
    "set_paused[resume]": (lambda: turn_queue.set_paused(AGENT, True),
                           lambda: turn_queue.set_paused(AGENT, False)),
    "set_paused[resume when running]": lambda: turn_queue.set_paused(AGENT, False),
    "take_over_at_head": lambda: turn_queue.take_over_at_head(
        "q1", agent_id=AGENT, trace_id=TRACE, prompt_admission_id=""),
    "remove_taken_over": lambda: turn_queue.remove_taken_over("q1"),
    "requeue_rows": lambda: turn_queue.requeue_rows(["q1"]),
    "move_to_back": lambda: turn_queue.move_to_back("q1"),
    "unpark": lambda: turn_queue.unpark("q1"),
    "discard_parked": lambda: turn_queue.discard_parked(AGENT),
    "requeue_parked": lambda: turn_queue.requeue_parked(AGENT),
    "mark_launched_call_open_unknown": lambda: turn_queue.mark_launched_call_open_unknown("q1"),
    "mark_started": lambda: turn_queue.mark_started("q1"),
    "remove": lambda: turn_queue.remove("q1"),
    "cancel": lambda: turn_queue.cancel("q1"),
    "update_text": lambda: turn_queue.update_text("q1", "edited"),
    "withdraw_unclaimed": lambda: turn_queue.withdraw_unclaimed("q1"),
    "claim_for_dispatch": lambda: turn_queue.claim_for_dispatch("q1"),
    "mark_unknown": lambda: turn_queue.mark_unknown("q1"),
    "withdraw_claimed": lambda: turn_queue.withdraw_claimed("q1"),
    "allow_paused_for": lambda: turn_queue.allow_paused_for("q1"),
    "claim": lambda: turn_queue.claim("q1"),
    "release_claim": lambda: turn_queue.release_claim("q1"),
    "remove_for_agent": lambda: turn_queue.remove_for_agent(AGENT),
    **{f"settle_receipt[{o}]": (lambda o=o: turn_queue.settle_receipt(AGENT, TRACE, o))
       for o in ("completed", "failed", "interrupted", "other")},
    "cancel_for_traces": lambda: _in_transaction(lambda c: turn_queue.cancel_for_traces(c, AGENT, [TRACE])),
    "reset_stale_claims": lambda: turn_queue.reset_stale_claims(30_000),
    "reset_stale_claims[exclude]": lambda: turn_queue.reset_stale_claims(30_000, exclude=("q1",)),
}


def _in_transaction(fn):
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        result = fn(con)
        con.execute("COMMIT")
        return result
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _insert(queue_id: str, status: str, *, at: int, trace: str = TRACE, agent: str = AGENT,
            allow_paused: int = 0) -> None:
    db.conn().execute(
        "INSERT INTO queued_turns (queue_id, agent_id, session, text, trace_id, client_msg_id,"
        " status, enqueued_at, started_at, claimed_at, allow_paused)"
        " VALUES (?, ?, 's', 'hello', ?, ?, ?, ?, ?, ?, ?)",
        (queue_id, agent, trace, queue_id, status, at,
         at if status not in ("queued", "parked", "claimed") else None,
         at if status == "claimed" else None, allow_paused))


def _rows() -> list[list]:
    return [list(r) for r in db.conn().execute(
        "SELECT queue_id, agent_id, trace_id, status, text, started_at, claimed_at, allow_paused"
        " FROM queued_turns ORDER BY queue_seq")]


def _queue_state(agent: str) -> list:
    return [turn_queue.revision(agent), turn_queue.is_paused(agent),
            turn_queue.stop_generation(agent), turn_queue.user_stop_generation(agent)]


def _value(result):
    if isinstance(result, dict):  # queue_seq is an autoincrement: it says nothing about the transition
        return {k: (v if isinstance(v, (int, str, bool, type(None))) else repr(v))
                for k, v in sorted(result.items()) if k != "queue_seq"}
    return result if isinstance(result, (int, str, bool, type(None))) else type(result).__name__


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(db, "now_ms", lambda: NOW)


def run(name: str, status: str) -> dict:
    """Each cell starts from a fresh store (the isolated database is per test,
    so rows, revisions and generations are cleared by hand)."""
    con = db.conn()
    for table in ("queued_turns", "queue_state_revisions"):
        con.execute(f"DELETE FROM {table}")
    con.execute("DELETE FROM settings WHERE key LIKE 'turn_queue.%'")
    _insert("q1", status, at=NOW - 60_000)       # a minute old: past the 30 s claim age
    _insert("q2", status, at=NOW - 50_000, trace=OTHER_TRACE, allow_paused=1)
    _insert("q3", status, at=NOW - 40_000, agent=OTHER_AGENT, allow_paused=1)
    _insert("q4", "queued", at=NOW - 30_000, trace=OTHER_TRACE)
    writer = WRITERS[name]
    if isinstance(writer, tuple):
        setup, writer = writer
        setup()
    before = {agent: _queue_state(agent) for agent in (AGENT, OTHER_AGENT)}
    try:
        returned, error = _value(writer()), None
    except Exception as exc:  # recorded, not swallowed: the refusal is part of the matrix
        returned, error = None, type(exc).__name__
    return {"returned": returned, "error": error, "rows": _rows(),
            "queue_state": {agent: [before[agent], _queue_state(agent)] for agent in (AGENT, OTHER_AGENT)}}


def matrix() -> dict:
    return {name: {status: run(name, status) for status in STATUSES} for name in WRITERS}


def test_queue_writers_match_the_matrix(clock):
    actual = matrix()
    if os.environ.get("UPDATE_LIFECYCLE_GOLDENS") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    expected = json.loads(GOLDEN.read_text())
    changed = sorted(f"{n}/{s}" for n in set(expected) | set(actual)
                     for s in STATUSES if expected.get(n, {}).get(s) != actual.get(n, {}).get(s))
    assert not changed, (f"queue transitions changed for {changed}; first: expected "
                         f"{expected.get(changed[0].split('/')[0], {}).get(changed[0].split('/')[1])}, got "
                         f"{actual.get(changed[0].split('/')[0], {}).get(changed[0].split('/')[1])}")


def test_the_matrix_shows_the_known_rules(clock):
    """Spot checks that read as the plan's facts, so the golden cannot hide them."""
    assert run("claim", "queued")["rows"][0][3] == "claimed"
    assert run("claim", "started")["rows"][0][3] == "started"
    assert run("reset_stale_claims", "claimed")["rows"][0][3] == "queued"
    assert run("reset_stale_claims[exclude]", "claimed")["rows"][0][3] == "claimed"
    # The newest settlement wins: a failed receipt of the same trace becomes done.
    assert run("settle_receipt[completed]", "failed")["rows"][0][3] == "done"
    assert run("settle_receipt[completed]", "cancelled")["rows"][0][3] == "cancelled"
    # Scope: only this agent's rows of this trace; a Stop clears only this agent's rights.
    cancelled = run("cancel_for_traces", "queued")["rows"]
    assert [r[3] for r in cancelled] == ["cancelled", "queued", "queued", "queued"]
    settled = run("settle_receipt[completed]", "started")["rows"]
    assert [r[3] for r in settled] == ["done", "started", "started", "queued"]
    stopped = run("set_paused[stop]", "queued")["rows"]
    assert [r[7] for r in stopped] == [0, 0, 1, 0]
    assert run("allow_paused", "queued")["returned"] is True
    assert run("allow_paused", "started")["returned"] is False


# --- the idle sweep: every gate and both timing branches (plan, section 1) ----
# States and liveness are per agent; a-2's rows pin that the sweep reads a-1's own.

def _state(agent: str, kind: str, ts: int) -> None:
    db.conn().execute("INSERT INTO state_log (agent_id, kind, ts) VALUES (?, ?, ?)", (agent, kind, ts))


@pytest.mark.parametrize("case, states, receipt_age, live, ended", [
    ("terminal newer and 60 s old", [(AGENT, "idle", NOW - 60_000)], 120_000, set(), True),
    ("terminal newer but only 59 s old", [(AGENT, "idle", NOW - 59_000)], 120_000, set(), False),
    ("terminal older than the receipt", [(AGENT, "idle", NOW - 180_000)], 120_000, set(), False),
    ("busy newest state", [(AGENT, "thinking", NOW - 60_000)], 25 * 3_600_000, set(), False),
    ("no state, receipt 24 h old", [], 24 * 3_600_000, set(), True),
    ("no state, receipt under 24 h", [], 24 * 3_600_000 - 1, set(), False),
    ("terminal newer but the agent is live", [(AGENT, "idle", NOW - 60_000)], 120_000, {AGENT}, False),
    ("liveness check raises", [(AGENT, "idle", NOW - 60_000)], 120_000, "raise", False),
    ("busy, while another agent has a newer terminal state",
     [(AGENT, "thinking", NOW - 100_000), (OTHER_AGENT, "idle", NOW - 60_000)], 120_000, set(), False),
    ("another agent is live, this one is not", [(AGENT, "idle", NOW - 60_000)], 120_000, {OTHER_AGENT}, True),
    ("no own state under 24 h, another agent's terminal state is newer",
     [(OTHER_AGENT, "idle", NOW - 60_000)], 120_000, set(), False),
])
def test_the_idle_sweep_keeps_its_gates(clock, case, states, receipt_age, live, ended):
    db.conn().execute("DELETE FROM queued_turns")
    _insert("q1", "started", at=NOW - receipt_age)
    for state in states:
        _state(*state)

    def is_live(agent_id):
        if live == "raise":
            raise RuntimeError("inspection failed")
        return agent_id in live
    result = turn_queue.terminalize_idle_agent_receipts(is_live, now_ms=NOW)
    assert (turn_queue.status("q1") == "ended") is ended, (case, result)


# --- the startup sweep of ended receipts ------------------------------------

def _turn(agent: str, trace: str, *, started: int, outcome: str | None) -> None:
    db.conn().execute(
        "INSERT INTO turns (agent_id, source, trace_id, started_at, settled_at, outcome) VALUES (?, 'user', ?, ?, ?, ?)",
        (agent, trace, started, started + 1 if outcome else None, outcome))


@pytest.mark.parametrize("case, turns, live, expected", [
    ("its turn completed", [(AGENT, TRACE, 0, "completed")], set(), "done"),
    ("its turn failed", [(AGENT, TRACE, 0, "failed")], set(), "failed"),
    ("its turn was interrupted", [(AGENT, TRACE, 0, "interrupted")], set(), "interrupted"),
    ("its turn settled with another outcome", [(AGENT, TRACE, 0, "odd")], set(), "ended"),
    ("a later turn of the agent began", [(AGENT, "t-later", NOW, None)], set(), "ended"),
    ("an earlier turn of the agent only", [(AGENT, "t-earlier", 0, "completed")], set(), "started"),
    ("no signal", [], set(), "started"),
    ("its trace holds a slot", [(AGENT, TRACE, 0, "completed")], {TRACE}, "started"),
    ("another agent's turn of the same trace settled", [(OTHER_AGENT, TRACE, 0, "completed")], set(), "started"),
    ("another agent's later turn began", [(OTHER_AGENT, "t-later", NOW, None)], set(), "started"),
])
def test_the_ended_receipt_sweep(clock, case, turns, live, expected):
    db.conn().execute("DELETE FROM queued_turns")
    _insert("q1", "started", at=NOW - 60_000)
    for agent, trace, started, outcome in turns:
        _turn(agent, trace, started=started, outcome=outcome)
    turn_queue.terminalize_ended_receipts(live)
    assert turn_queue.status("q1") == expected, case


# --- a launched listen-first call's row (call-open: ids only) ---------------

@pytest.mark.parametrize("status", STATUSES)
def test_a_failed_call_open_launch_becomes_unknown_only_from_claimed_or_started(clock, status):
    """mark_launched_call_open_unknown acts only on a call-open: row that is
    claimed or started (its one launch): the row becomes unknown with its text
    cleared. Every other status, and an ordinary id, is untouched."""
    db.conn().execute("DELETE FROM queued_turns")
    _insert("call-open:r1", status, at=NOW - 60_000)
    _insert("q2", status, at=NOW - 50_000)
    hit = turn_queue.mark_launched_call_open_unknown("call-open:r1")
    ordinary = turn_queue.mark_launched_call_open_unknown("q2")
    acts = status in ("claimed", "started")
    assert (hit, ordinary) == (acts, False)
    assert _rows() == [
        ["call-open:r1", AGENT, TRACE, "unknown" if acts else status, "" if acts else "hello",
         NOW - 60_000 if status not in ("queued", "parked", "claimed") else None,
         NOW - 60_000 if status == "claimed" else None, 0],
        ["q2", AGENT, TRACE, status, "hello",
         NOW - 50_000 if status not in ("queued", "parked", "claimed") else None,
         NOW - 50_000 if status == "claimed" else None, 0]]


def test_the_claim_reset_treats_a_missing_claim_time_as_stale(clock):
    """Review A3 (queue design delta F2): claimed_at is nullable, and the reset
    compares COALESCE(claimed_at, 0), so a claimed row without a claim time is
    always stale; an exclusion still keeps it."""
    db.conn().execute("DELETE FROM queued_turns")
    _insert("q1", "queued", at=NOW - 60_000)
    _insert("q2", "queued", at=NOW - 50_000)
    db.conn().execute("UPDATE queued_turns SET status = 'claimed', claimed_at = NULL")
    assert turn_queue.reset_stale_claims(30_000, exclude=("q2",)) == 1
    assert [(r[0], r[3], r[6]) for r in _rows()] == [("q1", "queued", None), ("q2", "claimed", None)]


@pytest.mark.parametrize("status", ["queued", "parked"])
def test_withdraw_unclaimed_keeps_a_waiting_row_with_a_claim_time(clock, status):
    """A claim stamp fences withdrawal even while the row is queued or parked.
    The ordinary unstamped neighbour can still be withdrawn."""
    _insert("q1", status, at=NOW - 60_000)
    _insert("q2", status, at=NOW - 50_000)
    db.conn().execute("UPDATE queued_turns SET claimed_at = ? WHERE queue_id = 'q1'",
                      (NOW - 60_000,))
    before = _rows()[0]

    assert turn_queue.withdraw_unclaimed("q1") is False
    assert _rows()[0] == before
    assert turn_queue.withdraw_unclaimed("q2") is True
    assert [(r[0], r[3], r[4]) for r in _rows()] == [
        ("q1", status, "hello"), ("q2", "withdrawn", "")]
