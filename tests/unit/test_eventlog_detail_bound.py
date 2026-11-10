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


def test_a_failure_while_bounding_never_reaches_the_caller(tmp_path, monkeypatch, capsys):
    def broken(detail):
        raise RecursionError("too deep")

    monkeypatch.setattr(eventlog, "_bound_detail", broken)
    eventlog.emit("server", "stillLogged", detail={"blob": "z" * 100_000})
    jsonl, _ = _sinks(tmp_path)
    assert jsonl["_truncated"]["error"] == "RecursionError"
    assert jsonl["_truncated"]["original_bytes"] > 100_000
    assert "eventlog detail bound failed" in capsys.readouterr().err


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
