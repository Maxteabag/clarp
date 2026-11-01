#!/usr/bin/env python3
"""ROWS scratch proof only (branch proof/desktop-rows-20261101, never merged).

Per-arm oracle over the arm's two runs (first, rerun): python3 rows/oracle.py ARM DIR
ARM = walk | bridge | fade | handle | neg | summary. Run k of ARM reads
DIR/r-ARM-k.rc, DIR/r-ARM-k.log (check.sh output) and DIR/r-ARM-k/app.log
(ROWS-SAMPLE / ROWS-MEASURE / ROWS-HANDLE lines). Writes DIR/ARM.verdict.
Observation arms (walk, bridge, fade) record what happened and exit 0 when
their data is complete; handle must be GREEN and neg must FAIL as required.
Exit: 0 expected/recorded, 2 missing/malformed/panic, 5 unexpected.
A row read is RELEVANT only for the sample's own pane, row id and batch.
"""
import json
import pathlib
import re
import sys

ARM, DIR = sys.argv[1], pathlib.Path(sys.argv[2])
ROW_FAILS = ("FAIL the row's height never changed from unsent to sent:", "FAIL the late line grows the row once")
COMPOSER_FAIL = "FAIL and the composer is empty at once: ["
MODES = {"walk": "walk", "bridge": "bridge", "fade": "fade", "handle": "handle", "neg": "handle"}
MAX = 20 * 1024 * 1024


def finish(code, **verdict):
    verdict.update(arm=ARM, exit=code)
    (DIR / f"{ARM}.verdict").write_text(json.dumps(verdict) + "\n")
    print(f"ROWS-ORACLE {json.dumps(verdict)[:4000]}")
    sys.exit(code)


def read(name):
    path = DIR / name
    if not path.is_file():
        finish(2, problem=f"missing {name}")
    if path.stat().st_size > MAX:
        finish(2, problem=f"{name} over {MAX} bytes")
    return path.read_text(errors="replace")


def load(run):
    stem = f"r-{ARM}-{run}"
    m = re.fullmatch(r"rc=(\d+)\s*", read(f"{stem}.rc"))
    if not m:
        finish(2, problem=f"malformed {stem}.rc")
    out, app = read(f"{stem}.log"), read(f"{stem}/app.log")
    if "panicked" in out or "panicked" in app:
        finish(2, problem=f"{stem}: the app panicked")
    grab = lambda tag: [json.loads(l.split(" ", 2)[2]) for l in app.splitlines() if l.startswith(f"perf {tag} ")]
    samples, events, handles = grab("ROWS-SAMPLE"), grab("ROWS-MEASURE"), grab("ROWS-HANDLE")
    if grab("ROWS-CAP"):
        finish(2, problem=f"{stem}: a sample or event cap was hit (incomplete record)", caps=grab("ROWS-CAP"))
    empty = re.search(r"^ok   the empty composer is drawn: \([^)]*, ([0-9.]+)\)$", out, re.M)
    if not samples or not events or not empty:
        finish(2, problem=f"{stem}: samples {len(samples)}, measure events {len(events)}, empty line {bool(empty)}")
    if any(s["mode"] != MODES[ARM] for s in samples):
        finish(5, problem=f"{stem}: sampler mode is not {MODES[ARM]}")
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    e2e = [l.strip() for l in out.splitlines() if l.startswith("E2E_")]
    oks = [l[5:] for l in out.splitlines() if l.startswith("ok   ")]
    return dict(rc=int(m.group(1)), fails=fails, e2e=e2e, samples=samples, events=events, handles=handles,
                empty=float(empty.group(1)), oks=oks)


def label(text):
    """An ok line's label: the text before ':', else the text with numbers as '#'
    (the same transform that froze rows/required_ok.txt)."""
    return (text.split(":")[0] if ":" in text else re.sub(r"[-+]?\d+(\.\d+)?", "#", text)).strip()


