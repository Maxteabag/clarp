#!/usr/bin/env bash
# The one supported way to start background work whose result must reach you.
#
#   run_detached_job.sh --goal PLAN_ID [--deadline SECONDS] \
#       SESSION JOB_ID KIND TITLE LOG -- COMMAND [ARGS...]
#   run_detached_job.sh --no-wake SESSION JOB_ID KIND TITLE LOG -- COMMAND...
#
# Exit 0 prints the job handle and means ready: the job is registered by its
# own worker, the goal's dependency on it is stored and re-read, and only then
# has the command started. Nothing is started otherwise:
#   2  usage error, 4  the goal cannot carry the wake (the reason is printed),
#   3  the dependency could not be attached; the command did NOT run,
#   1  the launch itself failed.
# --no-wake starts the command without a dependency: nobody will tell you
# when it ends. The goal must be yours and active; pick it with clarp-goal.
#
# When the job ends, the goal records how (succeeded, failed, cancelled, or
# failed/worker_vanished with an unknown outcome) and wakes you; if the
# deadline (default 6 h) passes first, you are woken to inspect it. Wakes
# follow the goal: none while it or your queue is paused, none on this Host
# while autonomous wakes are disabled.
#
# With a user systemd manager the worker runs as its own service, outside the
# Clarp runtime's cgroup, so a runtime restart does not kill it. The command
# gets your exact environment, umask and open-file limit; the environment and
# the arguments travel through private 0600 files, never through systemd's
# command line or logs. When the command ends its unit ends, and so does
# anything it left running. Without a user systemd manager the worker is
# detached in this service and is NOT restart-safe; the script says so.
#
# The worker heartbeats, attaches LOG, writes LOG.exit with the command's exit
# status and reports job-finish (0) or job-fail itself. A cancelled job stops
# its own command. JOB_ID names one run: use a new one for a new run.
# CLARP_AGENT_BG and CLARP_GOAL override the helper commands (split on
# whitespace).
set -u
read -r -a BG <<< "${CLARP_AGENT_BG:-clarp-agent-bg}"
read -r -a GOAL <<< "${CLARP_GOAL:-clarp-goal}"
# Per-unit values systemd sets for a service: the caller's describe the Clarp
# runtime, so the command gets the job unit's own instead.
UNIT_VARS=" INVOCATION_ID JOURNAL_STREAM SYSTEMD_EXEC_PID MANAGERPID NOTIFY_SOCKET WATCHDOG_PID WATCHDOG_USEC LISTEN_PID LISTEN_FDS LISTEN_FDNAMES MEMORY_PRESSURE_WATCH MEMORY_PRESSURE_WRITE RUNTIME_DIRECTORY STATE_DIRECTORY CACHE_DIRECTORY LOGS_DIRECTORY CONFIGURATION_DIRECTORY CREDENTIALS_DIRECTORY "
READY_WAIT=120  # how long a worker holds the command for its dependency

# One reading of a goal (clarp-goal get JSON on stdin) for every decision.
GOAL_PY='
import json, sys
mode, args = sys.argv[1], sys.argv[2:]
try:
    plan = json.load(sys.stdin)
except ValueError:
    sys.exit("clarp-goal did not return a goal")
if not isinstance(plan, dict):
    sys.exit("no such goal")
goal = plan.get("goal") or {}
cont, point = goal.get("continuation") or {}, goal.get("checkpoint") or {}
def other_dependency(key, handle):
    return cont.get("state") == "waiting" and (cont.get("dependency_key"), cont.get("job_handle")) != (key, handle)
if mode == "usable":
    session, key = args
    owner, status = plan.get("session"), plan.get("status")
    if owner != session:
        sys.exit("goal belongs to %s, not %s" % (owner, session))
    if not goal or status != "active" or not plan.get("recovery_enabled"):
        sys.exit("goal is %s and %s for recovery" % (status, "enrolled" if plan.get("recovery_enabled") else "not enrolled"))
    if cont.get("state") in ("attention", "blocked", "not_enrolled"):
        sys.exit("goal recovery is " + cont["state"] + ": " + str(cont.get("reason") or ""))
    if cont.get("observed_state") == "owner_changed":
        sys.exit("goal belongs to an earlier conversation; re-enroll it first")
    if other_dependency(key, None):
        # Printed for the shell to decide: an ended job is read first, not dropped.
        print("WAITING " + str(cont.get("job_handle") or ""))
        sys.exit("goal already waits on " + str(cont.get("dependency_key") or cont.get("job_handle") or "another dependency"))
    notes = {"host_paused": "autonomous wakes are paused on this Host: you will not be woken until they resume",
             "paused": "your queue is stopped: you will not be woken until it is resumed",
             "approval": "the goal waits for a pending answer or approval before it can wake you",
             "native_owned": "a native autonomous objective owns continuation: this goal wakes you only after it ends"}
    if cont.get("observed_state") in notes:
        print(notes[cont["observed_state"]])
