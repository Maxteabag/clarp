#!/bin/bash
# Installed immutable source directory passed by the reviewed user timer.
set -euo pipefail
SOURCE_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
export CLARP_BACKGROUND_WORKER_PID=$$
SESSION=rf-harnessc-4a4a
DAY=$(TZ=Europe/Oslo date +%F)
HANDLE=$(clarp-agent-bg "$SESSION" job-upsert "rf-harnessc-nightly-rolling-$DAY" nightly "Rolling main replay nightly $DAY")
clarp-agent-bg "$SESSION" job-active "$HANDLE"
clarp-agent-bg "$SESSION" job-heartbeat "$HANDLE"
# The controller blocks only in bounded Actions polling; heartbeat follows separately.
python "$SOURCE_DIR/controller.py" daily &
CHILD=$!
while kill -0 "$CHILD" 2>/dev/null; do
  if ! clarp-agent-bg "$SESSION" job-active "$HANDLE"; then
    kill "$CHILD" 2>/dev/null || true
    wait "$CHILD" || true
    exit 1
  fi
  clarp-agent-bg "$SESSION" job-heartbeat "$HANDLE"
  sleep 30 & wait $!
done
if wait "$CHILD"; then
  clarp-agent-bg "$SESSION" job-finish "$HANDLE"
else
  clarp-agent-bg "$SESSION" job-fail "$HANDLE" 'Nightly HOLD: inspect retained evidence; no anchor advance/retry'
  exit 1
fi
