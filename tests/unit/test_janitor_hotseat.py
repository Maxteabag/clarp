"""Hotseat switcher: proactive default-account switching from Hotseat readings."""
import json
import pytest
from lib import agents, db, janitors, janitor_builtins, settings_store
from lib import janitor_autonomy as service
from lib import janitor_hotseat as hotseat

def _check_again(lane):
    """Make a schedule lane due now, keeping its adaptive cadence."""
    import json
    key = "janitor-adaptive." + lane
    state = json.loads(settings_store.get_text(key, default="") or "{}")
    state["next_run_at"] = 0
    settings_store.set_text(key, json.dumps(state))



def claude_payload(**accounts):
    """alias -> (is_active, used_5h, used_7d) or (is_active, 'rejected') or (is_active, None)."""
    out = []
    for alias, spec in accounts.items():
        active = spec[0]
        if spec[1] == "rejected":
            usage = {"status": "rejected", "limited": True, "used_5h": 1, "used_7d": 0.5}
        elif spec[1] is None:
            usage = {"status": "unknown", "used_5h": None, "used_7d": None}
        else:
            usage = {"status": "allowed", "limited": False, "used_5h": spec[1], "used_7d": spec[2] if len(spec) > 2 else 0}
        out.append({"alias": alias, "is_active": active, "error": None, "usage": usage})
    return {"accounts": out}


def test_ladder_keeps_the_floor_then_ten_then_zero():
    assert hotseat.rungs(25) == [25, 10, 0]
    assert hotseat.rungs(10) == [10, 0]
    assert hotseat.rungs(7) == [7, 0]
    assert hotseat.rungs(0) == [0]


def accounts(**spec):
    return hotseat.parse_claude(claude_payload(**spec))


def test_no_switch_while_the_account_in_use_clears_the_floor():
    assert hotseat.decide(accounts(a=(True, 0.6), b=(False, 0.0)), 25)["action"] == "noop"


def test_switch_to_the_account_with_most_room_at_the_floor():
    decision = hotseat.decide(accounts(a=(True, 0.8), b=(False, 0.3), c=(False, 0.1, 0.2)), 25)
    assert decision["action"] == "switch" and decision["target"] == "c" and decision["rung"] == 25


def test_marginal_gain_below_the_floor_is_not_a_switch():
    # a has 20% left, b has 22%: nobody clears 25%, and a is not under 10%.
    assert hotseat.decide(accounts(a=(True, 0.8), b=(False, 0.78)), 25)["action"] == "noop"


def test_ladder_drops_to_ten_percent_when_no_account_clears_the_floor():
    decision = hotseat.decide(accounts(a=(True, 0.95), b=(False, 0.78)), 25)
    assert decision["action"] == "switch" and decision["target"] == "b" and decision["rung"] == 10


def test_ladder_drops_to_zero_when_only_scraps_remain():
    decision = hotseat.decide(accounts(a=(True, "rejected"), b=(False, 0.97)), 25)
    assert decision["action"] == "switch" and decision["target"] == "b" and decision["rung"] == 0
    assert hotseat.decide(accounts(a=(True, "rejected"), b=(False, "rejected")), 25)["action"] == "exhausted"


def test_unknown_readings_never_count_as_room():
    assert hotseat.decide(accounts(a=(True, None), b=(False, 0.0)), 25)["action"] == "skip"
    assert hotseat.decide(accounts(a=(True, 0.9), b=(False, None)), 25)["action"] == "noop"
    assert hotseat.decide(accounts(a=(True, "rejected"), b=(False, None)), 25)["action"] == "exhausted"
    assert hotseat.decide([], 25)["action"] == "skip"


def test_ties_prefer_the_larger_weekly_window():
    decision = hotseat.decide(accounts(a=(True, 0.9), b=(False, 0.2, 0.9), c=(False, 0.2, 0.1)), 25)
    assert decision["target"] == "c"


def test_claude_parsing_treats_errors_and_rejections_distinctly():
    payload = {"accounts": [
        {"alias": "x", "is_active": True, "error": "token expired", "usage": {"status": "allowed", "used_5h": 0.1}},
        {"alias": "y", "is_active": False, "error": None, "usage": {"status": "rejected", "limited": True, "used_5h": 1}},
        {"alias": "z", "is_active": False, "error": None, "usage": {"status": "allowed", "limited": False, "used_5h": 0.25, "used_7d": 0.5}},
        {"not": "an account"}]}
    parsed = {a["alias"]: a for a in hotseat.parse_claude(payload)}
    assert parsed["x"]["remaining"] is None
    assert parsed["y"]["remaining"] == 0
    assert parsed["z"]["remaining"] == 75 and parsed["z"]["secondary"] == 50


