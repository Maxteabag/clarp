"""A file that grows while a tail has it (``tail_keeps_frontier_when_grown``).

A transcript is appended to while its agent works, so a tail often finds the
file longer when it is done than when it began. It then gave up: found after
the parse, the same import went on as a full one
(``full_file_moved_during_parse``); found at publication, the rows were
stored but the frontier was dropped and the next import was full
(``publication_file_moved``).

Nothing about the bytes the tail parsed has changed when the file only
grew. With the switch on the tail goes on for those bytes and publishes its
frontier for them, once they are proven to be the file's still: the bytes
the parser was handed hash as the same range read afterwards, and the bytes
added to the running hash before the parse hash the same read again. The
import is still reported as not complete, so it comes again, and finds its
frontier: a tail. A file that was touched, rewritten in place or replaced
is refused as before.
"""
from __future__ import annotations

import io
import sqlite3

import pytest

from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_equivalence import _texts
from tests.readmodel.test_rm_n14_restart import _append, _decision, _world

BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)
SWITCH = pytest.mark.parametrize("switch", [True, False], ids=["frontier kept", "as before"])


def _how(switch, **more):
    return {"settings": {"tail_keeps_frontier_when_grown": switch}, **more}


def _moves(when, how="grow"):
    return {"source_moves": {"when": when, "how": how}}


def _retentions(report):
    return [e["code"] for e in report["tail_events"] if e["event"] == "transcriptTailRetention"]


def _checkpoint(database):
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT size, complete_size FROM transcript_import_checkpoints").fetchone()


def _stood(tmp_path, backend, switch):
    """Both lanes after a full import and one plain tail: a frontier stands
    and this process has read the file."""
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    full = harness.PersistentLane(reference, transcript, backend, "reference")
    actual = harness.PersistentLane(candidate, transcript, backend, "candidate")
    return reference, candidate, transcript, full, actual


@SWITCH
@pytest.mark.parametrize("when", ["parse", "store"])
@BACKENDS
def test_a_file_that_only_grew_while_the_tail_had_it(tmp_path, backend, when, switch):
    """During the parse or between the parse and the publication. What was
    parsed is imported and equals the full replay of the file as it was;
    the import says it is not complete. With the switch the frontier is for
    the parsed size and the next import is a tail; without it the next is a
    full import (or this one already was)."""
    reference, candidate, transcript, full, actual = _stood(tmp_path, backend, switch)
    with full, actual:
        full.tick(0)
        actual.tick(0, faults=_how(switch))
        _append(transcript, backend, 40)
        full.tick(1)
        assert _decision(actual.tick(1, faults=_how(switch))) == "tail_applied"
        earlier = _checkpoint(candidate)
        _append(transcript, backend, 41)
        parsed = transcript.stat().st_size
        full.tick(2)
        grown = actual.tick(2, faults=_how(switch, **_moves(when)))
        assert transcript.stat().st_size > parsed
        harness.assert_product_equal(reference, candidate)        # the file as it was parsed
        if switch:
            assert _decision(grown) == "deferred_tail_source_changed"
            assert grown["apply_mode"] == "deferred"
            assert "frontier_kept_source_grew" in _retentions(grown)
            assert _checkpoint(candidate) == (parsed, parsed)
        elif when == "parse":
            assert _decision(grown) == "full_file_moved_during_parse"
        else:
            assert _decision(grown) == "deferred_tail_source_changed"
            assert "frontier_kept_source_grew" not in _retentions(grown)
            assert _checkpoint(candidate) == earlier        # nothing was published
        full.tick(3)
        after = actual.tick(3, faults=_how(switch))
        harness.assert_product_equal(reference, candidate)
        if switch:
            assert (_decision(after), after["digest_reads"]) == ("tail_applied", ["added"])
        elif when == "store":
            assert _decision(after) == "full_no_frontier(publication_file_moved)"
        _append(transcript, backend, 42)
        full.tick(4)
        assert _decision(actual.tick(4, faults=_how(switch))) == "tail_applied"
        harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("how", ["touch", "rewrite", "replace"])
