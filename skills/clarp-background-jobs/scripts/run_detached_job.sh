#!/usr/bin/env bash
# Run a command as a registered Clarp background job whose own worker writes
# the completion receipt, and optionally wake you when it ends.
#
#   run_detached_job.sh [--goal PLAN_ID] [--deadline SECONDS] \
#       SESSION JOB_ID KIND TITLE LOG -- COMMAND [ARGS...]
#
# Prints the job handle once the job is registered, then returns 0. With --goal
# it first attaches a clarp-goal dependency on the job (deadline default 6 h)
# so the wake cannot be lost with your turn; exit 3 means the job is running
# but the dependency is NOT attached (the reason is printed).
#
# With a user systemd manager the worker runs as its own service, outside the
# Clarp runtime's cgroup, so a runtime restart does not kill it (setsid and
# nohup do not leave a cgroup). The command gets your exact environment, umask
# and open-file limit; the environment and the arguments travel through
# private 0600 files, never through systemd's command line or logs. The working
# directory is a unit property. When the command ends, the unit ends, and so
# does anything it left running. Without a user systemd manager the worker is
# detached in this service and is NOT restart-safe; the script says so.
#
# The worker owns the job: it registers with its own PID, heartbeats, attaches
# LOG, writes LOG.exit with the command's exit status and reports job-finish (0)
# or job-fail (anything else). A cancelled job stops its own command. A worker
# killed before reporting is reconciled to failed (worker_vanished), outcome
# unknown. JOB_ID names one run: a cancelled id is not reused (job-restart is
# for a deliberate rerun). CLARP_AGENT_BG and CLARP_GOAL override the helper
# commands (split on whitespace).
set -u
read -r -a BG <<< "${CLARP_AGENT_BG:-clarp-agent-bg}"
read -r -a GOAL <<< "${CLARP_GOAL:-clarp-goal}"
# Per-unit values systemd sets for a service: the caller's describe the Clarp
# runtime, so the command gets the job unit's own instead.
UNIT_VARS=" INVOCATION_ID JOURNAL_STREAM SYSTEMD_EXEC_PID MANAGERPID NOTIFY_SOCKET WATCHDOG_PID WATCHDOG_USEC LISTEN_PID LISTEN_FDS LISTEN_FDNAMES MEMORY_PRESSURE_WATCH MEMORY_PRESSURE_WRITE RUNTIME_DIRECTORY STATE_DIRECTORY CACHE_DIRECTORY LOGS_DIRECTORY CONFIGURATION_DIRECTORY CREDENTIALS_DIRECTORY "

report() {  # the receipt must not be lost to a busy database
  for delay in 1 2 4 8 16; do
    "${BG[@]}" "$session" "$@" >/dev/null 2>&1 && return 0
    "${BG[@]}" "$session" job-active "$handle" >/dev/null 2>&1
    [ $? = 1 ] && return 1  # already terminal (cancelled): nothing to report
    sleep "$delay"
  done
  return 1
}

spawn() {  # run "$@" as this shell's background job, in its own process group
  if command -v setsid >/dev/null 2>&1; then setsid "$@" &
  else set -m; "$@" & set +m; fi
}

if [ "${1:-}" = "__worker" ]; then
  envfile=$2 argfile=$3; shift 3
  if [ -n "$argfile" ]; then  # the arguments, kept out of systemd's command line
    args=()
    while IFS= read -r -d '' a; do args+=("$a"); done < "$argfile"
    rm -f "$argfile"
    set -- "${args[@]}"
  fi
  session=$1 job_id=$2 kind=$3 title=$4 log=$5; shift 6
  if [ -n "$envfile" ]; then
    # The worker takes only what its own helpers need, so caller values such
    # as IFS cannot change this script. The command gets the whole file
    # (exec_command below), which it deletes.
    while IFS= read -r -d '' kv; do
      case ${kv%%=*} in PATH|HOME|USER|LANG|LC_*|TMPDIR|XDG_*|DBUS_SESSION_BUS_ADDRESS|CLAUDE_PWA_*|CLARP_*)
        export -- "$kv" 2>/dev/null || true;; esac
    done < "$envfile"
  fi
  read -r -a BG <<< "${CLARP_AGENT_BG:-clarp-agent-bg}"
  exec 2>>"$log.worker.err"
  export CLARP_BACKGROUND_WORKER_PID=$$
  out=$("${BG[@]}" "$session" job-upsert "$job_id" "$kind" "$title"); status=$?
  handle=$(printf '%s\n' "$out" | tail -n 1)
  if [ "$status" != 0 ] || [ "${handle#bg1:}" = "$handle" ]; then
    printf 'job-upsert failed (exit %s): %s\n' "$status" "$out" > "$log.handle.err"
    [ -n "$envfile" ] && rm -f "$envfile"
    exit 1
  fi
  "${BG[@]}" "$session" job-log "$handle" "$log" >/dev/null 2>&1 || true
  echo "$handle" > "$log.handle.tmp" && mv "$log.handle.tmp" "$log.handle"
  if [ -n "$envfile" ]; then
    # Exactly the caller's environment, read from the private file (never an
    # argument list, which any local user can read), with this unit's own
    # per-unit systemd values in place of the runtime's.
    spawn python3 -c '
