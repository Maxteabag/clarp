"""The receipt pass does not read the evidence of a receipt it cannot change.

elitebook, 13 to 16 Nov 2026 (issue 363): the runtime's queue recovery ran the
pass about once a second for stretches of half an hour to two hours and
deferred receipts 32,887 times; 32,012 of those (97 %) were a ``started``
receipt whose latest turn row was still open. Deciding one walks the agent's
whole state history three times, 1.6 million state rows a minute in those
stretches, inside the pass's write transaction.

For that row the answer changes nothing: a started receipt needs no start,
and a turn that is still open gets no settle. The pass now passes over it
before it asks. Everything else is examined as before, and the receipts are
what they were, row for row.
"""
from __future__ import annotations

import json

import pytest

from lib import db, turn_queue

OPEN_SEEN = "open, its launch seen"
OPEN_TWO_ROWS = "open, two turn rows"
OPEN_BARE = "open, nothing seen"
SETTLED = "ended and settled"
SETTLED_FAILED = "ended and settled as failed"
ENDED_ONLY = "ended, not settled"
SETTLED_NOT_ENDED = "settled, no end written"
NO_TURN = "no turn row"
TURNS = (OPEN_SEEN, OPEN_TWO_ROWS, OPEN_BARE, SETTLED, SETTLED_FAILED, ENDED_ONLY,
         SETTLED_NOT_ENDED, NO_TURN)
OPEN = (OPEN_SEEN, OPEN_TWO_ROWS, OPEN_BARE)
STATUSES = ("queued", "claimed", "started")


@pytest.fixture(autouse=True)
def _the_pass_is_not_cut_short(monkeypatch):
    """The pass gives itself 200 ms and stops between two receipts when they
    are spent. On a loaded machine that ends a page early; these tests are
    about what a pass does with the receipts it reaches."""
    monkeypatch.setattr(turn_queue.time, "monotonic", lambda: 1000.0)


def _receipt(queue_id, *, agent="a", trace="t", status="queued"):
    turn_queue.enqueue(
        queue_id=queue_id, agent_id=agent, session="session", text="synthetic work",
        trace_id=trace, client_msg_id=queue_id, synthesize_audio=False,
        origin="agent", sender_agent_id="sender")
    if status == "claimed":
        assert turn_queue.claim(queue_id)
    elif status == "started":
        turn_queue.mark_started(queue_id)


def _turn_row(agent, trace, *, ended=False, settled=False, outcome=None, seen=True):
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO turns(agent_id,source,trace_id,started_at,ended_at,settled_at,outcome) "
        "VALUES (?,'synthetic',?,?,?,?,?)",
        (agent, trace, now, now if ended else None, now if settled else None, outcome))
    if seen:
        db.conn().execute(
            "INSERT INTO state_log(agent_id,kind,ts,detail) VALUES (?,'thinking',?,?)",
            (agent, now, json.dumps({"trace_id": trace, "source": "pwa", "dispatch": "claude"})))


def _turn(shape, *, agent="a", trace="t"):
    if shape == OPEN_SEEN:
        _turn_row(agent, trace)
    elif shape == OPEN_TWO_ROWS:                       # as every peer message on a Host has
        _turn_row(agent, trace)
        _turn_row(agent, trace, seen=False)
    elif shape == OPEN_BARE:
        _turn_row(agent, trace, seen=False)
    elif shape == SETTLED:
        _turn_row(agent, trace, ended=True, settled=True, outcome="completed")
    elif shape == SETTLED_FAILED:
        _turn_row(agent, trace, ended=True, settled=True, outcome="failed")
    elif shape == ENDED_ONLY:
        _turn_row(agent, trace, ended=True)
    elif shape == SETTLED_NOT_ENDED:
        _turn_row(agent, trace, settled=True, outcome="completed")
    else:
        assert shape == NO_TURN
    db.conn().commit()


TABLES = ("queued_turns", "queue_state_revisions")


def _tables():
    """Every column of every row the pass may write."""
    con = db.conn()
    return {name: [tuple(row) for row in con.execute(f"SELECT * FROM {name} ORDER BY 1, 2")]
            for name in TABLES}


