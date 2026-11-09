#!/bin/bash
# Installed immutable source directory passed by the reviewed user timer.
set -euo pipefail
SOURCE_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
export CLARP_BACKGROUND_WORKER_PID=$$
SESSION=rf-harnessc-4a4a
DAY=$(TZ=Europe/Oslo date +%F)
LOGDIR=/var/tmp/clarp-refactor-program/rf-harness/nightly-rolling-logs
mkdir -p "$LOGDIR"
LOG="$LOGDIR/$DAY.log"
# Never truncate/retry an already started date, even after an uncertain result.
( set -o noclobber; : > "$LOG" )
HANDLE=$(clarp-agent-bg "$SESSION" job-upsert "rf-harnessc-nightly-rolling-$DAY" nightly "Rolling main replay nightly $DAY")
export CLARP_NIGHTLY_JOB_HANDLE="$HANDLE"
clarp-agent-bg "$SESSION" job-log "$HANDLE" "$LOG"
clarp-agent-bg "$SESSION" job-active "$HANDLE"
clarp-agent-bg "$SESSION" job-heartbeat "$HANDLE"
python "$SOURCE_DIR/controller.py" daily >> "$LOG" 2>&1 &
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
RC=0
wait "$CHILD" || RC=$?
TEXT=$(python "$SOURCE_DIR/controller.py" report)
printf '%s\n' "$TEXT" >> "$LOG"
clarp-agent-bg "$SESSION" job-active "$HANDLE"
# One requested nightly result. Failed/uncertain message is retained, not resent.
if ! clarp-admin prompt --to hobbles-18b0 --from "$SESSION" --text "$TEXT" >> "$LOG" 2>&1; then
  printf '%s\n' 'Result delivery uncertain; reconcile before any resend' >> "$LOG"
  clarp-agent-bg "$SESSION" job-fail "$HANDLE" 'Nightly result delivery uncertain; retained log'
  exit 1
fi
if [ "$RC" = 0 ]; then
  clarp-agent-bg "$SESSION" job-finish "$HANDLE"
else
  clarp-agent-bg "$SESSION" job-fail "$HANDLE" 'Nightly HOLD: inspect retained evidence; no anchor advance/retry'
fi
exit "$RC"