import os, signal, sys
path, unit_vars, argv = sys.argv[1], sys.argv[2].split(), sys.argv[3:]
# Python ignores these at startup and exec keeps that: restore the defaults a
# command started by a shell would have (head closing a pipe, for one).
for sig in (signal.SIGPIPE, signal.SIGXFSZ):
    signal.signal(sig, signal.SIG_DFL)
with open(path, "rb") as f:
    raw = f.read()
os.unlink(path)
env = {}
for item in raw.split(b"\0"):
    name, sep, value = item.partition(b"=")
    if sep and name.decode(errors="replace") not in unit_vars:
        env[name] = value
for name in unit_vars:
    if name in os.environ:
        env[name.encode()] = os.environb[name.encode()]
try:
    os.execvpe(argv[0], argv, env)
except OSError as error:
    print(f"{argv[0]}: {error.strerror}", file=sys.stderr)
    sys.exit(127 if isinstance(error, FileNotFoundError) else 126)
' "$envfile" "$UNIT_VARS" "$@" >"$log" 2>&1 < /dev/null
  else spawn "$@" >"$log" 2>&1 < /dev/null; fi
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

usage() { sed -n '2,11p' "$0" >&2; exit 2; }
goal="" deadline=21600
while [ $# -gt 0 ]; do
  case $1 in
    --goal|--deadline)
      { [ $# -ge 2 ] && [ -n "$2" ]; } || usage
      if [ "$1" = --goal ]; then goal=$2; else deadline=$2; fi
      shift 2 ;;
    --*) [ "$1" = -- ] && break; usage ;;
    *) break ;;
  esac
