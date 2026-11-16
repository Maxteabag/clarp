"""The audit of a grown file, by digest (``audit_grown_by_digest``).

A tail does not read the bytes it imported before. The audit is what bounds
that: after ``audit_every`` imports or ``audit_seconds`` the next import of
a grown file was a full one, which parses the whole file, replays every
turn and so reads every imported byte. What it is there to find is whether
those bytes are still what was imported; that is one read and a comparison
with the stored digest. With the switch on a due audit of a grown file is
that read, then a tail, and the tail starts the audit's clock again. A
mismatch is a full import, as it always was.

``digest_reads`` is "all" where every imported byte was read again and
"added" where only the new ones were.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_equivalence import _rewritten_outside_the_samples, _texts
from tests.readmodel.test_rm_n14_restart import _append, _decision, _world

BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)
SWITCH = pytest.mark.parametrize("switch", [True, False], ids=["by digest", "as before"])


def _how(switch, **more):
    """What a full import reads is counted as the defaults read: the file a
    second time for its digest. Said, not left to the default
    (``full_digest_while_parsing`` is on in the Hosts' configuration)."""
    return {"settings": {"audit_grown_by_digest": switch, "full_digest_while_parsing": False,
                         **more}}


def _clock(database):
    """(imports since the audit, when it was audited) as the checkpoint has them."""
    with sqlite3.connect(database) as connection:
        [found] = connection.execute(
            "SELECT imports_since_audit, audited_at FROM transcript_import_checkpoints")
    return found


def _retentions(report):
    return [e["code"] for e in report["tail_events"] if e["event"] == "transcriptTailRetention"]


@SWITCH
@BACKENDS
def test_a_grown_file_whose_audit_is_due_by_count(tmp_path, backend, switch):
    """Every third import is an audit. By digest it is a tail that read every
    imported byte and found them the stored ones; the count starts again,
    and the imports between read only what was added."""
    how = _how(switch, audit_every=3)
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    seen = []
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=how)
            for tick in range(1, 9):
                _append(transcript, backend, 40 + tick)
                full.tick(tick)
                report = actual.tick(tick, faults=how)
                harness.assert_product_equal(reference, candidate)
                seen.append((_decision(report), report["digest_reads"],
                             "audited_by_digest" in _retentions(report), _clock(candidate)[0]))
    plain = ("tail_applied", ["added"], False)
    if switch:
        audit = ("tail_applied", ["all"], True, 0)
        assert seen == [(*plain, 1), (*plain, 2), (*plain, 3), audit,
                        (*plain, 1), (*plain, 2), (*plain, 3), audit]
    else:
        audit = ("full_audit", ["all"], False, 0)
        assert seen == [(*plain, 1), (*plain, 2), (*plain, 3), audit,
                        (*plain, 1), (*plain, 2), (*plain, 3), audit]


@SWITCH
@BACKENDS
def test_a_grown_file_whose_audit_is_due_by_time(tmp_path, backend, switch):
    """An hour after the last audit (a lane's clock is its tick, in
    seconds). The tail that audits by digest records when; a second later
    the audit is not due."""
    how = _how(switch)
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=how)
            _append(transcript, backend, 41)
            full.tick(3599)
            early = actual.tick(3599, faults=how)
            assert (_decision(early), early["digest_reads"]) == ("tail_applied", ["added"])
            before = _clock(candidate)
            _append(transcript, backend, 42)
            full.tick(3600)
            due = actual.tick(3600, faults=how)
            assert due["digest_reads"] == ["all"]
            assert _decision(due) == ("tail_applied" if switch else "full_audit")
            assert ("audited_by_digest" in _retentions(due)) is switch
            count, audited_at = _clock(candidate)
            assert count == 0 and audited_at - before[1] == 3_600_000
            harness.assert_product_equal(reference, candidate)
            _append(transcript, backend, 43)
            full.tick(3601)
            after = actual.tick(3601, faults=how)
            assert (_decision(after), after["digest_reads"]) == ("tail_applied", ["added"])
            assert _clock(candidate) == (1, audited_at)
            harness.assert_product_equal(reference, candidate)


