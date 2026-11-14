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


class ResetReplies:
    def __init__(self, on_offer=None, statuses=None):
        self.pending=None;self.on_offer=on_offer;self.statuses=iter(statuses or ["checking"]);self.calls=0
    def peek(self,provider,owner):return self.pending if provider=="claude" else None
    def reconciliation_due(self,provider,owner):return None
    def ack(self,provider,receipt):self.pending=None
    def offer(self,provider,field,models,owner):
        self.calls+=1
        if self.on_offer:self.on_offer()
        return next(self.statuses,"cooldown")
    def completion(self):
        self.pending={"id":"reset-action-1","result":{"status":"redeemed","account":"ai2","verified":True}}


def test_exhausted_switcher_resets_then_rereads_before_switch(switcher, monkeypatch):
    owner,worker,readings,calls,sent=switcher
    readings["claude"]=claude_payload(a=(True,"rejected"),b=(False,"rejected"))
    monkeypatch.setattr(service,"_provider_model_demand",lambda *a: (("Fable",),("Fable",)))
    replies=ResetReplies();worker.reset_worker=replies
    worker.hotseat_once()
    assert replies.calls==1 and not [c for c in calls if c[0]=="switch"]
    readings["claude"]=claude_payload(a=(True,"rejected"),b=(False,0))
    readings["claude"]["accounts"][1]["usage"]["scoped"]=[{"model":"Fable","used":0}]
    replies.completion();worker.hotseat_once()
    assert ("switch","hotseat","claude","b") in calls
    assert len([n for n in sent if n['preview'].startswith('Banked reset ')])==1
    from lib.conversation import load_conversation
    public=load_conversation(session=owner['session'],include_automated=False)
    assert any('Banked reset redeemed' in turn.get('text','') for turn in public['turns'])


def _configure_reset_command(monkeypatch,tmp_path):
    import dataclasses,sys
    from lib import config
    command=tmp_path/'command.py';receipt=tmp_path/'receipt.json';marker=tmp_path/'attempts'
    command.write_text("import pathlib,sys\np=pathlib.Path(sys.argv[2]);p.open('a').write('attempt\\n')\nprint(pathlib.Path(sys.argv[1]).read_text())\n")
    cfg=config.load();monkeypatch.setattr(config,'load',lambda:dataclasses.replace(cfg,claude_reset_command=(sys.executable,str(command),str(receipt),str(marker))))
    return receipt,marker


@pytest.mark.parametrize('completed',[False,True])
def test_reset_does_not_bypass_inflight_repair_or_spend_because_it_hides_a_healthy_seat(switcher,monkeypatch,tmp_path,completed):
    owner,worker,readings,calls,sent=switcher
    receipt,marker=_configure_reset_command(monkeypatch,tmp_path)
    receipt.write_text(json.dumps({'status':'redeemed','verified':True,'account':'ai2'}))
    from lib import janitor_reset
    worker.reset_worker=janitor_reset.ResetWorker(worker.stop_event)
    worker.auth_repair.profiles=(hotseat.RepairProfile("b","b@example.com","org"),)
    readings["claude"]=claude_payload(a=(True,"rejected"),b=(False,0))
    if completed:
        settings_store.set_text('hotseat-reset.claude.state',json.dumps({'id':'prior-completion','owner':owner['agent_id'],'result':{'status':'redeemed','verified':True,'account':'ai2'},'handled':False}))
    worker.hotseat_once()
    if worker.reset_worker.thread:worker.reset_worker.thread.join(3)
    assert not [c for c in calls if c[0]=='switch']
    assert not marker.exists(), 'authentication withholding must never cause grant spending'


def test_transient_reset_status_does_not_repeat_an_exhaustion_push(switcher,monkeypatch,tmp_path):
    from lib import janitor_reset
    owner,worker,readings,calls,sent=switcher
    receipt,marker=_configure_reset_command(monkeypatch,tmp_path)
    worker.reset_worker=janitor_reset.ResetWorker(worker.stop_event)
    readings["claude"]=claude_payload(a=(True,"rejected"),b=(False,"rejected"))
    clock=[db.now_ms()+1000];monkeypatch.setattr(db,'now_ms',lambda:clock[0])
    for status in ["cooldown","cooldown","busy","cooldown","no_eligible_grant","unverified"]:
        receipt.write_text(json.dumps({'status':status,'verified':False}))
        _check_again("hotseat-switcher.claude");worker.hotseat_once()
        if worker.reset_worker.thread:
            worker.reset_worker.thread.join(3)
            assert not worker.reset_worker.thread.is_alive()
        clock[0]+=1;worker.hotseat_once()  # Deliver an async completion, if any.
        clock[0]+=10*60_000
    assert len([n for n in sent if n.get('push')])==1