@pytest.mark.parametrize("when", ["parse", "store"])
@BACKENDS
def test_a_file_that_did_more_than_grow_is_refused_as_before(tmp_path, backend, when, how):
    """Touched without growing, a byte of the parsed range rewritten in place
    (with a record appended, so that it looks like growth), or replaced by a
    new file. While the file is parsed the switch changes nothing: each
    lane decides what it decided without it. The rows end as the full
    replay's every time."""
    seen = {}
    for switch in (True, False):
        where = tmp_path / ("on" if switch else "off")
        where.mkdir()
        reference, candidate, transcript, full, actual = _stood(where, backend, switch)
        with full, actual:
            full.tick(0)
            actual.tick(0, faults=_how(switch))
            _append(transcript, backend, 40)
            full.tick(1)
            assert _decision(actual.tick(1, faults=_how(switch))) == "tail_applied"
            _append(transcript, backend, 41)
            moved = actual.tick(2, faults=_how(switch, **_moves(when, how)))
            kept = "frontier_kept_source_grew" in _retentions(moved)
            full.tick(3)
            after = actual.tick(3, faults=_how(switch))
            assert _texts(candidate) == _texts(reference)
            seen[switch] = (_decision(moved), kept, _decision(after))
    if (when, how) == ("store", "rewrite"):
        # Rewritten after it was parsed: the frontier is for the bytes that
        # were parsed, as if the rewrite had come a moment after the import.
        # The next import finds the range changed, as it does for any
        # rewrite between two imports, and is full.
        assert seen[True] == ("deferred_tail_source_changed", True, "full_tail_rewritten")
        assert seen[False] == ("deferred_tail_source_changed", False,
                               "full_no_frontier(publication_file_moved)")
    else:
        assert seen[True] == seen[False] and seen[True][1] is False
        if when == "parse":
            assert seen[True][0] == "full_file_moved_during_parse"


@pytest.mark.parametrize("when", ["read", "parse", "store"])
@BACKENDS
def test_a_file_that_is_longer_and_older_than_it_was_did_not_only_grow(tmp_path, backend, when):
    """An append moves the mtime forward. A file that is longer and whose
    mtime is EARLIER than when the tail began was written by something that
    sets its times, so "longer" says nothing about the bytes that were
    there: refused, as without the switch, at each of the three moments.
    (From the review of #340: the mtime half of "only grew" had no test.)"""
    seen = {}
    for switch in (True, False):
        where = tmp_path / ("on" if switch else "off")
        where.mkdir()
        reference, candidate, transcript, full, actual = _stood(where, backend, switch)
        with full, actual:
            full.tick(0)
            actual.tick(0, faults=_how(switch))
            _append(transcript, backend, 40)
            full.tick(1)
            assert _decision(actual.tick(1, faults=_how(switch))) == "tail_applied"
            earlier = _checkpoint(candidate)
            if when == "read":
                _several_reads(transcript, backend, 41)
            else:
                _append(transcript, backend, 41)
            began = transcript.stat()
            moved = actual.tick(2, faults=_how(switch, **_moves(when, "grow_older")))
            now = transcript.stat()
            assert now.st_size > began.st_size and now.st_mtime_ns < began.st_mtime_ns
            kept = "frontier_kept_source_grew" in _retentions(moved)
            published = _checkpoint(candidate)
            full.tick(3)
            after = actual.tick(3, faults=_how(switch))
            assert _texts(candidate) == _texts(reference)
            seen[switch] = (_decision(moved), kept, published == earlier, _decision(after))
    assert seen[True] == seen[False]
    assert seen[True][1] is False
    if when == "store":
        assert seen[True] == ("deferred_tail_source_changed", False, True,
                              "full_no_frontier(publication_file_moved)")
    else:
        assert seen[True][0] == "full_file_moved_during_parse"


@BACKENDS
def test_bytes_rewritten_between_the_hash_and_the_parse_are_caught_by_the_second_hash(
        tmp_path, backend):
    """The tail adds the new bytes to its running hash and then parses them.
    A byte changes in between, and a record is appended. The parser and a
    read of the range afterwards agree (both see the new byte), but the
    running hash holds the old one: the digest the frontier would keep is
    not the file's. Adding the bytes a second time finds it."""
    seen = {}
    for switch in (True, False):
        where = tmp_path / ("on" if switch else "off")
        where.mkdir()
        reference, candidate, transcript, full, actual = _stood(where, backend, switch)
        with full, actual:
            full.tick(0)
            actual.tick(0, faults=_how(switch))
            _append(transcript, backend, 40)
            full.tick(1)
            assert _decision(actual.tick(1, faults=_how(switch))) == "tail_applied"
            _append(transcript, backend, 41)
            moved = actual.tick(2, faults=_how(switch, **_moves("before_parse", "rewrite")))
            assert moved["digest_reads"][0] == "added"
            full.tick(3)
            after = actual.tick(3, faults=_how(switch))
            assert _texts(candidate) == _texts(reference)
            seen[switch] = (_decision(moved), "frontier_kept_source_grew" in _retentions(moved),
                            _decision(after))
    assert seen[True] == seen[False]
    assert seen[True][:2] == ("full_file_moved_during_parse", False)


