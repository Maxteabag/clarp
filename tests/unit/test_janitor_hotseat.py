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


@pytest.mark.parametrize("active_failed", [False, True])
@pytest.mark.parametrize("emitter_code", ["auth_error", "provider_error", "timeout"])
def test_additive_auth_metadata_never_changes_ordinary_parse_or_decide(active_failed, emitter_code):
    # The Go emitter preserves this original error text and only appends fields.
    original = {"accounts": [
        {"alias": "a", "is_active": True, "error": "opaque" if active_failed else None,
         "usage": None if active_failed else {"used_5h": 0.9, "used_7d": 0.5}},
        {"alias": "b", "is_active": False, "error": "opaque", "usage": None},
        {"alias": "c", "is_active": False, "error": None, "usage": {"used_5h": 0.2, "used_7d": 0.1}}]}
    appended = json.loads(json.dumps(original))
    for row in appended["accounts"]:
        row["org_uuid"] = "private-exact-org-sentinel"
        row["error_code"] = emitter_code if row["error"] else None
    expected_accounts = [
        {"alias": "a", "active": True, "remaining": None if active_failed else 10,
         "secondary": None if active_failed else 50},
        {"alias": "b", "active": False, "remaining": None, "secondary": None},
        {"alias": "c", "active": False, "remaining": 80, "secondary": 90}]
    expected_decision = ({"action": "skip", "reason": "a: quota unknown"} if active_failed else
                         {"action": "switch", "active": "a", "remaining": 10, "target": "c",
                          "target_remaining": 80, "rung": 25})
    for payload in (original, appended):
        parsed = hotseat.parse_claude(payload)
        assert parsed == expected_accounts
        assert hotseat.decide(parsed, 25) == expected_decision


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


# 934 source-first controls: private identity/auth eligibility is distinct from
# fresh quota/model usability. These do not start a primitive or provider read.
_REPAIR_ORG = "00000000-0000-4000-8000-000000000001"
_REPAIR_NOW = 1_800_000_000_000


def _repair_report(**changes):
    row = {"alias": "saved", "email": "fixture@example.invalid", "org_uuid": _REPAIR_ORG,
           "is_active": False, "error": "opaque", "error_code": "auth_error", "usage": None}
    row.update(changes)
    return {"generated_at": _REPAIR_NOW / 1000, "accounts": [row]}


def test_auth_repair_option_is_false_until_explicitly_enabled():
    janitor_builtins.ensure_builtins()
    owner = create_switcher(enabled=False)
    assert owner["options"].get("auto_repair_auth") is False


@pytest.mark.parametrize("change", [
    {"error_code": None}, {"error_code": "provider_error"}, {"error_code": "timeout"},
    {"error_code": "exhausted"}, {"error": None}, {"error": 401},
    {"usage": {"status": "rejected", "limited": True, "used_5h": 1}},
    {"usage": {"status": "allowed", "limited": False, "used_5h": 0.1}},
    {"org_uuid": None}, {"org_uuid": "Display Org Name"},
    {"email": ""}, {"alias": "../saved"}, {"alias": "Saved"},
    {"is_active": True}, {"is_active": None}])
def test_repair_requires_a_canonical_inactive_exact_profile_and_typed_auth(change):
    assert hotseat.repair_profiles(_repair_report(**change)) == ()


def test_repair_profile_is_private_bounded_and_ambiguous_aliases_are_ineligible():
    report = _repair_report()
    profile, = hotseat.repair_profiles(report)
    assert (profile.alias, profile.email, profile.org_uuid) == ("saved", "fixture@example.invalid", _REPAIR_ORG)
    assert "fixture@example.invalid" not in repr(profile) and _REPAIR_ORG not in repr(profile)
    report["accounts"].append(dict(report["accounts"][0]))
    assert hotseat.repair_profiles(report) == ()
    report["accounts"] = report["accounts"] * 17
    assert hotseat.repair_profiles(report) == ()


def _usable_repair_report():
    return _repair_report(error=None, error_code=None, usage={
        "status": "allowed", "available": True, "limited": False, "used_5h": 0.2,
        "used_7d": 0.3, "blocked_models": [], "scoped": [
            {"model": "Fable", "used": 0.4, "exhausted": False}]})


@pytest.mark.parametrize("failure", ["identity", "alias", "auth", "quota", "model", "unknown_model",
                                     "degraded", "stale", "future", "missing_time"])
def test_repaired_auth_alone_never_proves_identity_and_requested_model_quota(failure):
    profile, = hotseat.repair_profiles(_repair_report())
    report = _usable_repair_report()
    row = report["accounts"][0]
    if failure == "identity": row["org_uuid"] = "00000000-0000-4000-8000-000000000002"
    elif failure == "alias": row["alias"] = "renamed"
    elif failure == "auth": row["error_code"] = "auth_error"
    elif failure == "quota": row["usage"]["used_5h"] = 1
    elif failure == "model": row["usage"]["scoped"][0]["used"] = 1
    elif failure == "unknown_model": row["usage"]["scoped"] = []
    elif failure == "degraded": row["usage"]["degraded"] = "per-model limits unavailable"
    elif failure == "stale": report["generated_at"] -= 600
    elif failure == "future": report["generated_at"] += 600
    else: report.pop("generated_at")
    assert not hotseat.repair_profile_usable(report, profile, requested_model="Fable",
                                            not_before_ms=_REPAIR_NOW - 1000, now_ms=_REPAIR_NOW + 1000)


def test_only_fresh_exact_identity_plus_known_model_headroom_is_usable():
    profile, = hotseat.repair_profiles(_repair_report())
    report = _usable_repair_report()
    assert hotseat.repair_profile_usable(report, profile, requested_model="Fable",
                                         not_before_ms=_REPAIR_NOW - 1000, now_ms=_REPAIR_NOW + 1000)
    assert not hotseat.repair_profile_usable(report, profile, requested_model="UnreportedModel",
                                            not_before_ms=_REPAIR_NOW - 1000, now_ms=_REPAIR_NOW + 1000)


def _repair_read_start(started_ms=None):
    import time
    return hotseat._RepairRead(db.now_ms() if started_ms is None else started_ms, time.monotonic())


def _repair_source():
    """A new genuine report for one direct component call, not a worker refresh."""
    started = db.now_ms()
    source = hotseat._RepairSource.capture({"generated_at": started / 1000}, _repair_read_start(started))
    assert source is not None
    return source