elif mode == "attached":
    key, handle = args
    live = plan.get("status") in ("active", "paused")
    sys.exit(0 if live and (cont.get("dependency_key"), cont.get("job_handle")) == (key, handle) else 1)
elif mode == "body":
    key, handle, log, due = args
    if other_dependency(key, handle):
        sys.exit("goal already waits on " + str(cont.get("dependency_key") or cont.get("job_handle") or "another dependency"))
    read = f"When woken by {key}: read {log}.exit and {log} before reporting; the job status says how the worker ended, not that the result is right."
    # Keep the own next work of the goal; replace, never stack, the launcher note.
    own = [part for part in (point.get("next_work") or "").split("\n\n") if part and not part.startswith("When woken by ")]
    print(plan["revision"])
    print(json.dumps({
        "progress": point.get("progress") or f"Started background job {key}",
        "next_work": "\n\n".join(own + [read]),
        "continuation": {"kind": "dependency", "key": key, "job_handle": handle,
                         "reason": f"Waiting for background job {key}", "due_at": int(due)}}))
'
goal_check() {  # goal_check PLAN MODE ARGS...: the goal JSON is read fresh each time
  local plan=$1; shift
  local json err
  err=$(mktemp) || return 2
  json=$("${GOAL[@]}" get "$plan" 2>"$err") || { cat "$err" >&2; rm -f "$err"; return 2; }
  rm -f "$err"
  printf '%s' "$json" | python3 -c "$GOAL_PY" "$@"
}

report() {  # the receipt must not be lost to a busy database
  for delay in 1 2 4 8 16; do
    "${BG[@]}" "$session" "$@" >/dev/null 2>&1 && return 0
    "${BG[@]}" "$session" job-active "$handle" >/dev/null 2>&1
    [ $? = 1 ] && return 1  # already terminal (cancelled): nothing to report
    sleep "$delay"
  done
  return 1
}