def test_verified_reset_read_failure_stays_failed_and_reports_the_spent_action(switcher):
    owner,worker,readings,calls,sent=switcher
    readings["claude"]=claude_payload(a=(True,"rejected"),b=(False,"rejected"))
    worker.reset_worker=replies=ResetReplies();worker.hotseat_once();replies.completion()
    def unreadable(command,provider):
        if provider=="claude":raise RuntimeError("private reading failure")
        return []
    worker.hotseat_read=unreadable;worker.hotseat_once()
    notes=[n for n in sent if n['preview'].startswith('Banked reset ')]
    assert len(notes)==1 and 'could not be refreshed' in notes[0]['preview']
    assert not notes[0]['push'] and not [c for c in calls if c[0]=='switch']


def test_notify_mode_never_runs_a_reset_command(switcher, monkeypatch, tmp_path):
    import dataclasses,sys
    from lib import config
    owner,worker,readings,calls,sent=switcher
    marker=tmp_path/'spent'
    cfg=config.load();monkeypatch.setattr(config,'load',lambda:dataclasses.replace(cfg,claude_reset_command=(sys.executable,'-c',f"open({str(marker)!r},'w').write('spent')")))
    current=janitors.get('hotseat');changed=janitors.configure('hotseat',current['revision'],options={'mode':'notify'});janitors.set_enabled('hotseat',changed['revision'],True)
    readings['claude']=claude_payload(a=(True,'rejected'),b=(False,'rejected'))
    worker.hotseat_once()
    if worker.reset_worker.thread:worker.reset_worker.thread.join(2)
    assert not marker.exists()


def test_host_reset_budget_survives_worker_restart_and_hang_does_not_hold_codex_lane(switcher,monkeypatch):
    import dataclasses,threading,time
    from lib import config,janitor_reset
    owner,worker,readings,calls,sent=switcher
    cfg=config.load();monkeypatch.setattr(config,'load',lambda:dataclasses.replace(cfg,claude_reset_command=('fake',)))
    entered=threading.Event();release=threading.Event()
    def hanging(*args,**kwargs):
        entered.set();assert release.wait(5);return {'status':'no_eligible_grant','verified':False}
    monkeypatch.setattr(janitor_reset,'attempt',hanging)
    readings['claude']=claude_payload(a=(True,'rejected'),b=(False,'rejected'))
    try:
        worker.hotseat_once()
        assert entered.wait(2) and worker.reset_worker.thread.is_alive()
        assert any(c[:3]==('read','hotseat','codex') for c in calls)
        another=janitor_reset.ResetWorker(threading.Event())
        actual=janitor_builtins.resolve(hotseat.ROLE)
        assert another.offer('claude','claude_reset_command',('Fable',),actual)=='cooldown'
    finally:
        release.set()
        if worker.reset_worker.thread:worker.reset_worker.thread.join(3)


def test_the_phone_is_pushed_only_when_no_account_can_serve(switcher):
    """Peter, 13 Nov 2026: no push for a switch; a push only when every account is out and work stopped."""
    owner, worker, readings, calls, sent = switcher
    readings["claude"] = claude_payload(a=(True, 1.0), b=(False, 1.0))
    worker.hotseat_once()
    assert not [c for c in calls if c[0] == "switch"]
    assert len(sent) == 1 and sent[0]["reason"] == "account_switch" and sent[0]["push"] is True
    assert "no saved account has more" in sent[0]["preview"]


def test_switch_advice_is_told_without_a_push(switcher):
    owner, worker, readings, calls, sent = switcher
    c = janitors.configure(owner["session"], owner["revision"], options={"mode": "notify"})
    janitors.set_enabled(c["session"], c["revision"], True)
    worker.hotseat_once()
    assert not [c for c in calls if c[0] == "switch"]
    assert len(sent) == 1 and sent[0]["preview"].startswith("Claude: would switch to b") and sent[0]["push"] is False


def test_switches_once_per_interval_and_tells_the_chat_without_a_push(switcher):
    owner, worker, readings, calls, sent = switcher
    worker.hotseat_once()
    assert [c for c in calls if c[0] == "switch"] == [("switch", "hotseat", "claude", "b")]
    assert len(sent) == 1 and sent[0]["preview"].startswith("Claude: switched to b") and sent[0]["push"] is False
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


