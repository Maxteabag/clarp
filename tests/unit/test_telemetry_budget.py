"""10 Nov 2026: telemetry.sqlite reached 16.2 GB while holding only 25 hours,
because detail expired by age alone. The live data now also holds a size budget."""
import pytest

from lib import batched_delete, telemetry

HOUR = telemetry.HOUR_MS
NOW = 40 * HOUR + 30 * 60 * 1000          # half past hour 40
ROWS = 100
DETAIL = '{"traceback":"' + "x" * 8000 + '"}'


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    monkeypatch.setattr(batched_delete, "PAUSE_S", 0)
    monkeypatch.setattr(telemetry, "UPSERT_PAUSE_S", 0)


def _fill(hour: int, rows: int = ROWS) -> None:
    telemetry.conn().executemany(
        """INSERT INTO diagnostic_events
           (ts,ts_iso,source,event,level,duration_ms,detail) VALUES (?,?,?,?,?,?,?)""",
        [(hour * HOUR + n, "t", "server", "noisy", "error", 10, DETAIL) for n in range(rows)])


def _hours() -> dict[int, int]:
    return {row[0]: row[1] for row in telemetry.conn().execute(
        "SELECT ts / ?, count(*) FROM diagnostic_events GROUP BY 1", (HOUR,))}


def _rollup() -> dict[int, int]:
    return {row[0]: row[1] for row in telemetry.conn().execute(
        "SELECT bucket_ms / ?, samples FROM hourly_metrics", (HOUR,))}


def test_nothing_goes_while_the_live_data_is_within_the_budget():
    for hour in (38, 39, 40):
        _fill(hour)
    counts = telemetry.rollup_and_prune(now_ms=NOW, budget_bytes=telemetry.live_bytes() * 2)
    assert counts["telemetry_budget_details"] == 0
    assert counts["telemetry_current_hour_cut"] == 0
    assert _hours() == {38: ROWS, 39: ROWS, 40: ROWS}
    assert not telemetry.over_budget(telemetry.live_bytes())


def test_the_oldest_whole_hours_go_first_and_keep_their_rollup():
    empty = telemetry.live_bytes()
    for hour in (36, 37, 38, 39, 40):
        _fill(hour)
    full = telemetry.live_bytes()
    per_hour = (full - empty) // 5
    budget = full - per_hour - per_hour // 2          # two hours too many
    assert telemetry.over_budget(budget)

    counts = telemetry.rollup_and_prune(now_ms=NOW, budget_bytes=budget)

    assert counts["telemetry_budget_details"] == 2 * ROWS
    assert counts["telemetry_current_hour_cut"] == 0
    assert counts["telemetry_details"] == 0           # nothing was 24 hours old
    assert _hours() == {38: ROWS, 39: ROWS, 40: ROWS}  # whole hours only, the current one untouched
    assert telemetry.live_bytes() <= budget
    # RM-N8c: the deleted hours were rolled up first, and a later pass does
    # not aggregate them again from nothing.
    assert _rollup() == {36: ROWS, 37: ROWS, 38: ROWS, 39: ROWS}
    again = telemetry.rollup_and_prune(now_ms=NOW, budget_bytes=budget)
    assert again["telemetry_budget_details"] == 0
    assert _rollup() == {36: ROWS, 37: ROWS, 38: ROWS, 39: ROWS}


def test_the_current_hour_is_cut_only_when_it_alone_is_over_the_budget():
    _fill(39)
    _fill(40, rows=4 * ROWS)
    current_hour_bytes = telemetry.live_bytes() * 4 // 5
    budget = current_hour_bytes // 2

    counts = telemetry.rollup_and_prune(now_ms=NOW, budget_bytes=budget)

    assert counts["telemetry_budget_details"] == ROWS          # the whole earlier hour first
    cut = counts["telemetry_current_hour_cut"]
    assert 0 < cut < 4 * ROWS
    assert telemetry.live_bytes() <= budget
    survivors = [row[0] for row in telemetry.conn().execute(
        "SELECT ts - ? FROM diagnostic_events ORDER BY ts", (40 * HOUR,))]
    # The oldest rows of the hour went; the newest are all still there.
    assert survivors == list(range(cut, 4 * ROWS))
    assert _rollup() == {39: ROWS}
    # Documented cost: once the hour ends, its rollup counts the survivors.
    telemetry.rollup_and_prune(now_ms=NOW + HOUR, budget_bytes=budget * 100)
    assert _rollup() == {39: ROWS, 40: 4 * ROWS - cut}


def test_budget_deletes_run_in_bounded_batches(monkeypatch):
    monkeypatch.setattr(batched_delete, "DELETE_BATCH", 25)
    for hour in (38, 39, 40):
        _fill(hour)
    executed: list[str] = []
    connection = telemetry.conn()
    connection.set_trace_callback(executed.append)
    try:
        counts = telemetry.rollup_and_prune(now_ms=NOW, budget_bytes=telemetry.live_bytes() // 2)
    finally:
        connection.set_trace_callback(None)
    assert counts["telemetry_budget_details"] >= ROWS
    deletes = [sql for sql in executed if sql.lstrip().upper().startswith("DELETE FROM DIAGNOSTIC_EVENTS")]
    assert len(deletes) >= counts["telemetry_budget_details"] // 25
    assert all("json_each" in sql for sql in deletes), "every delete names its rows; none scans under the lock"


def test_live_bytes_counts_pages_in_use_not_the_file():
    _fill(39, rows=4 * ROWS)
    full = telemetry.live_bytes()
    pages_before = telemetry.conn().execute("PRAGMA page_count").fetchone()[0]
    telemetry.rollup_and_prune(now_ms=NOW, budget_bytes=full // 4)
    # The file keeps its pages for reuse; the live size is what fell.
    assert telemetry.conn().execute("PRAGMA page_count").fetchone()[0] >= pages_before
    assert telemetry.live_bytes() <= full // 4
    _fill(40, rows=ROWS)
    assert telemetry.conn().execute("PRAGMA page_count").fetchone()[0] <= pages_before + 8, \
        "new rows fill the freed pages before the file grows"