def test_codex_parsing_uses_the_weekly_window_and_blocks():
    payload = {"accounts": [
        {"alias": "p", "is_active": True, "usage": {"usable": True, "blocked": False, "error": None, "worst_used": 0.95}},
        {"alias": "q", "is_active": False, "usage": {"usable": False, "blocked": True, "error": None, "worst_used": 1}},
        {"alias": "r", "is_active": False, "usage": {"usable": False, "blocked": False, "error": "revoked", "worst_used": None}},
        {"alias": "s", "is_active": False, "usage": {"usable": True, "blocked": False, "error": None, "worst_used": 0.4}}]}
    parsed = {a["alias"]: a for a in hotseat.parse_codex(payload)}
    assert (parsed["p"]["remaining"], parsed["q"]["remaining"], parsed["r"]["remaining"], parsed["s"]["remaining"]) == (5, 0, None, 60)
    assert hotseat.decide(list(parsed.values()), 10)["target"] == "s"


def test_previews_name_the_accounts_and_never_the_command_output():
    decision = {"action": "switch", "active": "a", "remaining": 4, "target": "b", "target_remaining": 90, "rung": 25}
    assert hotseat.preview("claude", decision, mode="automatic", switched=True) == "Claude: switched to b. b has 90% of the 5-hour window left; a had 4%"
    assert hotseat.preview("claude", decision, mode="notify").startswith("Claude: would switch to b")
    assert hotseat.preview("codex", decision, mode="automatic", switched=False).startswith("Codex: could not switch to b")
    assert "weekly" in hotseat.preview("codex", {"action": "exhausted", "active": "a", "remaining": 0}, mode="automatic")
    assert hotseat.preview("claude", {"action": "noop"}, mode="automatic") == ""


def create_switcher(enabled=True):
    """The switcher is optional: a user creates it from the catalog on a Host with hotseat."""
    agents.create_agent(persona="Hotseat switcher", voice_id="v", cwd="/tmp", session="hotseat", backend="codex")
    cfg = janitors.create("hotseat", template_id="account-hotseat")
    return janitors.set_enabled("hotseat", cfg["revision"], True) if enabled else cfg


def test_template_is_optional_paused_and_has_configurable_floors():
    assert "account-hotseat" not in janitor_builtins.ensure_builtins()
    catalog = {v["id"]: v for v in janitors.templates()}
    assert catalog["account-hotseat"]["creatable"] and catalog["account-hotseat"]["default_trigger_id"] == "account-switch-requested"
    owner = create_switcher(enabled=False)
    assert not owner["enabled"] and owner["builtin_role"] is None
    assert owner["options"]["claude_min_remaining"] == 25 and owner["options"]["codex_min_remaining"] == 10
    assert owner["options"]["mode"] == "automatic" and owner["options"]["hotseat_command"] == "hotseat"
    changed = janitors.configure(owner["session"], owner["revision"], options={"claude_min_remaining": 40, "codex_min_remaining": 5})
    assert changed["options"]["claude_min_remaining"] == 40
    with pytest.raises(janitors.JanitorError):
        janitors.configure(changed["session"], changed["revision"], options={"claude_min_remaining": 101})


@pytest.fixture
def switcher(monkeypatch):
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    janitor_builtins.ensure_builtins()
    owner = create_switcher()
    service.setup()
    readings = {"claude": claude_payload(a=(True, 0.9), b=(False, 0.1)),
                "codex": {"accounts": [{"alias": "p", "is_active": True, "usage": {"usable": True, "blocked": False, "error": None, "worst_used": 0.5}}]}}
    calls = []; sent = []
    def read(command, provider):
        calls.append(("read", command, provider))
        return hotseat.parse_claude(readings["claude"]) if provider == "claude" else hotseat.parse_codex(readings["codex"])
    def switch(command, provider, alias):
        calls.append(("switch", command, provider, alias)); return True
    worker = service.AutonomyJanitors(lambda *_: True, lambda p: sent.append(p) or {"sent": 1}, hotseat_read=read, hotseat_switch=switch)
    return owner, worker, readings, calls, sent


def test_switches_once_per_interval_and_notifies_the_phone(switcher):
    owner, worker, readings, calls, sent = switcher
    worker.hotseat_once()
    assert [c for c in calls if c[0] == "switch"] == [("switch", "hotseat", "claude", "b")]
    assert len(sent) == 1 and sent[0]["preview"].startswith("Claude: switched to b") and sent[0]["push"] is True
    assert json.loads(settings_store.get_text("hotseat-switcher.claude.last-switch"))["to"] == "b"
    runs = janitors.list_runs(owner["session"])
    assert {r["outcome"] for r in runs} == {"completed"} and len(runs) == 2
    calls.clear(); worker.hotseat_once()
    assert calls == [] and len(sent) == 1


