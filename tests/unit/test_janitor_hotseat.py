"""Hotseat switcher: proactive default-account switching from Hotseat readings."""
import json
import pytest
from lib import agents, db, janitors, janitor_builtins, settings_store
from lib import janitor_autonomy as service
from lib import janitor_hotseat as hotseat


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
    settings_store.set_int("hotseat-switcher.claude.last-check", 0)
    worker.hotseat_once()
    assert len(sent) == 1
    readings["claude"] = claude_payload(a=(True, 0.9), b=(False, 0.2))
    settings_store.set_int("hotseat-switcher.claude.last-check", 0)
    worker.hotseat_once()
    assert len(sent) == 1  # same advice, same accounts
    readings["claude"] = claude_payload(a=(True, 0.9), c=(False, 0.2))
    settings_store.set_int("hotseat-switcher.claude.last-check", 0)
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
    worker.hotseat_once(); settings_store.set_int("hotseat-switcher.claude.last-check", 0); worker.hotseat_once()
    assert len(sent) == 1 and "no saved account has more" in sent[0]["preview"]
    readings["claude"] = claude_payload(a=(True, 0.5), b=(False, "rejected"))
    settings_store.set_int("hotseat-switcher.claude.last-check", 0); worker.hotseat_once()
    assert settings_store.get_text("hotseat-switcher.claude.notified") == ""
    readings["claude"] = claude_payload(a=(True, "rejected"), b=(False, "rejected"))
    settings_store.set_int("hotseat-switcher.claude.last-check", 0); worker.hotseat_once()
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
