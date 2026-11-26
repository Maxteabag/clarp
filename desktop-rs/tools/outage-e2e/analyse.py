#!/usr/bin/env python3
"""Reads one e2e run (marks, timeline, gateway log) and prints what the user saw and when."""
import json, sys
d = sys.argv[1]
marks = {m["mark"]: m["wall_ms"] for m in map(json.loads, open(f"{d}/marks.jsonl"))}
timeline = [json.loads(l) for l in open(f"{d}/timeline.jsonl")]
gateway = [json.loads(l) for l in open(f"{d}/gateway.jsonl")]
def at(ms, base): return f"{(ms - base) / 1000:+.2f}s"
def short(seen):
    rows = [r["text"][:28] for r in seen["rows"]]
    return f'label={seen["label"]:<12} connected={str(seen["connected"]):<5} banner={seen["banner"]!r} row={seen["row_state"]} avail={seen["availability"]!r} outage={seen["outage"]} catching_up={seen.get("catching_up")} rows={len(rows)} last={rows[-1:]!r}'
print("== timeline (relative to each scenario start)")
for name, start, end in (("S1 502 burst", "s1-502-on", "s2-sigterm"), ("S2 restart + 44.5 s 502", "s2-sigterm", "s3-start")):
    print(f"-- {name}")
    base = marks[start]
    for m, ms in sorted(marks.items(), key=lambda kv: kv[1]):
        if marks[start] <= ms <= marks[end]:
            print(f"   mark {at(ms, base)} {m}")
    for entry in timeline:
        if marks[start] - 200 <= entry["wall_ms"] <= marks[end]:
            print(f"   {at(entry['wall_ms'], base)} {short(entry['seen'])}")
print("== checks")
def window(a, b): return [e for e in timeline if marks[a] <= e["wall_ms"] <= marks[b]]
ok = True
def check(cond, what):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + what)
s1 = window("s1-502-on", "s2-sigterm")
check(all(e["seen"]["banner"] == "" for e in timeline if e["wall_ms"] < marks["s3-start"]), "no banner (raw or other) at any time in S1 and S2")
check(all(e["seen"]["label"] == "live" for e in s1), "S1: the status label stays live (no indicator for a 2 s blip)")
check(all(e["seen"]["row_state"] != "offline" for e in s1), "S1: the row never turns offline")
s1_back = next(e for e in s1 if e["wall_ms"] > marks["s1-502-off"] and e["seen"]["connected"])
print(f"     S1 reconnected {at(s1_back['wall_ms'], marks['s1-502-off'])} after the gateway passed again")
s2 = window("s2-sigterm", "s3-start")
first_updating = next((e for e in timeline if e["wall_ms"] >= marks["s2-sigterm"] - 50 and (e["seen"]["availability"] or "").startswith("Host updating")), None)
check(first_updating is not None, "S2: the host-updating line shows")
if first_updating:
    print(f"     host-updating line {at(first_updating['wall_ms'], marks['s2-sigterm'])} after SIGTERM: {first_updating['seen']['availability']!r}")
drop = next(e for e in s2 if not e["seen"]["connected"])
indicator = next((e for e in s2 if e["seen"]["label"] == "reconnecting"), None)
check(indicator is not None, "S2: the calm indicator (reconnecting) shows in the long outage")
if indicator:
    print(f"     indicator {at(indicator['wall_ms'], drop['wall_ms'])} after the drop was seen ({at(indicator['wall_ms'], marks['s2-sigterm'])} after SIGTERM); row={indicator['seen']['row_state']} avail={indicator['seen']['availability']!r}")
    check(indicator["wall_ms"] - drop["wall_ms"] >= 14800, "S2: nothing shown before 15 s")
    check(all((e["seen"]["availability"] or "").startswith("Host updating") for e in s2 if indicator["wall_ms"] <= e["wall_ms"] < marks["s2-502-off"]), "S2: the host-updating line stays through the outage (not replaced by gateway words)")
held = [e for e in s2 if marks["s2-502-off"] <= e["wall_ms"] < marks["s2-reads-pass"]]
reopened = next((e for e in held if e["seen"]["connected"]), None)
check(reopened is not None, "S2: the stream came back while the gateway still refused the reads")
if reopened:
    print(f"     stream back {at(reopened['wall_ms'], marks['s2-502-off'])} after /events passed; reads held 502 until {at(marks['s2-reads-pass'], marks['s2-502-off'])}")
check(all(e["seen"]["label"] != "live" and e["seen"]["outage"] != "none" for e in held), "S2: a reopened stream alone does not end the outage (no live, outage kept while reads fail)")
check(all(not any(r["text"] == "gap two" for r in e["seen"]["rows"]) for e in held), "S2: nothing new was read while the reads failed (the probe is honest)")
back = next((e for e in s2 if e["wall_ms"] > marks["s2-502-off"] and e["seen"]["label"] == "live" and e["seen"]["connected"]), None)
check(back is not None and back["wall_ms"] >= marks["s2-reads-pass"], "S2: live only after the catch-up reads answered")
if back:
    print(f"     live again {at(back['wall_ms'], marks['s2-reads-pass'])} after the reads passed ({at(back['wall_ms'], marks['s2-sigterm'])} after SIGTERM)")
    check(any(r["text"] == "gap two" for r in back["seen"]["rows"]) and back["seen"]["row_state"] != "offline", "S2: when it says live, the roster and the open chat are already caught up")
final = timeline[-1]["seen"]
texts = [r["text"] for r in final["rows"]]
users = [t for t in texts if not t.startswith("Reply to")]
print("     final rows:", [t[:40] for t in texts])
check(users == ["history before", "gap one", "gap two"], "user turns once, in order")
replies = [t.split("<")[0].strip() for t in texts if t.startswith("Reply to")]
check(replies == ["Reply to [history before", "Reply to [gap one", "Reply to [gap two"], f"replies once, in order: {replies}")
idx = {t: i for i, t in enumerate(texts)}
caught = next((e for e in s2 if any(r["text"] == "gap two" for r in e["seen"]["rows"])), None)
if caught:
    print(f"     gap two shown {at(caught['wall_ms'], marks['s2-reads-pass'])} after the reads passed")
check(final["row_state"] != "offline" and final["availability"] is None, "after recovery the row is back and the host-updating line gone")
check(final["server_id"] and all(e["seen"]["server_id"] in ("", final["server_id"]) for e in timeline), f"one server identity throughout: {final['server_id']}")
resumes = [g for g in gateway if g.get("request", "").startswith("GET /events") and g.get("outcome") == "forwarded"]
print("     /events forwarded with Last-Event-ID:", [g["last_event_id"] for g in resumes])
check(all(g["last_event_id"] for g in resumes[1:]), "every reopened stream carried Last-Event-ID")
auth = [json.loads(l) for l in open(f"{d}/timeline-auth.jsonl")]
refused = next((e for e in auth if e["seen"]["label"] == "unauthorized"), None)
check(refused is not None and refused["t_ms"] < 2000, f"a token the Host never issued is said at once: {refused and refused['seen']['banner']!r} at {refused and refused['t_ms']} ms")
print("ALL PASS" if ok else "SOME FAILED")
