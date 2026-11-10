"""10 Nov 2026: one day's event log was 11.4 GB and hook.log 1.45 GB; nothing
limited either by size. The maintenance worker now holds both under a bound,
and only ever cuts or deletes files that are provably Clarp's own logs."""
from __future__ import annotations

import datetime
import errno
import fcntl
import json
import os
import threading

import pytest

from lib import eventlog, maintenance
from lib.hook_runtime import HookLogger

TODAY = datetime.date(2026, 11, 10)
NOON = datetime.datetime(2026, 11, 10, 12, tzinfo=datetime.timezone.utc)
DAYS14 = 14 * maintenance.DAY_MS


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _ns(path) -> list[int]:
    return [row["detail"]["n"] for row in _lines(path) if "n" in row.get("detail", {})]


def _record(n: int) -> dict:
    """One event as lib.eventlog writes it."""
    return {"ts": "2026-11-10T12:00:00.000Z", "source": "test", "event": "numbered",
            "level": "info", "detail": {"n": n, "pad": "p" * 40}}


def _numbered(path, count: int, *, start: int = 0) -> None:
    with path.open("a") as f:
        for n in range(start, start + count):
            f.write(json.dumps(_record(n), separators=(",", ":")) + "\n")


def _event_file(path, size: int) -> None:
    """An event log of exactly ``size`` bytes."""
    _numbered(path, 1)
    with path.open("a") as f:
        f.write("x" * (size - path.stat().st_size - 1) + "\n")
    assert path.stat().st_size == size


@pytest.fixture
def failures(monkeypatch) -> list[tuple]:
    """What the prune logged as failed: (event, exception class, detail)."""
    logged: list[tuple] = []
    monkeypatch.setattr(maintenance, "log_exception",
                        lambda event, exc, detail="": logged.append((event, type(exc).__name__, detail)))
    return logged


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
    numbers = _ns(path)                                # every line parses: it starts at a line
    assert numbers == list(range(numbers[0], 1000))


def test_keep_tail_never_keeps_more_than_half(tmp_path):
    path = tmp_path / "a.log"
    _numbered(path, 1000)
    before = path.stat().st_size
    maintenance.keep_tail(path, before)
    assert before // 2 - 200 < path.stat().st_size <= before // 2
    assert _ns(path)[-1] == 999


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
    numbers = _ns(path)
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
            eventlog._write_jsonl(_record(n), now)

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
    numbers = _ns(path)
    assert numbers == list(range(numbers[0], total))


# --- the event-log directory ---------------------------------------------------

def test_event_log_budget_deletes_the_oldest_days_first(tmp_path, failures):
    for day in ("2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"):
        _event_file(tmp_path / f"{day}.jsonl", 400)
    (tmp_path / "notes.jsonl").write_bytes(b"x" * 9000)       # not a daily log: not counted, not touched

    removed = maintenance.prune_event_logs(
        tmp_path, max_age_ms=DAYS14, today=TODAY, max_total_bytes=1000)

    assert removed == 4
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2026-11-09.jsonl", "2026-11-10.jsonl", "notes.jsonl"]
    assert failures == []


def test_event_logs_within_the_budget_are_left_alone(tmp_path, failures):
    for day in ("2026-11-08", "2026-11-09", "2026-11-10"):
        _event_file(tmp_path / f"{day}.jsonl", 400)
    assert maintenance.prune_event_logs(
        tmp_path, max_age_ms=DAYS14, today=TODAY, max_total_bytes=1200) == 0
    assert len(list(tmp_path.iterdir())) == 3
    assert maintenance.prune_event_logs(tmp_path / "absent", max_age_ms=DAYS14, today=TODAY,
                                        max_total_bytes=1) == 0
    assert failures == [], "no event log yet is not a failure"