def _put_back(tables):
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    for name, rows in tables.items():
        con.execute(f"DELETE FROM {name}")
        for row in rows:
            con.execute(f"INSERT INTO {name} VALUES ({','.join('?' for _ in row)})", row)
    con.execute("COMMIT")


def _pass(**kwargs):
    """One look at every receipt, from the first one: the pass keeps a cursor
    for its thread, and a test that looks twice starts both looks alike."""
    turn_queue._save_reap_cursor("convergence", db.conn(), None)
    return turn_queue.reconcile_receipts(**kwargs)


def _asked(monkeypatch):
    asked = []
    real = turn_queue._launch_status

    def launch_status(row):
        asked.append(str(row["queue_id"]))
        return real(row)
    monkeypatch.setattr(turn_queue, "_launch_status", launch_status)
    return asked


def _as_it_was(monkeypatch):
    """The pass before this change: every receipt with a turn row is asked."""
    monkeypatch.setattr(turn_queue, "_nothing_to_write", lambda row, turn: False)


def _status(queue_id):
    return db.conn().execute("SELECT status FROM queued_turns WHERE queue_id=?",
                             (queue_id,)).fetchone()["status"]


# --- the receipts are what they were ------------------------------------------

@pytest.mark.parametrize("shape", TURNS)
@pytest.mark.parametrize("status", STATUSES)
def test_the_receipts_are_row_for_row_what_the_pass_made_of_them_before(monkeypatch, status, shape):
    _receipt("q", status=status)
    _turn(shape)
    before = _tables()
    # A receipt that is started is stamped with the time: the same for both.
    monkeypatch.setattr(db, "now_ms", lambda: 1_800_000_000_000)

    told = []
    counts = _pass(on_deferred=lambda *args: told.append(args))
    now = _tables()

    _put_back(before)
    assert _tables() == before
    with monkeypatch.context() as earlier:
        _as_it_was(earlier)
        told_then = []
        counts_then = _pass(on_deferred=lambda *args: told_then.append(args))
        then = _tables()

    assert now == then and counts == counts_then
    passed_over = status == "started" and shape in OPEN
    if passed_over:
        assert now == before and told == []
    else:
        assert told == told_then                       # still told, where it was


@pytest.mark.parametrize("status,shape,expected", [
    ("queued", OPEN_SEEN, "started"), ("claimed", OPEN_SEEN, "started"),
    ("queued", SETTLED, "done"), ("claimed", SETTLED, "done"), ("started", SETTLED, "done"),
    ("started", SETTLED_NOT_ENDED, "done"), ("started", ENDED_ONLY, "ended"),
    ("started", OPEN_SEEN, "started"), ("started", OPEN_TWO_ROWS, "started"),
    ("queued", OPEN_TWO_ROWS, "queued"), ("claimed", OPEN_BARE, "claimed"),
])
def test_what_the_pass_makes_of_a_receipt(status, shape, expected):
    """The fixed points the comparison above rests on: it compares two passes
    that do something."""
    _receipt("q", status=status)
    _turn(shape)
    _pass()
    assert _status("q") == expected


# --- which receipts are still asked -------------------------------------------

@pytest.mark.parametrize("shape", OPEN)
def test_a_started_receipt_whose_turn_is_open_is_not_asked(monkeypatch, shape):
    _receipt("q", status="started")
    _turn(shape)
    asked = _asked(monkeypatch)
    _pass()
    assert asked == []


@pytest.mark.parametrize("shape", OPEN)
@pytest.mark.parametrize("status", ["queued", "claimed"])
def test_a_queued_or_claimed_receipt_with_an_open_turn_is_still_asked(monkeypatch, status, shape):
    """Its answer decides whether it becomes ``started``, and whether the
    recovery may launch it."""
    _receipt("q", status=status)
    _turn(shape)
    asked = _asked(monkeypatch)
    _pass()
    assert asked == ["q"]


@pytest.mark.parametrize("shape", [SETTLED, SETTLED_FAILED, ENDED_ONLY, SETTLED_NOT_ENDED])
def test_a_started_receipt_whose_turn_is_over_is_still_asked(monkeypatch, shape):
    """Ended or settled, either is enough: the pass settles on both."""
    _receipt("q", status="started")
    _turn(shape)
    asked = _asked(monkeypatch)
    _pass()
    assert asked == ["q"]