@pytest.fixture
def repair_ledger_case(monkeypatch, tmp_path):
    clock = [(_REPAIR_NOW // 86_400_000) * 86_400_000 + 3_600_000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    home = tmp_path / "repair-home"
    (home / ".claude" / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    janitor_builtins.ensure_builtins()
    owner = create_switcher()
    owner = janitors.configure(owner["session"], owner["revision"], options={"auto_repair_auth": True})
    owner = janitors.set_enabled(owner["session"], owner["revision"], True)
    from lib import turn_queue
    owner = {**owner, "_repair_stop_generation": turn_queue.user_stop_generation(owner["agent_id"])}
    profile, = hotseat.repair_profiles(_repair_report())
    def run(label):
        item = janitor_builtins.begin_run(hotseat.ROLE, label)
        assert item and janitor_builtins.claim_run(item["run_id"])
        janitor_builtins.complete_run(item["run_id"])
        return item["run_id"]
    return clock, owner, profile, run


def test_repair_ledger_consumes_one_attempt_per_run_and_backoff_survives_reload(repair_ledger_case):
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    rid = run("first")
    claim = ledger.reserve(profile, owner, rid, source=_repair_source())
    assert claim is not None
    assert ledger.finish(claim, "transient_failure", cleanup_confirmed=True)
    state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
    record = state["profiles"][claim.key]
    # Persisted repair references stay in the existing private status-identity
    # domain; the read-only OFF path cannot invent a second key protocol.
    assert claim.key == hotseat.hotseat_status.identity_key("claude", profile.alias,
        email=profile.email.casefold(), org=profile.org_uuid, account_id="auth-repair")
    assert record["alias_key"] == hotseat.hotseat_status.identity_key("claude", profile.alias, account_id="auth-repair-alias")
    assert (record["status"], record["attempts"], record["next_retry_at"]) == ("failed", 1, clock[0] + 6 * 3_600_000)
    other = hotseat.RepairProfile("other", "other@example.invalid", _REPAIR_ORG)
    assert hotseat.RepairLedger().reserve(other, owner, rid, source=_repair_source()) is None
    assert hotseat.RepairLedger().reserve(profile, owner, run("too-soon"), source=_repair_source()) is None
    clock[0] += 6 * 3_600_000 + 1
    second = hotseat.RepairLedger().reserve(profile, owner, run("second"), source=_repair_source())
    assert second is not None
    assert ledger.finish(second, "transient_failure", cleanup_confirmed=True)
    record = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))["profiles"][claim.key]
    assert record["attempts"] == 2 and record["next_retry_at"] == clock[0] + 24 * 3_600_000
    clock[0] = ((clock[0] // 86_400_000) + 1) * 86_400_000
    assert hotseat.RepairLedger().reserve(profile, owner, run("midnight"), source=_repair_source()) is None
    stored = settings_store.get_text(hotseat.REPAIR_STATE_KEY)
    assert profile.email not in stored and profile.org_uuid not in stored


def test_repair_ledger_shared_host_cap_and_restart_fail_closed(repair_ledger_case):
    clock, owner, profile, run = repair_ledger_case
    for i in range(3):
        candidate = hotseat.RepairProfile("saved-" + str(i), profile.email, profile.org_uuid)
        ledger = hotseat.RepairLedger()
        claim = ledger.reserve(candidate, owner, run("cap-" + str(i)), source=_repair_source())
        assert claim is not None
        assert ledger.finish(claim, "transient_failure", cleanup_confirmed=True)
        clock[0] += 1
    assert hotseat.RepairLedger().reserve(profile, owner, run("fourth"), source=_repair_source()) is None
    state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
    assert state["count"] == 3 and len(state["profiles"]) == 3


@pytest.mark.parametrize("phase", ["reserved", "spawn_pending", "running", "cleaned"])
def test_interrupted_repair_is_never_success_and_uncertain_effects_do_not_expire(repair_ledger_case, monkeypatch, phase):
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("interrupted"), source=_repair_source())
    assert claim is not None
    if phase != "reserved":
        assert ledger.phase(claim, "spawn_pending")
        if phase != "spawn_pending": assert ledger.phase(claim, "running")
        if phase == "cleaned": assert ledger.phase(claim, "cleaned")
    monkeypatch.setattr(hotseat, "_repair_controller_ended", lambda active: True)
    other = hotseat.RepairProfile("other", profile.email, profile.org_uuid)
    next_claim = hotseat.RepairLedger().reserve(other, owner, run("after-restart"), source=_repair_source())
    state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
    old = state["profiles"][claim.key]
    assert old["status"] == "failed" and old["last_code"] == "interrupted" and old["attempts"] == 1
    if phase in {"reserved", "cleaned"}:
        assert next_claim is not None
        assert old["unverified"] is (phase == "cleaned")
    else:
        assert next_claim is None and state["active"]["phase"] == "uncertain"
        clock[0] += 2 * 86_400_000
        assert hotseat.RepairLedger().reserve(other, owner, run("not-a-ttl"), source=_repair_source()) is None


def test_repair_ledger_real_concurrent_reservations_serialize_one_host(repair_ledger_case):
    import threading
    clock, owner, profile, run = repair_ledger_case
    ids = [run("concurrent-a"), run("concurrent-b")]
    barrier = threading.Barrier(2)
    claims, errors = [], []
    def reserve(rid):
        try:
            barrier.wait(timeout=5)
            claims.append(hotseat.RepairLedger().reserve(profile, owner, rid, source=_repair_source()))
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close_local()
    threads = [threading.Thread(target=reserve, args=(rid,)) for rid in ids]
    for thread in threads: thread.start()
    for thread in threads: thread.join(10)
    assert not any(thread.is_alive() for thread in threads), "repair reservation worker leaked"
    assert errors == [] and len([claim for claim in claims if claim is not None]) == 1
    state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
    assert state["count"] == 1


@pytest.mark.parametrize("corruption", ["json", "future_version", "future_clock"])
def test_repair_ledger_corruption_never_resets_credits(repair_ledger_case, corruption):
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("seed"), source=_repair_source())
    assert claim and ledger.finish(claim, "transient_failure", cleanup_confirmed=True)
    if corruption == "json": raw = "not JSON"
    else:
        state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
        if corruption == "future_version": state["v"] = 999
        else: state["clock"] = clock[0] + 86_400_000
        raw = json.dumps(state)
    settings_store.set_text(hotseat.REPAIR_STATE_KEY, raw)
    with pytest.raises(hotseat.RepairPolicyError):
        ledger.reserve(profile, owner, run("corrupt"), source=_repair_source())
    assert settings_store.get_text(hotseat.REPAIR_STATE_KEY) == raw


def test_repair_ledger_stop_resume_rejects_old_offer_and_late_success(repair_ledger_case):
    from lib import turn_queue
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("before-stop"), source=_repair_source())
    assert claim is not None
    assert ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    turn_queue.set_paused(owner["agent_id"], True, user_stop=True)
    turn_queue.set_paused(owner["agent_id"], False)
    assert ledger.finish(claim, "repaired", cleanup_confirmed=True)
    state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
    row = state["profiles"][claim.key]
    assert row["status"] == "failed" and row["last_code"] == "interrupted" and row["unverified"] is True
    other = hotseat.RepairProfile("other", profile.email, profile.org_uuid)
    assert ledger.reserve(other, owner, run("stale-stop-context"), source=_repair_source()) is None


def test_uncertain_terminal_receipt_is_idempotent_and_never_refunds(repair_ledger_case):
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("uncertain-terminal"), source=_repair_source())
    assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    assert ledger.finish(claim, "transient_failure", cleanup_confirmed=False)
    assert not ledger.finish(claim, "transient_failure", cleanup_confirmed=False)
    # A later positive owned cleanup acknowledgement may release resources, but
    # cannot rewrite the old failure as success, refund credit or hide possible publication.
    assert ledger.finish(claim, "transient_failure", cleanup_confirmed=True)
    state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
    row = state["profiles"][claim.key]
    assert state["active"] is None and state["count"] == 1
    assert (row["attempts"], row["failures"], row["status"], row["unverified"]) == (1, 1, "failed", True)


