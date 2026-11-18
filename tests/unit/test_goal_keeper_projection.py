"""The keeper's cheap idle pass must decide and mutate progress like a full read."""
import copy
import json

import pytest

from lib import db, goal_keeper
from tests.unit.test_goal_keeper import _config, _goal, _message, owner, helper  # noqa: F401


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


def test_busy_owner_and_idle_helper_match_plain_activity(owner, helper):
    _goal()
    config = _config(coalesce=1)
    now = db.now_ms()
    con = db.conn()
    def write(agent, revision, at, phase=None):
        _message(agent, "assistant", "evidence", at=at, phase=phase)
        con.execute("UPDATE messages SET revision=? WHERE rowid=last_insert_rowid()", (revision,))
        con.execute("INSERT INTO conversation_heads VALUES(?, 's', ?, 0) "
                    "ON CONFLICT(agent_id,backend_session_id) DO UPDATE SET revision=excluded.revision",
                    (agent, revision))

    write(owner, 1, now + 2)
    write(helper, 2, now + 2, "final")
    memo = goal_keeper.KeeperMemo()
    progress, reference = {}, {}
    def compare(at):
        assert goal_keeper.due(config, progress, at, memo=memo) == goal_keeper.due(config, reference, at)
        assert progress == reference
    compare(now+10)
    sql = []
    con.set_trace_callback(sql.append)
    try:
        goal_keeper.due(config, copy.deepcopy(progress), now+11, memo=memo)
    finally:
        con.set_trace_callback(None)
    assert not any("rowid IN (SELECT rowid FROM messages" in s for s in sql)
    write(owner, 3, now + 2, "final")  # same millisecond, new revision
    compare(now+12)
    compare(now+2000)


def test_carry_and_restore_reread_messages_without_ghosts(owner, monkeypatch):
    from lib import agents, message_store
    plan = _goal()
    now = db.now_ms()
    config, memo = _config(coalesce=0, sweep=False), goal_keeper.KeeperMemo()
    agents.open_turn(agent_id=owner, source="pwa", trace_id="owned")
    source, destination = "s", "continued"
    con = db.conn()
    snap = message_store.capture_assistant_state(agent_id=owner, backend_session_id=destination)
    _message(owner, "assistant", "complete", at=now + 1, phase="final")
    con.execute("INSERT INTO conversation_heads VALUES(?,?,?,0)", (owner, source, 1))
    plain_activities = []
    observe = goal_keeper._seen
    def tracing(*args, **kwargs):
        seen = observe(*args, **kwargs)
        plain_activities.append(seen)
        return seen
    monkeypatch.setattr(goal_keeper, "_seen", tracing)

    def compare():
        plain, actual = {}, {}
        plain_activities.clear()
        want = goal_keeper.due(config, plain, now+10)
        got = goal_keeper.due(config, actual, now+10, memo=memo)
        assert (actual, got) == (plain, want)
        assert len(plain_activities) == 2 and plain_activities[0] == plain_activities[1]
        return got

    assert compare() == [plan["plan_id"]]
    assert message_store.carry_conversation(agent_id=owner, from_session_id=source,
                                           to_session_id=destination) == 1
    assert compare() == [plan["plan_id"]]
    assert message_store.restore_assistant_state(agent_id=owner, backend_session_id=destination,
                                                trace_id="owned", snapshot=snap)
    assert compare() == []


def test_activity_hole_is_detected_and_plain_decision_used_on_twentieth_pass(owner, monkeypatch):
    from lib import log
    plan = _goal()
    now = db.now_ms()
    _message(owner, "assistant", "progress", phase="commentary", at=now+1)
    con = db.conn()
    con.execute("INSERT INTO conversation_heads VALUES(?,?,?,0)", (owner, "s", 1))
    config, memo = _config(coalesce=0, sweep=False), goal_keeper.KeeperMemo()
    for i in range(19):
        assert goal_keeper.due(config, {}, now+5+i, memo=memo) == []
    # Simulate a hypothetical writer that did not publish its phase change.
    con.execute("UPDATE messages SET phase='final' WHERE agent_id=?", (owner,))
    logs = []
    monkeypatch.setattr(log, "log", lambda event, detail: logs.append((event, detail)))
    assert goal_keeper.due(config, {}, now+30, memo=memo) == [plan["plan_id"]]
    assert len(logs) == 1 and logs[0][0] == "goalKeeperMemoMismatch"
    assert logs[0][1] == "goals=1 fields=activity,messages"


