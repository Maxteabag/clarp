#!/usr/bin/env python3
"""FAFB scratch proof only (branch proof/desktop-fafb-20261101, never merged).

Per-step oracle for the one dispatch: python3 fafb/oracle.py STEP DIR
STEP is fa | b-obs | b-green | b-neg | size. Each step's expectation is fixed
here; anything else exits non-zero, so the workflow stops before the next step.
Writes DIR/<step>.verdict (one JSON line). Exit codes: 0 expected, 2 missing or
malformed output, 3 L (a real late clear), 4 UNKNOWN, 5 unexpected outcome.
"""
import json
import os
import pathlib
import re
import sys

STEP, DIR = sys.argv[1], pathlib.Path(sys.argv[2])
HOST_FLOW = pathlib.Path(__file__).resolve().parent.parent / "engine" / "tests" / "host_flow.rs"
FA_TESTS = {
    "fafb_red_the_current_read_comes_before_the_select_record": True,      # should_panic
    "fafb_green_the_fixed_test_waits_for_the_select_record": False,
    "fafb_neg_a_select_never_recorded_fails_the_fixed_test": True,
    "fafb_neg_another_session_selected_fails_the_fixed_test": True,
}
MAX_LOG_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 80 * 1024 * 1024


def finish(code, **verdict):
    verdict.update(step=STEP, exit=code)
    (DIR / f"{STEP}.verdict").write_text(json.dumps(verdict) + "\n")
    print(f"FAFB-ORACLE {json.dumps(verdict)}")
    sys.exit(code)


def read(name):
    path = DIR / name
    if not path.is_file():
        finish(2, problem=f"missing {name}")
    if path.stat().st_size > MAX_LOG_BYTES:
        finish(2, problem=f"{name} over {MAX_LOG_BYTES} bytes")
    return path.read_text(errors="replace")


def rc(name):
    m = re.fullmatch(r"rc=(\d+)\s*", read(f"{name}.rc"))
    if not m:
        finish(2, problem=f"malformed {name}.rc")
    return int(m.group(1))


def fa():
    log, code = read("fa.log"), rc("fa")
    if "error[" in log or "could not compile" in log:
        finish(2, problem="compile error")
    red_line = next((n for n, l in enumerate(HOST_FLOW.read_text().splitlines(), 1) if "// FAFB-RED-UNWRAP" in l), None)
    problems = []
    if code != 0:
        problems.append(f"cargo test rc {code}")
    if not re.search(r"^test result: ok\. 4 passed; 0 failed; 0 ignored; 0 measured; 10 filtered out", log, re.M):
        problems.append("not exactly 4 passed / 0 failed / 10 filtered out")
    for name, panics in FA_TESTS.items():
        want = rf"^test {name}( - should panic)? \.\.\. ok$"
        line = re.search(want, log, re.M)
        if not line or bool(line.group(1)) != panics:
            problems.append(f"{name}: not '{'should panic ... ok' if panics else 'ok'}'")
    if red_line is None or not re.search(rf"panicked at engine/tests/host_flow\.rs:{red_line}:\d+:\ncalled `Option::unwrap\(\)` on a `None` value", log):
        problems.append(f"RED did not panic at the copied l.102 read (host_flow.rs:{red_line})")
    for marker in ("FAFB-RED-ORDER log_served=1 select_held=1 select_records=0", "FAFB-GREEN-ORDER log_served=1 select_held=1 select_records=0"):
        if log.count(marker) != 1:
            problems.append(f"ordering marker {marker.split()[0]} seen {log.count(marker)} times (want 1)")
    neg = log.count("timed out waiting for: the Host was told rachel is selected")
    if neg != 2:
        problems.append(f"negative-control panic seen {neg} times (want 2)")
    if problems:
        finish(5, problems=problems)
    finish(0, red_line=red_line, result="RED reproduced causally; GREEN waits for the record; both negatives fail")