def test_today_alone_over_the_budget_is_cut_down_and_says_so(event_log_dir, failures):
    today = event_log_dir / "2026-11-10.jsonl"
    _numbered(event_log_dir / "2026-11-09.jsonl", 10)
    _numbered(today, 1000)
    size = today.stat().st_size

    def prune() -> int:
        return maintenance.prune_event_logs(
            event_log_dir, max_age_ms=DAYS14, today=TODAY,
            max_total_bytes=40_000, today_keep_bytes=10_000)

    assert prune() == 1                                   # yesterday went first
    assert today.exists(), "today's file is never deleted"
    raw = today.read_bytes().splitlines(keepends=True)
    rows = [json.loads(line) for line in raw]
    numbers = [row["detail"]["n"] for row in rows[:-1]]
    assert numbers == list(range(numbers[0], 1000))       # the newest events, whole
    record = rows[-1]                                     # one record that says so
    assert record["event"] == "eventLogCut"
    assert f"removed_bytes={size - sum(len(line) for line in raw[:-1])}" in record["detail"]["msg"]
    assert "max_bytes=40000" in record["detail"]["msg"]
    assert today.stat().st_size <= 10_000 + 400
    # Holding the bound, the next pass changes nothing.
    assert prune() == 0
    assert today.read_bytes().splitlines(keepends=True) == raw
    assert failures == []


def test_what_the_event_log_writes_is_recognised_as_an_event_log(event_log_dir):
    """The budget only touches files that start like an event log, so the
    writer's own format must pass: emit, the drop report and the wait log."""
    eventlog.emit("server", "real", detail={"msg": "hello"})
    eventlog._report_dropped(NOON, dropped=True, force=True)
    names = sorted(p.name for p in event_log_dir.glob("*.jsonl"))
    dir_fd = maintenance._open_log_dir(event_log_dir)
    try:
        assert names and all(maintenance._is_event_log(dir_fd, name) for name in names)
    finally:
        os.close(dir_fd)
    for line in (event_log_dir / names[0]).read_bytes().splitlines():
        assert maintenance._EVENT_LOG_START.match(line), line[:60]


# --- only Clarp's own logs (review A2, F1) --------------------------------------

def _unrelated(path, size: int = 60_000) -> bytes:
    """A file that is none of this worker's business, but would qualify by
    size and even by content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _numbered(path, size // 100)
    return path.read_bytes()


def test_a_symlinked_dated_log_is_refused_and_its_target_survives(tmp_path, failures):
    logs = tmp_path / "logs"
    logs.mkdir()
    target = tmp_path / "elsewhere" / "state.sqlite"
    before = _unrelated(target)
    (logs / "2026-11-10.jsonl").symlink_to(target)        # today's log, over the budget
    (logs / "2026-10-01.jsonl").symlink_to(target)        # and one past its age

    assert maintenance.prune_event_logs(logs, max_age_ms=DAYS14, today=TODAY,
                                        max_total_bytes=1000, today_keep_bytes=100) == 0

    assert target.read_bytes() == before
    assert sorted(p.name for p in logs.iterdir()) == ["2026-10-01.jsonl", "2026-11-10.jsonl"]
    assert failures == [("eventLogPruneFail", "UnsafeLogPath", "2026-10-01.jsonl"),
                        ("eventLogPruneFail", "UnsafeLogPath", "2026-11-10.jsonl")]
    with pytest.raises(maintenance.UnsafeLogPath):
        maintenance.keep_tail(logs / "2026-11-10.jsonl", 100)
    assert target.read_bytes() == before


def test_a_symlinked_log_directory_is_refused_and_nothing_in_it_is_deleted(tmp_path, failures):
    real = tmp_path / "real"
    real.mkdir()
    _numbered(real / "2026-10-01.jsonl", 10)              # past its age
    _numbered(real / "2026-11-09.jsonl", 600)             # over the budget
    _numbered(real / "2026-11-10.jsonl", 600)
    before = {p.name: p.read_bytes() for p in real.iterdir()}
    link = tmp_path / "logs"
    link.symlink_to(real, target_is_directory=True)

    assert maintenance.prune_event_logs(link, max_age_ms=DAYS14, today=TODAY,
                                        max_total_bytes=1000, today_keep_bytes=100) == 0

    assert {p.name: p.read_bytes() for p in real.iterdir()} == before
    assert failures == [("eventLogPruneFail", "UnsafeLogPath", str(link))]


@pytest.mark.parametrize("root", ["", ".", "logs"])
def test_an_empty_or_relative_log_directory_is_refused(tmp_path, monkeypatch, failures, root):
    """CLAUDE_PWA_LOG_DIR='' is the working directory: every dated .jsonl
    there, whatever it is, would have been a candidate."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    for directory in (tmp_path, tmp_path / "logs"):
        _numbered(directory / "2026-10-01.jsonl", 10)
        _numbered(directory / "2026-11-10.jsonl", 600)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*.jsonl")}

    import pathlib
    assert maintenance.prune_event_logs(pathlib.Path(root), max_age_ms=DAYS14, today=TODAY,
                                        max_total_bytes=1000, today_keep_bytes=100) == 0

    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*.jsonl")} == before
    assert failures == [("eventLogPruneFail", "UnsafeLogPath", str(pathlib.Path(root)))]