@pytest.mark.parametrize("repair_enabled", [False, True])
def test_codex_switch_recycles_idle_connections(switcher, monkeypatch, repair_enabled):
    owner, worker, readings, calls, sent = switcher
    private_reads = []
    if repair_enabled:
        current = janitors.configure(owner["session"], owner["revision"], options={"auto_repair_auth": True})
        janitors.set_enabled(current["session"], current["revision"], True)
        def read_private(command):
            private_reads.append(command)
            payload = readings["claude"]
            return hotseat.parse_claude(payload), payload, _repair_read_start()
        monkeypatch.setattr(hotseat, "read_repair_accounts", read_private)
        monkeypatch.setattr(hotseat, "repair_requested_model", lambda: "Fable")
    readings["codex"] = {"accounts": [
        {"alias": "p", "is_active": True, "usage": {"usable": True, "blocked": False, "error": None, "worst_used": 0.95}},
        {"alias": "q", "is_active": False, "usage": {"usable": True, "blocked": False, "error": None, "worst_used": 0.2}}]}
    recycled = []
    # The Codex backend owns the runner recycle; Claude's is a no-op.
    monkeypatch.setattr(service.backends.by_id("codex"), "on_credential_change", lambda: recycled.append(True))
    worker.hotseat_once()
    assert ("switch", "hotseat", "codex", "q") in calls and recycled == [True]
    assert any(p["preview"].startswith("Codex: switched to q") for p in sent)
    assert len(private_reads) == int(repair_enabled), "Codex must not acquire the repair policy's extra read"
    assert ("read", "hotseat", "codex") in calls
    assert worker.auth_repair.thread is None


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


def _scheduled_repair_tick(clock, owner, worker):
    """A new real scheduled demand, not a second claim of a frozen-time run."""
    previous = {row["run_id"] for row in janitors.list_runs(owner["session"])}
    clock[0] += 1
    _check_again("hotseat-switcher.claude")
    worker.hotseat_once()
    new_runs = [row for row in janitors.list_runs(owner["session"])
                if row["run_id"] not in previous
                and row["configuration"].get("context", {}).get("summary") == "claude"]
    assert len(new_runs) == 1, "the scheduled proof did not admit a fresh Claude demand"
    assert new_runs[0]["status"] == "completed", "the scheduled proof did not finish its real demand"
    return new_runs[0]["run_id"]


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
    run_ids = []
    for _ in range(3):
        previous_reads = len(reads)
        run_ids.append(_scheduled_repair_tick(clock, owner, worker))
        assert len(reads) == previous_reads + 1, "a status dedupe tick skipped its actual read"
    assert len(set(run_ids)) == 3
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
    reads, switched, run_ids = [], [], []
    for _ in range(2):
        # A new service instance represents lost process-local bookkeeping.
        # Its ONLY debt authority is the persisted canonical alias binding.
        def read(command, provider):
            reads.append(provider)
            return hotseat.parse_claude(payload) if provider == "claude" else []
        worker = service.AutonomyJanitors(lambda *_: True, lambda _: {"sent": 1}, hotseat_read=read,
            hotseat_switch=lambda command, provider, alias: switched.append((provider, alias)) or True)
        previous_reads = reads.count("claude")
        run_ids.append(_scheduled_repair_tick(clock, owner, worker))
        assert reads.count("claude") == previous_reads + 1, "a restarted OFF lane skipped its actual read"
        assert worker.auth_repair.thread is None
        after = tuple(db.conn().execute("SELECT value,updated_at FROM settings WHERE key=?", (hotseat.REPAIR_STATE_KEY,)).fetchone())
        assert after == before
    assert len(set(run_ids)) == 2
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
    run_ids = [_scheduled_repair_tick(clock, owner, worker)]
    assert reads.count("claude") == 1
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
        previous_reads = reads.count("private-claude")
        run_ids.append(_scheduled_repair_tick(clock, owner, worker))
        assert reads.count("private-claude") == previous_reads + 1, "a negative proof skipped its actual read"
        state = json.loads(settings_store.get(hotseat.REPAIR_STATE_KEY))
        assert state["profiles"][claim.key]["unverified"] is True and switches == []
    payload = valid
    previous_reads = reads.count("private-claude")
    run_ids.append(_scheduled_repair_tick(clock, owner, worker))
    assert reads.count("private-claude") == previous_reads + 1, "the valid proof skipped its actual read"
    assert len(set(run_ids)) == 5
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
    reads = []
    def read_private(command):
        reads.append(command)
        return hotseat.parse_claude(payload), payload, _repair_read_start(clock[0])
    monkeypatch.setattr(hotseat, "read_repair_accounts", read_private)
    monkeypatch.setattr(hotseat, "repair_requested_model", lambda: "Fable")
    switched = []
    worker = service.AutonomyJanitors(lambda *_: True, lambda _: {"sent": 1}, hotseat_read=lambda *_: [],
        hotseat_switch=lambda command, provider, alias: switched.append(alias) or True)
    try:
        first_run = _scheduled_repair_tick(clock, owner, worker)
        assert len(reads) == 1
        assert entered.wait(5), "offer never reached the pre-reservation barrier"
        assert settings_store.get(hotseat.REPAIR_STATE_KEY) is None
        payload = _usable_repair_report()
        payload["generated_at"] = clock[0] / 1000
        payload["accounts"] += claude_payload(working=(True, 1.0), healthy=(False, 0.3))["accounts"]
        second_run = _scheduled_repair_tick(clock, owner, worker)
        assert second_run != first_run and len(reads) == 2
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