def check_step(name):
    out, code = read(f"{name}.log"), rc(name)
    app = read(f"{name}/app.log")
    if "panicked" in out or "panicked" in app:
        finish(2, problem="the app panicked")
    frames = [json.loads(l.split(" ", 2)[2]) for l in app.splitlines() if l.startswith("perf FAFB-FRAME ")]
    samples = [json.loads(l.split(" ", 2)[2]) for l in app.splitlines() if l.startswith("perf FAFB-SAMPLE ")]
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    e2e = [l.strip() for l in out.splitlines() if l.startswith("E2E_")]
    if len(frames) != 1:
        finish(2, problem=f"{len(frames)} FAFB-FRAME lines (want 1)")
    if not samples:
        finish(2, problem="no FAFB-SAMPLE lines (the per-sample record is mandatory)")
    f = frames[0]
    empty, bridge, drawn = f["empty"], f["bridge"], f["drawn"]
    bound = f["phase"] == "fade" and f["row_id"] != "" and f["sample_index"] == 0 and f["frame"] > f["frames_at_send"]
    if not bound or empty <= 20 or drawn is None:
        finish(4, problem="the capture is not bound to the send's first drawn fade frame", frame=f)
    composer_fail = [l for l in fails if l.startswith("FAIL and the composer is empty at once: [")]
    first_reported = None
    if composer_fail:
        first_reported = float(composer_fail[0].split("[", 1)[1].split(",")[0].rstrip("]"))
    return dict(rc=code, e2e=e2e, fails=fails, frame=f, samples=len(samples), empty=empty, bridge=bridge, drawn=drawn,
                first_reported=first_reported, composer_fail=bool(composer_fail))


def b_obs():
    r = check_step("b-obs")
    f = r["frame"]
    if f["b2"] or f["force_tall"]:
        finish(5, problem="b-obs ran with B2 or FORCE_TALL set", **r)
    late = r["drawn"] > r["empty"] + 1.0
    on_time = abs(r["drawn"] - r["empty"]) <= 1.0
    if late:
        finish(3, verdict="L", note="the drawn composer was not empty on the first fade frame: a real late clear; B2 steps not run", **r)
    if not on_time:
        finish(4, verdict="UNKNOWN", note="drawn height neither empty nor taller", **r)
    red = (r["rc"] != 0 and r["e2e"] == ["E2E_FAIL"] and r["fails"] == [l for l in r["fails"] if l.startswith("FAIL and the composer is empty at once: [")]
           and len(r["fails"]) == 1 and f["ticks_since_send"] == 0 and r["bridge"] > r["empty"] + 1.0
           and r["first_reported"] is not None and abs(r["first_reported"] - r["bridge"]) < 0.01)
    if not red:
        finish(4, verdict="M-but-RED-not-reproduced", note="drawn was empty, but the stale-bridge RED did not appear as the only failure", **r)
    finish(0, verdict="M", note="drawn empty at the first fade frame while the bridge was stale (ticks 0): the current assertion read the bridge", **r)


def b_green():
    r = check_step("b-green")
    ok = (r["rc"] == 0 and r["e2e"] == ["E2E_PASS"] and not r["fails"] and r["frame"]["b2"] and not r["frame"]["force_tall"]
          and abs(r["drawn"] - r["empty"]) <= 1.0)
    finish(0 if ok else 5, verdict="GREEN" if ok else "unexpected", **r)


def b_neg():
    r = check_step("b-neg")
    ok = (r["rc"] != 0 and r["e2e"] == ["E2E_FAIL"] and len(r["fails"]) == 1 and r["composer_fail"]
          and r["frame"]["b2"] and r["frame"]["force_tall"] and r["drawn"] > r["empty"] + 1.0
          and r["first_reported"] is not None and abs(r["first_reported"] - r["drawn"]) < 0.01)
    finish(0 if ok else 5, verdict="NEG-FAILS-AS-REQUIRED" if ok else "unexpected", **r)


def size():
    total = sum(p.stat().st_size for p in DIR.rglob("*") if p.is_file())
    finish(0 if total <= MAX_TOTAL_BYTES else 5, total_bytes=total, cap=MAX_TOTAL_BYTES)


{"fa": fa, "b-obs": b_obs, "b-green": b_green, "b-neg": b_neg, "size": size}.get(STEP, lambda: finish(2, problem="unknown step"))()