@SWITCH
@BACKENDS
def test_the_audit_finds_bytes_rewritten_in_place_either_way(tmp_path, backend, switch):
    """What the audit is for. One old record was rewritten in place where no
    stored sample looks, and a tail missed it: the store holds the old
    words. At the audit the imported bytes no longer hash to the stored
    digest. By digest that is found by the read, and the import is the full
    one that puts the rows right; without the switch the audit was that
    full import anyway."""
    reference, candidate, transcript, rewrite = _rewritten_outside_the_samples(tmp_path, backend)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=_how(switch))
            rewrite()
            full.tick(1)
            missed = actual.tick(1, faults=_how(switch))
            assert missed["apply_mode"] == "tail" and missed["prefix_changed"] == []
            assert not any("synthetic task 8" in text for _id, text in _texts(candidate))
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 301))
            full.tick(2)
            found = actual.tick(2, faults=_how(switch, audit_every=1))
    assert found["apply_mode"] == "full"
    assert _decision(found) == ("full_prefix_content_changed" if switch else "full_audit")
    [line] = found["prefix_changed"]
    assert f"file={transcript} what=imported_bytes" in line
    assert _texts(candidate) == _texts(reference)
    assert any("synthetic task 8" in text for _id, text in _texts(candidate))
    assert _clock(candidate)[0] == 0


@BACKENDS
def test_a_tail_too_large_is_still_a_full_import_when_its_audit_is_due(tmp_path, backend):
    """The audit by digest is a tail, so everything that refuses a tail
    still does. The file grew by more than a tail may hold."""
    how = _how(True, audit_every=1, max_tail_bytes=4096)
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=how)
            _append(transcript, backend, 40)
            full.tick(1)
            assert _decision(actual.tick(1, faults=how)) == "tail_applied"
            assert _clock(candidate)[0] == 1                # the next import's audit is due
            for ordinal in range(41, 49):
                _append(transcript, backend, ordinal)
            full.tick(2)
            report = actual.tick(2, faults=how)
            assert _decision(report) == "full_tail_too_large"
            assert "audited_by_digest" not in _retentions(report)
            assert _clock(candidate)[0] == 0
            harness.assert_product_equal(reference, candidate)


@BACKENDS
def test_an_unchanged_file_is_not_this_switchs_business(tmp_path, backend):
    """An unchanged file whose audit is due is read and compared without an
    import (``audit_unchanged_by_digest``), or imported in full; this switch
    changes neither."""
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    for unchanged_by_digest, wanted in ((True, "unchanged_file_audited"), (False, "full_audit")):
        how = _how(True, audit_unchanged_by_digest=unchanged_by_digest)
        with harness.PersistentLane(reference, transcript, backend, "reference") as full:
            with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
                full.tick(0)
                actual.tick(0, faults=how)
            with harness.PersistentLane(candidate, transcript, backend, "candidate") as restarted:
                report = restarted.tick(3600, faults=how)
        assert _decision(report) == wanted
        assert "audited_by_digest" not in _retentions(report)
        harness.assert_product_equal(reference, candidate)


# ---- every Nth audit is still the full import (audit_full_every) ----------------------

def _by_digest(database):
    """The checkpoint's count of audits by digest since its last full import."""
    with sqlite3.connect(database) as connection:
        [(count,)] = connection.execute(
            "SELECT json_extract(store_carry_json, '$.audits_by_digest') "
            "FROM transcript_import_checkpoints")
    return count


def _ticks(tmp_path, backend, how, last, *, other=None):
    """Imports 1 to ``last`` of a transcript that grows by a record each
    time, the audit due at every second one. [(tick, decision, said in the
    journal, the count)]. ``other``: {tick: settings for that one import}."""
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    seen = []
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0, faults=how)
            for tick in range(1, last + 1):
                _append(transcript, backend, 40 + tick)
                full.tick(tick)
                faults = how if tick not in (other or {}) else {
                    "settings": {**how["settings"], **other[tick]}}
                report = actual.tick(tick, faults=faults)
                harness.assert_product_equal(reference, candidate)
                seen.append((tick, _decision(report), "audited_by_digest" in _retentions(report),
                             _by_digest(candidate)))
    return seen


