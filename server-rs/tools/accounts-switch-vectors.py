#!/usr/bin/env python3
"""Saved-seat readings and switch decisions from the reference in a replay home."""
import importlib.util
import itertools
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
OUT = TOOLS.parent / "crates/clarp-kernel/tests/fixtures/accounts-switch-vectors.json"


def codex_reset_cases(policy):
    from lib import janitor_reset

    def window(used, label="Codex weekly"):
        return dict(label=label, used=used)

    def account(alias, windows, **reading):
        return dict(alias=alias, is_active=alias == "current",
                    usage=dict(windows=windows, worst_used=1, **reading))

    weekly = []

    def add(name, *rows):
        body = dict(accounts=rows)
        parsed = policy.parse_codex(body)
        weekly.append(dict(name=name, body=body, parsed=parsed,
                           eligible=policy.codex_weekly_spent(parsed)))

    add("no accounts")
    add("weekly evidence at the inclusive 99 percent bound",
        account("current", [window(0.99)]), account("backup", [window(0.99)]))
    add("all accounts above the bound",
        account("current", [window(0.991)]), account("backup", [window(1)]))
    add("the spare is just below the bound",
        account("current", [window(1)]), account("backup", [window(0.989999)]))
    add("the active account is just below the bound",
        account("current", [window(0.989999)]), account("backup", [window(1)]))
    add("a five-hour or model block does not count as weekly exhaustion",
        account("current", [window(1, "Codex 5-hour"), window(1, "GPT-5 weekly"),
                            window(1, "Spend control weekly"), window(1, "weekly"),
                            window(1, "Codex 7d"), window(0.5)], blocked=True, usable=False))
    add("a blocked account still reports its weekly number",
        account("current", [window(1)], blocked=True))
    add("an unusable account still reports a successful weekly read",
        account("current", [window(0.99)], usable=False))
    add("a failed read suppresses the weekly number",
        account("current", [window(1)], error="unavailable"), account("backup", [window(1)]))
    add("falsey error does not suppress the reading",
        account("current", [window(0.99)], error=""))
    add("a missing week on one account blocks the offer",
        account("current", [window(1)]), dict(alias="backup", usage=dict(worst_used=1)))
    add("malformed nonweekly entries are ignored",
        account("current", [None, "Codex weekly", {}, window(2, 7),
                            window("bad", "GPT-5 weekly"), window(1)]))
    add("account-wide labels include mixed case, tier, and Python whitespace",
        account("current", [window(0.99, "  cOdEX\x1fPro\tWEEKLY  ")]))
    add("two weekly numbers use the greatest regardless of order",
        account("current", [window(1), window(0.1)]),
        account("backup", [window(0.1), window(1)]))
    add("equal weekly values retain the first numeric representation",
        account("current", [window(1.0), window(1)]),
        account("backup", [window(1), window(1.0)]))
    add("an empty list of windows reports no week", account("current", []))
    add("a weekly row missing its used field invalidates the other weekly number",
        account("current", [dict(label="Codex weekly"), window(1)]))
    for invalid in [None, True, False, "1", [], {}]:
        add(f"an unreadable weekly value invalidates duplicate windows: {invalid!r}",
            account("current", [window(1), window(invalid)]))

    ledger = []
    now = 2 * janitor_reset.DAY_MS
    worker = janitor_reset.ResetWorker(None)
    cap_field = "codex_reset_max_per_day"
    expired = now - janitor_reset.DAY_MS
    for name, cap, saved in [
        ("missing ledger defaults to empty", 1, None),
        ("reset off", 0, "[]"),
        ("malformed ledger", 1, "{"),
        ("wrong top-level value", 1, "null"),
        ("boolean timestamp", 1, "[true]"),
        ("fractional timestamp type", 1, "[1.0]"),
        ("string after a valid timestamp", 1, json.dumps([now, str(now)])),
        ("expired and future timestamps do not count", 1, json.dumps([expired, now + 1])),
        ("the earliest unexpired timestamp counts", 1, json.dumps([expired + 1])),
        ("the present timestamp counts", 1, json.dumps([now])),
        ("a higher cap has room", 2, json.dumps([now])),
        ("a higher cap is reached", 2, json.dumps([expired + 1, now])),
    ]:
        with (patch.object(janitor_reset.settings_store, "get_text",
                           side_effect=lambda _, default: default if saved is None else saved),
              patch.object(janitor_reset.db, "now_ms", return_value=now),
              patch.object(janitor_reset.config, "load", return_value=SimpleNamespace(**{cap_field: cap}))):
            redemptions = worker.spent_today("codex")
            blocked = worker.capped("codex", cap_field)
        ledger.append(dict(name=name, cap=cap, count=None if redemptions is None else len(redemptions),
                           eligible=not blocked, blocked=blocked))
    return dict(weekly=weekly, ledger=ledger)