def test_claude_parse_and_decide_reject_weekly_exhaustion_and_unavailable_accounts():
    payload = {"accounts": [
        {"alias": "ai5", "is_active": True, "error": None, "usage": {
            "status": "allowed", "available": False, "limited": False,
            "used_5h": 0.0, "used_7d": 1.0, "status_7d": "rejected"}},
        {"alias": "ai6", "is_active": False, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.0, "used_7d": 1.0}},
        {"alias": "ai7", "is_active": False, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.3, "used_7d": 0.4}},
        {"alias": "overage", "is_active": False, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.1, "used_7d": 1.0, "extra_usage_enabled": True}},
    ]}
    parsed = {row["alias"]: row for row in hotseat.parse_claude(payload)}
    assert parsed["ai5"]["remaining"] == 0
    assert parsed["ai6"]["remaining"] == 0
    assert parsed["ai7"]["remaining"] == 70
    assert parsed["overage"]["remaining"] == 90
    decision = hotseat.decide(list(parsed.values()), 25)
    assert decision["action"] == "switch" and decision["target"] == "overage"


def test_hotseat_switches_for_model_scoped_fable_quota_skipping_weekly_exhausted_and_degraded(switcher):
    owner, worker, readings, calls, sent = switcher
    aid = agents.create_agent(persona="Koko", voice_id="v", cwd="/tmp", session="koko-8255", backend="claude")
    agents.update_agent(aid, model="claude-fable-5-2")
    agents.record_state(aid, "interrupted", {"account_recovery": "waiting", "reason": "usage_limit"})

    readings["claude"] = {"accounts": [
        {"alias": "ai9", "is_active": True, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.0, "used_7d": 0.63, "blocked_models": ["Fable"],
            "scoped": [{"model": "Fable", "used": 1.0, "exhausted": True}]}},
        {"alias": "ai5", "is_active": False, "error": None, "usage": {
            "status": "rejected", "available": False, "limited": True,
            "used_5h": 0.0, "used_7d": 1.0}},
        {"alias": "ai6", "is_active": False, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.05, "used_7d": 0.2, "blocked_models": [], "scoped": [],
            "degraded": "per-model limits unavailable"}},
        {"alias": "ai7", "is_active": False, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.2, "used_7d": 0.3, "blocked_models": [],
            "scoped": [{"model": "Fable", "used": 0.4, "exhausted": False}]}},
    ]}

    worker.hotseat_once()
    assert [c for c in calls if c[0] == "switch"] == [("switch", "hotseat", "claude", "ai7")]
    assert len(sent) == 1
    assert "switched to ai7 for Fable" in sent[0]["preview"]
    assert "60% Fable quota" in sent[0]["preview"]


def test_hotseat_fable_starvation_reports_honestly_without_thrashing(switcher):
    owner, worker, readings, calls, sent = switcher
    fable_aid = agents.create_agent(persona="Koko", voice_id="v", cwd="/tmp", session="koko-8255", backend="claude")
    agents.update_agent(fable_aid, model="claude-fable-5-2")
    agents.record_state(fable_aid, "interrupted", {"account_recovery": "waiting", "reason": "usage_limit"})

    readings["claude"] = {"accounts": [
        {"alias": "ai9", "is_active": True, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.0, "used_7d": 0.63, "blocked_models": ["Fable"]}},
        {"alias": "ai5", "is_active": False, "error": None, "usage": {
            "status": "rejected", "available": False, "limited": True,
            "used_5h": 0.0, "used_7d": 1.0}},
    ]}

    # 1. No other Claude models running: reports model exhaustion once, never switches to ai5.
    worker.hotseat_once()
    assert not [c for c in calls if c[0] == "switch"]
    claude_runs = [r for r in janitors.list_runs(owner["session"])
                   if ((r.get("demand_result") or {}).get("summary") or "").lower().startswith("claude:")]
    assert claude_runs[0]["demand_result"]["status"] == "exhausted"
    assert "0% Fable quota left and no saved account can serve Fable" in claude_runs[0]["demand_result"]["summary"]
    assert len(sent) == 1 and "no saved account can serve Fable" in sent[0]["preview"]

    _check_again("hotseat-switcher.claude")
    worker.hotseat_once()
    assert not [c for c in calls if c[0] == "switch"]
    assert len(sent) == 1

    # 2. Another non-blocked model (Sonnet) is actively running on ai9: keeps ai9 (noop)
    # with an honest summary rather than claiming "ai9 keeps 100% of the 5-hour window".
    sonnet_aid = agents.create_agent(persona="Builder", voice_id="v", cwd="/tmp", session="builder-1", backend="claude")
    agents.update_agent(sonnet_aid, model="claude-sonnet-4-6")
    agents.record_state(sonnet_aid, "thinking", {"source": "user"})

    _check_again("hotseat-switcher.claude")
    worker.hotseat_once()
    assert not [c for c in calls if c[0] == "switch"]
    claude_runs = [r for r in janitors.list_runs(owner["session"])
                   if ((r.get("demand_result") or {}).get("summary") or "").lower().startswith("claude:")]
    latest = claude_runs[0]["demand_result"]
    assert latest["status"] == "noop"
    assert "0% Fable quota left and no saved account can serve Fable" in latest["summary"]
    assert "keeping ai9 (100% of the 5-hour window) for running models" in latest["summary"]


