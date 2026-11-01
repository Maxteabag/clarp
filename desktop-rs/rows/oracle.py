#!/usr/bin/env python3
"""ROWS scratch proof only (branch proof/desktop-rows-20261101, never merged).

Per-arm oracle: python3 rows/oracle.py ARM DIR, ARM = r-walk | r-handle | r-neg.
Reads DIR/<arm>.rc, DIR/<arm>.log (check.sh output) and DIR/<arm>/app.log
(ROWS-SAMPLE / ROWS-MEASURE lines). Writes DIR/<arm>.verdict.
Exit: 0 expected, 2 missing/malformed/panic, 4 UNKNOWN (not reproduced or not
correlated), 5 unexpected.
"""
import json
import pathlib
import re
import sys

ARM, DIR = sys.argv[1], pathlib.Path(sys.argv[2])
ROW_FAILS = ("FAIL the row's height never changed from unsent to sent:", "FAIL the late line grows the row once")
COMPOSER_FAIL = "FAIL and the composer is empty at once: ["
MAX = 20 * 1024 * 1024


def finish(code, **verdict):
    verdict.update(arm=ARM, exit=code)
    (DIR / f"{ARM}.verdict").write_text(json.dumps(verdict) + "\n")
    print(f"ROWS-ORACLE {json.dumps(verdict)}")
    sys.exit(code)


def read(name):
    path = DIR / name
    if not path.is_file():
        finish(2, problem=f"missing {name}")
    if path.stat().st_size > MAX:
        finish(2, problem=f"{name} over {MAX} bytes")
    return path.read_text(errors="replace")


def load():
    m = re.fullmatch(r"rc=(\d+)\s*", read(f"{ARM}.rc"))
    if not m:
        finish(2, problem="malformed rc")
    out, app = read(f"{ARM}.log"), read(f"{ARM}/app.log")
    if "panicked" in out or "panicked" in app:
        finish(2, problem="the app panicked")
    samples = [json.loads(l.split(" ", 2)[2]) for l in app.splitlines() if l.startswith("perf ROWS-SAMPLE ")]
    events = [json.loads(l.split(" ", 2)[2]) for l in app.splitlines() if l.startswith("perf ROWS-MEASURE ")]
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    e2e = [l.strip() for l in out.splitlines() if l.startswith("E2E_")]
    empty = re.search(r"^ok   the empty composer is drawn: \([^)]*, ([0-9.]+)\)$", out, re.M)
    if not samples or not events or not empty:
        finish(2, problem=f"samples {len(samples)}, measure events {len(events)}, empty line {bool(empty)}")
    return int(m.group(1)), fails, e2e, samples, events, float(empty.group(1))


def correlate(samples, events):
    """Each sample whose row value is a SUPERSEDED report of that row (a later
    report of the same row and batch differs), with the reports for that row
    that arrived during the sample's composer read."""
    by_row = {}
    for e in events:
        by_row.setdefault(e["row_id"], []).append(e)
    found = []
    for s in samples:
        rid, row = s["row_id"], s["row"]
        if not rid or row is None:
            continue
        mine = [e for e in by_row.get(rid, []) if e["seq"] <= s["seq_after"]]
        if not mine:
            continue
        last = mine[-1]
        later = [e for e in by_row.get(rid, []) if e["seq"] > s["seq_after"] and e["batch"] == last["batch"]]
        provisional = abs(last["height"] - row) < 0.5 and any(abs(e["height"] - row) >= 0.5 for e in later)
        if provisional:
            inside = [e for e in mine if s["seq_before"] < e["seq"] <= s["seq_after"]]
            found.append({"at_ms": round(s["at_ms"], 1), "row_id": rid, "pane": s["pane"], "batch": last["batch"],
                          "row": row, "final": later[-1]["height"], "inside_reports": len(inside),
                          "report_seq": last["seq"], "seq_before": s["seq_before"], "seq_after": s["seq_after"]})
    return found


def r_walk():
    rc, fails, e2e, samples, events, empty = load()
    if any(s["mode"] != "walk" for s in samples):
        finish(5, problem="r-walk ran with the handle sampler")
    row_fails = [f for f in fails if f.startswith(ROW_FAILS)]
    prov = correlate(samples, events)
    inside = [p for p in prov if p["inside_reports"] > 0]
    base = dict(rc=rc, e2e=e2e, fails=fails, samples=len(samples), events=len(events),
                inside_events=sum(e["inside"] for e in events), provisional_samples=prov[:20], provisional_count=len(prov))
    if not row_fails:
        finish(4, verdict="NOT-REPRODUCED", note="no row-height FAIL this run: UNKNOWN, not a refutation", **base)
    if not prov:
        finish(4, verdict="REPRODUCED-UNCORRELATED", note="row FAIL without a sample on a superseded report: cause UNKNOWN", **base)
    finish(0, verdict="REPRODUCED-INSIDE" if inside else "REPRODUCED-GAP",
           note=("a superseded (provisional) row height was read in a sample whose composer read saw that row's report arrive"
                 if inside else "a superseded row height was read between its report and the correction, with no report during the read"),
           **base)


def r_handle():
    rc, fails, e2e, samples, events, empty = load()
    if any(s["mode"] != "handle" for s in samples):
        finish(5, problem="r-handle ran with the walk sampler")
    ok = rc == 0 and e2e == ["E2E_PASS"] and not fails
    inside = sum(e["inside"] for e in events)
    nan = sum(1 for s in samples if s["composer"] is None or s["composer"] != s["composer"])
    prov = correlate(samples, events)
    v = dict(rc=rc, e2e=e2e, fails=fails, samples=len(samples), events=len(events), inside_events=inside,
             composer_nan=nan, provisional_count=len(prov), provisional_samples=prov[:20])
    finish(0 if ok and inside == 0 and nan == 0 else 5, verdict="GREEN" if ok and inside == 0 and nan == 0 else "unexpected", **v)


def r_neg():
    rc, fails, e2e, samples, events, empty = load()
    comp = [f for f in fails if f.startswith(COMPOSER_FAIL)]
    first = float(comp[0].split("[", 1)[1].split(",")[0].rstrip("]")) if comp else None
    ok = (rc != 0 and e2e == ["E2E_FAIL"] and len(fails) == 1 and len(comp) == 1 and first is not None and first > empty + 1.0
          and all(s["mode"] == "handle" for s in samples))
    finish(0 if ok else 5, verdict="NEG-FAILS-AS-REQUIRED" if ok else "NEG-INCONCLUSIVE",
           rc=rc, e2e=e2e, fails=fails, first_reported=first, empty=empty)


{"r-walk": r_walk, "r-handle": r_handle, "r-neg": r_neg}.get(ARM, lambda: finish(2, problem="unknown arm"))()