def capture(place):
    spec = importlib.util.spec_from_file_location("replay_host", ROOT / "tests/replay/host.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    (place / "scenario-config.toml").write_text("")
    replay.isolate(place)
    replay.loopback_only()
    sys.path.insert(0, str(ROOT / "server"))
    from lib import janitor_hotseat as policy
    reads = []
    values = [None, False, True, "1", -1, 0, 0.005, 0.015, 0.5, 0.99, 0.995, 1, 2]
    for provider, parse in [("codex", policy.parse_codex), ("claude", policy.parse_claude)]:
        for body in [None, [], {}, {"accounts": [None, {}, {"alias": ""}]}]:
            reads.append(dict(provider=provider, body=body, parsed=parse(body)))
        for value in values:
            for overlay in [{}, {"error": "unavailable"}, {"blocked": True}, {"usable": False},
                            {"extra_usage_enabled": True}, {"overage_status": "allowed_warning"},
                            {"overage_status": "allowed"}, {"status_7d": "rejected"},
                            {"available": False}, {"status": "rejected"}, {"limited": True}]:
                usage = dict(used_5h=value, used_7d=value, worst_used=value, **overlay)
                body = {"accounts": [dict(alias="example", is_active=1, usage=usage),
                                     dict(alias="backup", error=True, usage=usage)]}
                reads.append(dict(provider=provider, body=body, parsed=parse(body)))
        for n in [0, 1, 31, 32, 33]:
            usage = {"used_5h": 0.1, "used_7d": 0.2, "degraded": True,
                     "blocked_models": ["opus"] * n + ["CUSTOM", "a/b", "claude-fable"],
                     "scoped": [None] * n + [
                         {"model": "claude-Fable", "used": 0.1},
                         {"model": "FABLE", "used": 0.4}, {"model": "Sonnet", "exhausted": True}]}
            body = {"accounts": [{"alias": "example", "usage": usage}]}
            reads.append(dict(provider=provider, body=body, parsed=parse(body)))
    def seat(alias, remaining, **more):
        return dict(alias=alias, remaining=remaining, secondary=more.pop("secondary", 10),
                    active=alias=="current", **more)
    decisions = []
    def add(rows, floor=25, required=(), running=(), last=None, now=1000000, cooldown=900000):
        args = dict(required_models=required, active_models=running, last_switch=last,
                    now_ms=now, cooldown_ms=cooldown)
        decisions.append(dict(accounts=rows, floor=floor, **args, result=policy.decide(rows, floor, **args)))
    add([])
    add([seat("other", 100)])
    for floor, current, other in itertools.product([0, 1, 10, 25, 100],
                                                   [None, 0, 1, 9, 10, 24, 25], [0, 1, 9, 10, 24, 25, 100]):
        add([seat("current",current),seat("z",other),seat("a",other)], floor)
    for running, required in [((),()), ((),("opus","sonnet")), (("sonnet",),("opus",)),
                              (("unknown family",),("custom","claude-OPUS","opus","bad/name"))]:
        for active, cand in itertools.product(
                [{}, {"degraded":True}, {"blocked_models":["opus"]}, {"scoped_remaining":{"opus":3}}],
                [{}, {"secondary":0}, {"degraded":True}, {"secondary":0,"overage":True},
                 {"blocked_models":["sonnet"]},{"scoped_remaining":{"opus":0,"sonnet":5}}]):
            for current in (0, 19, 52):
                add([seat("current",current,**active),seat("z",57,**cand),seat("b",15)],
                    required=required, running=running)
    for at in [None, False, "999999", 99999,100000,100001,1000000,1000001]:
        for have,other in [(0,9),(1,10),(9,24),(9,25),(24,26),(57,90)]:
            for req in [(),("opus",)]:
                add([seat("current",have,scoped_remaining={"opus":0}),seat("z",other)],
                    required=req,last={"from":"z","to":"current","at":at})
    # A sole active seat has healthy general quota but no Opus quota. Running
    # Sonnet work changes the outcome from exhausted to a blocked-model no-op.
    for running in [(), ("sonnet",)]:
        add([seat("current",70,scoped_remaining={"opus":0})],required=("opus",),running=running)
    add([seat("current",70,scoped_remaining={"opus":0}),
         seat("backup",85,scoped_remaining={"opus":40})],required=("opus",))
    from lib import hotseat_desk
    hotseat_desk.log = lambda *args, **kw: None
    hotseat_desk.now_ms = lambda: 1000000
    hotseat_desk._mark_active = lambda *args: None
    manual = []
    for provider in ("claude","codex"):
        for before, outcome in itertools.product(["unreadable", None, "current", "backup"], [False, True]):
            machine = hotseat_desk.HotseatDesk()
            def current(*_):
                if before == "unreadable":
                    raise OSError("stand-in unavailable")
                return before
            status, body = machine._switch(provider,"backup","stand-in",lambda *_:outcome,current,lambda *_:None)
            manual.append(dict(provider=provider,before=before,confirmed=outcome,status=status,body=body,
                               record=hotseat_desk._stored_switch(provider)))
    reports=[]
    by_shape={}
    for case in decisions:
        answer=case["result"]
        by_shape.setdefault((answer["action"],"model" in answer,"blocked_model" in answer),answer)
    for provider,decision,mode,switched in itertools.product(("claude","codex"),by_shape.values(),
                                                            ("notify","automatic"),(False,True)):
        preview=policy.preview(provider,decision,mode=mode,switched=switched)
        reports.append(dict(provider=provider,decision=decision,mode=mode,switched=switched,
                            expected=[preview,policy.run_summary(provider,decision,text=preview)]))
    return {"reads":reads, "decisions":decisions, "manual":manual, "reports":reports,
            "codex_reset":codex_reset_cases(policy)}


if __name__ == "__main__":
    with tool_scratch.directory() as directory:
        result=capture(Path(directory))
    OUT.write_text(json.dumps(result,sort_keys=True,separators=(",",":"))+"\n",encoding="ascii")