def test_claude_available_false_alone_marks_account_zero_remaining():
    payload = {"accounts": [
        {"alias": "a", "is_active": True, "error": None, "usage": {
            "status": "allowed", "available": False, "limited": False,
            "used_5h": 0.1, "used_7d": 0.2}},
        {"alias": "b", "is_active": False, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.3, "used_7d": 0.2}},
    ]}
    parsed = hotseat.parse_claude(payload)
    assert parsed[0]["remaining"] == 0
    decision = hotseat.decide(parsed, 25)
    assert decision["action"] == "switch" and decision["target"] == "b"


def test_hotseat_decide_never_oscillates_or_strands_running_models():
    def _acct(alias, active=False, used_5h=0.2, used_7d=0.3, **usage):
        return {"alias": alias, "is_active": active, "usage": {
            "used_5h": used_5h, "used_7d": used_7d, "status": "allowed", "limited": False, **usage,
        }}

    def _scoped(**families):
        return [
            {"model": name.capitalize(), "used": used, "exhausted": used >= 1.0}
            for name, used in families.items()
        ]

    def _sim(accounts, required, active, *, start_alias, ticks=12):
        state = [{**a, "is_active": a["alias"] == start_alias} for a in accounts]
        switches = []
        for _ in range(ticks):
            d = hotseat.decide(
                hotseat.parse_claude({"accounts": state}), 25,
                required_models=required, active_models=active,
            )
            if d["action"] == "switch":
                switches.append((d["active"], d["target"], d.get("model")))
                state = [{**a, "is_active": a["alias"] == d["target"]} for a in state]
        return switches, next(x["alias"] for x in state if x["is_active"])

    fable = ("claude-fable-5-2",)
    sonnet = ("claude-sonnet-5-5",)

    # 1. Fault 1a: a Fable-blocked with plenty of 5h room; b Fable-ok at 20% or 8% (under floor 25).
    for b_used_5h in (0.80, 0.92):
        two = [_acct("a", used_5h=0.1, blocked_models=["Fable"]), _acct("b", used_5h=b_used_5h)]
        sw_a, end_a = _sim(two, fable, fable, start_alias="a")
        sw_b, end_b = _sim(two, fable, fable, start_alias="b")
        assert sw_a == [("a", "b", "Fable")] and end_a == "b"
        assert sw_b == [] and end_b == "b"

    # 2. Fault 1b: two models (a: Fable=0, Sonnet=80; b: Fable=60, Sonnet=5) across running/queued/recent-limit mixes.
    flap_pair = [
        _acct("a", blocked_models=["Fable"], scoped=_scoped(fable=1.0, sonnet=0.2)),
        _acct("b", scoped=_scoped(fable=0.4, sonnet=0.95)),
    ]
    for req, act in (
        (fable + sonnet, fable + sonnet),
        (fable + sonnet, sonnet),
        (sonnet + fable, sonnet),
        (fable + sonnet, ()),
    ):
        sw_a, end_a = _sim(flap_pair, req, act, start_alias="a")
        sw_b, end_b = _sim(flap_pair, req, act, start_alias="b")
        assert len(sw_a) == 1 and end_a == "b"
        assert sw_b == [] and end_b == "b"

    # 3. Symmetric bottleneck (a: Fable=15, Sonnet=80; b: Fable=80, Sonnet=15) never ping-pongs.
    sym_pair = [
        _acct("a", scoped=_scoped(fable=0.85, sonnet=0.2)),
        _acct("b", scoped=_scoped(fable=0.2, sonnet=0.85)),
    ]
    for start in ("a", "b"):
        sw, end = _sim(sym_pair, fable + sonnet, fable + sonnet, start_alias=start)
        assert sw == [] and end == start

    # 4. Never strand another running model or an unblocked required model.
    mutually_exclusive = [
        _acct("a", True, blocked_models=["Fable"], scoped=_scoped(fable=1.0, sonnet=0.2)),
        _acct("b", blocked_models=["Sonnet"], scoped=_scoped(fable=0.2, sonnet=1.0)),
    ]
    d_keep = hotseat.decide(
        hotseat.parse_claude({"accounts": mutually_exclusive}), 25,
        required_models=fable + sonnet, active_models=fable + sonnet,
    )
    assert d_keep["action"] == "noop" and d_keep["blocked_model"] == "Fable"

    # Running model outside required_models (Sonnet running, Fable queued) is also protected from stranding.
    d_keep_running = hotseat.decide(
        hotseat.parse_claude({"accounts": mutually_exclusive}), 25,
        required_models=fable, active_models=sonnet,
    )
    assert d_keep_running["action"] == "noop" and d_keep_running["blocked_model"] == "Fable"

    # Unblocked required model (Fable at 5%, Sonnet blocked, none running) is not sacrificed even if b has higher leximin.
    unblocked_req_pair = [
        _acct("a", True, used_5h=0.95, blocked_models=["Sonnet"], scoped=_scoped(fable=0.95, sonnet=1.0)),
        _acct("b", used_5h=0.2, blocked_models=["Fable"], scoped=_scoped(fable=1.0, sonnet=0.2)),
    ]
    assert hotseat.decide(
        hotseat.parse_claude({"accounts": unblocked_req_pair}), 25,
        required_models=fable + sonnet, active_models=(),
    ) == {"action": "exhausted", "active": "a", "remaining": 0, "general_remaining": 5, "model": "Sonnet"}

    with_healthy_c = mutually_exclusive + [_acct("c", scoped=_scoped(fable=0.5, sonnet=0.5))]
    d_pick_c = hotseat.decide(
        hotseat.parse_claude({"accounts": with_healthy_c}), 25,
        required_models=fable + sonnet, active_models=fable + sonnet,
    )
    assert d_pick_c["action"] == "switch" and d_pick_c["target"] == "c"

    # 5. Model switch rejects a candidate no better than active on the model, or with 0 weekly secondary (non-overage).
    no_better = [
        _acct("a", True, used_5h=0.2, scoped=_scoped(fable=0.85)),
        _acct("b", used_5h=0.05, scoped=_scoped(fable=0.85)),
    ]
    assert hotseat.decide(
        hotseat.parse_claude({"accounts": no_better}), 25,
        required_models=fable, active_models=fable,
    )["action"] == "noop"

    assert hotseat.decide([
        {"alias": "a", "active": True, "remaining": 80, "secondary": 50, "blocked_models": ("fable",)},
        {"alias": "b", "active": False, "remaining": 80, "secondary": 0},
    ], 25, required_models=fable, active_models=fable)["action"] == "exhausted"

    assert hotseat.decide([
        {"alias": "a", "active": True, "remaining": 80, "secondary": 50, "blocked_models": ("fable",)},
        {"alias": "b", "active": False, "remaining": 80, "secondary": 0, "overage": True},
    ], 25, required_models=fable, active_models=fable)["target"] == "b"

    # 6. General switch ranks candidates that clear the rung on required models ahead of raw 5-hour room.
    ranked = [
        _acct("a", True, used_5h=0.95),
        _acct("b", used_5h=0.05, scoped=_scoped(fable=0.85)),  # 95% 5h, only 15% Fable (< 25)
        _acct("c", used_5h=0.40, scoped=_scoped(fable=0.40)),  # 60% 5h, 60% Fable (>= 25)
    ]
    d_ranked = hotseat.decide(
        hotseat.parse_claude({"accounts": ranked}), 25,
        required_models=fable, active_models=fable,
    )
    assert d_ranked["action"] == "switch" and d_ranked["target"] == "c"


