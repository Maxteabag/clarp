#!/usr/bin/env bash
# Run only after the user has authorized deployment of this verified ref.
set -euo pipefail
dry_run=false
if [[ ${1:-} == --dry-run ]]; then dry_run=true; shift; fi
if [[ $# != 3 || ! $2 =~ ^[0-9a-f]{40}$ ]]; then
    echo 'usage: update_with_job.sh [--dry-run] SESSION FULL_SHA PRIVATE_STATE_DIR' >&2
    exit 2
fi
session=$1
ref=$2
state_dir=$3
if $dry_run; then
    printf 'Authorized deployment command: clarp-admin update --ref %s\n' "$ref"
    printf 'Worker state: %s\nSession: %s\n' "$state_dir" "$session"
    exit 0
fi
umask 077
mkdir -p "$state_dir"
export CLARP_BACKGROUND_WORKER_PID=$$
handle=$(clarp-agent-bg "$session" job-upsert "host-update-${ref:0:12}" server-update 'Installing Host update' "$ref")
printf '%s\n' "$handle" > "$state_dir/job-handle"
clarp-agent-bg "$session" job-active "$handle"
(while sleep 45; do clarp-agent-bg "$session" job-heartbeat "$handle" || exit; done) &
heartbeat_pid=$!
trap 'kill "$heartbeat_pid" 2>/dev/null || true' EXIT
set +e
clarp-admin update --ref "$ref"
result=$?
set -e
kill "$heartbeat_pid" 2>/dev/null || true
wait "$heartbeat_pid" 2>/dev/null || true
if [[ $result == 0 ]]; then
    clarp-agent-bg "$session" job-finish "$handle"
else
    clarp-agent-bg "$session" job-fail "$handle" "Installer exited $result; inspect the private update log."
fi
printf '%s\n' "$result" > "$state_dir/installer-exit"
exit "$result"