def test_repair_candidate_debt_needs_fresh_identity_model_and_same_nonce(repair_ledger_case):
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("candidate-debt"), source=_repair_source())
    assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    assert ledger.finish(claim, "repaired", cleanup_confirmed=True)
    accounts = [{"alias": "working", "active": True, "remaining": 0, "secondary": 0},
                {"alias": profile.alias, "active": False, "remaining": 80, "secondary": 70}]
    report = _usable_repair_report()
    report["generated_at"] = clock[0] / 1000
    # Successful auth has no candidate authority. Neither an absent model nor
    # fresh-looking quota belonging to a replacement identity can clear debt.
    for model, payload in [("UnreportedModel", report), ("Fable", _repair_report())]:
        result = ledger.candidates(accounts, payload, owner=owner, requested_model=model,
                                   read_origin=_repair_read_start(clock[0]))
        assert result[0] == accounts[0] and result[1]["remaining"] is None
    changed = json.loads(json.dumps(report))
    changed["accounts"][0]["org_uuid"] = "00000000-0000-4000-8000-000000000002"
    assert ledger.candidates(accounts, changed, owner=owner, requested_model="Fable",
                             read_origin=_repair_read_start(clock[0]))[1]["remaining"] is None
    # A fresh read proves identity and quota independently. This is a new read,
    # not the primitive's success code; only decide() may subsequently switch.
    assert ledger.candidates(accounts, report, owner=owner, requested_model="Fable",
                             read_origin=_repair_read_start(clock[0])) == accounts
    state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
    assert state["profiles"][claim.key]["status"] == "repaired"
    assert state["profiles"][claim.key]["last_code"] == "verified"
    assert state["count"] == 1 and state["profiles"][claim.key]["attempts"] == 1
    # A different requested model needs its own current quota evidence even
    # after the prior model was verified. Caller data must not be mutated.
    assert ledger.candidates(accounts, report, owner=owner, requested_model="Other",
                             read_origin=_repair_read_start(clock[0]))[1]["remaining"] is None
    assert accounts[1]["remaining"] == 80


def test_repair_candidate_stop_uncertainty_and_invalid_ledger_fail_closed(repair_ledger_case):
    from lib import turn_queue
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("unsettled-candidate"), source=_repair_source())
    assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    assert ledger.finish(claim, "repaired", cleanup_confirmed=False)
    report = _usable_repair_report()
    report["generated_at"] = clock[0] / 1000
    accounts = [{"alias": "working", "active": True, "remaining": 10, "secondary": 20},
                {"alias": "saved", "active": False, "remaining": 80, "secondary": 70}]
    assert ledger.candidates(accounts, report, owner=owner, requested_model="Fable",
                             read_origin=_repair_read_start(clock[0]))[1]["remaining"] is None
    assert ledger.finish(claim, "transient_failure", cleanup_confirmed=True)
    turn_queue.set_paused(owner["agent_id"], True, user_stop=True)
    turn_queue.set_paused(owner["agent_id"], False)
    assert ledger.candidates(accounts, report, owner=owner, requested_model="Fable",
                             read_origin=_repair_read_start(clock[0]))[1]["remaining"] is None
    settings_store.set_text(hotseat.REPAIR_STATE_KEY, "corrupt")
    with pytest.raises(hotseat.RepairPolicyError):
        ledger.candidates(accounts, report, owner=owner, requested_model="Fable", read_origin=_repair_read_start(clock[0]))


@pytest.mark.parametrize("change", [
    {"code": "success"}, {"schema": "other/1"}, {"cleanup_confirmed": 1},
    {"detail_code": "raw stderr"}, {"token": "not-allowed"},
    {"code": "repaired", "cleanup_confirmed": False},
    {"code": "already_ok", "detail_code": "child_failed"},
    {"code": "transient_failure", "detail_code": "publication_unverified"},
    {"code": "transient_failure", "detail_code": "cleanup_uncertain"},
    {"code": "needs_user", "detail_code": "identity_verified"},
    {"code": "transient_failure", "detail_code": "identity_changed"},
    {"code": "cooldown", "detail_code": "verification_required"}])
def test_repair_primitive_receipt_is_closed_and_cleanup_is_not_optional(change):
    receipt = {"schema": "clarp.claude-login-repair/1", "code": "repaired",
               "detail_code": "identity_verified", "cleanup_confirmed": True}
    receipt.update(change)
    with pytest.raises(hotseat.RepairPolicyError):
        hotseat.parse_repair_result(json.dumps(receipt).encode())


def test_repair_primitive_receipt_rejects_duplicates_trailing_output_and_oversize():
    receipt = b'{"schema":"clarp.claude-login-repair/1","code":"already_ok","detail_code":"identity_verified","cleanup_confirmed":true}'
    assert hotseat.parse_repair_result(receipt).code == "already_ok"
    for raw in (receipt.replace(b'"code":"already_ok"', b'"code":"repaired","code":"already_ok"'),
                receipt + b'\nprivate-output', b' ' * 4097, b'\xff'):
        with pytest.raises(hotseat.RepairPolicyError):
            hotseat.parse_repair_result(raw)


@pytest.mark.parametrize("fence", ["service_stop", "user_stop_resume", "disable", "revision"])
def test_repair_worker_is_serial_and_stop_fences_late_success(repair_ledger_case, monkeypatch, fence):
    import threading
    clock, owner, profile, run = repair_ledger_case
    entered, release = threading.Event(), threading.Event()
    stop = threading.Event()
    calls = []
    def primitive(target, *, cancelled, started, **_extra):
        calls.append(target)
        assert started()
        entered.set()
        assert release.wait(5), "stand-in repair was not released"
        assert cancelled()
        return hotseat.RepairResult("repaired", True)
    monkeypatch.setattr(hotseat, "invoke_repair", primitive)
    worker = hotseat.RepairWorker(stop)
    payload = _repair_report()
    payload["generated_at"] = clock[0] / 1000
    rid = run("worker-stop")
    assert worker.offer(owner, rid, payload, read_origin=_repair_read_start(clock[0]))
    try:
        assert entered.wait(5), "worker never reached its owned primitive"
        assert not worker.offer(owner, rid, payload, read_origin=_repair_read_start(clock[0]))
        if fence == "service_stop": stop.set()
        elif fence == "user_stop_resume":
            from lib import turn_queue
            turn_queue.set_paused(owner["agent_id"], True, user_stop=True)
            turn_queue.set_paused(owner["agent_id"], False)
        elif fence == "disable": janitors.set_enabled(owner["session"], owner["revision"], False)
        else: janitors.configure(owner["session"], owner["revision"], options={"claude_min_remaining": 30})
    finally:
        release.set()
        worker.thread.join(5)
        assert not worker.thread.is_alive(), "stand-in repair worker leaked"
    state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
    assert len(calls) == 1 and state["active"] is None
    row, = state["profiles"].values()
    assert row["status"] == "failed" and row["last_code"] == "interrupted"
    assert row["unverified"] is True
    # New service + OFF after actual worker success/cancellation: only durable
    # publication debt can stop the generic-headroom path selecting this alias.
    current = janitors.get(owner["session"])
    current = janitors.configure(current["session"], current["revision"], options={"auto_repair_auth": False})
    janitors.set_enabled(current["session"], current["revision"], True)
    settings_store.set_int("hotseat-switcher.claude.last-check", 0)
    generic = _usable_repair_report()
    generic["accounts"][0]["usage"].pop("scoped")
    generic["accounts"] += claude_payload(working=(True, 1.0), healthy=(False, 0.3))["accounts"]
    before, switched = settings_store.get(hotseat.REPAIR_STATE_KEY), []
    restarted = service.AutonomyJanitors(lambda *_: True, lambda _: {"sent": 1},
        hotseat_read=lambda command, provider: hotseat.parse_claude(generic) if provider == "claude" else [],
        hotseat_switch=lambda command, provider, alias: switched.append(alias) or True)
    restarted.hotseat_once()
    assert switched == ["healthy"] and len(calls) == 1
    assert settings_store.get(hotseat.REPAIR_STATE_KEY) == before