def test_hotseat_demand_includes_unpaused_queued_and_recently_limited_agents_and_enforces_reversal_cooldown(switcher, monkeypatch):
    from lib import turn_queue

    owner, worker, readings, calls, sent = switcher
    now = [1_800_000_000_000]
    monkeypatch.setattr(db, "now_ms", lambda: now[0])

    def _fable_readings():
        return {"accounts": [
            {"alias": "ai9", "is_active": True, "error": None, "usage": {
                "status": "allowed", "available": True, "limited": False,
                "used_5h": 0.1, "used_7d": 0.4, "blocked_models": ["Fable"]}},
            {"alias": "ai7", "is_active": False, "error": None, "usage": {
                "status": "allowed", "available": True, "limited": False,
                "used_5h": 0.2, "used_7d": 0.3, "blocked_models": [],
                "scoped": [{"model": "Fable", "used": 0.3, "exhausted": False}]}},
        ]}

    # 1. Queued turn on an unpaused queue triggers model-scoped Fable switch; paused queue does not.
    q_aid = agents.create_agent(persona="Q", voice_id="v", cwd="/tmp", session="q-fable", backend="claude")
    agents.update_agent(q_aid, model="claude-fable-5-2")
    agents.record_state(q_aid, "done", {"source": "user"})
    turn_queue.enqueue(
        queue_id="q-1", agent_id=q_aid, session="q-fable", text="run",
        trace_id="t-q1", client_msg_id="q-1", synthesize_audio=False,
        origin="user", sender_agent_id="",
    )
    turn_queue.set_paused(q_aid, True, user_stop=True)
    readings["claude"] = _fable_readings()
    worker.hotseat_once()
    assert not [c for c in calls if c[0] == "switch"]

    turn_queue.set_paused(q_aid, False)
    _check_again("hotseat-switcher.claude")
    now[0] += 60_000
    worker.hotseat_once()
    assert [c for c in calls if c[0] == "switch"] == [("switch", "hotseat", "claude", "ai7")]
    turn_queue.remove("q-1")
    calls.clear()

    # 2. Reversal cooldown: ai7 is now active (8% 5h < rung 10) and ai9 has 18% (>= rung 10, < floor 25) -> holds ai7 during cooldown.
    readings["claude"] = {"accounts": [
        {"alias": "ai9", "is_active": False, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.82, "used_7d": 0.4}},
        {"alias": "ai7", "is_active": True, "error": None, "usage": {
            "status": "allowed", "available": True, "limited": False,
            "used_5h": 0.92, "used_7d": 0.3}},
    ]}
    _check_again("hotseat-switcher.claude")
    now[0] += 60_000
    worker.hotseat_once()
    assert not [c for c in calls if c[0] == "switch"]

    # After cooldown expires (20 minutes later), general ladder may switch to ai9.
    _check_again("hotseat-switcher.claude")
    now[0] += 20 * 60_000
    worker.hotseat_once()
    assert [c for c in calls if c[0] == "switch"] == [("switch", "hotseat", "claude", "ai9")]
    calls.clear()

    # 3. Recently usage-limited agent (within 15 min, without parked account_recovery) counts as model demand.
    lim_aid = agents.create_agent(persona="L", voice_id="v", cwd="/tmp", session="lim-fable", backend="claude")
    agents.update_agent(lim_aid, model="claude-fable-5-2")
    db.conn().execute(
        "INSERT INTO state_log (agent_id, ts, kind, detail) VALUES (?, ?, 'error', ?)",
        (lim_aid, now[0] - 2 * 3_600_000, json.dumps({"reason": "usage_limit"})),
    )
    readings["claude"] = _fable_readings()
    _check_again("hotseat-switcher.claude")
    now[0] += 20 * 60_000
    worker.hotseat_once()
    assert not [c for c in calls if c[0] == "switch"]

    db.conn().execute(
        "INSERT INTO state_log (agent_id, ts, kind, detail) VALUES (?, ?, 'error', ?)",
        (lim_aid, now[0] - 60_000, json.dumps({"reason": "usage_limit"})),
    )
    _check_again("hotseat-switcher.claude")
    now[0] += 60_000
    worker.hotseat_once()
    assert [c for c in calls if c[0] == "switch"] == [("switch", "hotseat", "claude", "ai7")]


