#!/usr/bin/env bash
# Run a command as a detached, registered Clarp background job whose own
# worker writes the completion receipt.
#
#   run_detached_job.sh SESSION JOB_ID KIND TITLE LOG -- COMMAND [ARGS...]
#
# Prints the job handle once the job is registered, then returns. The command
# keeps running after this turn ends. Pass the handle to a clarp-goal
# dependency before you reply, or nothing will wake you when it finishes.
#
# The worker (not the agent) owns the job: it registers with its own PID,
# heartbeats, attaches LOG, writes LOG.exit with the command's exit status and
# reports job-finish (0) or job-fail (anything else). If the job is cancelled,
# the worker stops its own command. A worker killed before reporting is
# reconciled to failed (worker_vanished) with an unknown outcome.
# JOB_ID names one run: a cancelled id is not reused (job-restart is for a
# deliberate rerun of that same target).
# CLARP_AGENT_BG overrides the helper command; it is split on whitespace.
set -u
read -r -a BG <<< "${CLARP_AGENT_BG:-clarp-agent-bg}"

report() {  # the receipt must not be lost to a busy database
  for delay in 1 2 4 8 16; do
    "${BG[@]}" "$session" "$@" >/dev/null 2>&1 && return 0
    "${BG[@]}" "$session" job-active "$handle" >/dev/null 2>&1
    [ $? = 1 ] && return 1  # already terminal (cancelled): nothing to report
    sleep "$delay"
  done
  return 1
}

if [ "${1:-}" = "__worker" ]; then
  shift
  session=$1 job_id=$2 kind=$3 title=$4 log=$5; shift 6
  export CLARP_BACKGROUND_WORKER_PID=$$
  out=$("${BG[@]}" "$session" job-upsert "$job_id" "$kind" "$title" 2>>"$log.worker.err"); status=$?
  handle=$(printf '%s\n' "$out" | tail -n 1)
  if [ "$status" != 0 ] || [ "${handle#bg1:}" = "$handle" ]; then
    printf 'job-upsert failed (exit %s): %s\n' "$status" "$out" > "$log.handle.err"
    exit 1
  fi
  "${BG[@]}" "$session" job-log "$handle" "$log" >/dev/null 2>&1 || true
  echo "$handle" > "$log.handle.tmp" && mv "$log.handle.tmp" "$log.handle"
  setsid "$@" >"$log" 2>&1 < /dev/null &
  child=$!
  running() { [ -n "$(jobs -rp)" ]; }  # this shell's own job table: reuse-safe
  stopped=0 beat=$SECONDS
  # Ownership every 5 s so a cancel takes effect promptly; a heartbeat at
  # most once a minute (the job times out after ten without one).
  while running; do
    for _ in 1 2 3 4 5; do running || break; sleep 1; done
    running || break
    "${BG[@]}" "$session" job-active "$handle" >/dev/null 2>&1
    case $? in
      0) if [ $((SECONDS - beat)) -ge 60 ]; then
           "${BG[@]}" "$session" job-heartbeat "$handle" >/dev/null 2>&1 || true; beat=$SECONDS
         fi ;;
      1) stopped=1  # cancelled or superseded: stop our own command
         running && kill -TERM -- "-$child" 2>/dev/null
         for _ in $(seq 10); do running || break; sleep 1; done
         running && kill -KILL -- "-$child" 2>/dev/null
         break ;;
      *) ;;  # state unknown: keep the work, ask again next round
    esac
  done
  wait "$child"; rc=$?
  echo "$rc" > "$log.exit"
  [ "$stopped" = 1 ] && exit 0  # the job is already terminal
  report job-progress "$handle" "Command exited $rc; log $log" || true
  if [ "$rc" = 0 ]; then report job-finish "$handle"
  else report job-fail "$handle" "exit $rc"; fi
  exit 0
fi

if [ $# -lt 7 ] || [ "$6" != "--" ]; then
  sed -n '2,9p' "$0" >&2; exit 2
fi
log=$5
case $log in /*) ;; *) echo "LOG must be an absolute path" >&2; exit 2;; esac
mkdir -p "$(dirname "$log")" || exit 2
rm -f "$log.handle" "$log.handle.err" "$log.exit"
setsid nohup "$0" __worker "$@" >/dev/null 2>"$log.worker.err" < /dev/null &
for _ in $(seq 300); do
  [ -s "$log.handle" ] && { cat "$log.handle"; exit 0; }
  [ -s "$log.handle.err" ] && { cat "$log.handle.err" >&2; exit 1; }
  sleep 0.1
done
echo "worker did not register within 30 s; see $log.worker.err" >&2
exit 1