decide() {  # decide OUTCOME: true when this side made the launch's one decision
  ( set -C; printf '%s\n' "$1" > "$run.decided" ) 2>/dev/null
}
decision() {  # the launch's decision, once written
  local d=""
  for _ in $(seq 50); do d=$(cat "$run.decided" 2>/dev/null); [ -n "$d" ] && break; sleep 0.1; done
  printf '%s' "$d"
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
  session=$1 job_id=$2 kind=$3 title=$4 log=$5 goal=$6 nonce=$7; shift 8
  run=$log.$nonce  # this launch's own signal files: a relaunch never reads them
  if [ -n "$envfile" ]; then
    # The worker takes only what its own helpers need, so caller values such
    # as IFS cannot change this script. The command gets the whole file
    # (below), which it deletes.
    while IFS= read -r -d '' kv; do
      case ${kv%%=*} in PATH|HOME|USER|LANG|LC_*|TMPDIR|XDG_*|DBUS_SESSION_BUS_ADDRESS|CLAUDE_PWA_*|CLARP_*)
        export -- "$kv" 2>/dev/null || true;; esac
    done < "$envfile"
  fi
  read -r -a BG <<< "${CLARP_AGENT_BG:-clarp-agent-bg}"
  read -r -a GOAL <<< "${CLARP_GOAL:-clarp-goal}"
  exec 2>>"$log.worker.err"
  export CLARP_BACKGROUND_WORKER_PID=$$
  out=$("${BG[@]}" "$session" job-upsert "$job_id" "$kind" "$title"); status=$?
  handle=$(printf '%s\n' "$out" | tail -n 1)
  if [ "$status" != 0 ] || [ "${handle#bg1:}" = "$handle" ]; then
    printf 'job-upsert failed (exit %s): %s\n' "$status" "$out" > "$run.handle.err"
    [ -n "$envfile" ] && rm -f "$envfile"
    exit 1
  fi
  "${BG[@]}" "$session" job-log "$handle" "$log" >/dev/null 2>&1 || true
  echo "$handle" > "$run.handle.tmp" && mv "$run.handle.tmp" "$run.handle"
  if [ "$goal" != - ]; then
    # Hold the command until the goal carries the wake. The launcher signals
    # it, and the worker checks the goal itself, so a launcher that died
    # after storing the dependency still gets its job run; one that died
    # before never starts it. Whether it starts is one atomic decision
    # (decide) that the launcher's abort competes for: the loser reports the
    # winner's outcome, so "not run" and "started" can never both be said.
    ready=0 waited=0
    while [ "$waited" -lt $((READY_WAIT * 2)) ]; do
      [ -e "$run.decided" ] && break
      if [ -e "$run.go" ] || { [ $((waited % 4)) = 0 ] && goal_check "$goal" attached "$job_id" "$handle" 2>/dev/null; }; then
        ready=1; break
      fi
      sleep 0.5; waited=$((waited + 1))
    done
    if [ "$ready" = 1 ] && decide started; then :; else
      # Whichever side reads the other's decision removes the signal files:
      # if this worker decided, the launcher may still need to read it.
      decided_here=0
      [ "$ready" = 1 ] || { decide notstarted && decided_here=1; }
      [ -n "$envfile" ] && rm -f "$envfile"
      report job-fail "$handle" "not started: the goal dependency was not attached" || true
      rm -f "$run.handle" "$run.go"
      [ "$decided_here" = 1 ] || rm -f "$run.decided"
      exit 0
    fi
  else
    decide started
  fi
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
  # The handle and the decision stay: only the launcher, after reporting, may
  # remove them (a fast command can end before the launcher has read them).
  rm -f "$run.go"
  echo "$rc" > "$log.exit"
  [ "$stopped" = 1 ] && exit 0  # the job is already terminal
  report job-progress "$handle" "Command exited $rc; log $log" || true
  if [ "$rc" = 0 ]; then report job-finish "$handle"
  else report job-fail "$handle" "exit $rc"; fi
  exit 0
fi

