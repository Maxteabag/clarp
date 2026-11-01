#!/usr/bin/env python3
"""ROWS scratch proof only (branch proof/desktop-rows-20261101, never merged; copied from the FAFB v9 admit.py).

Fail-closed admission before a proof phase: python3 rows/admit.py PHASE CAP_MINUTES
The job's first step wrote its start epoch to /tmp/rows-epoch. A phase is
admitted only if elapsed + its cap + the finalization reserve still fits the
30 min budget less the runner's own set-up allowance. Anything else (no or bad
epoch, too late) is STOP: exit 7, with the decision in /tmp/rows/<phase>.admission.
"""
import json
import pathlib
import sys
import time

BUDGET_S = 30 * 60
RUNNER_SETUP_S = 60          # 'Set up job' runs before the first step and is not in the epoch
RESERVE_S = 3 * 60           # stage (1) + upload (2), always kept free
phase, cap_s = sys.argv[1], int(sys.argv[2]) * 60
out = pathlib.Path("/tmp/rows")
out.mkdir(parents=True, exist_ok=True)
try:
    start = int(pathlib.Path("/tmp/rows-epoch").read_text().strip())
except (OSError, ValueError):
    start = None
now = int(time.time())
elapsed = None if start is None else now - start
deadline = BUDGET_S - RUNNER_SETUP_S
admitted = elapsed is not None and 0 <= elapsed and elapsed + cap_s + RESERVE_S <= deadline
record = {"phase": phase, "admitted": admitted, "elapsed_s": elapsed, "cap_s": cap_s, "reserve_s": RESERVE_S,
          "deadline_s": deadline, "epoch": start, "now": now}
(out / f"{phase}.admission").write_text(json.dumps(record) + "\n")
print(f"FAFB-ADMISSION {json.dumps(record)}")
sys.exit(0 if admitted else 7)
