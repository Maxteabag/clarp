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


TEST_HEADER = re.compile(r"^test (\S+)( - should panic)? \.\.\. ?(.*)$")
PANIC_AT = re.compile(r"^thread '([^']+)' \(\d+\) panicked at engine/tests/host_flow\.rs:(\d+):(\d+):$")


def fa_blocks(log):
    """libtest's output as test blocks. With --nocapture a test's own output
    sits between its 'test NAME ... ' header and its status, which is the
    block's last non-empty line ('ok' alone, or after '... ' when the test
    printed nothing). A block ends at the next header or at the blank line
    before 'test result:'. Returns (blocks, lines outside any block)."""
    lines = log.splitlines()
    try:
        start = lines.index("running 4 tests")
    except ValueError:
        return None, ["no 'running 4 tests' line"]
    end = next((n for n in range(start, len(lines)) if lines[n].startswith("test result: ")), None)
    if end is None:
        return None, ["no 'test result:' line"]
    blocks, stray = [], []
    for line in lines[start + 1:end]:
        m = TEST_HEADER.match(line)
        if m:
            blocks.append({"name": m.group(1), "should_panic": bool(m.group(2)), "body": [m.group(3)] if m.group(3) else []})
        elif blocks:
            blocks[-1]["body"].append(line)
        elif line.strip():
            stray.append(line)
    for b in blocks:
        body = [l for l in b["body"] if l.strip()]
        b["status"] = body[-1].strip() if body else ""
        b["output"] = body[:-1]
    return blocks, stray


def fa():
    log, code = read("fa.log"), rc("fa")
    if "error[" in log or "could not compile" in log:
        finish(2, problem="compile error")
    source = HOST_FLOW.read_text().splitlines()
    red_line = next((n for n, l in enumerate(source, 1) if "// FAFB-RED-UNWRAP" in l), None)
    until_line = next((n for n, l in enumerate(source, 1) if 'assert!(Instant::now() < deadline, "timed out waiting for: {what}' in l), None)
    problems = []
    if code != 0:
        problems.append(f"cargo test rc {code}")
    if red_line is None or until_line is None:
        finish(2, problem="the RED read or Driver::until's assert is not in host_flow.rs")
    summaries = re.findall(r"^test result: .*$", log, re.M)
    if not summaries:
        finish(2, problem="no 'test result:' line")
    if len(summaries) != 1 or not summaries[0].startswith("test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 10 filtered out;"):
        problems.append(f"summary lines {summaries}")
    if log.count("panicked at") != 3:
        problems.append(f"{log.count('panicked at')} panics in the whole log (want exactly 3: RED + 2 negatives)")
    blocks, stray = fa_blocks(log)
    if blocks is None:
        finish(2, problem=stray[0])
    if stray:
        problems.append(f"output before the first test: {stray[:3]}")
    names = [b["name"] for b in blocks]
    if sorted(names) != sorted(FA_TESTS) or len(set(names)) != len(names):
        problems.append(f"tests {names} (want each of {sorted(FA_TESTS)} exactly once)")
    want = {
        "fafb_red_the_current_read_comes_before_the_select_record":
            ("FAFB-RED-ORDER log_served=1 select_held=1 select_records=0", red_line, "called `Option::unwrap()` on a `None` value"),
        "fafb_green_the_fixed_test_waits_for_the_select_record":
            ("FAFB-GREEN-ORDER log_served=1 select_held=1 select_records=0", None, None),
        "fafb_neg_a_select_never_recorded_fails_the_fixed_test":
            (None, until_line, "timed out waiting for: the Host was told rachel is selected (error: "),
        "fafb_neg_another_session_selected_fails_the_fixed_test":
            (None, until_line, "timed out waiting for: the Host was told rachel is selected (error: "),
    }
    for b in blocks:
        name = b["name"]
        if name not in want:
            continue
        marker, line, message = want[name]
        if b["should_panic"] != FA_TESTS[name]:
            problems.append(f"{name}: should-panic flag {b['should_panic']} (want {FA_TESTS[name]})")
        if b["status"] != "ok":
            problems.append(f"{name}: status {b['status']!r} (want 'ok')")
        out = b["output"]
        markers = [l for l in out if l.startswith("FAFB-")]
        if markers != ([marker] if marker else []):
            problems.append(f"{name}: markers {markers} (want {[marker] if marker else []})")
        panics = [(n, PANIC_AT.match(l)) for n, l in enumerate(out) if "panicked at" in l]
        if line is None:
            if panics:
                problems.append(f"{name}: unexpected panic {out[panics[0][0]]!r}")
            continue
        if len(panics) != 1 or not panics[0][1]:
            problems.append(f"{name}: {len(panics)} panics (want exactly 1 in host_flow.rs)")
            continue
        n, m = panics[0]
        if m.group(1) != name or int(m.group(2)) != line:
            problems.append(f"{name}: panicked in {m.group(1)} at l.{m.group(2)} (want {name} at l.{line})")
        if n + 1 >= len(out) or not out[n + 1].startswith(message):
            problems.append(f"{name}: panic message {out[n + 1] if n + 1 < len(out) else None!r} (want {message!r}...)")
        others = [l for l in out[n + 2:] if not l.startswith("note: run with `RUST_BACKTRACE=1`")]
        if others:
            problems.append(f"{name}: unexpected output after the panic {others[:2]}")
    if problems:
        finish(5, problems=problems)
    finish(0, red_line=red_line, until_line=until_line, tests=names,
           result="RED reproduced causally; GREEN waits for the record; both negatives fail")


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