def test_reset_owner_disable_cancels_the_owned_command_group(switcher,monkeypatch,tmp_path):
    import dataclasses,sys,time,subprocess
    from lib import config,janitor_reset
    owner,worker,readings,calls,sent=switcher
    script=tmp_path/'hang.py';marker=tmp_path/'started'
    script.write_text("import pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text('ready')\ntime.sleep(60)\n")
    cfg=config.load();monkeypatch.setattr(config,'load',lambda:dataclasses.replace(cfg,claude_reset_command=(sys.executable,str(script),str(marker))))
    current=janitor_builtins.resolve(hotseat.ROLE)
    assert worker.reset_worker.offer('claude','claude_reset_command',('Fable',),current)=='checking'
    try:
        deadline=time.monotonic()+3
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
        assert marker.exists()
        changed=janitors.get('hotseat');janitors.set_enabled('hotseat',changed['revision'],False)
        worker.reset_worker.thread.join(3)
        assert not worker.reset_worker.thread.is_alive()
        result=worker.reset_worker.peek('claude',current)
        assert result and result['result']['verified'] is False
        assert result['result']['status']=='cancelled'
    finally:
        worker.stop()


@pytest.mark.parametrize('bound',[False,True])
def test_reset_notice_reaches_the_app_transcript_and_sse_once_per_action(switcher,monkeypatch,bound):
    from lib import backends,janitor_notices
    from lib.conversation import load_conversation
    owner,worker,readings,calls,sent=switcher
    if bound:
        agents.start_runtime(owner['agent_id'],'codex')
        agents.bind_backend_session(owner['agent_id'],'native-janitor-context')
    monkeypatch.setattr(backends,'find_session_jsonl',lambda *a,**kw:None)
    agents.store_transcript_turns(agent_id=owner['agent_id'],backend_session_id='old-context',source_file='/tmp/old-context.jsonl',turns=[{'role':'assistant','text':'UNRELATED OLD CONVERSATION','timestamp':'2026-01-01T00:00:00Z'}])
    emitted=[]
    class Stream:
        def broadcast(self,event):emitted.append(dict(event))
    stream=Stream()
    first=janitor_notices.publish(stream,owner,'one-reset','Banked reset redeemed for ai2; quota refill verified.')
    again=janitor_notices.publish(stream,owner,'one-reset','Banked reset redeemed for ai2; quota refill verified.')
    assert first['id']==again['id'] and again['created'] is False
    response=load_conversation(session=owner['session'],include_automated=False)
    assert len(response['turns'])==1 and response['turns'][0]['text'].startswith('Banked reset redeemed')
    assert response['conversation_id']==('native-janitor-context' if bound else janitor_notices.conversation_id(owner['agent_id']))
    assert all(e['type']=='transcript-updated' and e['session']==owner['session'] for e in emitted)