def test_notify_mode_reports_once_per_situation_without_switching(switcher):
    owner, worker, readings, calls, sent = switcher
    c = janitors.configure(owner["session"], owner["revision"], options={"mode": "notify"})
    janitors.set_enabled(c["session"], c["revision"], True)
    worker.hotseat_once()
    assert not [c for c in calls if c[0] == "switch"]
    assert len(sent) == 1 and sent[0]["preview"].startswith("Claude: would switch to b")
    _check_again("hotseat-switcher.claude")
    worker.hotseat_once()
    assert len(sent) == 1
    readings["claude"] = claude_payload(a=(True, 0.9), b=(False, 0.2))
    _check_again("hotseat-switcher.claude")
    worker.hotseat_once()
    assert len(sent) == 1  # same advice, same accounts
    readings["claude"] = claude_payload(a=(True, 0.9), c=(False, 0.2))
    _check_again("hotseat-switcher.claude")
    worker.hotseat_once()
    assert len(sent) == 2 and "c" in sent[1]["preview"]


def test_failed_switch_is_a_failed_run_with_a_notification(switcher):
    owner, worker, readings, calls, sent = switcher
    worker.hotseat_switch = lambda *_: False
    worker.hotseat_once()
    runs = {(r["demand_result"] or {}).get("status"): r for r in janitors.list_runs(owner["session"])}
    assert runs["switch"]["outcome"] == "failed" and "did not confirm" in runs["switch"]["error"]
    assert sent[0]["preview"].startswith("Claude: could not switch to b")
    assert settings_store.get_text("hotseat-switcher.claude.last-switch") == ""


def test_unreadable_hotseat_is_a_failed_run_and_no_switch(switcher):
    owner, worker, readings, calls, sent = switcher
    def broken(command, provider):
        raise RuntimeError("hotseat exited 1")
    worker.hotseat_read = broken
    worker.hotseat_once()
    assert not calls and not sent
    assert all(r["outcome"] == "failed" and "unavailable" in r["error"] for r in janitors.list_runs(owner["session"]))


def test_exhaustion_notifies_once_until_the_situation_changes(switcher):
    owner, worker, readings, calls, sent = switcher
    readings["claude"] = claude_payload(a=(True, "rejected"), b=(False, "rejected"))
    worker.hotseat_once(); _check_again("hotseat-switcher.claude"); worker.hotseat_once()
    assert len(sent) == 1 and "no saved account has more" in sent[0]["preview"]
    readings["claude"] = claude_payload(a=(True, 0.5), b=(False, "rejected"))
    _check_again("hotseat-switcher.claude"); worker.hotseat_once()
    assert settings_store.get_text("hotseat-switcher.claude.notified") == ""
    readings["claude"] = claude_payload(a=(True, "rejected"), b=(False, "rejected"))
    _check_again("hotseat-switcher.claude"); worker.hotseat_once()
    assert len(sent) == 2


def test_codex_switch_recycles_idle_connections(switcher, monkeypatch):
    owner, worker, readings, calls, sent = switcher
    readings["codex"] = {"accounts": [
        {"alias": "p", "is_active": True, "usage": {"usable": True, "blocked": False, "error": None, "worst_used": 0.95}},
        {"alias": "q", "is_active": False, "usage": {"usable": True, "blocked": False, "error": None, "worst_used": 0.2}}]}
    recycled = []
    # The Codex backend owns the runner recycle; Claude's is a no-op.
    monkeypatch.setattr(service.backends.by_id("codex"), "on_credential_change", lambda: recycled.append(True))
    worker.hotseat_once()
    assert ("switch", "hotseat", "codex", "q") in calls and recycled == [True]
    assert any(p["preview"].startswith("Codex: switched to q") for p in sent)


def test_paused_or_absent_switcher_does_nothing(monkeypatch):
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    janitor_builtins.ensure_builtins(); service.setup()
    worker = service.AutonomyJanitors(lambda *_: True, lambda p: pytest.fail("notified"),
                                     hotseat_read=lambda *_: pytest.fail("read"), hotseat_switch=lambda *_: pytest.fail("switched"))
    worker.hotseat_once()
    create_switcher(enabled=False)
    worker = service.AutonomyJanitors(lambda *_: True, lambda p: pytest.fail("notified"),
                                     hotseat_read=lambda *_: pytest.fail("read"), hotseat_switch=lambda *_: pytest.fail("switched"))
    worker.hotseat_once()


