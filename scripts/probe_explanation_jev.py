#!/usr/bin/env python3
"""Bounded paid probe of Jev template selection for tool explanations.

Dry run by default: prints the corpus, the request plan and the call ceiling
without a network call. A paid run needs `--approved-cap N` matching an
approved number of Jev requests (at most HARD_CAP); it stops at the cap, on the
first billing error or when the breaker opens. It uses a private temporary
database, synthetic activities only, and never runs any described command.

    python3 scripts/probe_explanation_jev.py              # plan only
    python3 scripts/probe_explanation_jev.py --approved-cap 12 --repetitions 3
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

HARD_CAP = 24
BATCH = 8

# expected: a read/list/search template Jev may pick, or "unknown" when the
# synthetic program's effect is opaque or could change something.
CORPUS = [
    ("eza -la src", "list_directory"), ("lsd --tree docs", "list_directory"), ("exa -1 server", "list_directory"),
    ("broot --cmd :pt src", "unknown"), ("dust -d 1 build", "unknown"), ("ugrep -rn TODO src", "search_text"),
    ("fzf --filter main src", "unknown"), ("glow README.md", "read_file"), ("hexyl logo.png", "read_file"),
    ("jq . package.json", "read_file"), ("yq . config.yaml", "read_file"), ("tokei src", "unknown"),
    ("filectl --mode=list /tmp/example", "unknown"), ("filectl --mode=delete /tmp/example", "unknown"),
    ("srm -r old-backups", "unknown"), ("wipe -rf cache", "unknown"), ("trash build", "unknown"),
    ("sd foo bar src/app.py", "unknown"), ("rename 's/a/b/' *.txt", "unknown"), ("gh pr view 12", "unknown"),
    ("psql -c 'select 1'", "unknown"), ("redis-cli keys '*'", "unknown"), ("aws s3 ls s3://bucket", "unknown"),
    ("mc cp a.txt b/", "unknown"),
]


def plan(repetitions):
    batches = -(-len(CORPUS) // BATCH)
    return {"activities": len(CORPUS), "batch_size": BATCH, "requests_per_repetition": batches,
            "repetitions": repetitions, "requests": batches * repetitions}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved-cap", type=int, default=0)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    request_plan = plan(args.repetitions)
    print(json.dumps({"plan": request_plan, "hard_cap": HARD_CAP}, indent=2))
    if not args.approved_cap:
        print("dry run: no Jev request sent")
        return 0
    if not 0 < args.approved_cap <= HARD_CAP or request_plan["requests"] > args.approved_cap:
        print(f"refusing: plan needs {request_plan['requests']} requests; approved cap {args.approved_cap}, hard cap {HARD_CAP}")
        return 2

    private = tempfile.mkdtemp(prefix="clarp-jev-probe-")
    os.environ["CLAUDE_PWA_DB"] = str(Path(private) / "state.sqlite")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
    from lib import judgment_sites, judgments, settings_store
    from lib import tool_explanation_templates as templates
    from lib.tool_explanations import normalize_activity
    if not judgments.api_key():
        print("no TypeSafe key configured")
        return 2
    settings_store.set_bool(judgments.KEY_ENABLED, True)
    settings_store.set_bool("judgments.explanations", True)
    criteria = {tid: t["description"] for tid, t in templates.TEMPLATES.items() if t["action"] in templates.LEARNABLE_ACTIONS}

    rows, sent = [], 0
    for repetition in range(args.repetitions):
        for start in range(0, len(CORPUS), BATCH):
            if sent >= args.approved_cap or judgments._breaker_open():
                break
            chunk = CORPUS[start:start + BATCH]
            entries = {}
            for i, (command, _) in enumerate(chunk):
                activity = normalize_activity({"name": "Bash", "command": command})
                route = templates.classify(activity)
                entries[str(i + 1)] = {"activity": activity, "candidates": route.get("candidates", []), "route": route}
            eligible = {k: v for k, v in entries.items() if v["route"].get("reason") in templates.JEV_REASONS}
            started = time.perf_counter()
            answer = judgment_sites.select_explanation_templates(
                {k: {"activity": v["activity"], "candidates": v["candidates"]} for k, v in eligible.items()}, criteria)
            sent += 1
            latency = (time.perf_counter() - started) * 1000
            for key, (command, expected) in zip(entries, chunk):
                got = (answer or {}).get(key) if key in eligible else {"reason": entries[key]["route"].get("reason")}
                rows.append({"repetition": repetition, "command": command, "expected": expected,
                             "picked": (got or {}).get("template_id") or "unknown",
                             "reason": (got or {}).get("reason", ""), "confidence": (got or {}).get("confidence"),
                             "batch_latency_ms": round(latency)})
            if answer is None:
                print("Jev did not answer; stopping")
                break
    decided = [r for r in rows if r["reason"] != "jev_unavailable"]
    wrong_pick = [r for r in decided if r["picked"] != "unknown" and r["picked"] != r["expected"]]
    summary = {"requests_sent": sent, "rows": len(rows),
               "hit_rate": round(sum(r["picked"] != "unknown" for r in decided) / max(1, len(decided)), 3),
               "agreement": round(sum(r["picked"] == r["expected"] for r in decided) / max(1, len(decided)), 3),
               "confident_wrong_picks": wrong_pick,
               "latency_ms_p50": statistics.median(r["batch_latency_ms"] for r in rows) if rows else None,
               "latency_ms_max": max((r["batch_latency_ms"] for r in rows), default=None)}
    print(json.dumps(summary, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
