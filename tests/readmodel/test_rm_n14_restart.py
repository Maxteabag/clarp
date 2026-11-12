"""The import frontier across a Host start, and what a tail reads.

Until schema 124 the two things that allow a tail import lived in the Host's
memory only: the frontier (the checkpoint's carry, the digest of the imported
bytes, the database rows it was published with) and the record that a file is
unchanged. Every Host start emptied both, so the first import of every
conversation after it was a full one, of a file that had not changed.

Each test runs a full-replay reference lane beside the candidate and compares
every product table after every tick. A lane is a process of its own: opening
a new one on the same database is a Host start.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from tests.readmodel import corpus
from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_equivalence import _clones


def _decision(report):
    [event] = [e for e in report["tail_events"] if e["event"] == "transcriptTailDecision"]
    return event["code"] + (f"({event['reason']})" if "reason" in event else "")


def _world(tmp_path, backend, records=3):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(records)))
    return reference, candidate, transcript


def _append(transcript, backend, ordinal):
    with transcript.open("ab") as output:
        output.write(harness.append_bytes(backend, ordinal))


def _frontiers(database):
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT count(*) FROM transcript_import_frontiers").fetchone()[0]


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_after_a_host_start_an_unchanged_file_is_not_imported_and_a_grown_one_is_a_tail(
        tmp_path, backend):
    reference, candidate, transcript = _world(tmp_path, backend)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as before:
            full.tick(0)
            assert _decision(before.tick(0)) == "full_no_frontier(missing_unknown)"
        assert _frontiers(candidate) == 1

        with harness.PersistentLane(candidate, transcript, backend, "candidate") as after:
            unchanged = after.tick(1)
            full.tick(1)
            assert _decision(unchanged) == "unchanged_file"
            assert unchanged["apply_mode"] == "unchanged" and unchanged["digest_reads"] == []
            harness.assert_product_equal(reference, candidate)

            # It grows: a tail. This process has not read the imported bytes
            # yet, so it reads them all once; after that only what is new.
            _append(transcript, backend, 3)
            full.tick(2)
            first = after.tick(2)
            assert (_decision(first), first["digest_reads"]) == ("tail_applied", ["all"])
            harness.assert_product_equal(reference, candidate)
            _append(transcript, backend, 4)
            full.tick(3)
            second = after.tick(3)
            assert (_decision(second), second["digest_reads"]) == ("tail_applied", ["added"])
            harness.assert_product_equal(reference, candidate)

        # Another start. The digest stored after "only what is new" is the
        # digest of the whole file: reading all of it again agrees.
        _append(transcript, backend, 5)
        full.tick(4)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as again:
            third = again.tick(4)
            assert (_decision(third), third["digest_reads"]) == ("tail_applied", ["all"])
            assert third["prefix_changed"] == []
            harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_reading_every_byte_at_every_tail_can_be_switched_back_on(tmp_path, backend):
    reference, candidate, transcript = _world(tmp_path, backend)
    every_tail = {"settings": {"digest_every_tail": True}}
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=every_tail)
            for tick in (1, 2, 3):
                _append(transcript, backend, 2 + tick)
                full.tick(tick)
                report = actual.tick(tick, faults=every_tail if tick < 3 else None)
                assert _decision(report) == "tail_applied"
                # The last tick runs with the default again: only the new bytes.
                assert report["digest_reads"] == (["all"] if tick < 3 else ["added"])
                harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_rows_written_while_the_host_was_down_make_the_next_change_a_full_import(
        tmp_path, backend):
    """The stored frontier holds only for the database rows it was published
    with. The runtime writes while the Host restarts; a full import may then
    rewrite rows though the file did not change (here: a turn row that the
    replies' traces go by), so it runs, as it did before."""
    reference, candidate, transcript = _world(tmp_path, backend)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as before:
            full.tick(0)
            before.tick(0)
        for database in (reference, candidate):
            with sqlite3.connect(database) as other:
                other.execute(
                    "INSERT INTO turns(agent_id,source,trace_id,started_at) VALUES (?,'pwa','while-down',?)",
                    (corpus.AGENT, harness.BASE_MS + 500))
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as after:
            assert _decision(after.tick(1)) == "full_no_frontier(missing_unknown)"
            full.tick(1)
            harness.assert_product_equal(reference, candidate)
            # That full import published again: the next change is a tail.
            _append(transcript, backend, 3)
            full.tick(2)
            assert _decision(after.tick(2)) == "tail_applied"
            harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_an_import_that_wrote_nothing_and_could_not_publish_keeps_the_stored_frontier(
        tmp_path, backend):
    """'Checkpoint not written': an import with nothing to write publishes by
    upgrading its read snapshot, which SQLite refuses once any other writer
    has committed. The frontier it started from is still the last one
    published, and its rows are untouched, so the next change is a tail from
    it. Before, the frontier was gone and the next import was a full one."""
    reference, candidate, transcript = _world(tmp_path, backend)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            _append(transcript, backend, 3)
            full.tick(1)
            assert _decision(actual.tick(1)) == "tail_applied"

            refused = actual.tick(2, faults={"checkpoint": True})      # nothing new to write
            full.tick(2)
            assert _decision(refused) == "tail_applied"
            assert [e["code"] for e in refused["tail_events"]
                    if e["event"] == "transcriptTailRetention"] == ["checkpoint_not_written"]
            harness.assert_product_equal(reference, candidate)

            _append(transcript, backend, 4)
            full.tick(3)
            report = actual.tick(3)
            # A tail, and without reading the file again: this process read it.
            assert (_decision(report), report["digest_reads"]) == ("tail_applied", ["added"])
            harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_an_import_that_wrote_rows_and_could_not_publish_is_followed_by_a_full_import(
        tmp_path, backend):
    """The other half: rows went in, the checkpoint did not. The stored
    frontier describes the rows before them, so it does not hold."""
    reference, candidate, transcript = _world(tmp_path, backend)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            _append(transcript, backend, 3)
            full.tick(1)
            assert _decision(actual.tick(1, faults={"checkpoint": True})) == "tail_applied"
            harness.assert_product_equal(reference, candidate)
            _append(transcript, backend, 4)
            full.tick(2)
            assert _decision(actual.tick(2)) == "full_no_frontier(publication_not_written)"
            harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("what", ["checkpoint_digest", "parser_version", "file_hash", "inputs_digest"])
def test_a_stored_frontier_that_does_not_match_is_not_used(tmp_path, what):
    backend = "claude"
    reference, candidate, transcript = _world(tmp_path, backend)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as before:
            full.tick(0)
            before.tick(0)
        with sqlite3.connect(candidate) as connection:
            assert connection.execute(
                f"UPDATE transcript_import_frontiers SET {what}='something-else'").rowcount == 1
        _append(transcript, backend, 3)
        full.tick(1)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as after:
            report = after.tick(1)
        if what == "file_hash":
            # Taken up, then refused when the imported bytes are read: the
            # digest of what is in the file is not the stored one.
            assert _decision(report) == "full_prefix_content_changed"
            [line] = report["prefix_changed"]
            assert f"file={transcript} what=imported_bytes" in line
        else:
            assert _decision(report) == "full_no_frontier(missing_unknown)"
            assert report["prefix_changed"] == []
        harness.assert_product_equal(reference, candidate)


def test_no_frontier_is_stored_for_a_session_with_oracle_history(tmp_path):
    """The one kind of session the process keeps no frontier for: after a
    Host start it is imported in full, as before."""
    backend = "claude"
    reference, candidate, transcript = _world(tmp_path, backend)
    for database in (reference, candidate):
        with sqlite3.connect(database) as other:
            # Whatever the table requires, with the agent as its own.
            columns = other.execute("PRAGMA table_info(oracle_delegations)").fetchall()
            values = {name: (corpus.AGENT if name == "agent_id" else
                             0 if "INT" in kind.upper() else "synthetic")
                      for _cid, name, kind, notnull, default, _pk in columns
                      if notnull and default is None or name == "agent_id"}
            other.execute(
                f"INSERT INTO oracle_delegations({','.join(values)}) "
                f"VALUES ({','.join('?' * len(values))})", list(values.values()))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as before:
            full.tick(0)
            before.tick(0)
        assert _frontiers(candidate) == 0
        _append(transcript, backend, 3)
        full.tick(1)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as after:
            assert _decision(after.tick(1)) == "full_no_frontier(missing_unknown)"
        harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("what", ["inode", "shrink", "prefix_rewritten"])
@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_a_file_that_is_not_the_imported_one_is_imported_in_full_and_named(tmp_path, backend, what):
    """After a Host start too: another file under the name, a shorter one, or
    one whose first bytes were rewritten. One line says which check found it."""
    reference, candidate, transcript = _world(tmp_path, backend, records=6)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as before:
            full.tick(0)
            before.tick(0)
        content = transcript.read_bytes()
        if what == "inode":
            replacement = transcript.with_suffix(".new")
            replacement.write_bytes(content + harness.append_bytes(backend, 6))
            replacement.replace(transcript)
        elif what == "shrink":
            transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(4)))
        else:
            changed = content.replace(b"synthetic request 0", b"synthetic REQUEST 0", 1)
            assert changed != content and len(changed) == len(content)
            transcript.write_bytes(changed + harness.append_bytes(backend, 6))
        full.tick(1)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as after:
            report = after.tick(1)
        assert _decision(report) == f"full_{what}"
        [line] = report["prefix_changed"]
        assert f"file={transcript} what={what} " in line
        harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_a_file_touched_while_the_host_was_down_is_read_not_taken_for_unchanged(tmp_path, backend):
    """The same size is not the same file: the mtime moved. Every imported
    byte is read; they are what they were, so it is a tail with nothing new."""
    reference, candidate, transcript = _world(tmp_path, backend)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as before:
            full.tick(0)
            before.tick(0)
        stat = transcript.stat()
        os.utime(transcript, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        full.tick(1)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as after:
            report = after.tick(1)
        assert (_decision(report), report["digest_reads"]) == ("tail_applied", ["all"])
        assert report["prefix_changed"] == []
        harness.assert_product_equal(reference, candidate)
