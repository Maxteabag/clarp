"""10 Nov 2026: one day's event log was 11.4 GB and hook.log 1.45 GB; nothing
limited either by size. The maintenance worker now holds both under a bound."""
from __future__ import annotations

import datetime
import json
import threading

import pytest

from lib import eventlog, maintenance
from lib.hook_runtime import HookLogger

TODAY = datetime.date(2026, 11, 10)


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _numbered(path, count: int, *, start: int = 0) -> None:
    with path.open("a") as f:
        for n in range(start, start + count):
            f.write(json.dumps({"n": n, "pad": "p" * 80}) + "\n")


@pytest.fixture
def event_log_dir(tmp_path, monkeypatch):
    """The event log writes into tmp_path, into the file named for TODAY."""
    from lib import diagnostics_settings
    diagnostics_settings.update({"enabled": True, "categories": list(diagnostics_settings.CATEGORIES)})
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    monkeypatch.setattr(eventlog, "_path_for", lambda now=None: tmp_path / f"{TODAY.isoformat()}.jsonl")
    return tmp_path


# --- keep_tail ---------------------------------------------------------------

def test_keep_tail_keeps_the_newest_whole_lines_in_place(tmp_path):
    path = tmp_path / "a.log"
    _numbered(path, 1000)
    before, inode = path.stat().st_size, path.stat().st_ino

    removed = maintenance.keep_tail(path, 5000)

    assert path.stat().st_ino == inode, "the same file, for a writer that has it open"
    assert removed == before - path.stat().st_size
    assert 4000 < path.stat().st_size <= 5000
    numbers = [row["n"] for row in _lines(path)]       # every line parses: it starts at a line
    assert numbers == list(range(numbers[0], 1000))


def test_keep_tail_never_keeps_more_than_half(tmp_path):
    path = tmp_path / "a.log"
    _numbered(path, 1000)
    before = path.stat().st_size
    maintenance.keep_tail(path, before)
    assert before // 2 - 200 < path.stat().st_size <= before // 2
    assert _lines(path)[-1]["n"] == 999


