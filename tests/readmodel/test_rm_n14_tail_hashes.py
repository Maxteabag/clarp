"""A tail that takes the hashes of its range from its parse
(``tail_hashes_while_parsing``).

A tail parses the file from its boundary to its end, and it read that range
four times: to hash the part an earlier import read and compare it with the
checkpoint's (``tail_rewritten``), to look for a record that reaches before
the boundary, to parse it, and to hash all of it for the next checkpoint.
The boundary moves only at a full import, so the range grows with every
tail until it reaches ``max_tail_bytes``.

With the switch on the two hashes come from the parse. The reader the parser
is handed hashes the bytes it passes on, and gives the digest of the earlier
import's part on the way past its last byte. That is used only when the file
did not change while it was parsed: the same file, size, modification and
change time as when the reader opened it, and the size the import began
with. Then the bytes the parser was handed are the file's and a read would
give the same digests. Otherwise the range is read, as it always was.

What the first hash is there to find, bytes of the earlier import rewritten
in place, is found after the parse instead of before it; the import is the
same full one.

The digests must be the ones a read gives. So every case here runs with the
switch off beside it, the checkpoint a tail leaves is compared between the
two, and its hash with a read of the file. ``range_hashes`` in a tick's
report is every read of a byte range made for its hash alone.
"""
from __future__ import annotations

import io
import sqlite3

import pytest

from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_carry import (  # noqa: F401 - frontier_state is a fixture
    World, _history, _turn, frontier_state)
from tests.readmodel.test_rm_n14_equivalence import _texts

BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)
HASHING = pytest.mark.parametrize("hashing", [True, False],
                                  ids=["hashed while parsed", "read for its hashes"])
TAIL = ("tail_applied", [])


def _settings(hashing, **more):
    return {"tail_hashes_while_parsing": hashing, **more}


def _checkpoint(world):
    with sqlite3.connect(world.candidate) as stored:
        return stored.execute(
            "SELECT boundary_seq, boundary_offset, complete_size, tail_hash "
            "FROM transcript_import_checkpoints").fetchone()


def _fresh(world, start, end):
    """What a read of the file's bytes [start, end) hashes to, now."""
    from lib import transcript_incremental as inc
    return inc.range_hash(world.transcript, start, end)


def _retentions(report):
    return [e["code"] for e in report["tail_events"] if e["event"] == "transcriptTailRetention"]


def _decision(report):
    [event] = [e for e in report["tail_events"] if e["event"] == "transcriptTailDecision"]
    return event["code"] + (f"({event['reason']})" if "reason" in event else "")


def _moved(world, moves):
    """One import during which the file moves under the candidate alone:
    the reference has replayed it as it was. The candidate's report."""
    faults = {**world.faults, "source_moves": moves}
    if world.state == "after a Host start":
        world.actual.__exit__(None, None, None)
        world.actual = harness.PersistentLane(
            world.candidate, world.transcript, world.backend, "candidate")
        world.actual.__enter__()
    elif world.state == "evicted":
        faults["evict"] = True
    world.full.tick(world.tick, faults=world.faults)
    world.report = world.actual.tick(world.tick, faults=faults)
    world.tick += 1
    return world.report


# ---- a file that stands still ---------------------------------------------------------

@HASHING
@BACKENDS
def test_a_tail_reads_its_range_for_its_hashes_or_takes_them_from_the_parse(
        tmp_path, backend, hashing):
    """The same decisions and the same checkpoint either way. Read for its
    hashes the range is read twice (the earlier import's part, then all of
    it); hashed while parsed, not at all. The hash the checkpoint keeps is
    what a read of the range gives."""
    with World(tmp_path, backend, _history(backend), settings=_settings(hashing)) as world:
        ordinal = world.started(6)
        for step in range(4):
            world.grow(_turn(backend, ordinal + step))
            assert world.imported() == TAIL
            _seq, offset, complete, tail_hash = _checkpoint(world)
            assert complete == world.transcript.stat().st_size
            assert world.report["range_hashes"] == (
                [] if hashing else [[offset, complete - 1024], [offset, complete]])
            assert tail_hash == _fresh(world, offset, complete)


