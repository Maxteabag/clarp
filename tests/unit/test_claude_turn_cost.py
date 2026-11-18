"""Claude Code reports a session cost at each result, including /compact."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib import db, eventlog, turn_lifecycle, turn_usage
from lib.turn_launcher import LauncherMixin


@pytest.fixture
def complete(monkeypatch):
    done = Mock()
    diagnostics = Mock()
    monkeypatch.setattr(turn_lifecycle, "try_transition", done)
    monkeypatch.setattr(eventlog, "emit", diagnostics)
    launcher = Mock()
    launcher._superseded.return_value = False

    def result(total, *, native="s", launched="s", fresh=False, trace="t", usage=None, backend="claude"):
        spec = SimpleNamespace(backend=backend, agent_id="agent", trace_id=trace,
                               context=None, origin="user", janitor_run_id="",
                               backend_session_id=launched, is_new_session=fresh)
        state = {"backend_session_id": native}
        _, callback, _ = LauncherMixin._attempt_callbacks(launcher, spec, 1, state)
        callback({"type": "result", "session_id": native, "usage": {"output_tokens": 1} if usage is None else usage,
                  "total_cost_usd": total})
        detail = done.call_args.args[2]
        assert "raw_total_cost_usd" not in detail
        diagnostic = diagnostics.call_args.kwargs["detail"]
        if backend == "claude":
            assert diagnostic["raw_total_cost_usd"] == total
        else:
            assert "raw_total_cost_usd" not in diagnostic
        row = db.conn().execute("SELECT cost_usd FROM turn_usage WHERE trace_id=? "
                                "ORDER BY usage_id DESC LIMIT 1", (trace,)).fetchone()
        assert detail["cost_usd"] == diagnostic["cost_usd"]
        if row is not None:
            assert row[0] == detail["cost_usd"]
        return detail["cost_usd"]

    return result


def test_resume_and_compact_use_durable_cost_cursor(complete):
    # Same-session results from three Claude Code 2.1.321 calls, the last a /compact.
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "claude-session-cost-2.1.321.json"
    first, second, compact = json.loads(fixture.read_text())
    assert complete(first["total_cost_usd"], usage=first["usage"], fresh=True) == pytest.approx(0.0765235)
    database = db.conn().execute("PRAGMA database_list").fetchone()[2]
    db.close_local()
    # The resumed turn's cursor calculation runs in a new Python process.
    resumed = subprocess.run([sys.executable, "-c", """