def test_repair_worker_off_stale_or_non_auth_offer_has_no_ledger_or_thread(repair_ledger_case, monkeypatch):
    import threading
    clock, owner, profile, run = repair_ledger_case
    def forbidden(*args, **kwargs):
        raise AssertionError("ineligible offer read repair state or started effects")
    monkeypatch.setattr(hotseat.RepairLedger, "reserve", forbidden)
    worker = hotseat.RepairWorker(threading.Event())
    assert not worker.offer({**owner, "options": {"auto_repair_auth": False}}, run("off"), _repair_report(),
                            read_origin=_repair_read_start(clock[0]))
    for index, report in enumerate((_repair_report(error_code="timeout"), _repair_report(), {"accounts": []})):
        assert not worker.offer(owner, run("ineligible-" + str(index)), report, read_origin=_repair_read_start(clock[0]))
    valid = _repair_report()
    valid["generated_at"] = clock[0] / 1000
    for index, bad_origin in enumerate((None, hotseat._RepairRead(clock[0], None),
            hotseat._RepairRead(clock[0], 10 ** 1000), hotseat._RepairRead("unknown", 0))):
        assert not worker.offer(owner, run("unknown-origin-" + str(index)), valid, read_origin=bad_origin)
    assert worker.thread is None


def test_repair_off_without_debt_keeps_existing_read_switch_with_one_lookup(switcher, monkeypatch):
    owner, worker, readings, calls, sent = switcher
    def forbidden(*args, **kwargs):
        raise AssertionError("repair OFF started private read, verification or worker")
    monkeypatch.setattr(hotseat, "read_repair_accounts", forbidden)
    monkeypatch.setattr(hotseat, "repair_requested_model", forbidden)
    monkeypatch.setattr(hotseat.RepairWorker, "offer", forbidden)
    original_get, lookups = settings_store.get, []
    def get(key):
        if key == hotseat.REPAIR_STATE_KEY: lookups.append(key)
        return original_get(key)
    monkeypatch.setattr(settings_store, "get", get)
    worker.hotseat_once()
    assert lookups == [hotseat.REPAIR_STATE_KEY]
    assert original_get(hotseat.REPAIR_STATE_KEY) is None
    assert [call for call in calls if call[0] == "switch"] == [("switch", "hotseat", "claude", "b")]
    assert len(sent) == 1 and sent[0]["reason"] == "account_switch"


def test_enabled_repair_is_after_healthy_selection_and_completed_run(repair_ledger_case, monkeypatch):
    import threading
    from lib import janitor_store
    clock, owner, profile, run = repair_ledger_case
    payload = _repair_report()
    payload["generated_at"] = clock[0] / 1000
    payload["accounts"] += claude_payload(working=(True, 1.0), healthy=(False, 0.1))["accounts"]
    entered, release = threading.Event(), threading.Event()
    switched = []
    def read_private(command):
        return hotseat.parse_claude(payload), payload, _repair_read_start(clock[0])
    def primitive(target, *, cancelled, started, **_extra):
        assert switched == ["healthy"]
        state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
        assert janitor_store.run_row(state["active"]["run_id"])["status"] == "completed"
        assert started()
        entered.set()
        assert release.wait(5), "stand-in repair was not released"
        return hotseat.RepairResult("repaired", True)
    monkeypatch.setattr(hotseat, "read_repair_accounts", read_private)
    monkeypatch.setattr(hotseat, "repair_requested_model", lambda: "Fable")
    monkeypatch.setattr(hotseat, "invoke_repair", primitive)
    monkeypatch.setattr(service.backends, "active_handles", lambda *args: [])
    worker = service.AutonomyJanitors(lambda *_: True, lambda _: {"sent": 1},
        hotseat_read=lambda *_: [], hotseat_switch=lambda command, provider, alias: switched.append(alias) or True)
    try:
        worker.hotseat_once()
        assert entered.wait(5), "enabled runtime did not offer its repair"
        # The main lane has returned while repair is still blocked; no active
        # ordinary run or selection mutex is retained by the background work.
        assert all(item["status"] == "completed" for item in janitors.list_runs(owner["session"]))
        assert not worker.stop_event.is_set()
    finally:
        release.set()
        if worker.auth_repair.thread is not None:
            worker.auth_repair.thread.join(5)
            assert not worker.auth_repair.thread.is_alive(), "repair stand-in leaked"
    state = json.loads(settings_store.get_text(hotseat.REPAIR_STATE_KEY))
    row, = state["profiles"].values()
    assert row["status"] == "repaired_unverified" and row["unverified"] is True
    assert profile.email not in json.dumps(state) and profile.org_uuid not in json.dumps(state)


def test_repair_status_sink_failure_cannot_block_healthy_selection(repair_ledger_case, monkeypatch):
    clock, owner, profile, run = repair_ledger_case
    payload = claude_payload(working=(True, 1.0), healthy=(False, 0.1))
    payload["generated_at"] = clock[0] / 1000
    def unavailable():
        raise OSError("private sentinel must not appear")
    monkeypatch.setattr(hotseat, "read_repair_accounts", lambda _: (hotseat.parse_claude(payload), payload, _repair_read_start(clock[0])))
    monkeypatch.setattr(hotseat, "repair_requested_model", lambda: "Fable")
    monkeypatch.setattr(hotseat, "repair_preview", unavailable)
    monkeypatch.setattr(service.backends, "active_handles", lambda *args: [])
    switched, sent = [], []
    worker = service.AutonomyJanitors(lambda *_: True, lambda notice: sent.append(notice) or {"sent": 1},
        hotseat_read=lambda *_: [], hotseat_switch=lambda _, provider, alias: switched.append(alias) or True)
    worker.hotseat_once()
    assert switched == ["healthy"]
    assert len(sent) == 1 and "private sentinel" not in json.dumps(sent)
    assert "Auth repair: status unavailable" in sent[0]["preview"]
    assert worker.auth_repair.thread is None


@pytest.fixture
def repair_standin(tmp_path, monkeypatch, fake_backend_cli):
    """The real adapter runs only a registered disposable leaf, never auth."""
    import sys
    directory = tmp_path / "repair-standin-bin"
    directory.mkdir()
    executable = directory / "claude-login"
    def install(body):
        executable.write_text("#!" + sys.executable + "\n" + body)
        executable.chmod(0o700)
        fake_backend_cli(executable)
        monkeypatch.setenv("PATH", str(directory))
    return install


@pytest.mark.parametrize("outcome", ["repaired", "already_ok", "needs_user", "identity_mismatch",
                                     "transient_failure", "cooldown", "malformed", "overflow", "nonzero"])