def test_real_hotseat_calls_are_bounded_and_parse_json(monkeypatch):
    seen = []
    class Proc:
        def __init__(self, code, out): self.returncode = code; self.stdout = out; self.stderr = ""
    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs["timeout"]))
        if argv[1:] == ["list", "--json"]: return Proc(0, json.dumps(claude_payload(a=(True, 0.5))))
        if argv[1:] == ["codex", "--json"]: return Proc(1, "")
        if argv[1] == "switch": return Proc(0, json.dumps({"alias": argv[2], "switched": True, "sessions_affected": 2}))
        return Proc(0, "")
    monkeypatch.setattr(hotseat.subprocess, "run", fake_run)
    assert hotseat.read_accounts("hotseat", "claude")[0]["remaining"] == 50
    with pytest.raises(RuntimeError):
        hotseat.read_accounts("hotseat", "codex")
    assert hotseat.switch_account("hotseat", "claude", "b") is True
    assert hotseat.switch_account("hotseat", "codex", "q") is True
    assert [argv for argv, _ in seen][2:] == [["hotseat", "switch", "b", "--yes", "--json"], ["hotseat", "codex-account", "switch", "q"]]
    assert all(kwargs_timeout <= 150 for _, kwargs_timeout in seen)


def _ownership_persisted_frame():
    """Actual durable candidates, including fields the public guard must preserve."""
    return {row["agent_id"]: dict(row) for row in db.conn().execute(
        "SELECT j.*,a.backend,a.model,a.effort,a.archived_at,a.deleted_at "
        "FROM janitor_configs j JOIN agents a ON a.agent_id=j.agent_id "
        "WHERE j.template_id=? ORDER BY j.agent_id", (hotseat.ROLE,))}


def _restore_enabled_overlap(config):
    """Deliberately inconsistent persisted fixture, NOT public-admissible config.

    Keep the real admission refusal, then seed only this named test row through
    the original storage boundary. No guard/resolver/owner result is mocked.
    """
    assert not db.conn().in_transaction
    before = _ownership_persisted_frame()
    row = before[config["agent_id"]]
    assert row["enabled"] == 0
    assert (row["revision"], row["generation"]) == (config["revision"], config["generation"])
    with pytest.raises(janitors.JanitorError) as refusal:
        janitors.set_enabled(config["session"], config["revision"], True)
    assert refusal.value.code == "scope_conflict"
    assert _ownership_persisted_frame() == before
    now = db.now_ms()
    # Independent fixture transaction, before capture/the real receipt writer;
    # use the original store context, not the transaction hook under test.
    with service.janitor_store.write() as c:
        service.janitor_store.set_config_enabled(c, config["agent_id"], True, now)
    assert not db.conn().in_transaction
    expected = {aid: dict(value) for aid, value in before.items()}
    expected[config["agent_id"]].update(enabled=1, revision=row["revision"] + 1,
        generation=row["generation"] + 1, last_error="", updated_at=now)
    assert _ownership_persisted_frame() == expected
    restored = janitors.get(config["session"])
    for key in ("agent_id", "template_id", "backend", "model", "effort", "revision", "generation"):
        assert restored[key] == expected[config["agent_id"]][key]
    assert restored["enabled"] is True
    assert restored["scope"] == json.loads(row["scope_json"])
    return restored


