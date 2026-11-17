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
        row = db.conn().execute("SELECT cost_usd FROM turn_usage ORDER BY usage_id DESC LIMIT 1").fetchone()
        assert detail["cost_usd"] == diagnostic["cost_usd"] == row[0]
        return row[0]

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


def test_rolling_cost_excludes_pre_activation_rows_but_counts_tokens():
    from lib import backend_usage_claude

    at = db.conn().execute("SELECT value FROM settings WHERE key='usage.claude_cost_activation_ms'").fetchone()
    assert at is not None
    since = int(at[0])
    for stamp, cost in [(since - 1, 400), (since, 2), (since + 1, 3)]:
        db.conn().execute("INSERT INTO turn_usage(backend, agent_id, at, cost_usd, tokens_in) "
                          "VALUES ('claude','agent',?,?,10)", (stamp, cost))
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