def test_uncertain_reset_reconciles_with_healthy_quota_and_ack_survives_restart(switcher,monkeypatch,tmp_path):
    import dataclasses,sys
    from lib import config,janitor_reset
    owner,worker,readings,calls,sent=switcher
    command=tmp_path/'reconcile.py';request=tmp_path/'request.json';spent=tmp_path/'spent'
    command.write_text("import json,pathlib,sys\nr=json.load(sys.stdin);pathlib.Path(sys.argv[1]).write_text(json.dumps(r))\nif not r.get('reconcile_only'):pathlib.Path(sys.argv[2]).write_text('UNAUTHORIZED WRITE')\nprint(json.dumps({'status':'no_pending','verified':False}))\n")
    cfg=config.load();monkeypatch.setattr(config,'load',lambda:dataclasses.replace(cfg,claude_reset_command=(sys.executable,str(command),str(request),str(spent))))
    settings_store.set_text('hotseat-reset.claude.state',json.dumps({'id':'interrupted-reset','owner':owner['agent_id'],'models':['Fable'],'next_at':0,'handled':True,'needs_reconcile':True,'result':{'status':'cancelled','verified':False,'needs_reconcile':True}}))
    readings['claude']=claude_payload(a=(True,.1),b=(False,.2))
    worker.hotseat_once()
    try:
        assert worker.reset_worker.thread is not None
        worker.reset_worker.thread.join(3)
        assert not worker.reset_worker.thread.is_alive()
        assert not spent.exists()
        assert json.loads(request.read_text()).get('reconcile_only') is True
        worker.hotseat_once()
        restarted=janitor_reset.ResetWorker(worker.stop_event)
        assert restarted.peek('claude',owner) is None
        assert restarted.reconciliation_due('claude',owner) is None
    finally:worker.stop()


def test_reset_worker_hands_its_database_connection_back(switcher,monkeypatch):
    import dataclasses,sqlite3
    from lib import config,janitor_reset
    owner,worker,readings,calls,sent=switcher
    cfg=config.load();monkeypatch.setattr(config,'load',lambda:dataclasses.replace(cfg,claude_reset_command=('fake',)))
    connections=[]
    def done(*a,**kw):
        connections.append(db.conn());return {'status':'no_eligible_grant','verified':False}
    monkeypatch.setattr(janitor_reset,'attempt',done)
    current=janitor_builtins.resolve(hotseat.ROLE)
    assert worker.reset_worker.offer('claude','claude_reset_command',('Fable',),current)=='checking'
    worker.reset_worker.thread.join(3)
    assert len(connections)==1
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):
        connections[0].execute('SELECT 1')
