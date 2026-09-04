#!/usr/bin/env python3
"""Run PR #25's explicitly enumerated cases; preserve red evidence, not xfails."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="directory for logs, raw reports and classified results")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "docs/reliability-audit-cases.json").read_text())
    cases = manifest["cases"]
    commands = {
        "pytest": ["uv", "run", "--frozen", "--group", "dev", "pytest",
                   "-n", "2", "--tb=short", f"--junitxml={output / 'python.xml'}",
                   *(case["node"] for case in cases if case["runner"] == "pytest")],
        "vitest": ["npm", "exec", "--", "vitest", "run",
                   *sorted({case["file"] for case in cases if case["runner"] == "vitest"}),
                   "--testNamePattern", "|".join(
                       re.escape(case["title"]) + "$" for case in cases
                       if case["runner"] == "vitest"),
                   "--reporter=json", f"--outputFile={output / 'javascript.json'}"],
    }
    # Avoid reporting stale success if a runner fails before writing its report.
    for name in ("python.xml", "javascript.json", "results.json"):
        (output / name).unlink(missing_ok=True)
    exits = {}
    for runner, command in commands.items():
        print(f"Running {runner} audit cases", flush=True)
        with (output / f"{runner}.log").open("w") as log:
            exits[runner] = subprocess.run(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT).returncode
    results = {}
    if (output / "python.xml").exists():
        for test in ET.parse(output / "python.xml").iter("testcase"):
            node = test.get("classname", "").replace(".", "/") + ".py::" + test.get("name", "")
            results[node] = (
                "failed" if test.find("failure") is not None else
                "error" if test.find("error") is not None else
                "skipped" if test.find("skipped") is not None else "passed")
    if (output / "javascript.json").exists():
        js = json.loads((output / "javascript.json").read_text())
        for suite in js["testResults"]:
            file = str(Path(suite["name"]).relative_to(ROOT))
            for test in suite["assertionResults"]:
                results[file + "::" + test["title"]] = test["status"]
    classified = []
    for case in cases:
        key = case.get("node") or case["file"] + "::" + case["title"]
        classified.append(case | {"result": results.get(key, "not-collected")})
    counts = {status: sum(case["result"] == status for case in classified)
              for status in sorted({case["result"] for case in classified})}
    report = dict(baseline=manifest["baseline"], runner_exit_codes=exits,
                  counts=counts, cases=classified)
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(counts, sort_keys=True))
    print(f"Classified evidence: {output / 'results.json'}")
    return 1 if any(exits.values()) or any(c["result"] != "passed" for c in classified) else 0


if __name__ == "__main__":
    raise SystemExit(main())