usage() { [ $# -gt 0 ] && echo "run_detached_job: $*" >&2; sed -n '2,16p' "$0" >&2; exit 2; }
goal="" nowake=0 deadline=21600
while [ $# -gt 0 ]; do
  case $1 in
    --goal|--deadline)
      { [ $# -ge 2 ] && [ -n "$2" ]; } || usage "$1 needs a value"
      if [ "$1" = --goal ]; then goal=$2; else deadline=$2; fi
      shift 2 ;;
    --no-wake) nowake=1; shift ;;
    --*) [ "$1" = -- ] && break; usage "unknown option $1" ;;
    *) break ;;
  esac
done
if [ -n "$goal" ] && [ "$nowake" = 1 ]; then usage "--goal and --no-wake exclude each other"; fi
if [ -z "$goal" ] && [ "$nowake" = 0 ]; then
  usage "choose the goal that should be woken (--goal PLAN_ID, see clarp-goal list), or --no-wake"
fi
case $deadline in *[!0-9]*) usage "--deadline is whole seconds" ;; esac
deadline=${deadline#"${deadline%%[!0]*}"}  # base 10: no octal, no overflow
[ ${#deadline} -le 7 ] && [ "${deadline:-0}" -ge 1 ] && [ "$deadline" -le 2592000 ] || usage "--deadline is 1 to 2592000 seconds"
if [ $# -lt 7 ] || [ "$6" != "--" ]; then usage; fi
session=$1 job_id=$2 log=$5
case $log in /*) ;; *) usage "LOG must be an absolute path" ;; esac
if [ -n "$goal" ]; then  # nothing starts unless this goal can carry the wake
  note=$(goal_check "$goal" usable "$session" "$job_id" 2>&1); case $? in
    0) [ -n "$note" ] && echo "run_detached_job: $note" >&2 ;;
    *) reason=$(printf '%s\n' "$note" | grep -v '^WAITING ')
       case $note in *"WAITING bg1:"*)  # never probe that job: a status check would heartbeat it
         reason="$reason; if that job has already ended, read its result (job detail, its LOG.exit) and checkpoint the goal with what you learned, then launch again";;
       esac
       echo "run_detached_job: goal $goal cannot carry this wake: $reason" >&2; exit 4 ;;
  esac
fi
mkdir -p "$(dirname "$log")" || exit 1
nonce=$(date +%s%N)-$$-$RANDOM
run=$log.$nonce
rm -f "$log.exit" "$log.unit"
due=$(( ($(date +%s) + deadline) * 1000 ))
set -- "${@:1:5}" "${goal:--}" "$nonce" "${@:6}"

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
  [ -s "$run.handle" ] && { handle=$(cat "$run.handle"); break; }
  [ -s "$run.handle.err" ] && { cat "$run.handle.err" >&2; cleanup; exit 1; }
  sleep 0.1
done
if [ -z "$handle" ]; then
  # Never leave an unregistered worker to run the command unseen.
  if [ -n "$unit" ]; then systemctl --user stop "$unit" >/dev/null 2>&1
  elif [ -n "$worker" ]; then kill -TERM "$worker" 2>/dev/null; fi
  cleanup
  sleep 1
  if [ -s "$run.handle" ]; then  # it registered just as it was stopped: close that job
    "${BG[@]}" "$session" job-cancel "$(cat "$run.handle")" >/dev/null 2>&1
    echo "run_detached_job: worker registered only after 30 s and was stopped; job $(cat "$run.handle") is closed as cancelled; see $log.worker.err" >&2
  else
    echo "run_detached_job: worker did not register within 30 s and was stopped; see $log.worker.err" >&2
  fi
  exit 1
fi

ready() {  # the command started: say so, whatever led here
  rm -f "$run.handle" "$run.go" "$run.decided"
  echo "$handle"
  [ -n "${1:-}" ] && echo "run_detached_job: $1" >&2
  [ "$nowake" = 1 ] && echo "run_detached_job: started without a wake (--no-wake)" >&2
  exit 0
}
not_started() {  # make the decision "not started", unless the worker already started
  if decide notstarted; then  # the worker reads this decision and cleans up
    echo "run_detached_job: $1; the command did NOT run (job $handle closed as not started)" >&2
    exit 3
  fi
  if [ "$(decision)" = notstarted ]; then
    rm -f "$run.handle" "$run.go" "$run.decided"
    echo "run_detached_job: $1; the command did NOT run (job $handle closed as not started)" >&2
    exit 3
  fi
  ready "the goal's dependency on $handle is stored and the worker started the command ($1 was only a failed confirmation)"
}
if [ -n "$goal" ]; then
  # Store the dependency, then read it back: only a stored wake is a wake.
  error=""
  for attempt in 1 2 3; do
    body=$(goal_check "$goal" body "$job_id" "$handle" "$log" "$due" 2>&1) || { error=$body; break; }
    rev=$(printf '%s\n' "$body" | head -n 1)
    error=$("${GOAL[@]}" checkpoint "$goal" "$rev" "$(printf '%s\n' "$body" | tail -n +2)" 2>&1 >/dev/null) && { error=""; break; }
    case $error in *"plan changed"*) sleep "$attempt";; *) break;; esac
  done
  [ -z "$error" ] || not_started "the goal dependency was not stored: $error"
  attached=1
  for attempt in 1 2 3; do
    goal_check "$goal" attached "$job_id" "$handle" 2>/dev/null && { attached=0; break; }
    sleep "$attempt"
  done
  [ "$attached" = 0 ] || not_started "the goal dependency did not read back"
  : > "$run.go"
fi
outcome=""
for _ in $(seq 300); do
  outcome=$(cat "$run.decided" 2>/dev/null)
  [ -n "$outcome" ] && break
  sleep 0.1
done
case $outcome in
  started) ready ;;
  notstarted)
    rm -f "$run.handle" "$run.go" "$run.decided"
    echo "run_detached_job: the worker gave up waiting for its wake and did NOT run the command; the goal's dependency is stored, so the goal will wake you with job $handle ended as not started" >&2
    exit 3 ;;
esac
echo "run_detached_job: job $handle is registered but did not report starting; see $log.worker.err" >&2
exit 1
