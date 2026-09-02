"""Per-turn usage accounting — the replacement for the statusline source.

The point of this table: it records what Clarp actually ran. The statusline it
replaces never fired under `-p`, so it captured none of Clarp's turns.
"""
from __future__ import annotations

import pathlib
import sys

_SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER))

from lib import backend_usage, turn_usage  # noqa: E402
from lib.db import now_ms  # noqa: E402


def _detail(tin=100, tout=50, cost=0.01, dur=1200):
    return {"tokens_in": tin, "tokens_out": tout,
            "cost_usd": cost, "duration_ms": dur, "trace_id": "t-1"}


def test_records_and_totals_a_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    turn_usage.record(backend="claude", agent_id="a1", detail=_detail())
    got = turn_usage.totals("claude")
    five = got["windows"]["five_hour"]
    assert five["turns"] == 1
    assert five["tokens_in"] == 100
    assert five["tokens_out"] == 50
    assert five["cost_usd"] == 0.01
    assert got["last_turn_at"] is not None


def test_sums_across_turns_and_separates_backends(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for _ in range(3):
        turn_usage.record(backend="claude", agent_id="a1", detail=_detail())
    turn_usage.record(backend="codex", agent_id="a2", detail=_detail(tin=7, tout=3))
    claude = turn_usage.totals("claude")["windows"]["five_hour"]
    codex = turn_usage.totals("codex")["windows"]["five_hour"]
    assert claude["turns"] == 3 and claude["tokens_in"] == 300
    assert codex["turns"] == 1 and codex["tokens_in"] == 7


def test_turn_with_no_usage_is_not_recorded(tmp_path, monkeypatch):
    """agy reports an empty usage dict; a failed turn may report nothing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    turn_usage.record(backend="agy", agent_id="a3",
                      detail={"trace_id": "t", "duration_ms": 5})
    assert turn_usage.totals("agy")["windows"]["five_hour"]["turns"] == 0
    assert turn_usage.totals("agy")["last_turn_at"] is None


def test_windows_are_rolling(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    turn_usage.record(backend="claude", agent_id="a1", detail=_detail())
    # Six hours on: outside five_hour, still inside seven_day.
    later = now_ms() + 6 * 60 * 60 * 1000
    got = turn_usage.totals("claude", now=later)
    assert got["windows"]["five_hour"]["turns"] == 0
    assert got["windows"]["seven_day"]["turns"] == 1


def test_backend_usage_reports_claude_from_accounting(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    turn_usage.record(backend="claude", agent_id="a1", detail=_detail())
    body = backend_usage.get_backend_usage(refresh_codex=False)
    claude = next(b for b in body["backends"] if b["backend"] == "claude")
    assert claude["source"] == "clarp-turn-accounting"
    # Spend, not headroom: there is no quota percentage to report.
    assert claude["used_percentage"] is None
    assert claude["windows"] == {}
    assert claude["totals"]["five_hour"]["tokens_in"] == 100
    assert claude["freshness"] == "fresh"


def test_claude_row_is_unknown_before_any_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    body = backend_usage.get_backend_usage(refresh_codex=False)
    claude = next(b for b in body["backends"] if b["backend"] == "claude")
    assert claude["freshness"] == "unknown"
    assert claude["error"] == "no turns recorded yet"


def test_prune_drops_old_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    turn_usage.record(backend="claude", agent_id="a1", detail=_detail())
    assert turn_usage.prune_old(max_age_ms=10 * 60 * 1000) == 0
    assert turn_usage.prune_old(max_age_ms=-1) == 1
