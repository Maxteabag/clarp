"""A full import reads its file once (``full_digest_while_parsing``).

A full import parsed the file and then read all of it a second time for the
digest its frontier keeps: twice the file, every time. The parser is handed
the bytes through a reader that hashes them as it takes them, so the digest
of what was parsed is there when the parse ends. With the switch on that
digest is used, when the file did not change while it was parsed (the same
size, modification time and change time as when it was opened). A file that
grew or was touched meanwhile is read again, as it always was.

The digest must be the one a second read gives, byte for byte. Here that is
checked against a digest the test takes of the file itself, and every case is
run with the switch off beside it. ``digest_reads`` counts the second reads:
"all" is every imported byte read again, [] is none.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_equivalence import _rewritten_outside_the_samples, _texts
from tests.readmodel.test_rm_n14_restart import _append, _decision, _world

ON = {"full_digest_while_parsing": True}
SWITCH = pytest.mark.parametrize("switch", [True, False], ids=["one pass", "as before"])
BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)


def _settings(switch, **more):
    return {"settings": {"full_digest_while_parsing": switch, **more}}


def _stored_digest(database):
    with sqlite3.connect(database) as connection:
        [(digest,)] = connection.execute("SELECT file_hash FROM transcript_import_frontiers")
    return digest


def _digest_of(transcript, size=None):
    """What a second read of the file gives: the digest the frontier keeps."""
    return hashlib.blake2b(transcript.read_bytes()[:size], digest_size=16).hexdigest()


def _retentions(report):
    return [e["code"] for e in report["tail_events"] if e["event"] == "transcriptTailRetention"]


@SWITCH
@BACKENDS
def test_a_full_import_of_a_file_that_stands_still_reads_it_once(tmp_path, backend, switch):
    """The first import, the tail after it, and a full import whose audit is
    due. The digest stored is the file's own each time; the tail goes on
    from the running hash the parse left, and reads only what was added."""
    how = _settings(switch)
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            first = actual.tick(0, faults=how)
            assert _decision(first) == "full_no_frontier(missing_unknown)"
            assert first["digest_reads"] == ([] if switch else ["all"])
            assert _stored_digest(candidate) == _digest_of(transcript)
            harness.assert_product_equal(reference, candidate)

            _append(transcript, backend, 40)
            full.tick(1)
            tail = actual.tick(1, faults=how)
            assert (_decision(tail), tail["digest_reads"]) == ("tail_applied", ["added"])
            assert _stored_digest(candidate) == _digest_of(transcript)
            harness.assert_product_equal(reference, candidate)

            _append(transcript, backend, 41)
            full.tick(2)
            audit = actual.tick(2, faults=_settings(switch, audit_every=1))
            assert _decision(audit) == "full_audit"
            assert audit["digest_reads"] == ([] if switch else ["all"])
            assert audit["prefix_changed"] == []
            assert _stored_digest(candidate) == _digest_of(transcript)
            harness.assert_product_equal(reference, candidate)

            _append(transcript, backend, 42)
            full.tick(3)
            again = actual.tick(3, faults=how)
            assert (_decision(again), again["digest_reads"]) == ("tail_applied", ["added"])
            assert _stored_digest(candidate) == _digest_of(transcript)
            harness.assert_product_equal(reference, candidate)


@SWITCH
@BACKENDS
def test_the_audit_still_finds_bytes_rewritten_in_place(tmp_path, backend, switch):
    """The audit's comparison: are the bytes an earlier import read still
    what they were? One old record was rewritten in place where no stored
    sample looks, and a tail missed it. The full import of the audit finds
    it, from the digest the parse took on its way past the earlier import's
    last byte, without a second read."""
    reference, candidate, transcript, rewrite = _rewritten_outside_the_samples(tmp_path, backend)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=_settings(switch))
            imported = _digest_of(transcript)
            rewrite()
            full.tick(1)
            missed = actual.tick(1, faults=_settings(switch))
            assert missed["apply_mode"] == "tail" and missed["prefix_changed"] == []
            assert _stored_digest(candidate) != _digest_of(transcript)       # the old bytes' digest
            assert imported != _digest_of(transcript, len(transcript.read_bytes()) - 1024)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 301))
            full.tick(2)
            found = actual.tick(2, faults=_settings(switch, audit_every=1))
    assert _decision(found) == "full_audit"
    assert found["digest_reads"] == ([] if switch else ["all"])
    [line] = found["prefix_changed"]
    assert f"file={transcript} what=imported_bytes" in line
    assert _stored_digest(candidate) == _digest_of(transcript)
    assert _texts(candidate) == _texts(reference)


@pytest.mark.parametrize("change", ["grow", "touch"])
@BACKENDS
def test_a_file_that_changed_while_it_was_parsed_is_read_again(tmp_path, backend, change):
    """Growth and a touch cannot be told from a rewrite without reading, so
    the switch changes nothing here: the second read is made, and what the
    import decides and keeps is what it decides and keeps with the switch
    off. The parser was handed the bytes up to the size the import began
    with; the digest is of those."""
    seen = {}
    for switch in (True, False):
        where = tmp_path / ("on" if switch else "off")
        where.mkdir()
        reference, candidate, transcript = _world(where, backend, records=40)
        size = transcript.stat().st_size
        with harness.PersistentLane(reference, transcript, backend, "reference") as full:
            with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
                full.tick(0)
                first = actual.tick(0, faults={**_settings(switch), "during_parse": change})
                assert first["digest_reads"] == ["all"]
                assert _stored_digest(candidate) == _digest_of(transcript, size)
                harness.assert_product_equal(reference, candidate)
                full.tick(1)
                after = actual.tick(1, faults=_settings(switch))
                harness.assert_product_equal(reference, candidate)
                assert _stored_digest(candidate) == _digest_of(transcript)
                seen[switch] = (_decision(first), _retentions(first), _decision(after),
                                after["digest_reads"])
    assert seen[True] == seen[False]
    assert seen[True][2:] == ("tail_applied", ["added"])


@pytest.mark.parametrize("change", ["rewrite", "rewrite_keep_times"])
@BACKENDS
def test_bytes_rewritten_while_the_file_was_parsed_are_caught_by_the_second_read(
        tmp_path, backend, change):
    """One byte of an old record changes in place after the parser read it.
    The parsed bytes are no longer the file's, and the second read says so:
    nothing is published. With the modification time put back only the
    change time differs, and that is enough for the second read to be made."""
    seen = {}
    for switch in (True, False):
        where = tmp_path / ("on" if switch else "off")
        where.mkdir()
        reference, candidate, transcript = _world(where, backend, records=40)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            first = actual.tick(0, faults={**_settings(switch), "during_parse": change})
        assert first["digest_reads"] == ["all"]
        assert "parsed_prefix_changed" in _retentions(first)
        with sqlite3.connect(candidate) as connection:
            assert connection.execute(
                "SELECT count(*) FROM transcript_import_frontiers").fetchone() == (0,)
        with harness.PersistentLane(reference, transcript, backend, "reference") as full:
            with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
                full.tick(1)
                again = actual.tick(1, faults=_settings(switch))
        # The rows are the full replay's. Not their revision numbers: the
        # rewritten row was written one import later than in the reference.
        assert _texts(candidate) == _texts(reference)
        assert any(text.startswith("Synthetic") for _id, text in _texts(candidate))
        assert _stored_digest(candidate) == _digest_of(transcript)
        assert again["digest_reads"] == ([] if switch else ["all"])      # the file stands still now
        seen[switch] = (_decision(first), _retentions(first), _decision(again))
    assert seen[True] == seen[False]


@SWITCH
@BACKENDS
def test_a_partial_last_record_publishes_nothing_either_way(tmp_path, backend, switch):
    """The file ends in half a record. The parser is handed it, the digest
    of those bytes is taken, and no frontier is published: the half record
    is read again when it is whole. Then the import is full and reads once."""
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    half = harness.append_bytes(backend, 40)
    with transcript.open("ab") as output:
        output.write(half[:len(half) // 2])
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            first = actual.tick(0, faults=_settings(switch))
            assert "partial_record" in _retentions(first) and first["digest_reads"] == []
            with sqlite3.connect(candidate) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM transcript_import_frontiers").fetchone() == (0,)
            harness.assert_product_equal(reference, candidate)
            with transcript.open("ab") as output:
                output.write(half[len(half) // 2:])
            full.tick(1)
            whole = actual.tick(1, faults=_settings(switch))
            assert whole["apply_mode"] == "full"
            assert whole["digest_reads"] == ([] if switch else ["all"])
            assert _stored_digest(candidate) == _digest_of(transcript)
            harness.assert_product_equal(reference, candidate)


@SWITCH
@BACKENDS
def test_an_unchanged_file_imported_in_full_at_its_audit_reads_once(tmp_path, backend, switch):
    """The earlier import read the whole file, so the digest of its bytes is
    the digest of all of them: taken as the parser reads its last byte."""
    how = _settings(switch, audit_unchanged_by_digest=False)
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=how)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as restarted:
            audit = restarted.tick(3600, faults=how)
    assert _decision(audit) == "full_audit"
    assert audit["digest_reads"] == ([] if switch else ["all"])
    assert audit["prefix_changed"] == []
    assert _stored_digest(candidate) == _digest_of(transcript)
    harness.assert_product_equal(reference, candidate)