def test_a_wrong_log_directory_keeps_files_that_are_not_event_logs(tmp_path, failures):
    """An absolute but wrong root: dated .jsonl files of somebody else's."""
    theirs = [tmp_path / "2026-11-09.jsonl", tmp_path / "2026-11-10.jsonl"]
    for path in theirs:
        path.write_text("".join(json.dumps({"row": n, "value": "v" * 60}) + "\n" for n in range(800)))
    database = tmp_path / "state.sqlite"
    database.write_bytes(b"SQLite format 3\x00" + b"\x00" * 50_000)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    assert maintenance.prune_event_logs(tmp_path, max_age_ms=DAYS14, today=TODAY,
                                        max_total_bytes=1000, today_keep_bytes=100) == 0

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert failures == [("eventLogPruneFail", "UnsafeLogPath", "2026-11-09.jsonl"),
                        ("eventLogPruneFail", "UnsafeLogPath", "2026-11-10.jsonl")]


def test_a_hard_linked_or_non_regular_entry_is_refused(tmp_path, failures):
    logs = tmp_path / "logs"
    logs.mkdir()
    other = tmp_path / "other-name"
    before = _unrelated(other)
    os.link(other, logs / "2026-11-10.jsonl")             # the same file under a second name
    (logs / "2026-10-01.jsonl").mkdir()                   # a directory named like an old log

    assert maintenance.prune_event_logs(logs, max_age_ms=DAYS14, today=TODAY,
                                        max_total_bytes=1000, today_keep_bytes=100) == 0

    assert other.read_bytes() == before and (logs / "2026-11-10.jsonl").read_bytes() == before
    assert (logs / "2026-10-01.jsonl").is_dir()
    assert failures == [("eventLogPruneFail", "UnsafeLogPath", "2026-10-01.jsonl"),
                        ("eventLogPruneFail", "UnsafeLogPath", "2026-11-10.jsonl")]


def test_a_directory_or_file_of_another_user_is_refused(tmp_path, monkeypatch, failures):
    _numbered(tmp_path / "2026-10-01.jsonl", 10)
    before = (tmp_path / "2026-10-01.jsonl").read_bytes()
    someone_else = os.geteuid() + 1
    with pytest.raises(maintenance.UnsafeLogPath):
        monkeypatch.setattr(maintenance.os, "geteuid", lambda: someone_else)
        maintenance._require_own_file(os.stat(tmp_path / "2026-10-01.jsonl"), "2026-10-01.jsonl")

    assert maintenance.prune_event_logs(tmp_path, max_age_ms=DAYS14, today=TODAY) == 0

    assert (tmp_path / "2026-10-01.jsonl").read_bytes() == before
    assert failures == [("eventLogPruneFail", "UnsafeLogPath", str(tmp_path))]


def _hook_lines(path, count: int) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for n in range(count):
            f.write(f"12:00:00 stop     1 line {n}\n")
    return path.read_bytes()


def test_a_symlinked_hook_log_is_refused_and_its_target_survives(tmp_path, failures):
    target = tmp_path / "elsewhere" / "state.sqlite"
    before = _hook_lines(target, 2000)                    # over the bound, and it even reads like a hook log
    hook_log = tmp_path / "cache" / "hook.log"
    hook_log.parent.mkdir()
    hook_log.symlink_to(target)

    with pytest.raises(maintenance.UnsafeLogPath):
        maintenance.prune_hook_log(hook_log, max_bytes=20_000, keep_bytes=4000)
    assert target.read_bytes() == before

    worker = maintenance.MaintenanceWorker(
        audio_dir=tmp_path, hook_log=hook_log,
        policy=maintenance.Policy(hook_log_max_bytes=20_000, hook_log_keep_bytes=4000))
    assert "hook_log_bytes" not in worker.check_sizes()
    assert target.read_bytes() == before
    assert ("hookLogPruneFail", "UnsafeLogPath", "") in failures