import sys
from lib import db, turn_usage
from pathlib import Path
db.reset_for_tests(Path(sys.argv[1]))
print(turn_usage.claude_cost_delta("s", float(sys.argv[2]), fresh=False))
""", database, str(second["total_cost_usd"])], cwd=Path(__file__).resolve().parents[2] / "server",
                             capture_output=True, text=True, check=True)
    assert float(resumed.stdout.strip().splitlines()[-1]) == pytest.approx(0.050047)
    assert complete(compact["total_cost_usd"], trace="compact", usage=compact["usage"]) == pytest.approx(0.0170705)
    assert complete(compact["total_cost_usd"], trace="compact", usage=compact["usage"]) == 0


@pytest.mark.parametrize(("launched", "native", "fresh"), [
    ("old", "old", False),       # first observation after upgrade
    ("parent", "resumed", False),
    ("fork-child", "fork-child", False),  # fork already bound at admission
    ("minted", "different", True),
])
def test_unknown_start_saves_a_baseline_without_charging(complete, launched, native, fresh):
    assert complete(70, launched=launched, native=native, fresh=fresh) is None
    assert complete(72, launched=native, native=native) == 2


def test_decreasing_cost_rebaselines_and_sessions_are_independent(complete):
    assert complete(10, fresh=True) == 10
    assert complete(2, native="other", launched="other", fresh=True) == 2
    assert complete(3) is None
    assert complete(4) == 1


def test_rolling_cost_uses_writer_marker_but_counts_all_tokens():
    from lib import backend_usage_claude

    at = db.conn().execute("SELECT value FROM settings WHERE key='usage.claude_cost_activation_ms'").fetchone()
    assert at is not None
    since = int(at[0])
    for stamp, cost, kind in [(since + 1, 400, "legacy"), (since - 1, 2, "per_turn"),
                              (since, 3, "per_turn")]:
        db.conn().execute("INSERT INTO turn_usage(backend, agent_id, at, cost_usd, tokens_in, cost_kind) "
                          "VALUES ('claude','agent',?,?,10,?)", (stamp, cost, kind))
    result = backend_usage_claude._claude_response_row(since + 2)
    assert result["cost_activation_ms"] == since
    for window in result["totals"].values():
        assert (window["turns"], window["tokens_in"], window["cost_usd"]) == (3, 30, 5)


def test_cursor_advance_survives_skipped_usage_insert(complete):
    # Crash after cursor commit and before the usage row: replay cannot rebill.
    assert turn_usage.claude_cost_delta("s", 8, fresh=True) == 8
    db.close_local()
    assert complete(8, fresh=False) == 0
    assert complete(9) == 1


def test_upgrading_preserves_prior_rows_and_activation(tmp_path):
    import sqlite3
    from lib import db_migrations

    con = sqlite3.connect(tmp_path / "upgrade.sqlite", isolation_level=None)
    db_migrations._migrate(con)
    con.execute("DROP TABLE usage_cursors")
    con.execute("DELETE FROM settings WHERE key='usage.claude_cost_activation_ms'")
    con.execute("PRAGMA user_version=127")
    con.execute("INSERT INTO turn_usage(backend,agent_id,at,cost_usd) VALUES('claude','a',1,200)")
    db_migrations._migrate(con)
    def watermark():
        return con.execute("SELECT value FROM settings WHERE key='usage.claude_cost_activation_ms'").fetchone()[0]
    activated = watermark()
    db_migrations._migrate(con)
    assert int(activated) > 1 and watermark() == activated
    assert con.execute("SELECT cost_usd FROM turn_usage").fetchall() == [(200,)]
    assert con.execute("SELECT count(*) FROM usage_cursors").fetchone() == (0,)
    con.close()


def test_cursor_failure_still_publishes_unknown_cost(complete, monkeypatch):
    import sqlite3

    monkeypatch.setattr(turn_usage, "claude_cost_delta",
                        Mock(side_effect=sqlite3.OperationalError("unavailable")))
    assert complete(3, fresh=True) is None


def test_each_unknown_total_logs_only_the_reason(monkeypatch):
    from lib import log as host_log

    notice = Mock()
    monkeypatch.setattr(host_log, "log", notice)
    assert turn_usage.claude_cost_delta("session", 50, fresh=False) is None
    assert turn_usage.claude_cost_delta("session", 2, fresh=False) is None
    assert turn_usage.claude_cost_delta("session", None, fresh=False) is None
    assert turn_usage.claude_cost_delta("session", 2, fresh=False) == 0
    assert [call.args for call in notice.call_args_list] == [
        ("claudeTurnCostUnknown", "reason=missing_baseline"),
        ("claudeTurnCostUnknown", "reason=decreased_total"),
        ("claudeTurnCostUnknown", "reason=missing_total"),
    ]


def test_opencode_already_supplies_per_turn_cost(complete):
    assert complete(5, backend="opencode") == 5
    assert complete(2, backend="opencode") == 2

@pytest.mark.parametrize("tokens", [False, True], ids=["empty-usage", "with-tokens"])
@pytest.mark.parametrize("has_total", [False, True], ids=["no-total", "with-total"])
@pytest.mark.parametrize("known", [False, True], ids=["unknown-baseline", "known-baseline"])
def test_row_presence_matches_original_result_fields(complete, tokens, has_total, known):
    if known:
        complete(5, fresh=True, trace="baseline")
    cost = complete(7 if has_total else None, usage={"input_tokens": 1} if tokens else {},
                    trace="observed")
    assert cost == (2 if has_total and known else None)
    count = db.conn().execute("SELECT count(*) FROM turn_usage WHERE trace_id='observed'").fetchone()[0]
    assert count == int(tokens or has_total)


def _old_result(connection, total, at):
    # The v127 writer's complete named column list, including across a drain.
    connection.execute(
        "INSERT INTO turn_usage (backend,agent_id,trace_id,tokens_in,tokens_out,cost_usd,duration_ms,at) "
        "VALUES ('claude','agent','old',1,2,?,3,?)", (total, at))


def test_old_writer_finishing_after_migration_is_excluded():
    import sqlite3
    from lib import db_migrations

    database = db.conn()
    path = database.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path, isolation_level=None) as old:
        old.execute("ALTER TABLE turn_usage DROP COLUMN cost_kind")
        old.execute("DROP TABLE usage_cursors")
        old.execute("DELETE FROM settings WHERE key='usage.claude_cost_activation_ms'")
        old.execute("PRAGMA user_version=127")
        _old_result(old, 40, 1)
        db_migrations._migrate(database)
        since = int(database.execute("SELECT value FROM settings WHERE key='usage.claude_cost_activation_ms'").fetchone()[0])
        _old_result(old, 40.25, since + 24)
    window = turn_usage.totals("claude", now=since + 25)["windows"]["five_hour"]
    assert window["turns"] == 1 and window["tokens_out"] == 2
    assert window["cost_usd"] is None


def test_old_writer_after_rollback_does_not_change_new_dollar_sum(complete):
    import sqlite3

    complete(3.25, trace="first-observation")
    assert complete(4, trace="new-result") == .75
    path = db.conn().execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as old:
        _old_result(old, 3, db.now_ms())
    window = turn_usage.totals("claude")["windows"]["five_hour"]
    assert window["turns"] == 3
    assert window["cost_usd"] == .75


def test_out_of_order_replay_rebaselines_and_can_repeat_a_rise(complete, monkeypatch):
    from lib import log as host_log

    notices = Mock()
    monkeypatch.setattr(host_log, "log", notices)
    costs = [complete(v, fresh=(i == 0), trace=f"attempt-{i}")
             for i, v in enumerate((1, 1.25, 1, 1.25))]
    assert costs == [1, .25, None, .25]
    assert ("claudeTurnCostUnknown", "reason=decreased_total") in [call.args for call in notices.call_args_list]
