"""The keeper's cheap idle pass must decide and mutate progress like a full read."""
import copy
import json

from lib import db, goal_keeper
from tests.unit.test_goal_keeper import _config, _goal, _message, owner  # noqa: F401


def test_steady_pass_uses_headers_and_preserves_fold_deadlines(owner):
    plan = _goal()
    now = db.now_ms()
    config = _config(coalesce=2)
    plain, cached = {}, {}
    memo = goal_keeper.KeeperMemo()
    def check(at):
        expected = goal_keeper.due(config, plain, at)
        actual = goal_keeper.due(config, cached, at, memo=memo)
        assert (actual, cached) == (expected, plain)
        return actual
    assert check(now) == []
    sql = []
    db.conn().set_trace_callback(sql.append)
    try:
        goal_keeper.due(config, copy.deepcopy(cached), now + 1, memo=memo)
    finally:
        db.conn().set_trace_callback(None)
    assert not any("SELECT p.*" in s or "SELECT goal_json" in s for s in sql)
    _message(owner, "assistant", "ready", phase="final", at=now + 20)
    assert check(now+30) == []
    assert check(now+2030) == [plan["plan_id"]]


def test_twentieth_pass_uses_authoritative_cursor_and_reports_once(owner, monkeypatch):
    plan = _goal()
    config = _config(coalesce=0, sweep=False)
    cache, progress = goal_keeper.KeeperMemo(), {}
    now = db.now_ms()
    _message(owner, "assistant", "done", phase="final", at=now+5)
    for n in range(19):
        assert goal_keeper.due(config, progress, now+20+n, memo=cache) == [plan["plan_id"]]
        progress.clear()
    # Plant an invalidation hole without changing revision, updated_at or the ledger.
    c = db.conn()
    row = c.execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (plan["plan_id"],)).fetchone()
    goal = json.loads(row[0])
    goal["updater"] = {"through_ms": now+15}
    c.execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?", (json.dumps(goal), plan["plan_id"]))
    logs = []
    from lib import log
    monkeypatch.setattr(log, "log", lambda event, detail: logs.append((event, detail)))
    expected_progress = {}
    assert goal_keeper.due(config, expected_progress, now+100) == []
    assert goal_keeper.due(config, progress, now+100, memo=cache) == []
    assert expected_progress == progress
    assert logs == [("goalKeeperMemoMismatch", "goals=1 fields=cursor")]
