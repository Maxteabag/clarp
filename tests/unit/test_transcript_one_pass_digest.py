"""The digests a full import takes while its parser reads (``_parsed_digests``)
are the ones a second read of the file gives (``_read_digest``), byte for byte.
"""
from __future__ import annotations

import hashlib
import os
import random

import pytest

from lib import transcript_incremental as inc


def _file(tmp_path, size, seed=1):
    chance = random.Random(seed)
    lines, total = [], 0
    while total < size:
        line = bytes(chance.choices(b"abcdefghij \xc3\xa9{}\"", k=chance.randint(0, 9000))) + b"\n"
        lines.append(line)
        total += len(line)
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(b"".join(lines)[:size - 1] + b"\n")
    return path


def _parsed(path, size, mark, *, how="lines"):
    """Read [0, size) the way a parser does, through the observing reader."""
    before = inc.identity(path)
    observed = inc._RangePath(path, start=0, end=size)
    observed._full_read = {"hash": hashlib.blake2b(digest_size=16), "bytes": 0, "opens": 0,
                           "file": None, "mark": mark}
    with observed.open(encoding="utf-8", errors="replace") as source:
        if how == "lines":
            for _line in source:
                pass
        else:
            while source.read(how):
                pass
    read = observed._full_read
    assert (read["opens"], read["bytes"]) == (1, size)
    evidence = (size, read["hash"].hexdigest(), *read["file"])
    return before, read, evidence


SIZE = 300_000


@pytest.mark.parametrize("how", ["lines", 1, 4096, 1 << 20])
@pytest.mark.parametrize("mark", [None, 0, 1, 4095, 4096, 4097, 8192, 65536, 131071, 131072,
                                  SIZE - 1, SIZE])
def test_the_parses_digests_are_a_second_reads(tmp_path, mark, how):
    path = _file(tmp_path, SIZE)
    before, read, evidence = _parsed(path, SIZE, mark, how=how)
    earlier = None if mark is None else (mark, "what the earlier import stored")
    (previous, current, hasher), why = inc._parsed_digests(read, evidence, before, earlier, path)
    assert why == "saved"
    again = inc._read_digest(path, SIZE if mark is None else mark, SIZE)
    assert (previous, current, hasher.hexdigest()) == (again[0], again[1], again[2].hexdigest())
    data = path.read_bytes()
    assert current == hashlib.blake2b(data, digest_size=16).hexdigest()
    assert previous == hashlib.blake2b(data[:SIZE if mark is None else mark],
                                       digest_size=16).hexdigest()
    # The running hash goes on as the second read's does: a later tail adds to it.
    with path.open("ab") as output:
        output.write(b"one more record\n")
    grown = SIZE + len(b"one more record\n")
    assert inc._read_digest(path, SIZE, grown, digest=hasher)[1] \
        == inc._read_digest(path, SIZE, grown)[1]
    # The parse's own hash was not advanced by that.
    assert read["hash"].hexdigest() == current


def test_a_parse_of_part_of_the_file_digests_that_part(tmp_path):
    """The parser is handed the bytes up to the size the import began with."""
    path = _file(tmp_path, SIZE)
    with path.open("ab") as output:
        output.write(b"appended before the parse opened the file\n")
    _before, read, evidence = _parsed(path, SIZE, 1000)
    assert evidence[1] == inc._read_digest(path, 1000, SIZE)[1]
    assert read["at_mark"] == inc._read_digest(path, 1000, SIZE)[0]


def _change(path, how):
    stat = path.stat()
    if how == "grown":
        with path.open("ab") as output:
            output.write(b"more\n")
    elif how == "touched":
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    elif how == "rewritten in place":
        with path.open("r+b") as output:
            output.write(b"X")
    elif how == "rewritten in place, times put back":
        with path.open("r+b") as output:
            output.write(b"X")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert path.stat().st_mtime_ns == stat.st_mtime_ns
    elif how == "replaced":
        data = path.read_bytes()
        other = path.with_suffix(".new")
        other.write_bytes(data)
        os.utime(other, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(other, path)
    elif how == "gone":
        path.unlink()


@pytest.mark.parametrize("how,why", [
    ("grown", "grew"), ("touched", "touched"), ("rewritten in place", "touched"),
    ("rewritten in place, times put back", "touched"),
    ("replaced", "identity_changed"), ("gone", "identity_changed")])
def test_a_file_that_changed_since_the_parse_opened_it_must_be_read(tmp_path, how, why):
    """And the one word the journal gets for it."""
    path = _file(tmp_path, SIZE)
    before, read, evidence = _parsed(path, SIZE, None)
    assert inc._parsed_digests(read, evidence, before, None, path)[1] == "saved"
    _change(path, how)
    assert inc._parsed_digests(read, evidence, before, None, path) == (None, why)


def test_a_file_that_is_shorter_than_it_was_is_another_file(tmp_path):
    path = _file(tmp_path, SIZE)
    before, read, evidence = _parsed(path, SIZE, None)
    with path.open("r+b") as output:
        output.truncate(SIZE - 10)
    assert inc._parsed_digests(read, evidence, before, None, path) == (None, "identity_changed")


def test_a_file_that_grew_before_the_parse_opened_it_must_be_read(tmp_path):
    path = _file(tmp_path, SIZE)
    before = inc.identity(path)
    with path.open("ab") as output:
        output.write(b"more\n")
    _later, read, evidence = _parsed(path, SIZE, None)
    assert inc._parsed_digests(read, evidence, before, None, path) == (None, "grew")


def test_without_the_switchs_mark_or_with_another_size_it_must_be_read(tmp_path):
    path = _file(tmp_path, SIZE)
    before, read, evidence = _parsed(path, SIZE, 5000)
    assert inc._parsed_digests(read, evidence, before, (5000, "x"), path)[1] == "saved"
    # The earlier import's size is not the one that was marked.
    moved = (None, "earlier_import_moved")
    assert inc._parsed_digests(read, evidence, before, (6000, "x"), path) == moved
    # No mark was asked for, and an earlier import is there to compare with.
    before, read, evidence = _parsed(path, SIZE, None)
    assert inc._parsed_digests(read, evidence, before, (5000, "x"), path) == moved
    # The parse was not observed, or not the whole of it was.
    assert inc._parsed_digests(None, evidence, before, None, path) == (None, "not_observed")
    assert inc._parsed_digests(read, None, before, None, path) == (None, "not_observed")


def test_the_switch_is_off_until_switched_on():
    assert inc._DEFAULTS["full_digest_while_parsing"] is False


def test_the_journals_line_is_a_word_a_count_a_backend_and_a_size(monkeypatch):
    from lib import log as host_log
    seen = []
    monkeypatch.setattr(host_log, "log", lambda event, detail="": seen.append((event, detail)))
    inc._second_read_log("saved", "claude", 300 * 1024 * 1024)
    inc._second_read_log("grew", "a backend nobody knows", None)
    assert seen == [("transcriptFullDigest", "code=saved count=1 backend=claude size=lt1024m"),
                    ("transcriptFullDigest", "code=grew count=1 backend=other size=unknown")]
