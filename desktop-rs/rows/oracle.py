#!/usr/bin/env python3
"""ROWS scratch proof only (branch proof/desktop-rows-20261101, never merged).

Per-arm oracle over the arm's ONE run: python3 rows/oracle.py ARM DIR
ARM = walk | handle | neg | summary. The run reads DIR/r-ARM-1.rc, DIR/r-ARM-1.log
(check.sh output) and DIR/r-ARM-1/app.log (ROWS-FLUSH / ROWS-SAMPLE / ROWS-MEASURE /
ROWS-HANDLE / ROWS-STAGE lines). Writes DIR/ARM.verdict.
walk records what happened and exits 0 when its data is complete; handle must be
GREEN and neg must FAIL as required.
Exit: 0 expected/recorded, 2 INCOMPLETE (missing/malformed data, overflow, a timeout
kill, no E2E line, a panic), 5 unexpected.
Verdict semantics (coord 747c82f3 s.1): CORRELATED-INSIDE / CORRELATED-GAP only say a
SUPERSEDED row height was read and correlated with the failing assertion. They do NOT
show that the composer query caused it: query causality is UNKNOWN. A C1 GREEN shows
only that C1 passes in that one run; it does not show that the walk was the cause.
"""
import json
import pathlib
import re
import sys

ARM, DIR = sys.argv[1], pathlib.Path(sys.argv[2])
RUN = "1"   # ROOT: one run per arm
ROW_FAILS = ("FAIL the row's height never changed from unsent to sent:", "FAIL the late line grows the row once")
NEVER = re.compile(r"^FAIL the row's height never changed from unsent to sent: ([0-9.]+)\.\.([0-9.]+)px over \d+ frames$")
LATE = "FAIL the late line grows the row once"
# coord 747c82f3 s.2: each row FAIL is scoped to the stage that judged it.
FAIL_STAGE = {"never": "quick confirmed", "late": "slow confirmed"}
COMPOSER_FAIL = "FAIL and the composer is empty at once: ["
MODES = {"walk": "walk", "handle": "handle", "neg": "handle"}
MAX = 20 * 1024 * 1024
TIMEOUT_RCS = (124, 137)    # `timeout` expiry / KILL


def finish(code, **verdict):
    verdict.update(arm=ARM, exit=code)
    (DIR / f"{ARM}.verdict").write_text(json.dumps(verdict) + "\n")
    print(f"ROWS-ORACLE {json.dumps(verdict)[:4000]}")
    sys.exit(code)


def read(name, required=True):
    path = DIR / name
    if not path.is_file():
        if required:
            finish(2, problem=f"INCOMPLETE: missing {name}")
        return None
    if path.stat().st_size > MAX:
        finish(2, problem=f"INCOMPLETE: {name} over {MAX} bytes")
    return path.read_text(errors="replace")


def label(text):
    """An ok line's label: the text before ':' (or all of it), with every number as '#'
    (the same transform that froze rows/required_ok.txt; run-to-run counts never matter)."""
    return re.sub(r"[-+]?\d+(\.\d+)?", "#", text.split(":")[0] if ":" in text else text).strip()


