"""Judge a tests.yml run for the refactor gate from its downloaded JUnit files.

Usage: gate_judge.py <dir> <conclusion> <run head sha> <candidate sha>
<dir> holds python.xml, javascript.xml and quarantine.txt (the candidate's list).
Exits 0 only for a PASS. Failure and error elements are counted on their own and
must agree with the summary counters, so a report that under-counts never passes.
"""
from __future__ import annotations

import json
import pathlib
import sys
import xml.etree.ElementTree as ET

FILES = ("python.xml", "javascript.xml")
# The tests.yml steps that produce each results file. A failed step can be waived
# only when it is one of these and every failure in its file is quarantined.
TEST_STEPS = {"Python unit, integration and contract tests": "python.xml",
              "JavaScript behavior and contract tests": "javascript.xml"}
FAILED = {"failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale"}
NOT_FAILED = {"success", "skipped", "neutral"}


def load_quarantine(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.split(" #")[0].strip() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def node_ids(classname: str | None, name: str | None) -> set[str]:
    parts = (classname or "").split(".")
    return {"/".join(parts[:i]) + ".py::" + "::".join(parts[i:] + [name or ""])
            for i in range(1, len(parts) + 1)}


def judge_file(path: pathlib.Path, quarantined: set[str]) -> tuple[list[str], list[str], int]:
    """(output lines, problems, tolerated failures) for one JUnit file."""
    lines, problems = [], []
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        return lines, [f"{path.name}: unreadable ({type(error).__name__})"], 0
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    leaves = [s for s in suites if s.find("testsuite") is None]
    if not leaves:
        return lines, [f"{path.name}: no testsuite"], 0
    totals = {k: 0 for k in ("tests", "failures", "errors", "skipped")}
    for suite in leaves:
        if suite.get("tests") is None:
            problems.append(f"{path.name}: a testsuite has no tests counter")
        for k in totals:
            try:
                value = int(suite.get(k) or 0)
            except ValueError:
                value = -1
            if value < 0:
                problems.append(f"{path.name}: bad {k} counter {suite.get(k)!r}")
            totals[k] += max(value, 0)
    cases = list(root.iter("testcase"))
    # pytest's tests counter is not the number of testcase elements, so only the
    # failure and error counters are checked against the elements.
    found = {"failures": sum(len(c.findall("failure")) for c in cases),
             "errors": sum(len(c.findall("error")) for c in cases)}
    lines.append(f"gate: {path.name}: " + " ".join(f"{k}={v}" for k, v in totals.items()))
    for k, n in found.items():
        if totals[k] != n:
            problems.append(f"{path.name}: summary says {k}={totals[k]} but the report has {n}")
        if root.tag == "testsuites" and root.get(k) is not None and int(root.get(k) or 0) != n:
            problems.append(f"{path.name}: testsuites says {k}={root.get(k)} but the report has {n}")
    if not cases or totals["tests"] == 0:
        problems.append(f"{path.name}: no tests")
    tolerated = real = 0
    for case in cases:
        if case.find("failure") is None and case.find("error") is None:
            continue
        hits = node_ids(case.get("classname"), case.get("name")) & quarantined
        if hits:
            tolerated += 1
            lines.append(f"gate:   QUARANTINED {min(hits)}")
        else:
            real += 1
            lines.append(f"gate:   FAILED {case.get('classname')}::{case.get('name')}")
    if real:
        problems.append(f"{path.name}: {real} failures or errors outside the quarantine")
    return lines, problems, tolerated


def unwaived_steps(path: pathlib.Path, tolerated_by_file: dict[str, int]) -> list[str]:
    """Why a failed run is not green apart from quarantined tests (empty when it is).

    Waiving needs positive evidence: complete job and step metadata, at least one
    failed job, and every failed job explained by failed test steps whose results
    files hold only quarantined failures. Anything partial or contradictory fails.
    """
    try:
        jobs = json.loads(path.read_text())["jobs"]
        assert isinstance(jobs, list)
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        return ["run concluded failure and its jobs could not be read"]
    if not jobs:
        return ["run concluded failure but lists no jobs"]
    problems, explained = [], 0
    for job in jobs:
        name, conclusion, steps = job.get("name"), job.get("conclusion"), job.get("steps")
        if job.get("status") != "completed" or conclusion not in ("success", "failure"):
            problems.append(f"job {name} is {job.get('status')}/{conclusion}")
            continue
        if not isinstance(steps, list) or not steps:
            problems.append(f"job {name} lists no steps")
            continue
        failed = [step.get("name") for step in steps if step.get("conclusion") in FAILED]
        unknown = [step.get("name") for step in steps if step.get("conclusion") not in FAILED | NOT_FAILED]
        if unknown:
            problems.append(f"job {name} has steps without a known conclusion: {unknown}")
        if any(step.get("status") != "completed" for step in steps):
            problems.append(f"job {name} has steps that did not complete")
        if conclusion == "success" and failed:
            problems.append(f"job {name} succeeded with failed steps")
        if conclusion == "failure" and not failed:
            problems.append(f"job {name} failed with no failed step")
        for step in failed:
            if tolerated_by_file.get(TEST_STEPS.get(step, "")):
                explained += 1
            else:
                problems.append(f"step '{step}' failed outside the quarantine")
    if not problems and not explained:
        problems.append("run concluded failure but no failed test step explains it")
    return problems


def judge(out: pathlib.Path, conclusion: str, run_sha: str, sha: str) -> tuple[list[str], bool]:
    quarantined = load_quarantine(out / "quarantine.txt")
    lines, problems, tolerated_by_file = [], [], {}
    if run_sha != sha:
        problems.append(f"run tested {run_sha}")
    for name in FILES:
        path = out / name
        if not path.exists():
            problems.append(f"{name} missing")
            continue
        file_lines, file_problems, file_tolerated = judge_file(path, quarantined)
        lines += file_lines
        problems += file_problems
        tolerated_by_file[name] = file_tolerated
    tolerated = sum(tolerated_by_file.values())
    if conclusion == "failure" and tolerated:
        problems += unwaived_steps(out / "jobs.json", tolerated_by_file)
    elif conclusion != "success":
        problems.append(f"run concluded {conclusion}")
    verdict = "FAIL " + "; ".join(problems) if problems else "PASS"
    note = f" ({tolerated} quarantined failures tolerated)" if tolerated and not problems else ""
    lines.append(f"gate: {verdict} {sha}{note}")
    return lines, not problems


if __name__ == "__main__":
    out, conclusion, run_sha, sha = sys.argv[1:5]
    output, ok = judge(pathlib.Path(out), conclusion, run_sha, sha)
    print("\n".join(output))
    sys.exit(0 if ok else 1)