def test_a_hook_log_under_a_symlinked_or_relative_directory_is_refused(tmp_path, monkeypatch):
    import pathlib
    real = tmp_path / "real"
    before = _hook_lines(real / "hook.log", 2000)
    (tmp_path / "cache").symlink_to(real, target_is_directory=True)
    monkeypatch.chdir(real)

    for path in (tmp_path / "cache" / "hook.log", pathlib.Path("hook.log"), pathlib.Path("") / "hook.log"):
        with pytest.raises(maintenance.UnsafeLogPath):
            maintenance.prune_hook_log(path, max_bytes=20_000, keep_bytes=4000)
    assert (real / "hook.log").read_bytes() == before


def test_a_hook_log_that_does_not_read_like_one_is_left_alone(tmp_path):
    """An absolute but wrong CLARP_CACHE_DIR with somebody else's hook.log."""
    path = tmp_path / "hook.log"
    path.write_text("commit-msg hook ran for abc123\n" * 2000)
    before = path.read_bytes()
    with pytest.raises(maintenance.UnsafeLogPath):
        maintenance.prune_hook_log(path, max_bytes=20_000, keep_bytes=4000)
    assert path.read_bytes() == before


# --- failures to inspect are logged, not skipped (review A2, F2) ------------------

def _failing_stat(monkeypatch, name: str, error: OSError) -> None:
    stat = os.stat

    def fail(path, *args, **kwargs):
        if path == name and "dir_fd" in kwargs:
            raise error
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(maintenance.os, "stat", fail)


def test_a_dated_log_that_cannot_be_inspected_is_logged_not_skipped(tmp_path, monkeypatch, failures):
    for day in ("2026-10-01", "2026-10-02"):
        _numbered(tmp_path / f"{day}.jsonl", 10)
    _failing_stat(monkeypatch, "2026-10-01.jsonl", PermissionError(errno.EACCES, "Permission denied"))

    assert maintenance.prune_event_logs(tmp_path, max_age_ms=DAYS14, today=TODAY) == 1

    assert failures == [("eventLogPruneFail", "PermissionError", "2026-10-01.jsonl")]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-10-01.jsonl"]   # the other one still went


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any directory")
def test_a_log_directory_that_cannot_be_read_is_logged(tmp_path, failures):
    logs = tmp_path / "logs"
    logs.mkdir()
    _numbered(logs / "2026-10-01.jsonl", 10)
    logs.chmod(0)
    try:
        assert maintenance.prune_event_logs(logs, max_age_ms=DAYS14, today=TODAY) == 0
    finally:
        logs.chmod(0o700)
    assert failures == [("eventLogPruneFail", "PermissionError", str(logs))]
    assert (logs / "2026-10-01.jsonl").exists()


def test_a_hook_log_that_cannot_be_inspected_reaches_the_workers_log(tmp_path, monkeypatch, failures):
    hook_log = tmp_path / "hook.log"
    _hook_lines(hook_log, 2000)
    _failing_stat(monkeypatch, "hook.log", OSError(errno.EIO, "Input/output error"))

    with pytest.raises(OSError) as raised:
        maintenance.prune_hook_log(hook_log, max_bytes=20_000, keep_bytes=4000)
    assert raised.value.errno == errno.EIO

    worker = maintenance.MaintenanceWorker(audio_dir=tmp_path, hook_log=hook_log)
    assert "hook_log_bytes" not in worker.check_sizes()
    assert ("hookLogPruneFail", "OSError", "") in failures
    assert "hook_log_bytes" not in worker.run_once(checkpoint=False)
    assert failures.count(("hookLogPruneFail", "OSError", "")) == 2


# --- hook.log ------------------------------------------------------------------

def test_hook_log_under_its_bound_is_untouched(tmp_path):
    path = tmp_path / "hook.log"
    path.write_text("12:00:00 stop     1 hello\n" * 10)
    before = path.read_text()
    assert maintenance.prune_hook_log(path, max_bytes=10_000, keep_bytes=1000) == 0
    assert path.read_text() == before
    # Absence is the one thing that is silently nothing to do.
    assert maintenance.prune_hook_log(tmp_path / "absent.log", max_bytes=10, keep_bytes=1) == 0
    assert maintenance.prune_hook_log(tmp_path / "no-such-dir" / "hook.log", max_bytes=10, keep_bytes=1) == 0


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