def test_plan_memo_projection_covers_what_due_reads():
    """Trace row and goal lookups through the decision helpers, including aliases."""
    import ast
    import inspect

    columns = {"plan_id", "agent_id", "created_at", "status"}
    allowed = {("checkpoint",), ("checkpoint", "at"), ("updater",),
               ("updater", "through_ms"), ("updater", "through_event"), ("updater", "progress"),
               ("continuation",), ("continuation", "state"), ("continuation", "due_at")}
    root = ("goal",)
    for fn in (goal_keeper._due, goal_keeper._seen, goal_keeper.activity, goal_keeper.cursor,
               goal_keeper.stall, goal_keeper._needs_catch_up, goal_keeper._needs_stall_flag):
        tree = ast.parse(inspect.getsource(fn))
        aliases = {"plan_row": ("row",), "row": ("row",)} if fn != goal_keeper.activity else {"plan_row": ("row",)}

        def path(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id)
            if isinstance(node, ast.BoolOp):
                return next((v for arg in node.values if (v := path(arg)) is not None), None)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_goal":
                return root
            if isinstance(node, ast.Subscript):
                prefix, key = path(node.value), node.slice
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                prefix, key = path(node.func.value), node.args[0]
            else:
                return None
            if prefix is None:
                return None
            assert isinstance(key, ast.Constant), (fn.__name__, "unbounded projection access")
            lookup = prefix + (key.value,)
            if prefix == ("row",):
                assert key.value in columns, (fn.__name__, lookup)
            elif prefix[:1] == root:
                assert lookup[1:] in allowed, (fn.__name__, lookup)
            return lookup

        for node in sorted(ast.walk(tree), key=lambda n: getattr(n, "lineno", 0)):
            result = path(node)
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                result = path(node.value)
                if result is not None:
                    aliases[node.targets[0].id] = result


def test_two_scopes_restart_and_fallback_leave_the_same_progress(owner, helper, monkeypatch):
    from lib import log

    plan = _goal()
    now = db.now_ms()
    _message(helper, "assistant", "done", at=now + 1, phase="final")
    states = [({}, {}, goal_keeper.KeeperMemo()) for _ in range(2)]
    configs = [_config(coalesce=0), _config(scope={"exclude_agent_ids": [owner]})]
    for tick in range(4):
        if tick == 2:
            states = [(p, q, goal_keeper.KeeperMemo()) for p, q, _ in states]
        for config, (plain, cached, memo) in zip(configs, states):
            expected = goal_keeper.due(config, plain, now+10+tick)
            assert goal_keeper.due(config, cached, now+10+tick, memo=memo) == expected
            assert plain == cached
    plain, cached, memo = states[0]
    notices = []
    monkeypatch.setattr(log, "log", lambda event, detail: notices.append((event, detail)))
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(memo, "facts", fail)
    assert goal_keeper.due(configs[0], cached, now+20, memo=memo) == goal_keeper.due(configs[0], plain, now+20)
    assert plain == cached and notices == [("goalKeeperMemoFallback", "error_type=RuntimeError")]


def test_same_ms_ledger_invalidates_cursor_and_sweep_reads_untokened_wait(owner):
    plan = _goal()
    pid = plan["plan_id"]
    c = db.conn()
    now = db.now_ms()
    cfg, memo = _config(coalesce=0), goal_keeper.KeeperMemo()
    def compare(at, sweep=False):
        progress = {"caught_up": {pid: now}, "next_sweep_at": at if sweep else at+10000}
        reference = copy.deepcopy(progress)
        wanted = goal_keeper.due(cfg, reference, at)
        assert goal_keeper.due(cfg, progress, at, memo=memo) == wanted
        assert progress == reference
        return wanted
    assert compare(now) == []
    _message(owner, "assistant", "finished", at=now+2, phase="final")
    assert compare(now+10) == [pid]
    from lib import task_plans
    # updater_apply keeps the owner's revision. Freeze the clock so only
    # the newest ledger event distinguishes the second cursor from the first.
    c.execute("UPDATE task_plans SET updated_at=? WHERE plan_id=?", (now, pid))
    def update_cursor(through):
        data = json.loads(c.execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (pid,)).fetchone()[0])
        data["updater"] = {"through_ms": through}
        data["history"].append(dict(at=now, kind="updater", detail={}))
        with c:
            task_plans._goal_save(c, pid, data)
    update_cursor(now+5)
    assert compare(now+11) == []
    update_cursor(now)
    assert compare(now+12) == [pid]
    data = json.loads(c.execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (pid,)).fetchone()[0])
    data["updater"] = {"through_ms": now+100}
    data["continuation"] = {"state":"waiting"}
    c.execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?", (json.dumps(data),pid))
    assert compare(now+5*86400000, sweep=True) == []
    data["continuation"] = {}
    c.execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?", (json.dumps(data),pid))
    assert compare(now+5*86400000, sweep=True) == [pid]