@BACKENDS
def test_the_checkpoints_are_the_same_with_the_switch_as_without(tmp_path, backend):
    """Tick for tick: a full import, tails, an audit (a full import that
    moves the boundary), tails again."""
    seen = {}
    for hashing in (True, False):
        home = tmp_path / ("on" if hashing else "off")
        home.mkdir()
        with World(home, backend, _history(backend), settings=_settings(hashing)) as world:
            ordinal = world.started(6)
            rows = [_checkpoint(world)]
            for step in range(7):
                world.grow(_turn(backend, ordinal + step))
                if step == 3:
                    world.faults["settings"] = _settings(hashing, audit_every=1)
                decision = world.imported()
                world.faults["settings"] = _settings(hashing)
                # The offsets are of this lane's own file, the hashes of its bytes.
                rows.append((decision, _checkpoint(world)))
            seen[hashing] = rows
    assert seen[True] == seen[False]
    assert [decision for decision, _row in seen[True][1:]].count(("full_audit", [])) == 1


# ---- bytes of the earlier import rewritten in place -------------------------------------

@HASHING
@BACKENDS
def test_bytes_of_the_earlier_import_rewritten_in_place_are_found_either_way(
        tmp_path, backend, hashing):
    """One byte of a record between the boundary and the earlier import's
    end changes in place, and the file grows. Read before the parse or
    hashed during it, the range is not what the checkpoint says: the same
    full import, and the rows are the full replay's. (A process's first tail
    reads every imported byte before it parses; with the hash left to the
    parse that read comes first and says the same under its own word.)"""
    with World(tmp_path, backend, _history(backend), settings=_settings(hashing)) as world:
        ordinal = world.started(6)
        _seq, offset, complete, _hash = _checkpoint(world)
        data = world.transcript.read_bytes()
        with world.transcript.open("r+b") as output:
            output.seek(data.rindex(b"synthetic step", offset, complete))
            output.write(b"S")
        world.grow(_turn(backend, ordinal))
        world.published = False
        decision, _named = world.imported()
        by_the_read = hashing and world.state == "after a Host start"
        assert decision == ("full_prefix_content_changed" if by_the_read else "full_tail_rewritten")
        [line] = world.report["prefix_changed"]
        assert ("what=imported_bytes" if by_the_read else "what=tail_rewritten") in line
        assert any(text.startswith("Synthetic step") for _id, text in _texts(world.candidate))
        world.published = True
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == TAIL