def test_repair_adapter_real_standin_closed_output_and_exit(repair_standin, outcome):
    details = {"repaired": "identity_verified", "already_ok": "identity_verified",
               "needs_user": "verification_required", "identity_mismatch": "identity_changed",
               "transient_failure": "exchange_failed", "cooldown": "profile_busy"}
    receipt = {"schema": "clarp.claude-login-repair/1", "code": outcome if outcome in details else "repaired",
               "detail_code": details.get(outcome, "identity_verified"),
               "cleanup_confirmed": True}
    body = "import sys\n"
    # Bind the actual maintained argv boundary without launching that helper.
    body += "assert sys.argv[1:] == ['repair','saved','--expected-email','fixture@example.invalid','--expected-org','" + _REPAIR_ORG + "','--json']\n"
    if outcome == "overflow": body += "print('x' * 4097)\n"
    elif outcome == "malformed": body += "print('private-output-is-not-a-receipt')\n"
    else: body += "print(" + repr(json.dumps(receipt)) + ", flush=True)\n"
    if outcome == "nonzero": body += "raise SystemExit(1)\n"
    repair_standin(body)
    profile, = hotseat.repair_profiles(_repair_report())
    result = hotseat.invoke_repair(profile, cancelled=lambda: False, started=lambda: True, source=_repair_source())
    if outcome in details:
        assert result == hotseat.RepairResult(outcome, True)
    else:
        assert result == hotseat.RepairResult("transient_failure", False)


@pytest.mark.parametrize("ignore_term", [False, True])
def test_repair_adapter_cancellation_reaps_owned_leaf_without_claiming_primitive_cleanup(repair_standin, tmp_path, ignore_term):
    ready = tmp_path / "standin-started"
    repair_standin("import pathlib,signal\n" +
        ("signal.signal(signal.SIGTERM,signal.SIG_IGN)\n" if ignore_term else "") +
        "pathlib.Path(" + repr(str(ready)) + ").write_text('ready')\nsignal.pause()\n")
    profile, = hotseat.repair_profiles(_repair_report())
    result = hotseat.invoke_repair(profile, cancelled=ready.exists, started=lambda: True, source=_repair_source())
    assert ready.exists()
    # A forced stop is not the maintained primitive's positive cleanup receipt.
    assert result == hotseat.RepairResult("interrupted", False)


def test_repair_adapter_stall_has_one_operational_deadline(repair_standin, monkeypatch):
    import time
    # Preserve the real selector/TERM/reap boundary, with a shorter test-only
    # envelope. No sleeps-as-synchronisation or actual maintained CLI is used.
    monkeypatch.setattr(hotseat, "REPAIR_ATTEMPT_SECONDS", 12)
    repair_standin("import signal\nsignal.pause()\n")
    profile, = hotseat.repair_profiles(_repair_report())
    started = time.monotonic()
    result = hotseat.invoke_repair(profile, cancelled=lambda: False, started=lambda: True, source=_repair_source())
    assert result == hotseat.RepairResult("transient_failure", False)
    assert time.monotonic() - started < 15, "owned leaf exceeded the finite timeout/cleanup envelope"


@pytest.mark.parametrize("code,status", [
    ("repaired", "repaired_unverified"), ("already_ok", "repaired_unverified"),
    ("needs_user", "needs_user"), ("identity_mismatch", "needs_user"),
    ("transient_failure", "failed"), ("cooldown", "cooldown")])
def test_repair_outcome_status_is_durable_and_never_refunds(repair_ledger_case, code, status):
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("status-" + code), source=_repair_source())
    assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    assert ledger.finish(claim, code, cleanup_confirmed=True)
    state = hotseat.RepairLedger()._load(clock[0])
    row = state["profiles"][claim.key]
    assert (row["status"], row["last_code"], row["attempts"]) == (status, code, 1)
    assert state["active"] is None and state["count"] == 1
    assert row["unverified"] is (code in {"repaired", "already_ok"})
    assert row["next_retry_at"] == clock[0] + 6 * 3_600_000
    assert hotseat.RepairLedger().reserve(profile, owner, run("immediate-" + code), source=_repair_source()) is None


def test_repair_status_notifies_once_while_ordinary_decision_is_noop(repair_ledger_case, monkeypatch):
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("unverified-status"), source=_repair_source())
    assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    assert ledger.finish(claim, "repaired", cleanup_confirmed=True)
    payload = _usable_repair_report()
    payload["generated_at"] = clock[0] / 1000
    payload["accounts"] += claude_payload(working=(True, 0.1))["accounts"]
    reads, notices = [], []
    def read_private(command):
        reads.append(command)
        return hotseat.parse_claude(payload), payload, _repair_read_start(clock[0])
    def no_switch(*args):
        raise AssertionError("repair status alone switched a healthy default")
    monkeypatch.setattr(hotseat, "read_repair_accounts", read_private)
    monkeypatch.setattr(hotseat, "repair_requested_model", lambda: "UnreportedModel")
    worker = service.AutonomyJanitors(lambda *_: True, lambda notice: notices.append(notice) or {"sent": 1},
                                     hotseat_read=lambda *_: [], hotseat_switch=no_switch)
    for _ in range(3):
        _check_again("hotseat-switcher.claude")
        worker.hotseat_once()
    assert len(reads) == 3 and len(notices) == 1
    assert notices[0]["preview"] == "Auth repair: repaired_unverified: 1"
    assert profile.email not in json.dumps(notices) and profile.org_uuid not in json.dumps(notices)
    assert worker.auth_repair.thread is None


def test_repair_worker_reports_custom_root_before_ledger_or_primitive(repair_ledger_case, monkeypatch, tmp_path):
    import threading
    clock, owner, profile, run = repair_ledger_case
    rid = run("unsupported-root")
    payload = _repair_report()
    payload["generated_at"] = clock[0] / 1000
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-root"))
    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported root reached repair state or primitive")
    monkeypatch.setattr(hotseat.RepairLedger, "_load", forbidden)
    monkeypatch.setattr(hotseat, "invoke_repair", forbidden)
    codes = []
    monkeypatch.setattr(hotseat, "_repair_diagnostic", codes.append)
    worker = hotseat.RepairWorker(threading.Event())
    assert worker.offer(owner, rid, payload, read_origin=_repair_read_start(clock[0]))
    worker.thread.join(5)
    assert not worker.thread.is_alive(), "unsupported-root worker did not finish"
    assert codes == ["unsupported_root"]
    assert settings_store.get(hotseat.REPAIR_STATE_KEY) is None


