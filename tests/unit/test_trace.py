"""Tests for trace ID generation.

The file-based marker (~/.cache/clarp/trace/<session>) was retired
when the SQLite traces table became authoritative. Only new_id()
remains in this module.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import trace  # noqa: E402


def test_new_id_is_16_hex_chars():
    tid = trace.new_id()
    assert isinstance(tid, str)
    assert len(tid) == 16
    int(tid, 16)  # parses as hex


def test_new_id_is_unique_per_call():
    """16 hex chars = 64 bits of entropy; collisions across 100 calls
    would mean something is badly wrong with the RNG."""
    seen = {trace.new_id() for _ in range(100)}
    assert len(seen) == 100


def test_new_trace_id_is_the_minting_point_and_new_id_its_alias():
    tid = trace.new_trace_id()
    assert trace.is_canonical(tid)
    assert trace.new_id is trace.new_trace_id


def test_parse_trace_id_accepts_canonical_and_stored_legacy_forms():
    assert trace.parse_trace_id("0123456789abcdef") == "0123456789abcdef"
    assert trace.parse_trace_id(" 0123456789ABCDEF\n") == "0123456789abcdef"
    legacy_uuid = "123e4567-e89b-12d3-a456-426614174000"
    assert trace.parse_trace_id(legacy_uuid) == legacy_uuid
    assert trace.parse_trace_id("dream-0123456789abcdef") == "dream-0123456789abcdef"
    assert not trace.is_canonical(legacy_uuid)


def test_parse_trace_id_rejects_everything_else():
    for bad in ("", "   ", "0123456789abcde", "0123456789abcdefg", "xyz",
                "dream-", "dream-0123", None, 12, b"0123456789abcdef"):
        assert trace.parse_trace_id(bad) is None, bad
