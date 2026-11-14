"""``parse_range(..., observed=...)``: the digest of the bytes a tail's parser
was handed, for ``tail_keeps_frontier_when_grown``. It is the range's own
digest only when the parser opened the range once and read all of it."""
from __future__ import annotations

from lib import backends, transcript_incremental as inc


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