@HASHING
@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_an_unchanged_file_is_still_read_for_the_earlier_imports_hash(tmp_path, backend, hashing):
    """Only a tail that is going to parse its range can leave the hash to
    the parse. A file that did not grow is not parsed: after a Host start a
    byte rewritten in place there (the mtime put back) is found by the read,
    with the switch as without."""
    import os

    from tests.readmodel.test_rm_n14_restart import _append, _world
    how = {"settings": _settings(hashing)}
    reference, candidate, transcript = _world(tmp_path, backend, records=6)
    with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
        actual.tick(0, faults=how)
        _append(transcript, backend, 6)
        assert _decision(actual.tick(1, faults=how)) == "tail_applied"
    with sqlite3.connect(candidate) as stored:
        offset, complete = stored.execute(
            "SELECT boundary_offset, complete_size FROM transcript_import_checkpoints").fetchone()
    data, stat = transcript.read_bytes(), transcript.stat()
    with transcript.open("r+b") as output:
        output.seek(data.rindex(b"synthetic step", offset, complete))
        output.write(b"S")
    os.utime(transcript, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as restarted:
            full.tick(2)
            found = restarted.tick(2, faults=how)
    assert _decision(found) == "full_tail_rewritten"
    assert _texts(candidate) == _texts(reference)
    assert any(text.startswith("Synthetic step") for _id, text in _texts(candidate))


# ---- every way a file can move while the tail has it -------------------------------------
# The reviewer's attacks on the full import's digest (#336) and on the tail
# that keeps its frontier (#340), turned on this one: the file changes
# before the parser opens it, while its reader is in the middle of the
# range, when it has read its last byte, and before the rows are stored.

SEVERAL = 3 * io.DEFAULT_BUFFER_SIZE // 1024 + 2        # records: several reads of the range
WHEN = ("before_parse", "read", "parse", "store")
HOW = ("grow", "grow_older", "touch", "replace", "rewrite", "rewrite an earlier record",
       "rewrite an earlier record, the mtime put back", "rewrite a new record, the mtime put back")


def _move(world, when, how, ordinal):
    """The fault for this way of moving. ``ordinal``: the first record this
    import adds (those before it an earlier tail imported)."""
    earlier, added = f"synthetic step {ordinal - 1}", f"synthetic step {ordinal + SEVERAL - 1}"
    return {"when": when, **{
        "rewrite an earlier record": {"how": "rewrite_at", "words": earlier},
        "rewrite an earlier record, the mtime put back": {"how": "rewrite_kept", "words": earlier},
        "rewrite a new record, the mtime put back": {"how": "rewrite_kept", "words": added},
    }.get(how, {"how": how})}


def _run(home, backend, hashing, when, how, keep):
    settings = _settings(hashing, tail_keeps_frontier_when_grown=keep)
    with World(home, backend, _history(backend), settings=settings) as world:
        ordinal = world.started(6)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == TAIL
        before = _checkpoint(world)
        with world.transcript.open("ab") as output:
            for number in range(ordinal + 1, ordinal + 1 + SEVERAL):
                output.write(harness.append_bytes(backend, number))
        report = _moved(world, _move(world, when, how, ordinal + 1))
        after = _checkpoint(world)
        published = after is not None and after[2] != before[2]
        # What the checkpoint says of the range is what a read of it gives,
        # unless the file was moved again after the tail had its hashes.
        fresh = None if not published else _fresh(world, after[1], after[2])
        world.published = False
        world.full.tick(world.tick)
        world.actual.tick(world.tick, faults=world.faults)
        world.tick += 1
        world.full.tick(world.tick)
        last = world.actual.tick(world.tick, faults=world.faults)
        assert _texts(world.candidate) == _texts(world.reference)
        return {"decision": _decision(report), "kept": sorted(set(_retentions(report))),
                "published": published, "checkpoint": after and after[1:],
                "is a read's": None if not published else after[3] == fresh,
                "then": _decision(last)}


@pytest.mark.parametrize("keep", [False, True], ids=["as it is", "frontier kept when grown"])
@pytest.mark.parametrize("how", HOW)
@pytest.mark.parametrize("when", WHEN)
@BACKENDS
def test_however_the_file_moves_the_tails_hashes_are_a_reads(tmp_path, backend, when, how, keep):
    """With the switch and without it, side by side. The decision, what is
    kept and the checkpoint are the same, with one kind of exception, below;
    and where a checkpoint was written before the file moved again, its
    hash is what a read of the range gives."""
    seen = {}
    for hashing in (True, False):
        home = tmp_path / ("on" if hashing else "off")
        home.mkdir()
        seen[hashing] = _run(home, backend, hashing, when, how, keep)
    on, off = seen[True], seen[False]
    if when != "store":
        assert on["is a read's"] in (None, True) and off["is a read's"] in (None, True)
    if on != off:
        # The one kind of difference, and it is in the switch's favour. A
        # byte of a record an earlier import read changes while this tail
        # runs, in a way the other checks let pass (the size and the mtime
        # are what they were, or the file only grew and may). Read for its
        # hashes, that part was read before the change: the tail goes on,
        # and the change waits for the audit. With the switch the part is
        # compared after the parse, and this tail is the full import.
        assert how.startswith("rewrite an earlier record") and when != "store"
        assert off["decision"] in ("tail_applied", "deferred_tail_source_changed")
        assert off["published"] and on["decision"] == "full_tail_rewritten"