done
case $deadline in *[!0-9]*) usage ;; esac
deadline=${deadline#"${deadline%%[!0]*}"}  # base 10: no octal, no overflow
[ ${#deadline} -le 7 ] && [ "${deadline:-0}" -ge 1 ] && [ "$deadline" -le 2592000 ] || usage
if [ $# -lt 7 ] || [ "$6" != "--" ]; then usage; fi
session=$1 job_id=$2 log=$5
case $log in /*) ;; *) echo "LOG must be an absolute path" >&2; exit 2;; esac
mkdir -p "$(dirname "$log")" || exit 2
rm -f "$log.handle" "$log.handle.err" "$log.exit" "$log.unit"
due=$(( ($(date +%s) + deadline) * 1000 ))

envfile="" argfile="" unit="" worker=""
cleanup() { rm -f "$envfile" "$argfile"; }
if command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  dir=${XDG_RUNTIME_DIR:-}
  [ -d "$dir" ] && [ -w "$dir" ] || dir=${TMPDIR:-/tmp}
  envfile=$(umask 077; mktemp "$dir/clarp-job-env.XXXXXXXX") || exit 1
  argfile=$(umask 077; mktemp "$dir/clarp-job-args.XXXXXXXX") || { cleanup; exit 1; }
  env -0 > "$envfile"
  printf '%s\0' "$@" > "$argfile"
  soft=$(ulimit -Sn) hard=$(ulimit -Hn)
  [ "$soft" = unlimited ] && soft=infinity; [ "$hard" = unlimited ] && hard=infinity
  unit="clarp-job-$(printf '%s' "$job_id" | tr -c 'A-Za-z0-9_-' '_' | cut -c1-80)-$(date +%s)-$RANDOM"
  # Type=exec: a failure to start (missing directory, say) fails right here.
  if ! systemd-run --user --quiet --collect --unit="$unit" --expand-environment=no \
        -p Type=exec -p KillMode=control-group -p UMask="$(umask)" \
        -p LimitNOFILE="$soft:$hard" --working-directory="$PWD" \
        /bin/bash "$(readlink -f "$0")" __worker "$envfile" "$argfile" 2>"$log.worker.err"; then
    cleanup; cat "$log.worker.err" >&2; exit 1
  fi
  echo "$unit" > "$log.unit"
else
  echo "run_detached_job: no user systemd manager; the worker stays in this service and is NOT restart-safe" >&2
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$0" __worker "" "" "$@" >/dev/null 2>"$log.worker.err" < /dev/null &
  else
    nohup "$0" __worker "" "" "$@" >/dev/null 2>"$log.worker.err" < /dev/null &
  fi
  worker=$!
fi
handle=""
for _ in $(seq 300); do
  [ -s "$log.handle" ] && { handle=$(cat "$log.handle"); break; }
  [ -s "$log.handle.err" ] && { cat "$log.handle.err" >&2; cleanup; exit 1; }
  sleep 0.1
done
if [ -z "$handle" ]; then
  # Never leave an unregistered worker to run the command unseen.
  if [ -n "$unit" ]; then systemctl --user stop "$unit" >/dev/null 2>&1
  elif [ -n "$worker" ]; then kill -TERM "$worker" 2>/dev/null; fi
  cleanup
  sleep 1
  if [ -s "$log.handle" ]; then  # it registered just as it was stopped: close that job
    "${BG[@]}" "$session" job-cancel "$(cat "$log.handle")" >/dev/null 2>&1
    echo "worker registered only after 30 s and was stopped; job $(cat "$log.handle") is closed as cancelled; see $log.worker.err" >&2
  else
    echo "worker did not register within 30 s and was stopped; see $log.worker.err" >&2
  fi
  exit 1
fi
echo "$handle"
[ -z "$goal" ] && exit 0

# Attach the dependency now, while this turn is still running. One goal waits
# on one dependency: another one is never replaced. The goal's own progress and
# next work stay; the deadline is this call's (a goal's routine recovery timer
# is not a deadline for the job).
for attempt in 1 2 3; do
  current=$("${GOAL[@]}" get "$goal" 2>&1) || { echo "run_detached_job: job running, dependency NOT attached: $current" >&2; exit 3; }
  body=$(printf '%s' "$current" | python3 -c '
import json, sys
plan = json.load(sys.stdin); key, handle, log, due = sys.argv[1:5]; due = int(due)
goal = plan.get("goal") or {}
cont, point = goal.get("continuation") or {}, goal.get("checkpoint") or {}
if cont.get("state") == "waiting" and (cont.get("dependency_key"), cont.get("job_handle")) != (key, handle):
    sys.exit("goal already waits on " + str(cont.get("dependency_key") or cont.get("job_handle") or "another dependency"))
read = f"When woken by {key}: read {log}.exit and {log} before reporting; the job status says how the worker ended, not that the result is right."
print(plan["revision"])
print(json.dumps({
    "progress": point.get("progress") or f"Started background job {key}",
    "next_work": "\n\n".join(filter(None, [point.get("next_work"), read])),
    "continuation": {"kind": "dependency", "key": key, "job_handle": handle,
                     "reason": f"Waiting for background job {key}", "due_at": due}}))
' "$job_id" "$handle" "$log" "$due" 2>&1) || { echo "run_detached_job: job running, dependency NOT attached: $body" >&2; exit 3; }
  rev=$(printf '%s\n' "$body" | head -n 1)
  error=$("${GOAL[@]}" checkpoint "$goal" "$rev" "$(printf '%s\n' "$body" | tail -n +2)" 2>&1 >/dev/null) && exit 0
  case $error in *"plan changed"*) sleep "$attempt"; continue;; esac
  break
done
echo "run_detached_job: job running, dependency NOT attached: $error" >&2
echo "attach it yourself (clarp-goal checkpoint with continuation kind dependency, key $job_id, job_handle $handle)" >&2
exit 3