@BACKENDS
def test_a_byte_of_an_earlier_tails_record_rewritten_after_the_parser_read_it(tmp_path, backend):
    """The tail parses from its boundary, so it reads again records an
    earlier tail imported. One of those changes in place after the parser
    read it, and a record is appended. The bytes this import added to the
    running hash are untouched, so the second hash agrees; what the parser
    was handed no longer hashes as the range read afterwards. Refused."""
    seen = {}
    for switch in (True, False):
        where = tmp_path / ("on" if switch else "off")
        where.mkdir()
        reference, candidate, transcript, full, actual = _stood(where, backend, switch)
        with full, actual:
            full.tick(0)
            actual.tick(0, faults=_how(switch))
            _append(transcript, backend, 40)
            full.tick(1)
            assert _decision(actual.tick(1, faults=_how(switch))) == "tail_applied"
            _append(transcript, backend, 41)
            moves = {"source_moves": {"when": "parse", "how": "rewrite_at",
                                      "words": "synthetic step 40"}}
            moved = actual.tick(2, faults=_how(switch, **moves))
            full.tick(3)
            after = actual.tick(3, faults=_how(switch))
            assert _texts(candidate) == _texts(reference)
            assert any(text == "Synthetic step 40" for _id, text in _texts(candidate))
            seen[switch] = (_decision(moved), "frontier_kept_source_grew" in _retentions(moved),
                            _decision(after))
    assert seen[True] == seen[False]
    assert seen[True][:2] == ("full_file_moved_during_parse", False)


# ---- the file moves while the tail's reader is in the middle of it ---------------------

def _several_reads(transcript, backend, first):
    """Records enough that the reader takes the tail in several reads. The
    last one's number."""
    last = first + 3 * io.DEFAULT_BUFFER_SIZE // 1024 + 1
    with transcript.open("ab") as output:
        for ordinal in range(first, last + 1):
            output.write(harness.append_bytes(backend, ordinal))
    return last


def _boundary(database):
    with sqlite3.connect(database) as connection:
        [(offset,)] = connection.execute(
            "SELECT boundary_offset FROM transcript_import_checkpoints")
    return offset


@SWITCH
@BACKENDS
def test_a_file_that_grows_while_the_reader_is_in_the_middle_of_the_tail(tmp_path, backend, switch):
    """What a working agent does to its transcript: a record is appended
    while the tail's reader has taken some of its range and not the rest.
    The bytes of the range are what they were, so it is the case of growth
    found after the parse."""
    reference, candidate, transcript, full, actual = _stood(tmp_path, backend, switch)
    with full, actual:
        full.tick(0)
        actual.tick(0, faults=_how(switch))
        _append(transcript, backend, 40)
        full.tick(1)
        assert _decision(actual.tick(1, faults=_how(switch))) == "tail_applied"
        _several_reads(transcript, backend, 41)
        parsed, boundary = transcript.stat().st_size, _boundary(candidate)
        full.tick(2)
        grown = actual.tick(2, faults=_how(switch, **_moves("read")))
        assert boundary < grown["source_moved_at"] < parsed < transcript.stat().st_size
        harness.assert_product_equal(reference, candidate)        # the file as it was parsed
        if switch:
            assert _decision(grown) == "deferred_tail_source_changed"
            assert "frontier_kept_source_grew" in _retentions(grown)
            assert _checkpoint(candidate) == (parsed, parsed)
        else:
            assert _decision(grown) == "full_file_moved_during_parse"
        full.tick(3)
        after = actual.tick(3, faults=_how(switch))
        harness.assert_product_equal(reference, candidate)
        if switch:
            assert (_decision(after), after["digest_reads"]) == ("tail_applied", ["added"])