@pytest.fixture
def notification_ownership(switcher, monkeypatch):
    """Real configured owners and one deterministic scheduler tick; all I/O is synthetic."""
    import datetime
    owner, worker, readings, calls, sent = switcher
    quota = janitor_builtins.get_builtin("quota-monitor")
    quota = janitors.set_enabled(quota["session"], quota["revision"], True)
    clock = [1_800_000_000_000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    stamp = datetime.datetime.fromtimestamp(clock[0] / 1000, datetime.timezone.utc).isoformat()
    window = {"window_id": "ownership-window", "used_percentage": 90,
              "observed_at": stamp, "freshness": "fresh"}
    usage = {"providers": {"claude": {"provider_instance_id": "opaque-ownership-account", "windows": [window]}}}
    def read_usage():
        calls.append(("usage",))
        return usage
    worker.usage_read = read_usage
    original_notify = worker.notify
    def notify(payload):
        assert not db.conn().in_transaction, "notification ran inside the receipt writer"
        return original_notify(payload)
    worker.notify = notify
    return {"owner": owner, "quota": quota, "worker": worker, "readings": readings,
            "calls": calls, "sent": sent, "clock": clock, "usage": usage, "window": window}


def _ownership_receipts(case):
    rows = db.conn().execute("SELECT * FROM janitor_quota_receipts WHERE json_extract(payload_json,'$.reason')='quota_threshold'").fetchall()
    return [dict(row) for row in rows]


def _quota_count(case):
    run = janitors.list_runs(case["quota"]["session"])[0]
    return run["demand_result"]["item_count"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_one_tick_threshold_and_automatic_switch_have_one_notification(notification_ownership, provider):
    c = notification_ownership
    if provider == "codex":
        c["usage"]["providers"] = {"codex": c["usage"]["providers"].pop("claude")}
        c["window"]["used_percentage"] = 95
        c["readings"]["claude"] = claude_payload(a=(True, 0.5))
        c["readings"]["codex"] = {"accounts": [
            {"alias": "p", "is_active": True, "usage": {"usable": True, "worst_used": 0.95}},
            {"alias": "q", "is_active": False, "usage": {"usable": True, "worst_used": 0.1}}]}
    assert janitor_builtins.resolve(hotseat.ROLE)["agent_id"] == c["owner"]["agent_id"]
    c["worker"].run_once()
    assert [p["reason"] for p in c["sent"]] == ["account_switch"]
    audit, = _ownership_receipts(c)
    assert "cancelled" in json.loads(audit["delivery_json"])
    assert json.loads(audit["payload_json"])["reason"] == "quota_threshold"
    assert _quota_count(c) == 0
    assert c["calls"].count(("usage",)) == 1
    assert len([v for v in c["calls"] if v[0] == "read"]) == 2
    assert len([v for v in c["calls"] if v[0] == "switch"]) == 1
    c["worker"].deliver_notifications(c["quota"])
    assert len(c["sent"]) == 1


def test_automatic_noop_owns_threshold_without_any_push_and_keeps_watermark(notification_ownership):
    c = notification_ownership
    cfg = janitors.configure(c["owner"]["session"], c["owner"]["revision"], options={"claude_min_remaining": 10})
    janitors.set_enabled(cfg["session"], cfg["revision"], True)
    c["window"]["used_percentage"] = 80
    c["readings"]["claude"] = claude_payload(a=(True, 0.8), b=(False, 0.1))
    c["worker"].run_once()
    assert c["sent"] == []
    audit, = _ownership_receipts(c)
    assert "cancelled" in json.loads(audit["delivery_json"])
    assert _quota_count(c) == 0
    key = service.quota_window_key("claude", "opaque-ownership-account", c["window"])
    assert json.loads(settings_store.get_text(key))["remaining"] == 20
    c["clock"][0] += 1000
    _check_again("quota-keeper")
    _check_again("hotseat-switcher.claude")
    c["worker"].run_once()
    assert c["sent"] == [] and len(_ownership_receipts(c)) == 1


@pytest.mark.parametrize("state", ["notify", "disabled", "missing", "ambiguous", "restricted", "unrunnable", "unsupported"])
def test_threshold_notice_remains_when_no_automatic_provider_owner(notification_ownership, monkeypatch, state):
    c = notification_ownership
    owner = c["owner"]
    if state == "notify":
        cfg = janitors.configure(owner["session"], owner["revision"], options={"mode": "notify"})
        janitors.set_enabled(cfg["session"], cfg["revision"], True)
    elif state == "disabled":
        janitors.set_enabled(owner["session"], owner["revision"], False)
    elif state == "missing":
        agents.soft_delete(owner["agent_id"])
    elif state == "ambiguous":
        agents.create_agent(persona="Other switcher", voice_id="v", cwd="/tmp", session="other-hotseat", backend="codex")
        cfg = janitors.create("other-hotseat", template_id="account-hotseat")
        _restore_enabled_overlap(cfg)
    elif state == "restricted":
        aid = agents.create_agent(persona="Scope fixture", voice_id="v", cwd="/tmp", session="scope-fixture", backend="codex")
        cfg = janitors.configure(owner["session"], owner["revision"], scope={"agent_ids": [aid]})
        janitors.set_enabled(cfg["session"], cfg["revision"], True)
    elif state == "unrunnable":
        original = janitors._execution
        def unavailable(role, *args, **kwargs):
            if role == hotseat.ROLE:
                raise janitors.JanitorError("Synthetic unavailable executor")
            return original(role, *args, **kwargs)
        monkeypatch.setattr(janitors, "_execution", unavailable)
    else:
        c["usage"]["providers"] = {"openai": c["usage"]["providers"].pop("claude")}
    c["worker"].run_once()
    thresholds = [p for p in c["sent"] if p["reason"] == "quota_threshold"]
    assert len(thresholds) == 1
    assert thresholds[0]["preview"] == ("openai" if state == "unsupported" else "claude") + ": 10% quota remaining"
    audit, = _ownership_receipts(c)
    assert json.loads(audit["delivery_json"]) == {"sent": 1}
    assert _quota_count(c) == 1
    if state == "notify":
        assert [p["reason"] for p in c["sent"]] == ["quota_threshold", "account_switch"]
        assert not [v for v in c["calls"] if v[0] == "switch"]


def test_current_configuration_is_read_after_usage_not_cached_before_it(notification_ownership):
    c = notification_ownership
    original = c["worker"].usage_read
    def usage_after_mode_change():
        cfg = janitors.configure(c["owner"]["session"], c["owner"]["revision"], options={"mode": "notify"})
        janitors.set_enabled(cfg["session"], cfg["revision"], True)
        return original()
    c["worker"].usage_read = usage_after_mode_change
    c["worker"].run_once()
    assert [p["reason"] for p in c["sent"]] == ["quota_threshold", "account_switch"]


def test_owner_resolution_fault_keeps_threshold_notice_without_extra_probe(notification_ownership, monkeypatch, capsys):
    c = notification_ownership
    original = janitors._execution
    def broken(role, *args, **kwargs):
        if role == hotseat.ROLE:
            raise RuntimeError("PRIVATE_RESOLUTION_SENTINEL")
        return original(role, *args, **kwargs)
    monkeypatch.setattr(janitors, "_execution", broken)
    c["worker"].quota_once()
    assert [p["reason"] for p in c["sent"]] == ["quota_threshold"]
    assert c["calls"] == [("usage",)]
    captured = capsys.readouterr()
    assert "PRIVATE_RESOLUTION_SENTINEL" not in captured.out + captured.err


@pytest.mark.parametrize("mode", ["notify", "ask", "automatic"])
def test_exhaustion_keeps_hotseat_notice_and_recovery_authority(notification_ownership, monkeypatch, mode):
    from lib import artifacts, turn_dispatch
    c = notification_ownership
    quota = janitors.configure(c["quota"]["session"], c["quota"]["revision"], options={"recovery_mode": mode})
    c["quota"] = janitors.set_enabled(quota["session"], quota["revision"], True)
    c["window"]["used_percentage"] = 100
    c["readings"]["claude"] = claude_payload(a=(True, "rejected"), b=(False, "rejected"))
    monkeypatch.setattr(turn_dispatch, "account_selector", lambda provider: ("synthetic-selector",))
    recovered = []
    def recover(*args):
        assert not db.conn().in_transaction
        recovered.append(args)
        return {"status": "recovering"}
    c["worker"].recover = recover
    c["worker"].run_once()
    assert [p["reason"] for p in c["sent"]] == ["account_switch"]
    assert c["sent"][0]["preview"] == "Claude: a has 0% of the 5-hour window left and no saved account has more"
    audit, = _ownership_receipts(c)
    assert "cancelled" in json.loads(audit["delivery_json"])
    payload = json.loads(audit["payload_json"])
    assert _quota_count(c) == 0
    if mode == "notify":
        assert recovered == [] and "recovery" not in payload
    elif mode == "automatic":
        assert len(recovered) == 1 and payload["recovery"]["status"] == "recovering"
    else:
        assert recovered == [] and payload["recovery"]["status"] == "awaiting_approval"
        decision = db.conn().execute("SELECT decision_id,revision FROM artifact_decisions WHERE artifact_id=?",
                                     (payload["recovery"]["artifact_id"],)).fetchone()
        artifacts.resolve(decision["decision_id"], expected_revision=decision["revision"], choice="accepted")
        c["worker"].recover_approved(c["quota"])
        c["worker"].recover_approved(c["quota"])
        assert len(recovered) == 1


def test_suppressed_insert_is_never_visible_pending_to_an_independent_reader(notification_ownership, monkeypatch):
    import sqlite3
    c = notification_ownership
    with sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True) as observer:
        seen = []
        original = service.janitor_store.set_quota_receipt_delivery
        def cancelling(receipt_id, delivery_json, *args, **kwargs):
            seen.append(observer.execute("SELECT COUNT(*) FROM janitor_quota_receipts WHERE receipt_id=? AND delivery_json IS NULL", (receipt_id,)).fetchone()[0])
            original(receipt_id, delivery_json, *args, **kwargs)
            seen.append(observer.execute("SELECT COUNT(*) FROM janitor_quota_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()[0])
        monkeypatch.setattr(service.janitor_store, "set_quota_receipt_delivery", cancelling)
        c["worker"].quota_once()
        assert seen == [0, 0]
        assert observer.execute("SELECT COUNT(*) FROM janitor_quota_receipts WHERE delivery_json IS NULL").fetchone()[0] == 0
        assert observer.execute("SELECT COUNT(*) FROM janitor_quota_receipts").fetchone()[0] == 1
    assert c["sent"] == []


@pytest.mark.parametrize("fault", ["insert", "cancel"])
def test_new_receipt_fault_rolls_back_without_notify_or_recovery(notification_ownership, monkeypatch, fault):
    import sqlite3
    c = notification_ownership
    c["window"]["used_percentage"] = 100
    quota = janitors.configure(c["quota"]["session"], c["quota"]["revision"], options={"recovery_mode": "automatic"})
    c["quota"] = janitors.set_enabled(quota["session"], quota["revision"], True)
    c["worker"].request_recovery = lambda *a: pytest.fail("recovery admitted before receipt transaction succeeded")
    name = "insert_quota_receipt" if fault == "insert" else "set_quota_receipt_delivery"
    original = getattr(service.janitor_store, name)
    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("INJECTED_RECEIPT_FAULT")
    monkeypatch.setattr(service.janitor_store, name, fail_after_write)
    with pytest.raises(sqlite3.OperationalError, match="INJECTED_RECEIPT_FAULT"):
        c["worker"].quota_once()
    assert db.conn().execute("SELECT COUNT(*) FROM janitor_quota_receipts").fetchone()[0] == 0
    assert c["sent"] == [] and not db.conn().in_transaction


def test_old_pending_receipt_conflict_keeps_normal_delivery_metadata(notification_ownership):
    import datetime
    c = notification_ownership
    run = janitor_builtins.begin_run("quota-monitor", "pending-before-ownership")
    assert run and janitor_builtins.claim_run(run["run_id"])
    stamp = int(datetime.datetime.fromisoformat(c["window"]["observed_at"]).timestamp() * 1000)
    rid = service.digest(["claude", "opaque-ownership-account", "ownership-window", stamp, c["quota"]["generation"]])
    payload = {"notification_id": rid, "session": c["quota"]["session"], "agent_id": c["quota"]["agent_id"],
               "persona": c["quota"]["name"], "preview": "claude: 10% quota remaining", "push": True,
               "reason": "quota_threshold", "recovery_mode": "notify", "owner_generation": c["quota"]["generation"]}
    assert service.janitor_store.insert_quota_receipt(rid, run["run_id"], json.dumps(payload), c["clock"][0])
    janitor_builtins.complete_run(run["run_id"])
    c["worker"].run_once()
    assert [p["reason"] for p in c["sent"]] == ["quota_threshold", "account_switch"]
    audit, = _ownership_receipts(c)
    assert audit["receipt_id"] == rid
    assert json.loads(audit["delivery_json"]) == {"sent": 1}
    assert json.loads(audit["payload_json"]) == payload


def test_receipt_writer_never_builds_full_janitor_views(notification_ownership, monkeypatch):
    from contextlib import contextmanager
    c = notification_ownership
    inside = [False]
    observed = []
    original_tx = settings_store.transaction
    @contextmanager
    def receipt_transaction():
        with original_tx():
            inside[0] = True
            try:
                yield
            finally:
                inside[0] = False
    monkeypatch.setattr(settings_store, "transaction", receipt_transaction)
    for module, name in [(janitor_builtins, "resolve"), (janitor_builtins, "eligible"),
                         (janitors, "get"), (janitors, "_execution"),
                         (janitors, "_model"), (janitors, "_cadence")]:
        original = getattr(module, name)
        def observing(*args, _original=original, _name=name, **kwargs):
            observed.append((_name, inside[0]))
            return _original(*args, **kwargs)
        monkeypatch.setattr(module, name, observing)
    c["worker"].run_once()
    assert observed and any(name == "resolve" for name, _ in observed)
    assert not [name for name, under_writer in observed if under_writer], observed
    assert [p["reason"] for p in c["sent"]] == ["account_switch"]


def _restricted_ownership_sibling():
    aid = agents.create_agent(persona="Restricted switcher", voice_id="v", cwd="/tmp",
                              session="restricted-hotseat", backend="codex")
    target = agents.create_agent(persona="Scope target", voice_id="v", cwd="/tmp",
                                 session="restricted-target", backend="codex")
    cfg = janitors.create("restricted-hotseat", template_id="account-hotseat", scope={"agent_ids": [target]})
    return _restore_enabled_overlap(cfg)


def test_one_unrestricted_owner_still_suppresses_with_restricted_sibling(notification_ownership):
    c = notification_ownership
    _restricted_ownership_sibling()
    assert janitor_builtins.resolve(hotseat.ROLE)["agent_id"] == c["owner"]["agent_id"]
    c["worker"].run_once()
    assert [p["reason"] for p in c["sent"]] == ["account_switch"]
    audit, = _ownership_receipts(c)
    assert "cancelled" in json.loads(audit["delivery_json"])


@pytest.mark.parametrize("change", ["new_candidate", "enable", "delete", "archive", "mode", "scope", "effort", "sibling_scope"])
def test_candidate_set_and_all_fingerprints_are_rechecked_at_receipt_transaction(notification_ownership, monkeypatch, change):
    from contextlib import contextmanager
    c = notification_ownership
    sibling = None
    if change in {"enable", "sibling_scope"}:
        sibling = _restricted_ownership_sibling()
        if change == "enable":
            sibling = janitors.configure(sibling["session"], sibling["revision"], scope={})
            sibling = janitors.set_enabled(sibling["session"], sibling["revision"], False)
    capture_frame = _ownership_persisted_frame()
    assert capture_frame[c["owner"]["agent_id"]]["enabled"] == 1
    if sibling is not None:
        actual = capture_frame[sibling["agent_id"]]
        assert bool(actual["enabled"]) == sibling["enabled"]
        assert json.loads(actual["scope_json"]) == sibling["scope"]
        assert (actual["revision"], actual["generation"]) == (sibling["revision"], sibling["generation"])
    assert janitor_builtins.resolve(hotseat.ROLE)["agent_id"] == c["owner"]["agent_id"]
    assert _ownership_persisted_frame() == capture_frame
    original_tx = settings_store.transaction
    changed = []
    @contextmanager
    def change_before_writer():
        # Actual config mutation AFTER outside capture and BEFORE BEGIN: no
        # in-memory owner stub, sleep, nested transaction or manufactured receipt.
        if not changed:
            changed.append(True)
            owner = c["owner"]
            if change == "new_candidate":
                agents.create_agent(persona="New switcher", voice_id="v", cwd="/tmp", session="new-hotseat", backend="codex")
                new = janitors.create("new-hotseat", template_id="account-hotseat")
                _restore_enabled_overlap(new)
            elif change == "enable":
                _restore_enabled_overlap(sibling)
            elif change == "delete": agents.soft_delete(owner["agent_id"])
            elif change == "archive": agents.set_archived(owner["agent_id"], True)
            elif change == "mode": janitors.configure(owner["session"], owner["revision"], options={"mode": "notify"})
            elif change == "scope":
                target = agents.create_agent(persona="New target", voice_id="v", cwd="/tmp", session="new-target", backend="codex")
                janitors.configure(owner["session"], owner["revision"], scope={"agent_ids": [target]})
            elif change == "effort":
                janitors.configure(owner["session"], owner["revision"], effort="high" if owner["effort"] != "high" else "low")
            else:
                # configure pauses and bumps both clocks; restore enabled state
                # explicitly. This is scope + revision/generation change, not
                # a fictional scope-only public transition.
                prior = _ownership_persisted_frame()[sibling["agent_id"]]
                configured = janitors.configure(sibling["session"], sibling["revision"], scope={})
                paused = _ownership_persisted_frame()[sibling["agent_id"]]
                assert paused["enabled"] == 0 and json.loads(paused["scope_json"]) == {"agent_ids": [], "exclude_agent_ids": []}
                assert paused["revision"] == prior["revision"] + 1
                assert paused["generation"] == prior["generation"] + 1
                _restore_enabled_overlap(configured)
                restored = _ownership_persisted_frame()[sibling["agent_id"]]
                assert restored["enabled"] == 1 and json.loads(restored["scope_json"]) == {"agent_ids": [], "exclude_agent_ids": []}
                assert restored["revision"] == prior["revision"] + 2
                assert restored["generation"] == prior["generation"] + 2
                assert {k: v for k, v in restored.items() if k not in
                        {"scope_json", "enabled", "revision", "generation", "last_error", "updated_at"}} == {
                        k: v for k, v in prior.items() if k not in
                        {"scope_json", "enabled", "revision", "generation", "last_error", "updated_at"}}
        assert not db.conn().in_transaction
        with original_tx():
            yield
    monkeypatch.setattr(settings_store, "transaction", change_before_writer)
    c["worker"].run_once()
    assert changed == [True]
    assert len([p for p in c["sent"] if p["reason"] == "quota_threshold"]) == 1
    audit, = _ownership_receipts(c)
    assert json.loads(audit["delivery_json"]) == {"sent": 1}


def test_unstable_capture_is_not_retried_or_used_to_suppress(notification_ownership, monkeypatch):
    c = notification_ownership
    original = janitor_builtins.resolve
    resolved = []
    def changed_during_resolve(role, **kwargs):
        owner = original(role, **kwargs)
        if role == hotseat.ROLE:
            resolved.append(True)
            janitors.configure(owner["session"], owner["revision"], options={"mode": "notify"})
        return owner
    monkeypatch.setattr(janitor_builtins, "resolve", changed_during_resolve)
    c["worker"].quota_once()
    assert resolved == [True]
    assert [p["reason"] for p in c["sent"]] == ["quota_threshold"]


def test_fingerprint_read_failure_inside_writer_is_not_a_fallback_delivery(notification_ownership, monkeypatch):
    import sqlite3
    c = notification_ownership
    original = service.janitor_store.admission_fingerprint
    def broken_under_writer(*args, **kwargs):
        if args and args[0] == c["owner"]["agent_id"] and db.conn().in_transaction:
            raise sqlite3.OperationalError("INJECTED_FINGERPRINT_READ_FAULT")
        return original(*args, **kwargs)
    monkeypatch.setattr(service.janitor_store, "admission_fingerprint", broken_under_writer)
    with pytest.raises(sqlite3.OperationalError, match="INJECTED_FINGERPRINT_READ_FAULT"):
        c["worker"].quota_once()
    assert c["sent"] == []
    assert db.conn().execute("SELECT COUNT(*) FROM janitor_quota_receipts").fetchone()[0] == 0
    assert not db.conn().in_transaction
