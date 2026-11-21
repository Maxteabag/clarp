#!/usr/bin/env python3
"""Record the Python Host's public projection of updates from a Codex thread."""
import importlib.util
import json
from pathlib import Path
import sys

import scratch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE.parent / "crates/clarp-kernel/tests/fixtures/agent-goal-vectors.json"
CASES = [
    {"objective": "finish the work", "status": "active", "tokenBudget": 5000,
     "createdAt": 1795000000, "updatedAt": 1795000001},
    {"objective": "finish the work", "status": "paused", "tokenBudget": 5000,
     "tokensUsed": 12, "timeUsedSeconds": 2},
    {"objective": "finish the work", "status": "usageLimited",
     "createdAt": 0, "updatedAt": "1795000009000"},
    {"objective": "finish the work", "status": " budgetLimited ",
     "tokenBudget": 0, "tokensUsed": 50, "timeUsedSeconds": 31},
    {"objective": "finish the work", "status": "blocked", "updatedAt": -1},
    {"objective": "finish the work", "status": "complete",
     "updatedAt": 100000000000, "tokensUsed": 53, "timeUsedSeconds": 34},
    {"objective": "other work", "createdAt": 99999999999, "updatedAt": "unknown"},
    {"objective": "bad status", "status": "unknown"},
]


@scratch.case("clarp-agent-goal-")
def project(place):
    spec = importlib.util.spec_from_file_location("replay_host", ROOT / "tests/replay/host.py")
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    host.isolate(place)
    host.loopback_only()
    sys.path.insert(0, str(ROOT / "server"))
    from lib import agent_goals, db

    rows = []
    for index, raw in enumerate(CASES):
        now = 1795000020000 + index * 1000
        db.now_ms = lambda: now
        try:
            row = agent_goals.upsert(
                "agent", session="ada", backend="codex", goal=agent_goals.from_codex(raw)
            )
        except ValueError:
            row = None
        rows.append({"raw": raw, "now": now, "public": agent_goals.public(row)})
    return rows


if __name__ == "__main__":
    OUT.write_text(json.dumps(project(), ensure_ascii=True, indent=2) + "\n")