def test_keep_tail_handles_an_empty_file_and_one_endless_line(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_bytes(b"")
    assert maintenance.keep_tail(empty, 10) == 0
    endless = tmp_path / "endless.log"
    endless.write_bytes(b"x" * 10_000)
    assert maintenance.keep_tail(endless, 100) == 10_000     # no line start to keep from
    assert endless.read_bytes() == b""


def test_lines_appended_during_the_copy_are_kept(tmp_path, monkeypatch):
    path = tmp_path / "a.log"
    _numbered(path, 1000)
    copy, calls = maintenance._copy_within, []

    def copy_then_append(fd, src, dst, length):
        end = copy(fd, src, dst, length)
        if not calls:
            _numbered(path, 50, start=1000)       # a writer lands after the lock-free copy
        calls.append(length)
        return end

    monkeypatch.setattr(maintenance, "_copy_within", copy_then_append)
    maintenance.keep_tail(path, 5000)
    numbers = [row["n"] for row in _lines(path)]
    assert numbers == list(range(numbers[0], 1050))
    assert calls[1] > 0, "the finish copied what was appended meanwhile"


def test_a_disk_too_full_for_the_copy_empties_the_file_instead(tmp_path, monkeypatch):
    import errno
    path = tmp_path / "a.log"
    _numbered(path, 1000)
    size = path.stat().st_size

    def full(fd, src, dst, length):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(maintenance, "_copy_within", full)
    assert maintenance.keep_tail(path, 5000) == size
    assert path.read_bytes() == b""

    def unreadable(fd, src, dst, length):
        raise OSError(errno.EIO, "Input/output error")

    _numbered(path, 1000)
    monkeypatch.setattr(maintenance, "_copy_within", unreadable)
    with pytest.raises(OSError):
        maintenance.keep_tail(path, 5000)       # any other failure is the caller's to log
    assert path.stat().st_size == size


def test_no_event_is_lost_or_torn_while_a_writer_appends_under_flock(tmp_path):
    """The event log's writer (append mode, flock per write) against repeated
    cuts: what is left is always a gapless run of whole lines up to the last."""
    path = tmp_path / "2026-11-10.jsonl"
    path.touch()
    total = 4000
    now = datetime.datetime(2026, 11, 10, 12, tzinfo=datetime.timezone.utc)
    cuts: list[int] = []

    def write():
        for n in range(total):
            eventlog._write_jsonl({"n": n, "pad": "p" * 80}, now)

    writer = threading.Thread(target=write)
    eventlog_dir, eventlog.LOG_DIR = eventlog.LOG_DIR, tmp_path
    try:
        writer.start()
        while writer.is_alive():
            if path.stat().st_size > 40_000:
                cuts.append(maintenance.keep_tail(path, 10_000))
        writer.join()
    finally:
        eventlog.LOG_DIR = eventlog_dir
    assert len(cuts) > 3 and all(cuts)
    numbers = [row["n"] for row in _lines(path)]
    assert numbers == list(range(numbers[0], total))


# --- the event-log directory ---------------------------------------------------

def test_event_log_budget_deletes_the_oldest_days_first(tmp_path):
    for day in ("2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"):
        (tmp_path / f"{day}.jsonl").write_bytes(b"x" * 399 + b"\n")
    (tmp_path / "notes.jsonl").write_bytes(b"x" * 9000)       # not a daily log: not counted, not touched

    removed = maintenance.prune_event_logs(
        tmp_path, max_age_ms=14 * maintenance.DAY_MS, today=TODAY, max_total_bytes=1000)

    assert removed == 4
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2026-11-09.jsonl", "2026-11-10.jsonl", "notes.jsonl"]


def test_event_logs_within_the_budget_are_left_alone(tmp_path):
    for day in ("2026-11-08", "2026-11-09", "2026-11-10"):
        (tmp_path / f"{day}.jsonl").write_bytes(b"x" * 399 + b"\n")
    assert maintenance.prune_event_logs(
        tmp_path, max_age_ms=14 * maintenance.DAY_MS, today=TODAY, max_total_bytes=1200) == 0
    assert len(list(tmp_path.iterdir())) == 3


def test_today_alone_over_the_budget_is_cut_down_and_says_so(event_log_dir):
    today = event_log_dir / "2026-11-10.jsonl"
    _numbered(event_log_dir / "2026-11-09.jsonl", 10)
    _numbered(today, 1000)
    size = today.stat().st_size

    removed = maintenance.prune_event_logs(
        event_log_dir, max_age_ms=14 * maintenance.DAY_MS, today=TODAY,
        max_total_bytes=40_000, today_keep_bytes=10_000)

    assert removed == 1                                   # yesterday went first
    assert today.exists(), "today's file is never deleted"
    rows = _lines(today)
    numbers = [row["n"] for row in rows[:-1]]
    assert numbers == list(range(numbers[0], 1000))       # the newest events, whole
    record = rows[-1]                                     # one record that says so
    assert record["event"] == "eventLogCut"
    assert f"removed_bytes={size - sum(len(json.dumps(r)) + 1 for r in rows[:-1])}" in record["detail"]["msg"]
    assert "max_bytes=40000" in record["detail"]["msg"]
    assert today.stat().st_size <= 10_000 + 400
    # Holding the bound, the next pass changes nothing.
    assert maintenance.prune_event_logs(
        event_log_dir, max_age_ms=14 * maintenance.DAY_MS, today=TODAY,
        max_total_bytes=40_000, today_keep_bytes=10_000) == 0
    assert _lines(today) == rows


# --- hook.log ------------------------------------------------------------------

def test_hook_log_under_its_bound_is_untouched(tmp_path):
    path = tmp_path / "hook.log"
    path.write_text("12:00:00 stop     1 hello\n" * 10)
    before = path.read_text()
    assert maintenance.prune_hook_log(path, max_bytes=10_000, keep_bytes=1000) == 0
    assert path.read_text() == before
    assert maintenance.prune_hook_log(tmp_path / "absent.log", max_bytes=10, keep_bytes=1) == 0


def test_hook_log_over_its_bound_keeps_its_newest_lines(tmp_path):
    path = tmp_path / "hook.log"
    with path.open("a") as f:
        for n in range(2000):
            f.write(f"12:00:00 stop     1 line {n}\n")
    size = path.stat().st_size

    removed = maintenance.prune_hook_log(path, max_bytes=20_000, keep_bytes=4000)

    lines = path.read_text().splitlines()
    assert removed == size - sum(len(line) + 1 for line in lines[:-1])
    kept = [int(line.rsplit(" ", 1)[1]) for line in lines[:-1]]
    assert kept == list(range(kept[0], 2000))
    assert f"hookLogCut removed_bytes={removed} max_bytes=20000" in lines[-1]
    assert path.stat().st_size <= 4000 + 200
    # The hooks keep appending to the same file afterwards.
    HookLogger("stop", path).log("after the cut")
    assert path.read_text().splitlines()[-1].endswith("after the cut")


# --- the worker ----------------------------------------------------------------

def test_the_worker_bounds_the_hook_log_it_was_given(tmp_path):
    hook_log = tmp_path / "cache" / "hook.log"
    hook_log.parent.mkdir()
    hook_log.write_text("12:00:00 stop     1 line\n" * 2000)
    policy = maintenance.Policy(hook_log_max_bytes=20_000, hook_log_keep_bytes=4000)

    counts = maintenance.MaintenanceWorker(
        audio_dir=tmp_path, policy=policy, hook_log=hook_log).run_once(checkpoint=False)

    assert counts["hook_log_bytes"] > 20_000
    assert hook_log.stat().st_size <= 4200
    assert "hook_log_bytes" not in maintenance.MaintenanceWorker(
        audio_dir=tmp_path, policy=policy).run_once(checkpoint=False)


def test_the_default_bounds_are_the_documented_ones():
    from lib import telemetry
    policy = maintenance.Policy()
    assert policy.event_log_max_bytes == 2 * 1024 ** 3
    assert policy.event_log_today_keep_bytes == 512 * 1024 ** 2
    assert (policy.hook_log_max_bytes, policy.hook_log_keep_bytes) == (100 * 1024 ** 2, 20 * 1024 ** 2)
    assert telemetry.DETAIL_BUDGET_BYTES == 1024 ** 3
    assert eventlog.MAX_DETAIL_BYTES == 32 * 1024


def test_the_size_check_rolls_telemetry_up_only_when_it_is_over_budget(tmp_path, monkeypatch):
    from lib import telemetry
    calls: list[dict] = []
    monkeypatch.setattr(telemetry, "rollup_and_prune",
                        lambda **kw: calls.append(kw) or {"telemetry_budget_details": 7})
    worker = maintenance.MaintenanceWorker(audio_dir=tmp_path, hook_log=tmp_path / "hook.log")

    monkeypatch.setattr(telemetry, "over_budget", lambda: False)
    assert worker.check_sizes() == {"event_logs": 0, "hook_log_bytes": 0}
    assert calls == []

    monkeypatch.setattr(telemetry, "over_budget", lambda: True)
    assert worker.check_sizes()["telemetry_budget_details"] == 7
    assert calls == [{"checkpoint": False}], "no exclusive checkpoint outside the hourly pass"


def test_one_failing_size_check_does_not_stop_the_others(tmp_path, monkeypatch):
    from lib import telemetry

    def fail(*args, **kwargs):
        raise OSError("disk says no")

    monkeypatch.setattr(maintenance, "prune_event_logs", fail)
    monkeypatch.setattr(maintenance, "prune_hook_log", fail)
    monkeypatch.setattr(telemetry, "over_budget", lambda: True)
    monkeypatch.setattr(telemetry, "rollup_and_prune", lambda **kw: {"telemetry_budget_details": 3})
    worker = maintenance.MaintenanceWorker(audio_dir=tmp_path, hook_log=tmp_path / "hook.log")
    assert worker.check_sizes() == {"telemetry_budget_details": 3}


def test_the_sizes_are_checked_between_the_hourly_passes(tmp_path):
    checks: list[int] = []
    worker = maintenance.MaintenanceWorker(
        audio_dir=tmp_path, interval_sec=0.2,
        policy=maintenance.Policy(size_check_interval_sec=0.02))
    worker.check_sizes = lambda: checks.append(1) or {}

    assert worker._wait_for_next_pass() is False          # the interval ran out: time for a full pass
    assert 2 <= len(checks) <= 10

    worker._stop.set()
    checks.clear()
    assert worker._wait_for_next_pass() is True           # stopped: no further check
    assert checks == []