def test_it_is_the_latest_turn_row_that_counts(monkeypatch):
    """An older attempt that is still open does not hide a later one that ended."""
    _receipt("q", status="started")
    _turn_row("a", "t", seen=False)
    _turn_row("a", "t", ended=True, settled=True, outcome="completed")
    db.conn().commit()
    asked = _asked(monkeypatch)
    _pass()
    assert asked == ["q"] and _status("q") == "done"


def test_a_receipt_behind_one_that_is_passed_over_is_decided_in_the_same_pass(monkeypatch):
    _receipt("running", status="started", trace="t1")
    _turn(OPEN_TWO_ROWS, trace="t1")
    _receipt("over", status="started", trace="t2")
    _turn(SETTLED, trace="t2")
    asked = _asked(monkeypatch)
    turn_queue.reconcile_receipts()
    assert asked == ["over"] and _status("over") == "done" and _status("running") == "started"


def test_the_cursor_moves_past_a_page_of_receipts_that_are_passed_over(monkeypatch):
    for n in range(100):
        _receipt(f"running-{n:03d}", status="started", agent=f"a{n % 4}", trace=f"t{n:03d}")
        _turn_row(f"a{n % 4}", f"t{n:03d}", seen=False)
    _receipt("over", status="started", trace="t-over")
    _turn(SETTLED, trace="t-over")
    asked = _asked(monkeypatch)

    turn_queue.reconcile_receipts()                    # the first page: a hundred, all running
    assert asked == [] and _status("over") == "started"
    turn_queue.reconcile_receipts()                    # the next page begins behind them

    assert asked == ["over"] and _status("over") == "done"


# --- what it no longer reads, and no longer says -------------------------------

def _state_log_statements(run):
    statements = []
    con = db.conn()
    con.set_trace_callback(statements.append)
    try:
        run()
    finally:
        con.set_trace_callback(None)
    return [sql for sql in statements if "state_log" in sql]


def test_a_page_of_started_receipts_with_open_turns_reads_no_state_history(monkeypatch):
    for n in range(12):
        agent = f"a{n % 3}"
        _receipt(f"q{n}", status="started", agent=agent, trace=f"t{n}")
        _turn(OPEN_TWO_ROWS if n % 2 else OPEN_SEEN, agent=agent, trace=f"t{n}")
    for n in range(200):                               # a history worth not walking
        db.conn().execute("INSERT INTO state_log(agent_id,kind,ts,detail) VALUES (?,'idle',?,?)",
                          (f"a{n % 3}", db.now_ms(), json.dumps({"trace_id": f"old-{n}"})))
    db.conn().commit()
    before = _tables()

    assert _state_log_statements(_pass) == []
    assert _tables() == before

    _as_it_was(monkeypatch)                            # and what the same page cost before
    assert len(_state_log_statements(_pass)) >= 12 * 2
    assert _tables() == before


def test_the_journal_line_and_the_callers_word_go_only_for_the_receipts_passed_over(capsys):
    _receipt("running", status="started", trace="t1")
    _turn(OPEN_TWO_ROWS, trace="t1")
    _receipt("waiting", status="queued", trace="t2")
    _turn(OPEN_TWO_ROWS, trace="t2")
    _receipt("held", status="claimed", trace="t3")
    _turn(OPEN_BARE, trace="t3")
    capsys.readouterr()
    told = []

    turn_queue.reconcile_receipts(on_deferred=lambda queue_id, _seq, reason: told.append((queue_id, reason)))

    lines = [line for line in capsys.readouterr().err.splitlines() if "queueReceiptDeferred" in line]
    assert sorted(told) == [("held", "unknown"), ("waiting", "unknown")]
    assert len(lines) == 2 and not any("queue=running" in line for line in lines)
    assert any("queue=waiting" in line for line in lines) and any("queue=held" in line for line in lines)


def test_the_callers_skip_is_not_asked_about_a_receipt_that_is_passed_over():
    _receipt("running", status="started")
    _turn(OPEN_TWO_ROWS)
    seen = []
    turn_queue.reconcile_receipts(skip=lambda queue_id, _seq: seen.append(queue_id) or False)
    assert seen == []


# --- a pass that ends at its limit says so --------------------------------------

