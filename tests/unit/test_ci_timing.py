"""The opt-in CI timing writer: per-phase lines above a threshold, a bounded file,
a summary that still counts what it did not write."""
import json
from types import SimpleNamespace

import ci_timing


def _report(nodeid, when, seconds):
    return SimpleNamespace(nodeid=nodeid, when=when, outcome="passed", duration=seconds)


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_writes_meta_long_phases_and_a_summary_of_the_short_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_timing, "_state", {})
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
    ci_timing.session_start(str(tmp_path))
    ci_timing.phase(_report("t::slow", "call", 0.5))
    ci_timing.phase(_report("t::fast", "call", 0.001))
    ci_timing.session_end(0)
    rows = _lines(tmp_path / "timing-gw7.jsonl")
    assert [r["type"] for r in rows] == ["meta", "test", "summary"]
    assert rows[0]["worker"] == "gw7" and rows[0]["cpus"]
    assert (rows[1]["id"], rows[1]["phase"], rows[1]["sec"]) == ("t::slow", "call", 0.5)
    assert rows[2]["short_phases"]["count"] == 1 and rows[2]["truncated_lines"] == 0


def test_the_file_is_bounded_and_the_summary_reports_the_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_timing, "_state", {})
    monkeypatch.setattr(ci_timing, "MAX_BYTES", 2048)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    ci_timing.session_start(str(tmp_path))
    for n in range(100):
        ci_timing.phase(_report(f"t::case_{n}", "call", 1.0))
    ci_timing.session_end(1)
    path = tmp_path / "timing-gw0.jsonl"
    rows = _lines(path)
    assert path.stat().st_size <= 2048 + 512        # cap plus the final summary line
    assert rows[-1]["type"] == "summary" and rows[-1]["truncated_lines"] > 0
    assert rows[-1]["exit"] == 1


def test_without_a_directory_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_timing, "_state", {})
    ci_timing.session_start("")
    ci_timing.phase(_report("t::x", "call", 9.0))
    ci_timing.session_end(0)
    assert list(tmp_path.iterdir()) == []


def test_a_test_that_replaces_threading_timer_cannot_break_the_writer(tmp_path, monkeypatch):
    # CI 40223435447: a test patched threading.Timer with a factory function, and
    # isinstance(t, threading.Timer) raised inside the logreport hook, crashing
    # an xdist worker. The writer binds the real class at import instead.
    import threading
    monkeypatch.setattr(ci_timing, "_state", {})
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    ci_timing.session_start(str(tmp_path))
    monkeypatch.setattr(threading, "Timer", lambda *a, **k: None)
    ci_timing.phase(_report("t::patched", "call", 0.5))
    ci_timing.session_end(0)
    rows = _lines(tmp_path / "timing-gw3.jsonl")
    assert [r["type"] for r in rows] == ["meta", "test", "summary"]
    assert isinstance(rows[1]["timers"], int) and rows[-1]["errors"] == 0


def test_any_error_in_the_writer_is_counted_never_raised(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(ci_timing, "_state", {})
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw4")
    ci_timing.session_start(str(tmp_path))

    def broken():
        raise RuntimeError("thread table unavailable")
    monkeypatch.setattr(threading, "enumerate", broken)
    ci_timing.phase(_report("t::x", "call", 0.5))         # must not raise
    ci_timing.session_end(0)
    assert _lines(tmp_path / "timing-gw4.jsonl")[-1]["errors"] == 1