REQUIRED = [l.strip() for l in (pathlib.Path(__file__).resolve().parent / "required_ok.txt").read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def load():
    stem = f"r-{ARM}-{RUN}"
    m = re.fullmatch(r"rc=(\d+)\s*", read(f"{stem}.rc"))
    if not m:
        finish(2, problem=f"INCOMPLETE: malformed {stem}.rc")
    rc = int(m.group(1))
    out = read(f"{stem}.log")
    e2e = [l.strip() for l in out.splitlines() if l.startswith("E2E_")]
    if rc in TIMEOUT_RCS or not e2e:
        partial = read(f"{stem}/app.partial.log", required=False)
        finish(2, problem=f"INCOMPLETE: {stem} timed out or was killed (rc {rc}, E2E lines {e2e}); not proof",
               partial_log_bytes=None if partial is None else len(partial))
    app = read(f"{stem}/app.log")
    if "panicked" in out or "panicked" in app:
        finish(2, problem=f"INCOMPLETE: {stem}: the app panicked")
    stage, taken, samples, events, handles, overflow = None, set(), [], [], [], []
    for line in app.splitlines():
        if not line.startswith("perf ROWS-"):
            continue
        tag, body = line.split(" ", 2)[1:]
        obj = json.loads(body)
        if tag == "ROWS-FLUSH":
            stage = obj["stage"]
            taken = set(obj["taken"])
        elif tag == "ROWS-SAMPLE":
            obj["flush"] = stage
            samples.append(obj)
        elif tag == "ROWS-MEASURE":
            obj["flush"] = stage
            obj["taken"] = obj["seq"] in taken
            events.append(obj)
        elif tag == "ROWS-HANDLE":
            handles.append(obj)
        elif tag == "ROWS-OVERFLOW":
            overflow.append(obj)
    if overflow:
        finish(2, problem=f"INCOMPLETE: {stem}: overflow (data dropped)", overflow=overflow)
    empty = re.search(r"^ok   the empty composer is drawn: \([^)]*, ([0-9.]+)\)$", out, re.M)
    if not samples or not events or not empty:
        finish(2, problem=f"INCOMPLETE: {stem}: samples {len(samples)}, measure events {len(events)}, empty line {bool(empty)}")
    if any(s["mode"] != MODES[ARM] for s in samples):
        finish(5, problem=f"{stem}: sampler mode is not {MODES[ARM]}")
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    oks = [l[5:] for l in out.splitlines() if l.startswith("ok   ")]
    return dict(rc=rc, fails=fails, e2e=e2e, oks=oks, samples=samples, events=events, handles=handles,
                empty=float(empty.group(1)))


def superseded(r, flush):
    """Samples of stage `flush` that read a SUPERSEDED height: the value the book
    held came from its last TAKEN report for (pane, row), that report's batch is
    the sample's row_batch, and a later TAKEN report of the same (pane, batch, row)
    differs. Counted only with NO input change: the same stage later reads that
    final height with the same status, pending and failed flags (and the same row
    id, by the key). For each: the relevant reports (same pane, batch, row) before
    / inside / after the sample's composer read, and any relevant report the app
    dropped (app_dropped)."""
    out = []
    taken = [e for e in r["events"] if e["taken"]]
    stage_samples = [s for s in r["samples"] if s["flush"] == flush]
    for s in stage_samples:
        if not s["row_id"] or s["row"] is None:
            continue
        mine = [e for e in taken if e["pane"] == s["pane"] and e["row_id"] == s["row_id"]]
        upto = [e for e in mine if e["seq"] <= s["seq_after"]]
        if not upto:
            continue
        last = upto[-1]
        if abs(last["height"] - s["row"]) >= 0.5 or s["row_batch"] != last["batch"]:
            continue
        key = [e for e in mine if e["batch"] == last["batch"]]
        later = [e for e in key if e["seq"] > s["seq_after"] and abs(e["height"] - s["row"]) >= 0.5]
        if not later:
            continue
        final = later[0]["height"]
        settled = [t for t in stage_samples if t["index"] > s["index"] and t["row_id"] == s["row_id"]
                   and t["pane"] == s["pane"] and t["row"] is not None and abs(t["row"] - final) < 0.5]
        if not settled:
            continue
        t = settled[0]
        if (t["status"], t["pending"], t["failed"]) != (s["status"], s["pending"], s["failed"]):
            continue   # an input change explains the height change: not superseded
        dropped = [e for e in r["events"] if e.get("app_dropped") and e["pane"] == s["pane"] and e["row_id"] == s["row_id"]
                   and e["batch"] == last["batch"]]
        out.append({"index": s["index"], "stage": flush, "at_ms": round(s["at_ms"], 1), "pane": s["pane"],
                    "row_id": s["row_id"], "batch": last["batch"], "row": s["row"], "final": final,
                    "status": s["status"], "pending": s["pending"], "failed": s["failed"],
                    "before": sum(e["seq"] <= s["seq_before"] for e in key),
                    "inside": sum(s["seq_before"] < e["seq"] <= s["seq_after"] for e in key),
                    "after": sum(e["seq"] > s["seq_after"] for e in key), "relevant_dropped": len(dropped)})
    return out


def classify(r):
    row_fails = [f for f in r["fails"] if f.startswith(ROW_FAILS)]
    per_fail = []
    for f in row_fails:
        m = NEVER.match(f)
        stage = FAIL_STAGE["never" if m else "late"]
        cand = superseded(r, stage)
        if m:
            low, high = float(m.group(1)), float(m.group(2))
            cand = [p for p in cand if abs(p["row"] - low) < 0.5 and abs(p["final"] - high) < 0.5]
        if not cand:
            status = "UNMATCHED"
        elif any(p["relevant_dropped"] for p in cand):
            status = "UNKNOWN-RELEVANT-DROPPED"
        elif any(p["inside"] > 0 for p in cand):
            status = "INSIDE"
        else:
            status = "GAP"
        per_fail.append({"fail": f, "stage": stage, "status": status, "matched": cand[:5]})
    only_rows = bool(r["fails"]) and all(f.startswith(ROW_FAILS) for f in r["fails"])
    statuses = {p["status"] for p in per_fail}
    if not row_fails:
        cls = "NO-ROW-FAIL"
    elif not (r["rc"] != 0 and r["e2e"] == ["E2E_FAIL"] and only_rows):
        cls = "OTHER-FAILS"
    elif statuses & {"UNMATCHED", "UNKNOWN-RELEVANT-DROPPED"}:
        cls = "NOT-REPRODUCED-UNCORRELATED"    # cause UNKNOWN
    elif statuses == {"INSIDE"}:
        cls = "CORRELATED-INSIDE"               # correlation only; query causality UNKNOWN
    else:
        cls = "CORRELATED-GAP"                  # correlation only; cause UNKNOWN
    return dict(cls=cls, rc=r["rc"], e2e=r["e2e"], fails=r["fails"], per_fail=per_fail, samples=len(r["samples"]),
                events=len(r["events"]), inside_any=sum(e["inside"] for e in r["events"]),
                app_dropped=sum(bool(e.get("app_dropped")) for e in r["events"]),
                composer_fail=any(f.startswith(COMPOSER_FAIL) for f in r["fails"]))


def walk():
    c = classify(load())
    finish(0, verdict="RECORDED", run=c, note="correlation only; query causality UNKNOWN")


def handle():
    r = load()
    c = classify(r)
    h = r["handles"]
    nan = sum(1 for s in r["samples"] if s["composer"] is None)
    bound = len(h) == 1 and h[0]["found"] and h[0]["handle_height"] is not None and abs(h[0]["handle_height"] - r["empty"]) < 0.5
    missing = [l for l in REQUIRED if l not in {label(o) for o in r["oks"]}]
    green = (r["rc"] == 0 and r["e2e"] == ["E2E_PASS"] and not r["fails"] and not missing and c["inside_any"] == 0
             and nan == 0 and bound)
    finish(0 if green else 5, verdict="GREEN" if green else "unexpected", run=dict(c, nan=nan, handle=h,
           handle_same_source_as_empty=bound, missing_required_ok=missing),
           note="GREEN shows only that C1 passes in this one run; it does not show the walk was the cause")


def neg():
    r = load()
    comp = [f for f in r["fails"] if f.startswith(COMPOSER_FAIL)]
    first = float(comp[0].split("[", 1)[1].split(",")[0].rstrip("]")) if comp else None
    # Bound to the stage: the Reduce-Motion fade send's samples, the FIRST one's composer == the FAIL's first value.
    # ... flushed by the 'faded' stage, which judges 'and the composer is empty at once' (send_checks l.691 at 53fd).
    fade = [s for s in r["samples"] if s["mode"] == "handle" and s["reduced"] and s["phase"] == "fade" and s["flush"] == "faded"]
    send = min((s["send"] for s in fade), default=None)
    stage = sorted((s for s in r["samples"] if s["send"] == send), key=lambda s: s["index"])
    first_fade = next((s for s in stage if s["phase"] == "fade"), None)
    nan = sum(1 for s in stage if s["composer"] is None)
    bound = first_fade is not None and first is not None and first_fade["composer"] is not None and abs(first_fade["composer"] - first) < 0.01
    ok = (r["rc"] != 0 and r["e2e"] == ["E2E_FAIL"] and len(r["fails"]) == 1 and len(comp) == 1 and first is not None
          and first > r["empty"] + 1.0 and abs(first - 66.0) < 1.0 and bound and nan == 0)
    finish(0 if ok else 5, verdict="NEG-FAILS-AS-REQUIRED" if ok else "NEG-INCONCLUSIVE",
           run=dict(rc=r["rc"], fails=r["fails"], first_reported=first, empty=r["empty"], stage_send=send,
                    stage_first_fade_sample=first_fade, stage_nan=nan, bound_to_stage=bound))


def summary():
    v = {}
    for arm in ("walk", "handle", "neg"):
        p = DIR / f"{arm}.verdict"
        v[arm] = json.loads(p.read_text()) if p.is_file() else None
    if any(x is None or x["exit"] != 0 for x in v.values()):
        finish(2, problem="an arm verdict is missing or not exit 0", arms={a: (x and x["exit"]) for a, x in v.items()})
    w = v["walk"]["run"]["cls"]
    finish(0, walk=w, statement=(f"walk: {w} (correlation only; query causality UNKNOWN). C1 GREEN in this one run "
                                 "(not a causal control). NEG fails as required."))


{"walk": walk, "handle": handle, "neg": neg, "summary": summary}.get(ARM, lambda: finish(2, problem="unknown arm"))()