@pytest.mark.parametrize("debt", ["repaired_unverified", "late_stop", "uncertain", "rebound_alias"])
def test_repair_off_restart_quarantines_only_prior_debt_without_mutating_it(repair_ledger_case, monkeypatch, debt):
    from lib import turn_queue
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("off-debt-" + debt), source=_repair_source())
    assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    if debt == "late_stop":
        turn_queue.set_paused(owner["agent_id"], True, user_stop=True)
        turn_queue.set_paused(owner["agent_id"], False)
    assert ledger.finish(claim, "repaired", cleanup_confirmed=debt != "uncertain")
    owner = janitors.configure(owner["session"], owner["revision"], options={"auto_repair_auth": False})
    owner = janitors.set_enabled(owner["session"], owner["revision"], True)
    payload = _usable_repair_report()
    if debt == "rebound_alias": payload["accounts"][0]["org_uuid"] = "00000000-0000-4000-8000-000000000002"
    # No authoritative model scope is read OFF. Generic headroom must neither
    # release prior debt nor make an old repaired alias the switch target.
    payload["accounts"][0]["usage"].pop("scoped")
    payload["accounts"] += claude_payload(working=(True, 1.0), healthy=(False, 0.3))["accounts"]
    before = tuple(db.conn().execute("SELECT value,updated_at FROM settings WHERE key=?", (hotseat.REPAIR_STATE_KEY,)).fetchone())
    def forbidden(*args, **kwargs):
        raise AssertionError("OFF ran repair admission/auth/verification/preview work")
    monkeypatch.setattr(hotseat, "read_repair_accounts", forbidden)
    monkeypatch.setattr(hotseat, "repair_requested_model", forbidden)
    monkeypatch.setattr(hotseat.RepairLedger, "reserve", forbidden)
    monkeypatch.setattr(hotseat, "invoke_repair", forbidden)
    monkeypatch.setattr(hotseat, "repair_preview", forbidden)
    reads, switched = [], []
    for _ in range(2):
        # A new service instance represents lost process-local bookkeeping.
        # Its ONLY debt authority is the persisted canonical alias binding.
        def read(command, provider):
            reads.append(provider)
            return hotseat.parse_claude(payload) if provider == "claude" else []
        worker = service.AutonomyJanitors(lambda *_: True, lambda _: {"sent": 1}, hotseat_read=read,
            hotseat_switch=lambda command, provider, alias: switched.append((provider, alias)) or True)
        _check_again("hotseat-switcher.claude")
        worker.hotseat_once()
        assert worker.auth_repair.thread is None
        after = tuple(db.conn().execute("SELECT value,updated_at FROM settings WHERE key=?", (hotseat.REPAIR_STATE_KEY,)).fetchone())
        assert after == before
    assert switched == [("claude", "healthy"), ("claude", "healthy")]
    assert reads.count("claude") == 2 and reads.count("codex") == 1
    assert profile.email not in before[0] and profile.org_uuid not in before[0]


@pytest.mark.parametrize("corruption", ["json", "future_clock", "version", "unreadable", "missing_salt", "regenerated_salt"])
def test_repair_off_bad_debt_fails_claude_automatic_choice_without_affecting_codex(switcher, monkeypatch, tmp_path, corruption):
    owner, worker, readings, calls, sent = switcher
    if corruption == "json": raw = "private-sentinel not-json"
    elif corruption == "unreadable": raw = "unreadable sentinel"
    elif corruption in {"missing_salt", "regenerated_salt"}:
        home = tmp_path / "repair-home"
        (home / ".claude" / "profiles").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        # Create a real valid debt using the maintained ledger/HMAC. Only its
        # canonical salt is made unreadable; OFF must never regenerate it.
        owner = janitors.configure(owner["session"], owner["revision"], options={"auto_repair_auth": True})
        owner = janitors.set_enabled(owner["session"], owner["revision"], True)
        from lib import turn_queue
        context = {**owner, "_repair_stop_generation": turn_queue.user_stop_generation(owner["agent_id"])}
        run = janitor_builtins.begin_run(hotseat.ROLE, "salt-fault")
        assert run and janitor_builtins.claim_run(run["run_id"])
        janitor_builtins.complete_run(run["run_id"])
        profile, = hotseat.repair_profiles(_repair_report())
        ledger = hotseat.RepairLedger()
        claim = ledger.reserve(profile, context, run["run_id"], source=_repair_source())
        assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
        assert ledger.finish(claim, "repaired", cleanup_confirmed=True)
        owner = janitors.configure(owner["session"], owner["revision"], options={"auto_repair_auth": False})
        owner = janitors.set_enabled(owner["session"], owner["revision"], True)
        raw = settings_store.get(hotseat.REPAIR_STATE_KEY)
        if corruption == "regenerated_salt":
            # The existing observational path can regenerate a missing salt.
            # Its new valid-format value must not orphan old repair debt.
            settings_store.delete(hotseat.hotseat_status.SALT_KEY)
            hotseat.hotseat_status.record_read("claude", readings["claude"])
    else:
        raw = json.dumps({"v": 1 if corruption == "future_clock" else 2, "identity_binding": None, "clock": db.now_ms() + 60_000,
                          "day": db.now_ms() // 86_400_000, "count": 0, "active": None, "runs": [], "profiles": {}})
    settings_store.set_text(hotseat.REPAIR_STATE_KEY, raw)
    readings["codex"] = {"accounts": [
        {"alias": "p", "is_active": True, "usage": {"usable": True, "blocked": False, "worst_used": 1}},
        {"alias": "q", "is_active": False, "usage": {"usable": True, "blocked": False, "worst_used": 0.1}}]}
    original_get = settings_store.get
    def get(key):
        if corruption == "unreadable" and key == hotseat.REPAIR_STATE_KEY: raise OSError("private-sentinel")
        if corruption == "missing_salt" and key == hotseat.hotseat_status.SALT_KEY: return None
        return original_get(key)
    monkeypatch.setattr(settings_store, "get", get)
    diagnostics = []
    monkeypatch.setattr(hotseat, "_repair_diagnostic", diagnostics.append)
    worker.hotseat_once()
    assert [call for call in calls if call[0] == "switch"] == [("switch", "hotseat", "codex", "q")]
    assert diagnostics == ["debt_quarantine_unavailable"]
    assert original_get(hotseat.REPAIR_STATE_KEY) == raw
    assert "private-sentinel" not in json.dumps(sent)



def test_repair_reenable_requires_new_exact_identity_and_model_proof(repair_ledger_case, monkeypatch):
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("reenable-proof"), source=_repair_source())
    assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    assert ledger.finish(claim, "repaired", cleanup_confirmed=True)
    owner = janitors.configure(owner["session"], owner["revision"], options={"auto_repair_auth": False})
    owner = janitors.set_enabled(owner["session"], owner["revision"], True)
    payload = _usable_repair_report()
    payload["generated_at"] = clock[0] / 1000
    payload["accounts"] += claude_payload(working=(True, 1.0))["accounts"]
    notices, switches, reads = [], [], []
    def read(command, provider):
        reads.append(provider)
        return hotseat.parse_claude(payload) if provider == "claude" else []
    def read_private(command):
        reads.append("private-claude")
        return hotseat.parse_claude(payload), payload, _repair_read_start(clock[0])
    def switch(command, provider, alias):
        state = json.loads(settings_store.get(hotseat.REPAIR_STATE_KEY))
        assert state["profiles"][claim.key]["last_code"] == "verified"
        switches.append((provider, alias))
        return True
    monkeypatch.setattr(hotseat, "read_repair_accounts", read_private)
    monkeypatch.setattr(hotseat, "repair_requested_model", lambda: "Fable")
    worker = service.AutonomyJanitors(lambda *_: True, lambda notice: notices.append(notice) or {"sent": 1},
                                     hotseat_read=read, hotseat_switch=switch)
    before = settings_store.get(hotseat.REPAIR_STATE_KEY)
    worker.hotseat_once()
    assert switches == [] and settings_store.get(hotseat.REPAIR_STATE_KEY) == before
    owner = janitors.configure(owner["session"], owner["revision"], options={"auto_repair_auth": True})
    owner = janitors.set_enabled(owner["session"], owner["revision"], True)
    # Force the ordinary existing cadence due for the new owner generation;
    # no fixture replaces the actual enablement/admission/verification logic.
    settings_store.set_int("hotseat-switcher.claude.last-check", 0)
    valid = json.loads(json.dumps(payload))
    for rejection in ("stale", "identity", "model"):
        payload = json.loads(json.dumps(valid))
        if rejection == "stale": payload["generated_at"] -= 600
        elif rejection == "identity": payload["accounts"][0]["org_uuid"] = "00000000-0000-4000-8000-000000000002"
        else: payload["accounts"][0]["usage"]["scoped"] = []
        _check_again("hotseat-switcher.claude")
        worker.hotseat_once()
        state = json.loads(settings_store.get(hotseat.REPAIR_STATE_KEY))
        assert state["profiles"][claim.key]["unverified"] is True and switches == []
    payload = valid
    _check_again("hotseat-switcher.claude")
    worker.hotseat_once()
    state = json.loads(settings_store.get(hotseat.REPAIR_STATE_KEY))
    assert switches == [("claude", "saved")]
    assert state["profiles"][claim.key]["status"] == "repaired" and state["profiles"][claim.key]["unverified"] is False
    assert state["count"] == 1 and state["profiles"][claim.key]["attempts"] == 1
    assert reads.count("claude") == 1 and reads.count("private-claude") == 4
    assert worker.auth_repair.thread is None



def test_repair_verification_cannot_borrow_a_custom_root_or_changed_salt(repair_ledger_case, monkeypatch, tmp_path):
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("binding-proof"), source=_repair_source())
    assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    assert ledger.finish(claim, "repaired", cleanup_confirmed=True)
    report = _usable_repair_report()
    report["generated_at"] = clock[0] / 1000
    accounts = hotseat.parse_claude(report) + [{"alias": "working", "active": True, "remaining": 50, "secondary": 50}]
    before = settings_store.get(hotseat.REPAIR_STATE_KEY)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "foreign-root"))
    result = ledger.candidates(accounts, report, owner=owner, requested_model="Fable", read_origin=_repair_read_start(clock[0]))
    assert result[0]["remaining"] is None and result[1:] == accounts[1:]
    assert settings_store.get(hotseat.REPAIR_STATE_KEY) == before
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    salt = settings_store.get(hotseat.hotseat_status.SALT_KEY)
    settings_store.set_text(hotseat.hotseat_status.SALT_KEY, "1" * 32 if salt != "1" * 32 else "2" * 32)
    with pytest.raises(hotseat.RepairPolicyError):
        ledger.candidates(accounts, report, owner=owner, requested_model="Fable", read_origin=_repair_read_start(clock[0]))
    assert settings_store.get(hotseat.REPAIR_STATE_KEY) == before



