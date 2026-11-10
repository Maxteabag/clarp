"""10 Nov 2026: error events with a 3.2 MB traceback each wrote 34 GB of
diagnostics in a day. One event's serialised detail is bounded in both sinks."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import eventlog  # noqa: E402

import pytest  # noqa: E402

CAP = eventlog.MAX_DETAIL_BYTES


@pytest.fixture(autouse=True)
def _diagnostics_on(tmp_path, monkeypatch):
    from lib import diagnostics_settings
    diagnostics_settings.update({"enabled": True, "categories": list(diagnostics_settings.CATEGORIES)})
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)


def _sinks(tmp_path) -> tuple[dict, dict]:
    """The one emitted event as (JSONL detail, SQLite detail)."""
    from lib import telemetry
    lines = [json.loads(line) for path in tmp_path.glob("*.jsonl")
             for line in path.read_text().splitlines()]
    rows = telemetry.conn().execute("SELECT detail FROM diagnostic_events").fetchall()
    assert len(lines) == 1 and len(rows) == 1
    assert len(rows[0]["detail"]) <= CAP
    assert len(json.dumps(lines[0]["detail"], separators=(",", ":"))) <= CAP
    return lines[0]["detail"], json.loads(rows[0]["detail"])


def test_an_ordinary_detail_is_written_unchanged(tmp_path):
    detail = {"msg": "hello", "n": 3, "nested": {"a": [1, 2, "x" * 2000]}}
    eventlog.emit("server", "ordinary", detail=detail)
    jsonl, sqlite_row = _sinks(tmp_path)
    assert jsonl == sqlite_row == detail


def test_a_giant_traceback_keeps_its_head_and_tail_in_both_sinks(tmp_path):
    frames = "".join(f'  File "reconcile.py", line {n}, in has_live_work\n' for n in range(60_000))
    traceback_text = "Traceback (most recent call last):\n" + frames + "RuntimeUnavailable: socket closed\n"
    assert len(traceback_text) > 3_000_000
    eventlog.emit("server", "reconcileSpawningFail", level="error", detail={
        "msg": "agent-1", "error_type": "RuntimeUnavailable", "traceback": traceback_text})
    jsonl, sqlite_row = _sinks(tmp_path)
    assert jsonl == sqlite_row
    assert jsonl["msg"] == "agent-1" and jsonl["error_type"] == "RuntimeUnavailable"
    assert jsonl["traceback"].startswith("Traceback (most recent call last):")
    assert jsonl["traceback"].endswith("RuntimeUnavailable: socket closed\n")
    assert "chars omitted" in jsonl["traceback"]
    # The tail, where the failure is named, gets the larger share.
    head, _, tail = jsonl["traceback"].partition("chars omitted")
    assert len(tail) > len(head)
    assert jsonl["_truncated"]["limit_bytes"] == CAP
    assert jsonl["_truncated"]["original_bytes"] > 3_000_000


def test_emit_exception_bounds_the_traceback_it_formats(tmp_path):
    def deep(n):
        if n == 0:
            raise ValueError("bottom")
        deep(n - 1)

    try:
        deep(400)
    except ValueError as exc:
        # The same object raised again and again, as the incident's was.
        for _ in range(300):
            try:
                raise exc
            except ValueError:
                pass
        eventlog.emit_exception("server", "boom", exc, detail={"msg": "x" * 100_000})
    jsonl, _ = _sinks(tmp_path)
    assert jsonl["error"] == "bottom" and jsonl["error_type"] == "ValueError"
    assert jsonl["traceback"].rstrip().endswith("ValueError: bottom")
    assert "_truncated" in jsonl


@pytest.mark.parametrize("value", [
    '"' * 200_000,                      # every character doubles when encoded
    "\\" * 200_000,
    "\x00\x01" * 100_000,               # six bytes each when encoded
    "æøå\U0001f600" * 50_000,
])
def test_strings_that_grow_when_encoded_still_fit(tmp_path, value):
    eventlog.emit("server", "nasty", detail={"value": value, "other": value})
    jsonl, _ = _sinks(tmp_path)
    assert "_truncated" in jsonl


def test_a_detail_of_many_small_values_becomes_a_bounded_preview(tmp_path):
    detail = {f"key{n}": [n, "v"] for n in range(20_000)}
    eventlog.emit("server", "wide", detail=detail)
    jsonl, _ = _sinks(tmp_path)
    assert set(jsonl) == {"_truncated", "_preview"}
    assert jsonl["_preview"].startswith('{"key0":[0,"v"]')
    assert jsonl["_preview"].endswith('"key19999":[19999,"v"]}')
    assert jsonl["_truncated"]["original_bytes"] == len(json.dumps(detail, separators=(",", ":")))


def test_an_unserialisable_giant_detail_is_bounded_too(tmp_path):
    eventlog.emit("server", "odd", detail={"thing": object(), "blob": "y" * 500_000})
    jsonl, _ = _sinks(tmp_path)
    assert "_truncated" in jsonl and "_repr" in jsonl


def _events(tmp_path) -> tuple[list[str], list[str]]:
    from lib import telemetry
    lines = [json.loads(line)["event"] for path in tmp_path.glob("*.jsonl")
             for line in path.read_text().splitlines()]
    rows = [row[0] for row in telemetry.conn().execute("SELECT event FROM diagnostic_events")]
    return lines, rows


def _broken_bounder(detail):
    raise RecursionError("too deep")


def test_a_failure_while_bounding_never_reaches_the_caller(tmp_path, monkeypatch):
    reported: list[tuple] = []
    monkeypatch.setattr(eventlog, "_bound_detail", _broken_bounder)
    monkeypatch.setattr(eventlog, "log_fault",
                        lambda code, exc, **kw: reported.append((code, type(exc).__name__, kw)))
    eventlog.emit("server", "stillLogged", detail={"blob": "z" * 100_000})
    jsonl, sqlite_row = _sinks(tmp_path)
    assert _events(tmp_path) == (["stillLogged"], ["stillLogged"])
    assert jsonl == sqlite_row
    assert set(jsonl) == {"_truncated"}
    assert jsonl["_truncated"]["error"] == "RecursionError"
    assert jsonl["_truncated"]["limit_bytes"] == CAP
    # The whole record's size, named as that: the detail alone was not measured.
    assert jsonl["_truncated"]["record_original_bytes"] > 100_000
    assert "original_bytes" not in jsonl["_truncated"]
    assert reported == [("eventlogDetailBoundFailed", "RecursionError", {"structured": False})]


def test_a_failing_bounder_with_a_failing_stderr_still_delivers_the_event(tmp_path, monkeypatch):
    """The report of the failure must not come before, or prevent, the event."""
    class BrokenStderr:
        def write(self, value):
            raise OSError("stderr is gone")

        def flush(self):
            raise OSError("stderr is gone")

    monkeypatch.setattr(eventlog, "_bound_detail", _broken_bounder)
    monkeypatch.setattr(sys, "stderr", BrokenStderr())
    eventlog.emit("server", "stillLogged", level="error", detail={"blob": "z" * 100_000})   # must not raise
    monkeypatch.undo()
    jsonl, sqlite_row = _sinks(tmp_path)
    assert _events(tmp_path) == (["stillLogged"], ["stillLogged"])
    assert jsonl == sqlite_row
    assert jsonl["_truncated"]["error"] == "RecursionError"
    assert jsonl["_truncated"]["record_original_bytes"] > 100_000


def test_a_detail_too_deep_for_the_shortening_walk_is_still_delivered(tmp_path):
    """It serialises (the C encoder) but the recursive walk gives up."""
    deep: dict = {"leaf": "y" * 100_000}
    for _ in range(sys.getrecursionlimit() // 2):
        deep = {"d": deep}
    try:
        json.dumps(deep)
    except RecursionError:
        pytest.skip("this interpreter cannot serialise the fixture either")
    eventlog.emit("server", "deep", detail=deep)
    jsonl, sqlite_row = _sinks(tmp_path)
    assert jsonl == sqlite_row and "_truncated" in jsonl


def test_a_detail_exactly_at_the_cap_is_unchanged_and_one_byte_more_is_shortened(tmp_path):
    at_cap = {"m": "x" * (CAP - len('{"m":""}'))}
    assert len(json.dumps(at_cap, separators=(",", ":"))) == CAP
    eventlog.emit("server", "atCap", detail=at_cap)
    jsonl, sqlite_row = _sinks(tmp_path)
    assert jsonl == sqlite_row == at_cap


def test_one_byte_over_the_cap_is_shortened(tmp_path):
    over = {"m": "x" * (CAP - len('{"m":""}') + 1)}
    eventlog.emit("server", "overCap", detail=over)
    jsonl, _ = _sinks(tmp_path)
    assert jsonl["_truncated"]["original_bytes"] == CAP + 1


def test_a_deferred_wait_record_is_bounded_before_it_is_queued(tmp_path, capsys):
    """sqliteWriteWait is made outside emit() and queued for both sinks until
    its transaction ends: it passes the same bound, when it is made."""
    from lib import db, log, telemetry

    def written() -> tuple[list[dict], list]:
        lines = [json.loads(line) for path in tmp_path.glob("*.jsonl")
                 for line in path.read_text().splitlines()]
        rows = telemetry.conn().execute(
            "SELECT event, detail FROM diagnostic_events ORDER BY event_id").fetchall()
        return lines, rows

    oversized = "waited " + "w" * 200_000 + " for the writer"
    connection = db.conn()
    telemetry.conn()
    capsys.readouterr()
    try:
        connection.execute("BEGIN IMMEDIATE")
        log.sqlite_write_wait(oversized, connection)
        log.sqlite_write_wait("waited 6.2s", connection)
        # Nothing reaches any sink while the transaction is open ...
        eventlog.flush_deferred()
        assert written() == ([], [])
        assert "sqliteWriteWait" not in capsys.readouterr().err
        # ... and what waits in the queue is bounded already.
        queued = [item.record for batch in eventlog._DEFERRED.values() for item in batch]
        assert [len(json.dumps(record["detail"], separators=(",", ":"))) <= CAP for record in queued] == [True, True]
        assert queued[1]["detail"] == {"msg": "waited 6.2s"}
        connection.execute("COMMIT")
    finally:
        db.close_local()

    lines, rows = written()
    assert [line["event"] for line in lines] == ["sqliteWriteWait", "sqliteWriteWait"]
    assert [row["event"] for row in rows] == ["sqliteWriteWait", "sqliteWriteWait"]
    for line, row in zip(lines, rows):
        assert len(row["detail"]) <= CAP
        assert line["detail"] == json.loads(row["detail"]), "both sinks get the same bounded record"
    bounded, small = lines[0]["detail"], lines[1]["detail"]
    assert bounded["msg"].startswith("waited www") and bounded["msg"].endswith("w for the writer")
    assert bounded["_truncated"] == {
        "original_bytes": len(json.dumps({"msg": oversized}, separators=(",", ":"))), "limit_bytes": CAP}
    assert small == {"msg": "waited 6.2s"}               # under the cap: exactly as before


def test_a_failed_bounding_in_the_deferred_path_touches_no_sink_before_the_commit(
        tmp_path, monkeypatch, capfd):
    """The two repairs together: the wait record is made while its transaction
    is open, so preparing it must not write anywhere, a fault report included.
    The real fault reporter is in place and would print at once if called."""
    from lib import db, log, telemetry

    def written() -> tuple[list[dict], list]:
        lines = [json.loads(line) for path in tmp_path.glob("*.jsonl")
                 for line in path.read_text().splitlines()]
        rows = telemetry.conn().execute(
            "SELECT event, detail FROM diagnostic_events ORDER BY event_id").fetchall()
        return lines, rows

    reports: list[str] = []
    real_fault = eventlog.log_fault

    def fault(code, exc, **kw):
        reports.append(code)
        return real_fault(code, exc, **kw)

    monkeypatch.setattr(eventlog, "log_fault", fault)
    monkeypatch.setattr(log, "_FAULT_LOG_THROTTLE", {})          # nothing throttled: a report would print
    monkeypatch.setattr(eventlog, "_bound_detail", _broken_bounder)
    oversized = "waited " + "w" * 200_000
    connection = db.conn()
    telemetry.conn()
    capfd.readouterr()
    try:
        connection.execute("BEGIN IMMEDIATE")
        log.sqlite_write_wait(oversized, connection)
        eventlog.flush_deferred()                                # a premature flush is refused too
        eventlog._flush_all_deferred()
        before = capfd.readouterr()                              # file descriptors 1 and 2, print or raw write
        assert (before.out, before.err) == ("", "")
        assert written() == ([], [])
        assert reports == []
        queued = [item.record for batch in eventlog._DEFERRED.values() for item in batch]
        assert [record["detail"]["_truncated"]["error"] for record in queued] == ["RecursionError"]
        connection.execute("COMMIT")
    finally:
        db.close_local()

    lines, rows = written()
    assert [line["event"] for line in lines] == ["sqliteWriteWait"]
    assert [row["event"] for row in rows] == ["sqliteWriteWait"]
    fallback = lines[0]["detail"]
    assert fallback == json.loads(rows[0]["detail"])
    assert set(fallback) == {"_truncated"}
    assert fallback["_truncated"]["error"] == "RecursionError"      # the diagnostic, in both sinks
    assert fallback["_truncated"]["limit_bytes"] == CAP
    assert fallback["_truncated"]["record_original_bytes"] > 200_000
    assert "sqliteWriteWait" in capfd.readouterr().err              # the wait line itself, after the commit
    assert reports == []

    # The direct path still reports, and the reporter was live all along.
    eventlog.emit("server", "direct", detail={"blob": "z" * 100_000})
    assert reports == ["eventlogDetailBoundFailed"]
    assert "eventlogDetailBoundFailed error_type=RecursionError" in capfd.readouterr().err
