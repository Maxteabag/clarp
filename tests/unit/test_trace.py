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