def test_scheduled_selection_withholds_an_offer_before_its_durable_claim(repair_ledger_case, monkeypatch):
    import threading
    clock, owner, profile, run = repair_ledger_case
    entered, release = threading.Event(), threading.Event()
    original_reserve = hotseat.RepairLedger.reserve
    def held_reserve(ledger, *args, **kwargs):
        entered.set()
        assert release.wait(5), "pre-reservation stand-in was not released"
        return original_reserve(ledger, *args, **kwargs)
    monkeypatch.setattr(hotseat.RepairLedger, "reserve", held_reserve)
    def forbidden(*args, **kwargs):
        raise AssertionError("stopped pre-reservation offer started auth")
    monkeypatch.setattr(hotseat, "invoke_repair", forbidden)
    payload = _repair_report()
    payload["generated_at"] = clock[0] / 1000
    payload["accounts"] += claude_payload(working=(True, 0.1))["accounts"]
    monkeypatch.setattr(hotseat, "read_repair_accounts", lambda _: (hotseat.parse_claude(payload), payload, _repair_read_start(clock[0])))
    monkeypatch.setattr(hotseat, "repair_requested_model", lambda: "Fable")
    switched = []
    worker = service.AutonomyJanitors(lambda *_: True, lambda _: {"sent": 1}, hotseat_read=lambda *_: [],
        hotseat_switch=lambda command, provider, alias: switched.append(alias) or True)
    try:
        worker.hotseat_once()
        assert entered.wait(5), "offer never reached the pre-reservation barrier"
        assert settings_store.get(hotseat.REPAIR_STATE_KEY) is None
        payload = _usable_repair_report()
        payload["generated_at"] = clock[0] / 1000
        payload["accounts"] += claude_payload(working=(True, 1.0), healthy=(False, 0.3))["accounts"]
        _check_again("hotseat-switcher.claude")
        worker.hotseat_once()
        assert switched == ["healthy"]
    finally:
        worker.stop()
        release.set()
        if worker.auth_repair.thread is not None:
            worker.auth_repair.thread.join(5)
            assert not worker.auth_repair.thread.is_alive(), "pre-reservation stand-in leaked"
    assert worker.auth_repair.profiles == ()


@pytest.mark.parametrize("boundary,advance,expected_effects", [
    ("owner", "wall_expiry", 0), ("root", "wall_expiry", 0), ("phase", "wall_expiry", 0),
    ("pre_spawn", "wall_expiry", 0), ("pre_spawn", "mono_expiry", 0),
    ("owner", "rollback", 0), ("owner", "within_budget", 1)])
def test_genuine_repair_offer_expires_across_worker_admission_and_spawn_waits(
        repair_ledger_case, repair_standin, tmp_path, monkeypatch, boundary, advance, expected_effects):
    import threading
    import time
    clock, owner, profile, run = repair_ledger_case
    entered, release = threading.Event(), threading.Event()
    inside_adapter = [False]
    held = [False]
    real_mono, mono_offset = time.monotonic, [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: real_mono() + mono_offset[0])
    marker = tmp_path / "synthetic-effect"
    receipt = json.dumps({"schema": "clarp.claude-login-repair/1", "code": "already_ok",
                          "detail_code": "identity_verified", "cleanup_confirmed": True})
    repair_standin("import pathlib\npathlib.Path(" + repr(str(marker)) + ").write_text('started')\nprint(" + repr(receipt) + ",flush=True)\n")
    def barrier():
        if not held[0]:
            held[0] = True
            entered.set()
            assert release.wait(5), "source freshness barrier was not released"
    original_owner = hotseat._repair_owner_snapshot
    def owner_snapshot(*args):
        if boundary == "owner": barrier()
        return original_owner(*args)
    monkeypatch.setattr(hotseat, "_repair_owner_snapshot", owner_snapshot)
    original_root = hotseat._repair_process_identity
    def root():
        if boundary == "root": barrier()
        return original_root()
    monkeypatch.setattr(hotseat, "_repair_process_identity", root)
    original_phase = hotseat.RepairLedger.phase
    def phase(ledger, claim, stage):
        if boundary == "phase" and stage == "spawn_pending": barrier()
        return original_phase(ledger, claim, stage)
    monkeypatch.setattr(hotseat.RepairLedger, "phase", phase)
    original_matches = hotseat._repair_owner_matches
    def matches(*args):
        if boundary == "pre_spawn" and inside_adapter[0]: barrier()
        return original_matches(*args)
    monkeypatch.setattr(hotseat, "_repair_owner_matches", matches)
    original_invoke = hotseat.invoke_repair
    def invoke(*args, **kwargs):
        inside_adapter[0] = True
        try:return original_invoke(*args, **kwargs)
        finally:inside_adapter[0] = False
    monkeypatch.setattr(hotseat, "invoke_repair", invoke)
    payload = _repair_report()
    payload["generated_at"] = clock[0] / 1000
    rid = run("freshness-" + boundary + "-" + advance)
    worker = hotseat.RepairWorker(threading.Event())
    assert worker.offer(owner, rid, payload, read_origin=_repair_read_start(clock[0]))
    try:
        assert entered.wait(5), "genuine offer never reached its freshness boundary"
        if advance == "wall_expiry": clock[0] += 61_000
        elif advance == "mono_expiry": mono_offset[0] += 61
        elif advance == "rollback": clock[0] -= 1000
        else: clock[0] += 10_000
    finally:
        release.set()
        worker.thread.join(8)
        assert not worker.thread.is_alive(), "freshness stand-in worker leaked"
    assert int(marker.exists()) == expected_effects
    if boundary in {"owner", "root"} and not expected_effects:
        assert settings_store.get(hotseat.REPAIR_STATE_KEY) is None


