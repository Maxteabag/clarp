"""When a tail gives the writer away (``tail_yield_after_first_write``).

An import's batch ends after a number of rows or a time budget, counted from
BEGIN IMMEDIATE, and the writer is then given away for a moment. A tail's
transaction begins with reading and hashing the session's rows; on a long
conversation that alone exceeds the budget, so the tail yielded before it had
written anything, and whoever was waiting for the writer committed in that
gap. A foreign commit during a yield refuses the tail's publication, and the
import after it is a full one.

With the switch a tail's budgets count from its first write. Only WHEN the
writer is given away changes; what the publication fence checks does not.
Each test is a pair with the full-replay reference lane, with the switch on
and off, in the three places a frontier can be.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_carry import (  # noqa: F401 - frontier_state is a fixture
    World, _history, _turn, frontier_state)

SWITCH = pytest.mark.parametrize("switch", [True, False], ids=["switch on", "switch off"])
# What a batch that wrote no row has changed when it ends: the conversation's
# head, which every batch writes on its way out.
HEAD_ONLY = 1
BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)


def _settings(switch):
    return {"tail_yield_after_first_write": True} if switch else None


@SWITCH
@BACKENDS
def test_a_tail_does_not_give_the_writer_away_before_it_has_written(tmp_path, backend, switch):
    """A time budget of nothing stands for a conversation whose rows take
    longer to read than the budget allows: every batch is over it at once.
    Off, the tail's first yield comes with nothing written. On, it comes
    after the first row; a tail with nothing to write does not yield at all."""
    with World(tmp_path, backend, _history(backend), settings=_settings(switch)) as world:
        ordinal = world.started(6)
        world.faults = {**world.faults, "single_row_batches": False, "write_budget_seconds": 0.0}
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        assert world.report["yields"] >= 1
        if switch:
            assert world.report["changes_at_first_yield"] > HEAD_ONLY
        else:
            assert world.report["changes_at_first_yield"] == HEAD_ONLY
        # Nothing new: the tail reads, finds nothing to write, and ends.
        assert world.imported()[0] in ("tail_applied", "unchanged_file")
        assert world.report["yields"] == 0
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_the_row_budget_of_a_tail_counts_from_its_first_write_too(tmp_path, backend):
    """Three rows a batch and no time budget. A tail reads the turns from its
    boundary, which stand as they are, before it comes to the new ones. With
    the switch its batch of three begins at the first row it writes; counted
    from BEGIN IMMEDIATE the batch would be full already and end one row
    after."""
    with World(tmp_path, backend, _history(backend),
               settings={"tail_yield_after_first_write": True}) as world:
        ordinal = world.started(6)
        with sqlite3.connect(world.candidate) as stored:
            boundary, first_new = stored.execute(
                "SELECT boundary_seq, turn_count FROM transcript_import_checkpoints").fetchone()
        assert first_new - boundary >= 3            # that many turns stand before the new ones
        world.faults = {**world.faults, "single_row_batches": False, "commit_every": 3,
                        "write_budget_seconds": 1e9}
        world.grow(_turn(backend, ordinal) + _turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])
        assert world.report["yields"] >= 1
        assert world.report["first_yield_seq"] == first_new + 3


@SWITCH
@BACKENDS
def test_a_foreign_commit_in_a_tails_first_yield_is_still_caught(tmp_path, backend, switch):
    """One row a batch, and another writer commits in the first gap the tail
    leaves: with the switch that gap comes after the tail's first row instead
    of before it. Either way the fence sees the foreign commit, the tail's
    rows stand, its frontier is not published and the next import is full."""
    with World(tmp_path, backend, _history(backend), settings=_settings(switch)) as world:
        ordinal = world.started(6)
        world.grow(_turn(backend, ordinal))
        foreign = {**world.faults, "client_on_yield": True, "yield_at": 1}
        report = world.actual.tick(world.tick, faults=(
            {**foreign, "evict": True} if world.state == "evicted" else foreign))
        assert report["yield_intervention"] == 1
        # The reference replays every turn: the same commit before the same row.
        world.full.tick(world.tick, faults={**foreign, "yield_before_seq": report["yield_seq"]})
        world.tick += 1
        harness.assert_product_equal(world.reference, world.candidate)
        codes = [(e["event"], e["code"]) for e in report["tail_events"]]
        assert ("transcriptTailRetention", "checkpoint_foreign_commit_yield") in codes
        assert ("transcriptTailDecision", "tail_applied") in codes
        assert (report["changes_at_first_yield"] > HEAD_ONLY) is switch
        with sqlite3.connect(world.candidate) as stored:
            assert stored.execute(
                "SELECT count(*) FROM messages WHERE message_id='u-interleaved'").fetchone()[0] == 1
        world.published = False
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported()[0].startswith("full_")
        world.published = True
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == ("tail_applied", [])