REQUIRED = [l.strip() for l in (pathlib.Path(__file__).resolve().parent / "required_ok.txt").read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def provisional(r):
    """Samples that read a SUPERSEDED report of their own pane/row/batch, with the
    relevant reports before, inside and after that sample's composer read."""
    out = []
    for s in r["samples"]:
        if not s["row_id"] or s["row"] is None:
            continue
        rel = [e for e in r["events"] if e["row_id"] == s["row_id"] and e["pane"] == s["pane"]]
        upto = [e for e in rel if e["seq"] <= s["seq_after"]]
        if not upto:
            continue
        last = upto[-1]
        later = [e for e in rel if e["seq"] > s["seq_after"] and e["batch"] == last["batch"]]
        if abs(last["height"] - s["row"]) < 0.5 and any(abs(e["height"] - s["row"]) >= 0.5 for e in later):
            same = [e for e in rel if e["batch"] == last["batch"]]
            out.append({"index": s["index"], "send": s["send"], "at_ms": round(s["at_ms"], 1), "pane": s["pane"], "row_id": s["row_id"],
                        "batch": last["batch"], "sample_batch": s["batch"], "row": s["row"], "final": later[-1]["height"],
                        "before": sum(e["seq"] <= s["seq_before"] for e in same),
                        "inside": sum(s["seq_before"] < e["seq"] <= s["seq_after"] for e in same),
                        "after": sum(e["seq"] > s["seq_after"] for e in same)})
    return out


# The send check's order: send 1 is the 0.8 s confirm ('never changed' FAIL),
# send 2 the 2.9 s send ('late line' FAIL).
NEVER = re.compile(r"^FAIL the row's height never changed from unsent to sent: ([0-9.]+)\.\.([0-9.]+)px over \d+ frames$")
LATE = "FAIL the late line grows the row once"


def classify(r):
    prov = provisional(r)
    row_fails = [f for f in r["fails"] if f.startswith(ROW_FAILS)]
    per_fail = []
    for f in row_fails:
        m = NEVER.match(f)
        if m:
            low, high = float(m.group(1)), float(m.group(2))
            cand = [p for p in prov if p["send"] == 1 and abs(p["row"] - low) < 0.5 and abs(p["final"] - high) < 0.5]
        else:
            cand = [p for p in prov if p["send"] == 2]
        status = "UNMATCHED" if not cand else ("INSIDE" if any(p["inside"] > 0 for p in cand) else "GAP")
        per_fail.append({"fail": f, "status": status, "matched": cand[:5]})
    only_rows = r["fails"] and all(f.startswith(ROW_FAILS) for f in r["fails"])
    if not row_fails:
        cls = "NO-ROW-FAIL"
    elif not (r["rc"] != 0 and r["e2e"] == ["E2E_FAIL"] and only_rows):
        cls = "OTHER-FAILS"               # row FAILs with other FAILs or a wrong rc/E2E: not this symptom alone
    elif any(p["status"] == "UNMATCHED" for p in per_fail):
        cls = "NOT-REPRODUCED-UNCORRELATED"
    elif all(p["status"] == "INSIDE" for p in per_fail):
        cls = "REPRODUCED"                # each row FAIL matched by its own send's provisional read with a relevant report inside the composer read
    else:
        cls = "SYMPTOM-GAP-CAUSE-UNKNOWN"  # matched provisional reads, but no relevant report during the read
    return dict(cls=cls, rc=r["rc"], e2e=r["e2e"], fails=r["fails"], per_fail=per_fail, samples=len(r["samples"]),
                events=len(r["events"]), inside_any=sum(e["inside"] for e in r["events"]),
                app_dropped=sum(e.get("app_dropped", False) for e in r["events"]), provisional=prov[:20],
                provisional_count=len(prov), composer_fail=any(f.startswith(COMPOSER_FAIL) for f in r["fails"]))


def observe():
    runs = {k: classify(load(k)) for k in ("1", "2")}
    finish(0, verdict="RECORDED", runs=runs)


def handle():
    runs, bad = {}, []
    for k in ("1", "2"):
        r = load(k)
        c = classify(r)
        h = r["handles"]
        nan = sum(1 for s in r["samples"] if s["composer"] is None)
        bound = len(h) == 1 and h[0]["found"] and h[0]["handle_height"] is not None and abs(h[0]["handle_height"] - r["empty"]) < 0.5
        have = {label(o) for o in r["oks"]}
        missing = [l for l in REQUIRED if l not in have]
        green = (r["rc"] == 0 and r["e2e"] == ["E2E_PASS"] and not r["fails"] and not missing and c["inside_any"] == 0
                 and nan == 0 and bound)
        runs[k] = dict(c, nan=nan, handle=h, handle_same_source_as_empty=bound, missing_required_ok=missing, green=green)
        if not green:
            bad.append(k)
    finish(0 if not bad else 5, verdict="GREEN" if not bad else "unexpected", not_green_runs=bad, runs=runs)


def neg():
    runs, bad = {}, []
    for k in ("1", "2"):
        r = load(k)
        comp = [f for f in r["fails"] if f.startswith(COMPOSER_FAIL)]
        first = float(comp[0].split("[", 1)[1].split(",")[0].rstrip("]")) if comp else None
        ok = (r["rc"] != 0 and r["e2e"] == ["E2E_FAIL"] and len(r["fails"]) == 1 and len(comp) == 1
              and first is not None and first > r["empty"] + 1.0 and abs(first - 66.0) < 1.0)
        runs[k] = dict(rc=r["rc"], fails=r["fails"], first_reported=first, empty=r["empty"], ok=ok)
        if not ok:
            bad.append(k)
    finish(0 if not bad else 5, verdict="NEG-FAILS-AS-REQUIRED" if not bad else "NEG-INCONCLUSIVE", runs=runs)


def summary():
    v = {}
    for arm in ("walk", "bridge", "fade", "handle", "neg"):
        p = DIR / f"{arm}.verdict"
        v[arm] = json.loads(p.read_text()) if p.is_file() else None
    if any(x is None or x["exit"] != 0 for x in v.values()):
        finish(2, problem="an arm verdict is missing or not exit 0", arms={a: (x and x["exit"]) for a, x in v.items()})
    cls = {a: [v[a]["runs"][k]["cls"] for k in ("1", "2")] for a in ("walk", "bridge", "fade")}
    finish(0, walk=cls["walk"], bridge=cls["bridge"], fade=cls["fade"],
           mechanism=("INSIDE (a relevant measurer report during the walk read) in " + str(cls["walk"].count("REPRODUCED")) + "/2 walk runs")
           if "REPRODUCED" in cls["walk"] else "NOT REPRODUCED as inside: cause UNKNOWN (see SYMPTOM-GAP / UNCORRELATED / OTHER-FAILS)",
           c1="GREEN in both runs with 0 relevant inside reports; NEG fails as required in both")


{"walk": observe, "bridge": observe, "fade": observe, "handle": handle, "neg": neg, "summary": summary}.get(
    ARM, lambda: finish(2, problem="unknown arm"))()
