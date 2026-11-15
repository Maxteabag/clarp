"""What a skipped checkpoint write costs (``transcriptCheckpointWriteSkip``).

A tail that finds every row as it should be writes nothing but its
checkpoint. It does that from its probe pass, by turning the read snapshot
it holds into a write, so that the checkpoint describes exactly the rows it
read. SQLite refuses that when another writer has committed since the
snapshot began ('database is locked'), and the checkpoint is skipped. On a
Host whose runtime writes a live row four times a second that is one tail
in ten.

This pins what follows, because it decided against a change: the process
drops its frontier, the store still has the earlier import's, and the next
import takes that up and is a tail. It reads the session's rows once more
than a tail from a held frontier does, and not the file. Writing the
checkpoint under the write lock instead (the locked pass a tail with rows
to write takes) would read the rows twice more, under the lock, to save
that one read.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_restart import _append, _decision, _frontiers, _world

BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)


def _retentions(report):
    return [e["code"] for e in report["tail_events"] if e["event"] == "transcriptTailRetention"]


def _touch(transcript):
    """The mtime moves and no byte does (not into the future: a later append
    must not look like an mtime that went back)."""
    stat = transcript.stat()
    os.utime(transcript, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000))


@pytest.mark.parametrize("other_writer", [True, False], ids=["another writer commits", "nobody else"])
@BACKENDS
def test_a_tail_with_nothing_to_write_and_the_import_after_it(tmp_path, backend, other_writer):
    """The file is touched, so the tail parses its range again and finds
    every row there already. With another writer's commit between its read
    and its checkpoint, the checkpoint is skipped."""
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            _append(transcript, backend, 40)
            full.tick(1)
            assert _decision(actual.tick(1)) == "tail_applied"
            _touch(transcript)
            full.tick(2)
            quiet = actual.tick(2, faults={"commit_before_checkpoint": other_writer})
            assert quiet.get("probe_commits", []) == ([True] if other_writer else [])
            assert _decision(quiet) == "tail_applied"
            assert ("checkpoint_not_written" in _retentions(quiet)) is other_writer
            if other_writer:
                with sqlite3.connect(reference) as other:     # the other writer's row, there too
                    other.execute("INSERT INTO settings(key, value, updated_at) "
                                  "VALUES ('lane.other_writer', '2', 0)")
            harness.assert_product_equal(reference, candidate)
            assert _frontiers(candidate) == 1                 # the earlier import's, or this one's
            _append(transcript, backend, 41)
            full.tick(3)
            after = actual.tick(3)
            assert _decision(after) == "tail_applied"
            assert after["frontier_held"] is not other_writer
            assert after["digest_reads"] == ["added"]         # the file is not read again
            assert after["session_reads"]["every_row"] == (4 if other_writer else 3)
            harness.assert_product_equal(reference, candidate)
