"""Revisions compared by what they order (``harness._revisions_by_order``).

The message clock gives every changed row a number. Once an import has
removed a slot or parked a row, every batch it commits takes a number too
(``message_writes.commit_batch``), so the numbers of the rows after it depend
on where the writer gave the lock away: after 200 rows, after 25 ms of wall
clock, before each row or only once it has written. Two runs that batch
differently number the same rows, in the same order, differently (issue 349).

The lane compares the numbers themselves at the defaults, where it makes both
lanes batch alike. In the Hosts' configuration a tail gives the writer away
only once it has written (``tail_yield_after_first_write``) and the
reference's replay before every batch, so there it compares what the numbers
order: the rows' order of writing, which rows share a number, and how many
rows a head, a replace revision and the clock are not older than.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_equivalence import _clones


def _view(rows, *, head, replace=0, clock=None):
    """A product with these (message id, revision) rows and this head."""
    return {
        "messages": {"columns": ["message_id", "revision"], "rows": [list(row) for row in rows]},
        "conversation_heads": {"columns": ["agent_id", "backend_session_id", "revision",
                                           "replace_revision"],
                               "rows": [["a", "s", head, replace]]},
        "message_clock": {"columns": ["singleton", "revision"],
                          "rows": [[0, head if clock is None else clock]]},
    }


ROWS = [("m1", 3), ("m2", 4), ("m3", 5)]
ordered = harness._revisions_by_order


def test_numbers_unused_between_the_rows_change_nothing():
    """A batch end took 5 and 7: the rows carry 3, 4, 6, 8 instead of 3 to 6."""
    quiet = _view([*ROWS, ("m4", 6)], head=6, replace=6)
    starved = _view([("m1", 3), ("m2", 4), ("m3", 6), ("m4", 8)], head=9, replace=9)
    assert quiet != starved
    assert ordered(quiet) == ordered(starved)


@pytest.mark.parametrize("other", [
    [("m1", 3), ("m2", 5), ("m3", 4)],          # written in another order
    [("m1", 3), ("m2", 4), ("m3", 4)],          # two rows share a number
    [("m1", 3), ("m2", 4)],                     # a row is missing
], ids=["another order", "a shared number", "a missing row"])
def test_what_the_numbers_order_is_still_compared(other):
    assert ordered(_view(ROWS, head=5)) != ordered(_view(other, head=5))


@pytest.mark.parametrize("head,replace,clock", [
    (4, 0, 5),      # the head is older than the last row: a reader at the head has not seen it
    (5, 4, 5),      # a replace that the last row is newer than
    (5, 5, 5),      # a replace where there was none
    (5, 0, 4),      # a clock behind a row
], ids=["a head behind a row", "a replace behind a row", "a replace that was none",
        "a clock behind a row"])
def test_a_head_a_replace_revision_and_the_clock_are_compared_by_the_rows_they_cover(
        head, replace, clock):
    assert ordered(_view(ROWS, head=5)) != ordered(_view(ROWS, head=head, replace=replace,
                                                         clock=clock))


def test_a_head_ahead_of_every_row_covers_what_one_at_the_last_row_covers():
    """A batch end after the last row leaves the head one above it."""
    assert ordered(_view(ROWS, head=5, replace=5)) == ordered(_view(ROWS, head=6, replace=6))


def test_a_configuration_that_is_not_one_is_refused(monkeypatch):
    monkeypatch.setenv("RM_N14_CONFIGURATION", "host")
    with pytest.raises(ValueError):
        harness.configuration()


def test_a_replace_revision_below_every_row_is_not_the_same_as_none():
    """Both cover no row. One lane removed a slot and wrote every row again,
    the other never said a replace: a client of the first was told to drop
    its copy. Counted as rows covered alone, the two read the same."""
    never = _view(ROWS, head=5, replace=0)
    below = _view(ROWS, head=5, replace=2)
    assert ordered(never) != ordered(below)
    # Where it lies below the rows does not matter, only that it was said.
    assert ordered(below) == ordered(_view(ROWS, head=5, replace=1))
    # And one at a row is still told from one below them all.
    assert ordered(below) != ordered(_view(ROWS, head=5, replace=3))


def test_an_empty_conversation_that_said_a_replace_is_not_one_that_did_not():
    assert ordered(_view([], head=4, replace=0)) != ordered(_view([], head=4, replace=4))
    assert ordered(_view([], head=4, replace=4)) == ordered(_view([], head=9, replace=7))


def test_the_hosts_configuration_is_the_seven_switches_under_a_tests_own(monkeypatch):
    """What both Hosts run with (16 Nov 2026). Every one is a switch the
    importer has and leaves off by default."""
    from lib import transcript_incremental as inc
    monkeypatch.delenv("RM_N14_CONFIGURATION", raising=False)
    assert harness.configured() == {} and harness.tail_yields_before_it_writes()
    monkeypatch.setenv("RM_N14_CONFIGURATION", "hosts")
    assert harness.configured() == dict.fromkeys(
        ("carry_live_rows", "tail_yield_after_first_write", "carry_settled_turns",
         "boundary_skips_dropped_records", "tail_keeps_frontier_when_grown",
         "audit_grown_by_digest", "full_digest_while_parsing"), True)
    assert all(inc._DEFAULTS[switch] is False for switch in harness.HOSTS_SWITCHES)
    assert harness.configured(carry_live_rows=False)["carry_live_rows"] is False
    assert not harness.tail_yields_before_it_writes()
    assert harness.tail_yields_before_it_writes({"settings": {"tail_yield_after_first_write": False}})


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_batches_that_end_at_other_rows_number_the_same_rows_in_the_same_order(tmp_path, backend):
    """The case of issue 349, made on purpose. A /send row written while the
    first import yields stands in for the transcript's first prompt, so the
    next import removes that slot and is a full one in both lanes. One lane
    ends a batch at every row, as a starved machine's clock made it; the
    other commits one batch. The numbers differ; what they order does not."""
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(3)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            faults = {"single_row_batches": True, "client_on_yield": True}
            full.tick(0, faults=faults)
            actual.tick(0, faults=faults)
            harness.assert_product_equal(reference, candidate, revisions="value")
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 3))
            starved = full.tick(1, faults={"write_budget_seconds": 0.0})
            quiet = actual.tick(1)
            assert quiet["apply_mode"] == "full" and starved["yields"] > quiet["yields"] == 0
            with pytest.raises(AssertionError, match="product_state_differs"):
                harness.assert_product_equal(reference, candidate, revisions="value")
            harness.assert_product_equal(reference, candidate, revisions="order")
            # Left to the configuration: the numbers at the defaults, their
            # order with the Hosts' switches.
            if harness.configuration() == "hosts":
                harness.assert_product_equal(reference, candidate)
            else:
                with pytest.raises(AssertionError, match="product_state_differs"):
                    harness.assert_product_equal(reference, candidate)
    clocks = []
    for database in (reference, candidate):
        with sqlite3.connect(database) as connection:
            clocks.append(connection.execute("SELECT revision FROM message_clock").fetchone()[0])
    assert clocks[0] > clocks[1]


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_in_the_lane_the_wall_clock_ends_no_batch(tmp_path, backend):
    """A writer that takes 30 ms a statement, as on a starved machine. A
    Host's batch ends after 25 ms, so there every row would end one. In the
    lane a batch ends by its rows, so that two lanes starved differently
    batch alike; a test that wants the clock sets its budget."""
    _reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(2)))
    with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
        slow = actual.tick(0, faults={"slow_writer": 0.03})
        assert slow["apply_mode"] == "full" and slow["yields"] == 0
        with transcript.open("ab") as output:
            output.write(harness.append_bytes(backend, 2))
        clocked = actual.tick(1, faults={"slow_writer": 0.03, "write_budget_seconds": 0.025})
        assert clocked["yields"] >= 1


def _store(tmp_path, name, *, head):
    """One conversation with messages up to revision 5 and this head (None: no head row)."""
    path = tmp_path / name
    with sqlite3.connect(path) as store:
        store.execute("CREATE TABLE messages (agent_id, backend_session_id, revision)")
        store.execute("CREATE TABLE conversation_heads (agent_id, backend_session_id, revision)")
        store.executemany("INSERT INTO messages VALUES ('a', 's', ?)", [(3,), (5,), (4,)])
        if head is not None:
            store.execute("INSERT INTO conversation_heads VALUES ('a', 's', ?)", (head,))
    return path


@pytest.mark.parametrize("head,covered", [(5, True), (9, True), (4, False), (None, False)],
                         ids=["at its messages", "above them", "below them", "no head"])
def test_a_head_below_its_messages_is_refused_by_the_lane(tmp_path, head, covered):
    """The agent list reads what changed from the heads alone (issue 371); the
    lane asks of every store it compares that no import left a head behind."""
    store = _store(tmp_path, "store.sqlite", head=head)
    if covered:
        harness.assert_heads_cover_messages(store)
    else:
        with pytest.raises(AssertionError, match="head_below_its_messages"):
            harness.assert_heads_cover_messages(store)


def test_the_comparison_asks_about_the_heads_of_both_stores(tmp_path):
    good = _store(tmp_path, "good.sqlite", head=5)
    behind = _store(tmp_path, "behind.sqlite", head=4)
    for reference, candidate in ((good, behind), (behind, good)):
        with pytest.raises(AssertionError, match="head_below_its_messages"):
            harness.assert_product_equal(reference, candidate)