class _Clock:
    """The pass's clock, moved on by ``step`` seconds at every reading."""

    def __init__(self, monkeypatch, step):
        self.now, self.step = 1000.0, step
        monkeypatch.setattr(turn_queue.time, "monotonic", self.read)

    def read(self):
        self.now += self.step
        return self.now


def _cut_lines(capsys):
    return [line.split("queueReceiptPassCut", 1)[1].strip()
            for line in capsys.readouterr().err.splitlines() if "queueReceiptPassCut" in line]


def _waiting(count):
    """Queued receipts with an open turn: each one is asked about."""
    for n in range(count):
        _receipt(f"q{n:02d}", trace=f"t{n:02d}")
        _turn_row("a", f"t{n:02d}", seen=False)
    db.conn().commit()


def test_a_pass_that_ends_at_its_limit_says_how_many_receipts_were_left(monkeypatch, capsys):
    """The review of #365: a pass on a loaded copy of a Host's database
    examined nothing before its 200 ms were spent, and said nothing."""
    _waiting(30)
    asked = _asked(monkeypatch)
    capsys.readouterr()
    _Clock(monkeypatch, 0.01)

    _pass()

    assert 0 < len(asked) < 30
    assert _cut_lines(capsys) == [f"left={30 - len(asked)}"]


def test_the_next_pass_goes_on_from_there_and_says_nothing_when_it_reaches_the_end(monkeypatch, capsys):
    _waiting(30)
    asked = _asked(monkeypatch)
    _Clock(monkeypatch, 0.01)
    _pass()
    reached = len(asked)
    capsys.readouterr()
    monkeypatch.setattr(turn_queue.time, "monotonic", lambda: 1000.0)

    turn_queue.reconcile_receipts()                    # no new cursor: it goes on

    assert asked == [f"q{n:02d}" for n in range(30)] and reached < 30
    assert _cut_lines(capsys) == []


def test_a_pass_whose_limit_is_spent_before_it_examines_anything_says_all_were_left(monkeypatch, capsys):
    """Reading the page alone took the 200 ms: nothing is examined, the cursor
    does not move, and the next pass stands at the same place."""
    _waiting(7)
    asked = _asked(monkeypatch)
    capsys.readouterr()
    # The clock is read once for the limit, once for each of the three
    # statuses the page is read by, then for the writer's admission: at 60 ms
    # a reading the page is read in full and nothing is left for the writer.
    _Clock(monkeypatch, 0.06)

    assert turn_queue.reconcile_receipts() == {}
    assert turn_queue.reconcile_receipts() == {}       # the same thread, the same place

    assert asked == [] and _cut_lines(capsys) == ["left=7", "left=7"]


def test_a_pass_whose_limit_is_spent_while_it_reads_its_page_says_so_without_a_count(monkeypatch, capsys):
    _waiting(7)
    asked = _asked(monkeypatch)
    capsys.readouterr()
    _Clock(monkeypatch, 0.1)                           # spent after the first status read

    assert turn_queue.reconcile_receipts() == {}

    assert asked == [] and _cut_lines(capsys) == ["left=unknown"]
    monkeypatch.setattr(turn_queue.time, "monotonic", lambda: 1000.0)
    turn_queue.reconcile_receipts()                    # the cursor was not thrown away or moved
    assert len(asked) == 7


def test_a_pass_with_no_receipts_at_all_writes_no_such_line(capsys):
    capsys.readouterr()
    assert _pass() == {}
    assert _cut_lines(capsys) == []


def test_a_pass_that_examines_its_whole_page_writes_no_such_line(monkeypatch, capsys):
    _waiting(5)
    _receipt("running", status="started", trace="t-running")
    _turn(OPEN_TWO_ROWS, trace="t-running")
    asked = _asked(monkeypatch)
    capsys.readouterr()

    _pass()

    assert len(asked) == 5 and _cut_lines(capsys) == []


def test_the_line_names_no_receipt(monkeypatch, capsys):
    _waiting(30)
    capsys.readouterr()
    _Clock(monkeypatch, 0.01)
    _pass()
    lines = [line for line in capsys.readouterr().err.splitlines() if "queueReceiptPassCut" in line]
    assert len(lines) == 1 and "queue=" not in lines[0] and "agent=" not in lines[0]
