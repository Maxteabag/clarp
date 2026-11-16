"""``parse_range(..., observed=...)``: the digest of the bytes a tail's parser
was handed, for ``tail_keeps_frontier_when_grown``. It is the range's own
digest only when the parser opened the range once and read all of it."""
from __future__ import annotations

import pytest

from lib import backends, transcript_incremental as inc


def test_only_grew_is_the_same_file_longer_and_not_older():
    """``_only_grew``: what lets a tail keep its frontier when its file moved."""
    before = inc.Identity(device=7, inode=11, size=1000, mtime_ns=5_000)
    grew = lambda **changed: inc._only_grew(before, inc.Identity(**{
        "device": 7, "inode": 11, "size": 1001, "mtime_ns": 5_000, **changed}))
    assert grew() and grew(mtime_ns=5_001) and grew(size=10**12)
    assert not grew(mtime_ns=4_999)                 # longer, and older than it was
    assert not grew(size=1000) and not grew(size=1000, mtime_ns=6_000) and not grew(size=999)
    assert not grew(inode=12) and not grew(device=8)


def _file(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(b"".join(b"record %d\n" % n for n in range(4000)))
    return path


def _parser(monkeypatch, read):
    monkeypatch.setattr(backends, "parse_turns", lambda backend, path: read(path) or [])


def test_a_parser_that_reads_the_whole_range_once_leaves_the_ranges_digest(tmp_path, monkeypatch):
    path = _file(tmp_path)

    def read(ranged):
        with ranged.open() as source:
            for _line in source:
                pass

    _parser(monkeypatch, read)
    for start, end in ((0, 20000), (1234, 20000), (0, path.stat().st_size), (500, 500)):
        observed = {}
        inc.parse_range("claude", path, start, end, observed=observed)
        assert observed["hash"] == inc.range_hash(path, start, end)
    # The file is longer than the range; the digest is of the range.
    with path.open("ab") as output:
        output.write(b"appended\n")
    observed = {}
    inc.parse_range("claude", path, 1234, 20000, observed=observed)
    assert observed["hash"] == inc.range_hash(path, 1234, 20000)


def test_a_parser_that_stops_early_or_opens_twice_leaves_none(tmp_path, monkeypatch):
    path = _file(tmp_path)

    def half(ranged):
        with ranged.open() as source:
            source.read(100)

    def twice(ranged):
        for _ in range(2):
            with ranged.open() as source:
                source.read()

    def never(ranged):
        return None

    for read in (half, twice, never):
        _parser(monkeypatch, read)
        observed = {}
        inc.parse_range("claude", path, 0, 20000, observed=observed)
        assert observed == {"hash": None}, read.__name__


def test_without_the_asking_nothing_is_hashed(tmp_path, monkeypatch):
    path = _file(tmp_path)
    seen = []
    _parser(monkeypatch, lambda ranged: seen.append(getattr(ranged, "_full_read", None)))
    inc.parse_range("claude", path, 0, 20000)
    assert seen == [None]


# ---- a mark in the range, and whether the file stood still (tail_hashes_while_parsing) ----

def _read_all(ranged):
    with ranged.open() as source:
        for _line in source:
            pass


def test_a_mark_in_the_range_leaves_the_digest_of_the_bytes_up_to_it(tmp_path, monkeypatch):
    """The earlier import's part of the range, hashed on the way past its
    last byte, is what ``range_hash`` reads; and the file as the reader
    found it when it opened it."""
    path = _file(tmp_path)
    _parser(monkeypatch, _read_all)
    stat = path.stat()
    for start, end, mark in ((1234, 20000, 5000), (1234, 20000, 0), (1234, 20000, 18766),
                             (0, 9000, 1), (0, 9000, 8192), (0, 9000, 8193), (500, 500, 0)):
        observed = {"mark": mark}
        inc.parse_range("claude", path, start, end, observed=observed)
        assert observed["hash"] == inc.range_hash(path, start, end)
        assert observed["at_mark"] == inc.range_hash(path, start, start + mark), (start, end, mark)
        assert observed["opened"] == (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
                                      stat.st_ctime_ns)
    # No mark asked, none given.
    observed = {}
    inc.parse_range("claude", path, 0, 9000, observed=observed)
    assert set(observed) == {"hash"}
    # A parser that did not read it all leaves nothing to go by.
    _parser(monkeypatch, lambda ranged: ranged.open().readline())
    observed = {"mark": 10}
    inc.parse_range("claude", path, 0, 9000, observed=observed)
    assert (observed["hash"], observed["at_mark"], observed["opened"]) == (None, None, None)


def _observed(tmp_path, monkeypatch):
    path = _file(tmp_path)
    _parser(monkeypatch, _read_all)
    before = inc.identity(path)
    observed = {"mark": 100}
    inc.parse_range("claude", path, 0, before.size, observed=observed)
    return path, before, observed


def test_the_parse_stood_when_the_file_is_what_its_reader_opened(tmp_path, monkeypatch):
    path, before, observed = _observed(tmp_path, monkeypatch)
    assert inc._parse_stood(observed, path, before)
    assert not inc._parse_stood({}, path, before)
    assert not inc._parse_stood(None, path, before)
    assert not inc._parse_stood({**observed, "opened": None}, path, before)


@pytest.mark.parametrize("how", ["grown", "touched", "rewritten in place",
                                 "rewritten in place, the mtime put back", "replaced", "gone",
                                 "grown before the reader opened it",
                                 "rewritten before the reader opened it, the mtime put back"])
def test_the_parse_did_not_stand_when_the_file_changed(tmp_path, monkeypatch, how):
    """Each way a file can change under a tail's parse. The last two change
    it between the import's own look at the file and the reader's."""
    import os
    path = _file(tmp_path)
    _parser(monkeypatch, _read_all)
    before = inc.identity(path)
    stat = path.stat()
    if how == "grown before the reader opened it":
        with path.open("ab") as output:
            output.write(b"more\n")
    elif how == "rewritten before the reader opened it, the mtime put back":
        with path.open("r+b") as output:
            output.write(b"X")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    observed = {"mark": 100}
    inc.parse_range("claude", path, 0, before.size, observed=observed)
    if how == "grown":
        with path.open("ab") as output:
            output.write(b"more\n")
    elif how == "touched":
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    elif how.startswith("rewritten in place"):
        with path.open("r+b") as output:
            output.write(b"X")
        if how.endswith("put back"):
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            assert path.stat().st_mtime_ns == stat.st_mtime_ns
    elif how == "replaced":
        other = path.with_suffix(".new")
        other.write_bytes(path.read_bytes())
        os.utime(other, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(other, path)
    elif how == "gone":
        path.unlink()
    stood = inc._parse_stood(observed, path, before)
    # A rewrite before the reader opened the file is in what it was handed:
    # the digests are the file's, and they are what tells (tail_rewritten).
    assert stood is (how == "rewritten before the reader opened it, the mtime put back")


def test_decide_leaves_the_earlier_imports_bytes_to_the_parse_when_told(tmp_path):
    path = _file(tmp_path)
    ident = inc.identity(path)
    checkpoint = inc.Checkpoint(
        agent_id="a", backend_session_id="s", source_file=str(path), backend="claude",
        parser_version=inc.parser_version("claude"), device=ident.device, inode=ident.inode,
        size=ident.size, mtime_ns=ident.mtime_ns, complete_size=ident.size,
        boundary_offset=0, boundary_seq=0, turn_count=0,
        prefix_hash=inc.prefix_hash(path, 0), tail_hash="0" * 32,
        prefix_turns_hash="", parser_carry={}, store_carry={},
        imports_since_audit=0, audited_at=0, updated_at=0)
    how = dict(now_ms=0, audit_every=10**9, audit_seconds=float("inf"), max_tail_bytes=10**9)
    assert inc.decide(checkpoint, "claude", path, ident, **how) == "tail_rewritten"
    assert inc.decide(checkpoint, "claude", path, ident, tail_hash_later=True, **how) == ""


def test_the_hashing_tail_is_off_until_switched_on():
    assert inc._DEFAULTS["tail_hashes_while_parsing"] is False