def test_a_session_with_no_head_forces_plain_read_even_if_another_head_exists(owner):
    _goal()
    now = db.now_ms()
    cfg, memo = _config(coalesce=0,sweep=False), goal_keeper.KeeperMemo()
    db.conn().execute("INSERT INTO conversation_heads VALUES(?,?,?,0)", (owner, "other-session", 10))
    assert goal_keeper.due(cfg, {}, now, memo=memo) == []
    _message(owner, "assistant", "final", at=now+1, phase="final")  # in session s without a head
    reference = {}
    result = goal_keeper.due(cfg, reference, now+10)
    assert result and result == goal_keeper.due(cfg, {}, now+10, memo=memo)


def test_foreign_commit_between_token_and_message_read_waits_for_next_pass(owner, monkeypatch):
    import sqlite3
    from lib import goal_keeper_memo

    plan = _goal()
    con = db.conn()
    path = con.execute("PRAGMA database_list").fetchone()[2]
    now = db.now_ms()
    con.execute("INSERT INTO conversation_heads VALUES(?,?,?,0)", (owner, "s", 1))
    snapshot = goal_keeper_memo._head_token
    committed = []
    def after_token(connection, agent):
        token = snapshot(connection, agent)
        if not committed:
            with sqlite3.connect(path) as other:
                other.execute("INSERT INTO messages(message_id,agent_id,backend_session_id,role,text,phase,"
                              "updated_at,revision,seq) VALUES('foreign',?,'s','assistant','row','final',?,2,0)",
                              (owner, now+1))
                other.execute("UPDATE conversation_heads SET revision=2 WHERE agent_id=?", (owner,))
            committed.append(True)
        return token
    monkeypatch.setattr(goal_keeper_memo, "_head_token", after_token)
    cfg, memo = _config(coalesce=0), goal_keeper.KeeperMemo()
    assert goal_keeper.due(cfg, {}, now+5, memo=memo) == []
    assert committed == [True]
    assert goal_keeper.due(cfg, {}, now+6, memo=memo) == [plan["plan_id"]]


def test_nonmaximum_session_and_replace_head_are_part_of_key(owner):
    plan = _goal()
    now, con = db.now_ms(), db.conn()
    _message(owner, "assistant", "early", at=now+1, phase="commentary")
    con.executemany("INSERT INTO conversation_heads VALUES(?,?,?,0)",
                    [(owner, "s", 7), (owner, "higher", 100)])
    memo, cfg = goal_keeper.KeeperMemo(), _config(coalesce=0)
    assert goal_keeper.due(cfg, {}, now+10, memo=memo) == []
    con.execute("UPDATE messages SET phase='final' WHERE agent_id=?", (owner,))
    con.execute("UPDATE conversation_heads SET replace_revision=7 WHERE agent_id=? AND backend_session_id='s'",
                (owner,))
    assert goal_keeper.due(cfg, {}, now+11, memo=memo) == [plan["plan_id"]]


def test_eviction_and_new_helper_membership_take_a_plain_read(owner, helper):
    _goal()
    cfg, memo, now = _config(), goal_keeper.KeeperMemo(), db.now_ms()
    con = db.conn()
    con.executemany("INSERT INTO conversation_heads VALUES(?,?,?,0)", [(a, "s", 4) for a in (owner, helper)])
    goal_keeper.due(cfg, {}, now, memo=memo)
    con.execute("UPDATE agents SET parent_agent_id=NULL WHERE agent_id=?", (helper,))
    goal_keeper.due(cfg, {}, now+1, memo=memo)
    assert helper not in memo.messages
    con.execute("UPDATE agents SET parent_agent_id=? WHERE agent_id=?", (owner, helper))
    statements = []
    con.set_trace_callback(statements.append)
    try:
        goal_keeper.due(cfg, {}, now+2, memo=memo)
    finally:
        con.set_trace_callback(None)
    reads = [s for s in statements if "rowid IN (SELECT rowid FROM messages" in s]
    assert len(reads) == 1 and helper in reads[0]


def test_moving_message_cursor_drops_then_recovers_final_under_the_same_heads(owner):
    plan = _goal()
    con, now = db.conn(), db.now_ms()
    pid = plan["plan_id"]
    _message(owner, "assistant", "final", at=now+10, phase="final")
    con.execute("INSERT INTO conversation_heads VALUES(?,?,?,0)", (owner, "s", 10))
    cfg, memo = _config(coalesce=0, sweep=False), goal_keeper.KeeperMemo()
    for offset, expected in ((0, [pid]), (10, []), (9, [pid])):
        row = con.execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (pid,)).fetchone()
        goal = json.loads(row[0])
        goal["updater"] = {"through_ms": now + offset}
        con.execute("UPDATE task_plans SET goal_json=?,revision=revision+1 WHERE plan_id=?",
                    (json.dumps(goal), pid))
        baseline, progress = {}, {}
        assert goal_keeper.due(cfg, baseline, now+20) == expected
        assert goal_keeper.due(cfg, progress, now+20, memo=memo) == expected
        assert progress == baseline


