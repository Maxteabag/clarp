#!/usr/bin/env python3
"""Saved-seat readings and switch decisions from the reference in a replay home."""
import importlib.util
import itertools
import json
from pathlib import Path
import sys
import tempfile

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
OUT = TOOLS.parent / "crates/clarp-kernel/tests/fixtures/accounts-switch-vectors.json"


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
    return {"reads":reads, "decisions":decisions}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        result=capture(Path(directory))
    OUT.write_text(json.dumps(result,sort_keys=True,separators=(",",":"))+"\n",encoding="ascii")