TAIL, FULL = "tail_applied", "full_audit"


@pytest.mark.parametrize("every", [1, 2, 3])
@BACKENDS
def test_every_nth_due_audit_is_still_the_full_import(tmp_path, backend, every):
    """A full import replays every turn and so puts right what a tail left
    wrong; the audit by digest checks only the bytes. So one audit in
    ``audit_full_every`` stays what it was. With 1 every audit is the full
    import, which is how it is without the switch."""
    seen = _ticks(tmp_path, backend, _how(True, audit_every=1, audit_full_every=every), 12)
    wanted, since = [], 0
    for tick in range(1, 13):
        if tick % 2:
            wanted.append((tick, TAIL, False, since or None))        # not due
        elif since + 1 < every:
            since += 1
            wanted.append((tick, TAIL, True, since))                 # the audit, by digest
        else:
            since = 0
            wanted.append((tick, FULL, False, None))                 # the Nth: a full import
    assert seen == wanted
    assert [decision for _tick, decision, _said, _count in seen].count(FULL) == 6 // every


@BACKENDS
def test_a_full_import_for_another_reason_starts_the_count_again(tmp_path, backend):
    """Two audits by digest, then a full import that is no audit (the tail
    had grown past what a tail may hold). It replayed every turn too, so the
    next two audits are by digest again, and the one after is full."""
    how = _how(True, audit_every=1, audit_full_every=3)
    seen = _ticks(tmp_path, backend, how, 11, other={5: {"max_tail_bytes": 4096}})
    assert seen == [
        (1, TAIL, False, None), (2, TAIL, True, 1), (3, TAIL, False, 1), (4, TAIL, True, 2),
        (5, "full_tail_too_large", False, None),
        (6, TAIL, False, None), (7, TAIL, True, 1), (8, TAIL, False, 1), (9, TAIL, True, 2),
        (10, TAIL, False, 2), (11, FULL, False, None)]


@BACKENDS
def test_with_the_other_switches_on_the_audits_are_the_same(tmp_path, backend):
    """The switches a Host can run with beside this one: the full import
    that digests while it parses, the live rows, a turn's end as a tail, the
    yield after the first write. The audits fall where they fall without
    them, each import leaves what the full replay leaves (``_ticks`` compares
    every one), and the full import that every third audit still is reads
    its file once."""
    alone = _how(True, audit_every=1, audit_full_every=3)
    others = {"full_digest_while_parsing": True, "carry_live_rows": True,
              "carry_settled_turns": True, "tail_yield_after_first_write": True}
    together = {"settings": {**alone["settings"], **others}}
    where = [tmp_path / "alone", tmp_path / "together"]
    for directory in where:
        directory.mkdir()
    assert _ticks(where[0], backend, alone, 7) == _ticks(where[1], backend, together, 7) == [
        (1, TAIL, False, None), (2, TAIL, True, 1), (3, TAIL, False, 1), (4, TAIL, True, 2),
        (5, TAIL, False, 2), (6, FULL, False, None), (7, TAIL, False, None)]
    reference, candidate, transcript = _world(tmp_path, backend, records=40)
    with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
        actual.tick(0, faults=together)
        said = []
        for tick in range(1, 7):
            _append(transcript, backend, 40 + tick)
            report = actual.tick(tick, faults=together)
            said.append((_decision(report), report["digest_reads"], report["second_read"]))
    assert said == [
        (TAIL, ["added"], []), (TAIL, ["all"], []), (TAIL, ["added"], []), (TAIL, ["all"], []),
        (TAIL, ["added"], []), (FULL, [], ["saved"])]


def test_the_switch_is_off_until_switched_on():
    from lib import transcript_incremental as inc
    assert inc._DEFAULTS["audit_grown_by_digest"] is False
    assert inc._DEFAULTS["audit_unchanged_by_digest"] is True
    assert inc._DEFAULTS["audit_full_every"] == 8