@pytest.mark.parametrize("analyzed", [False, True])
@pytest.mark.parametrize("mixed,archived", [(False, False), (True, False), (True, True)])
def test_capped_ready_prefix_stays_in_rowid_order_on_every_pass(monkeypatch, analyzed, mixed, archived):
    from lib import log
    con, now = db.conn(), db.now_ms()
    goal = json.dumps({"updater": {"through_ms": now-1}})
    expected = []
    for i in range(5):
        agent = f"owner-{i}"
        con.execute("INSERT INTO agents(agent_id,persona,voice_id,cwd,session,created_at) "
                    "VALUES(?,?,?,?,?,?)", (agent, agent, "v", "/tmp", agent, now-500))
        _message(agent, "assistant", "complete", at=now, phase="final")
        con.execute("INSERT INTO conversation_heads VALUES(?,?,1,0)", (agent, "s"))
    for i in range(540):
        agent = f"owner-{i % 5}"
        status = "active"
        if i >= 40 or (mixed and i % 9 == 0):
            status = "completed"
        elif mixed and i % 3 == 0:
            status = "paused"
        con.execute("INSERT INTO task_plans(plan_id,agent_id,session,title,status,created_at,"
                    "updated_at,goal_json,recovery_enabled) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"p{i}", agent, agent, "Fixture", status, now-400+i, now+i, goal, i % 2))
        if status in ("active", "paused") and not (archived and i % 5 < 3):
            expected.append(f"p{i}")
    if archived:
        con.execute("UPDATE agents SET archived_at=? WHERE agent_id IN ('owner-0','owner-1','owner-2')", (now,))
    if analyzed:
        con.execute("ANALYZE")
    cfg, memo = _config(coalesce=0, sweep=False), goal_keeper.KeeperMemo()
    logs = []
    monkeypatch.setattr(log, "log", lambda event, detail: logs.append(event))
    plain = goal_keeper.due(cfg, {}, now+20000)
    observations = {}
    for n in range(1, 22):
        ready = goal_keeper.due(cfg, {}, now+20000, memo=memo)
        if n in (1, 19, 20, 21):
            observations[n] = ready
    assert plain == expected
    assert observations == dict.fromkeys((1, 19, 20, 21), expected)
    assert all(ready[:5] == plain[:5] for ready in observations.values())
    assert "goalKeeperMemoMismatch" not in logs


def test_helper_messages_only_wake_the_current_parent(owner, helper, tmp_path):
    from lib import agents, message_store
    first = _goal()["plan_id"]
    peer = agents.create_agent(persona="Peer", session="peer", backend="codex", voice_id="v", cwd=str(tmp_path))
    now = db.now_ms()
    con = db.conn()
    con.execute("INSERT INTO task_plans(plan_id,agent_id,session,title,created_at,updated_at,goal_json) "
                "VALUES('peer-goal',?,'peer','Peer',?,?,?)",
                (peer, now-100, now, json.dumps({"updater": {"through_ms": now - 100}})))
    for a in (owner, peer):
        message_store.record_user_message(agent_id=a, backend_session_id="s", client_msg_id=f"relay-{a}",
                                          text="Synthetic handoff", sender_agent_id=helper)
    memo, cfg = goal_keeper.KeeperMemo(), _config(coalesce=0, sweep=False)
    for parent, wanted in [(owner, first), (peer, "peer-goal")]:
        agents.set_lineage(helper, parent_agent_id=parent, role="helper")
        for _ in range(2):
            assert goal_keeper.due(cfg, {}, now+20000) == [wanted]
            assert goal_keeper.due(cfg, {}, now+20000, memo=memo) == [wanted]


def test_autonomy_keeps_scope_self_checks_independent(owner, monkeypatch):
    from lib import janitor_autonomy as ja
    configs = [_config(scope={"agent_ids": [owner]}), _config(scope={"exclude_agent_ids": [owner]})]
    configs[1]["agent_id"] = "keeper-2"
    worker = ja.AutonomyJanitors(dispatch=None, notify=None)
    monkeypatch.setattr(ja.janitor_builtins, "eligible", lambda role: configs)
    monkeypatch.setattr(ja.janitors, "get_progress", lambda _: {})
    monkeypatch.setattr(ja.janitors, "save_progress", lambda *args: None)
    observations = []
    def inspect(config, progress, now, *, memo):
        observations.append(memo)
        return []
    monkeypatch.setattr(goal_keeper, "due", inspect)
    worker.goal_keeper_once()
    first, second = observations
    assert first is not second
    configs.pop(0)
    worker.goal_keeper_once()
    assert observations[-1] is second and first not in worker._keeper_memos.values()
    configs[0]["generation"] += 1
    worker.goal_keeper_once()
    assert observations[-1] is not second and second not in worker._keeper_memos.values()