@pytest.mark.parametrize("where", ["behind the reader", "ahead of the reader"])
@BACKENDS
def test_a_byte_rewritten_while_the_reader_is_in_the_middle_of_the_tail(tmp_path, backend, where):
    """A byte of the range changes in place while the reader is in the
    middle of it, and a record is appended, so that it looks like growth.
    Behind the reader: the parser was handed the old byte, and the range
    read afterwards has the new one. Ahead of it: the parser and the read
    afterwards agree on the new byte, and the running hash, taken before the
    parse, holds the old one. Each of the two proofs refuses one of them."""
    behind = where == "behind the reader"
    seen = {}
    for switch in (True, False):
        home = tmp_path / ("on" if switch else "off")
        home.mkdir()
        reference, candidate, transcript, full, actual = _stood(home, backend, switch)
        with full, actual:
            full.tick(0)
            actual.tick(0, faults=_how(switch))
            _append(transcript, backend, 40)
            full.tick(1)
            assert _decision(actual.tick(1, faults=_how(switch))) == "tail_applied"
            last = _several_reads(transcript, backend, 41)
            words = f"step {40 if behind else last}"
            data = transcript.read_bytes()
            target = data.index(f"synthetic {words}".encode())
            assert target > _boundary(candidate)
            moves = {"when": "read", "how": "rewrite_at", "words": f"synthetic {words}"}
            moved = actual.tick(2, faults=_how(switch, source_moves=moves))
            assert (target < moved["source_moved_at"]) == behind
            assert moved["source_moved_at"] < len(data)
            full.tick(3)
            after = actual.tick(3, faults=_how(switch))
            assert _texts(candidate) == _texts(reference)
            assert any(text == f"Synthetic {words}" for _id, text in _texts(candidate))
            seen[switch] = (_decision(moved), "frontier_kept_source_grew" in _retentions(moved),
                            _decision(after))
    assert seen[True] == seen[False]
    assert seen[True][:2] == ("full_file_moved_during_parse", False)


@SWITCH
@BACKENDS
def test_the_first_tail_after_a_host_start_whose_file_grows(tmp_path, backend, switch):
    """That tail has read every imported byte before it parses, so the
    second proof is a second read of them all. Still less than the full
    import it would otherwise be."""
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=_how(switch))
        _append(transcript, backend, 40)
        parsed = transcript.stat().st_size
        full.tick(1)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as restarted:
            grown = restarted.tick(1, faults=_how(switch, **_moves("parse")))
            harness.assert_product_equal(reference, candidate)
            if switch:
                assert _decision(grown) == "deferred_tail_source_changed"
                assert grown["digest_reads"] == ["all", "all"]
                assert "frontier_kept_source_grew" in _retentions(grown)
                assert _checkpoint(candidate) == (parsed, parsed)
            else:
                assert _decision(grown) == "full_file_moved_during_parse"
            full.tick(2)
            after = restarted.tick(2, faults=_how(switch))
            harness.assert_product_equal(reference, candidate)
            if switch:
                assert (_decision(after), after["digest_reads"]) == ("tail_applied", ["added"])


@pytest.mark.parametrize("when", ["read", "parse", "store"])
@BACKENDS
def test_an_audit_by_digest_whose_file_grows_is_still_the_audit(tmp_path, backend, when):
    """With ``audit_grown_by_digest`` as well: the tail that audits read
    every imported byte, the file grows before it publishes, and the
    frontier it keeps starts the audit's clock again. Growth while the tail
    is parsed costs a second read of the imported bytes, for the second
    proof; without this switch the same import went on as a full one, which
    parsed the whole file and read it again (the review of #339)."""
    how = {"settings": {"tail_keeps_frontier_when_grown": True, "audit_grown_by_digest": True,
                        "audit_every": 1}}
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=how)
            _append(transcript, backend, 40)
            full.tick(1)
            assert _decision(actual.tick(1, faults=how)) == "tail_applied"
            if when == "read":
                _several_reads(transcript, backend, 41)
            else:
                _append(transcript, backend, 41)
            full.tick(2)
            grown = actual.tick(2, faults={**how, **_moves(when)})
            assert _decision(grown) == "deferred_tail_source_changed"
            assert grown["digest_reads"] == (["all"] if when == "store" else ["all", "all"])
            assert {"audited_by_digest", "frontier_kept_source_grew"} <= set(_retentions(grown))
            with sqlite3.connect(candidate) as connection:
                assert connection.execute(
                    "SELECT imports_since_audit FROM transcript_import_checkpoints").fetchone() == (0,)
            harness.assert_product_equal(reference, candidate)
            full.tick(3)
            assert _decision(actual.tick(3, faults=how)) == "tail_applied"
            harness.assert_product_equal(reference, candidate)


def test_the_switch_is_off_until_switched_on():
    from lib import transcript_incremental as inc
    assert inc._DEFAULTS["tail_keeps_frontier_when_grown"] is False