@pytest.mark.parametrize("external_reap", [False, True])
def test_repair_adapter_never_uses_group_number_after_leader_identity_is_released(
        repair_ledger_case, repair_standin, monkeypatch, external_reap):
    import os
    import signal
    import threading
    clock, owner, profile, run = repair_ledger_case
    receipt = json.dumps({"schema": "clarp.claude-login-repair/1", "code": "already_ok",
                          "detail_code": "identity_verified", "cleanup_confirmed": True})
    repair_standin("print(" + repr(receipt if external_reap else "bad-json") + ",flush=True)\n")
    original_popen, original_group, original_waitid = hotseat.subprocess.Popen, os.killpg, os.waitid
    owned, after_release, before_release, waits_after_release = {}, [], [], []
    def popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        owned.update(proc=proc, released=False)
        real_poll, real_wait = proc.poll, proc.wait
        def poll(*args, **kwargs):
            result = real_poll(*args, **kwargs)
            if result is not None: owned["released"] = True
            return result
        def wait(*args, **kwargs):
            if owned["released"]: waits_after_release.append(True)
            result = real_wait(*args, **kwargs)
            owned["released"] = True
            return result
        proc.poll, proc.wait = poll, wait
        return proc
    monkeypatch.setattr(hotseat.subprocess, "Popen", popen)
    def group(pid, signum):
        assert pid == owned["proc"].pid, "operation escaped the owned fixture group"
        if owned["released"]:
            # Model a reusable number without EVER signalling a foreign group.
            after_release.append(signum)
            raise ProcessLookupError
        # This exact child relationship, even when zombie, reserves the number.
        original_waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        before_release.append(signum)
        return original_group(pid, signum)
    monkeypatch.setattr(os, "killpg", group)
    original_phase = hotseat.RepairLedger.phase
    def phase(ledger, claim, stage):
        if external_reap and stage == "running":
            # A distinct actor has actually reaped this disposable owned leaf.
            # ECHILD must become UNKNOWN, never numeric-group retry authority.
            owned["proc"].wait(timeout=3)
        return original_phase(ledger, claim, stage)
    monkeypatch.setattr(hotseat.RepairLedger, "phase", phase)
    payload = _repair_report()
    payload["generated_at"] = clock[0] / 1000
    worker = hotseat.RepairWorker(threading.Event())
    assert worker.offer(owner, run("group-identity-" + str(external_reap)), payload, read_origin=_repair_read_start(clock[0]))
    worker.thread.join(12)
    assert not worker.thread.is_alive(), "ownership stand-in worker leaked"
    assert owned["released"] is True
    assert after_release == [], "numeric group used after its leader identity was released"
    assert waits_after_release == [], "numeric wait used after its leader identity was released"
    if external_reap: assert before_release == []
    else: assert signal.SIGTERM in before_release
    row, = json.loads(settings_store.get(hotseat.REPAIR_STATE_KEY))["profiles"].values()
    assert row["status"] == "failed" and row["unverified"] is True


@pytest.mark.parametrize("elapsed,wall_delta,expected", [(61, 0, False), (61, 40_000, False), (10, 10_000, True)])
def test_repair_read_keeps_its_monotonic_origin_before_offer(
        repair_ledger_case, monkeypatch, elapsed, wall_delta, expected):
    import threading
    import time
    from types import SimpleNamespace
    clock, owner, profile, run = repair_ledger_case
    rid = run("read-origin-" + str(elapsed) + "-" + str(wall_delta))
    payload = _repair_report()
    payload["generated_at"] = clock[0] / 1000
    real_mono, offset = time.monotonic, [0]
    monkeypatch.setattr(time, "monotonic", lambda: real_mono() + offset[0])
    def read(command, args, timeout):
        assert command == "synthetic-hotseat" and args == ["list", "--json"] and timeout == 60
        offset[0] += elapsed
        clock[0] += wall_delta
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
    monkeypatch.setattr(hotseat, "_run", read)
    effects = []
    def primitive(profile, *, started, **kwargs):
        assert started()
        effects.append(True)
        return hotseat.RepairResult("already_ok", True)
    monkeypatch.setattr(hotseat, "invoke_repair", primitive)
    _, report, origin = hotseat.read_repair_accounts("synthetic-hotseat")
    worker = hotseat.RepairWorker(threading.Event())
    accepted = worker.offer(owner, rid, report, read_origin=origin)
    if worker.thread is not None:
        worker.thread.join(5)
        assert not worker.thread.is_alive(), "read-origin stand-in worker leaked"
    assert accepted is expected
    assert len(effects) == int(expected)
    if not expected: assert settings_store.get(hotseat.REPAIR_STATE_KEY) is None



def test_repaired_candidate_verification_does_not_borrow_a_stalled_read(repair_ledger_case, monkeypatch):
    import time
    from types import SimpleNamespace
    clock, owner, profile, run = repair_ledger_case
    ledger = hotseat.RepairLedger()
    claim = ledger.reserve(profile, owner, run("verification-origin"), source=_repair_source())
    assert claim and ledger.phase(claim, "spawn_pending") and ledger.phase(claim, "running")
    assert ledger.finish(claim, "repaired", cleanup_confirmed=True)
    payload = _usable_repair_report()
    payload["generated_at"] = clock[0] / 1000
    payload["accounts"] += claude_payload(working=(True, 1.0), healthy=(False, 0.3))["accounts"]
    real_mono, offset = time.monotonic, [0]
    monkeypatch.setattr(time, "monotonic", lambda: real_mono() + offset[0])
    def read(command, args, timeout):
        offset[0] += 61  # Real read elapsed; wall time has not progressed.
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
    monkeypatch.setattr(hotseat, "_run", read)
    monkeypatch.setattr(hotseat, "repair_requested_model", lambda: "Fable")
    switched = []
    worker = service.AutonomyJanitors(lambda *_: True, lambda _: {"sent": 1}, hotseat_read=lambda *_: [],
        hotseat_switch=lambda command, provider, alias: switched.append(alias) or True)
    worker.hotseat_once()
    assert switched == ["healthy"]
    row = json.loads(settings_store.get(hotseat.REPAIR_STATE_KEY))["profiles"][claim.key]
    assert row["status"] == "repaired_unverified" and row["unverified"] is True
    assert worker.auth_repair.thread is None
