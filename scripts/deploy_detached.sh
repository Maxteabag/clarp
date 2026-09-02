#!/usr/bin/env bash
# Start or inspect a claude-pwa deploy that survives interrupted shells.
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="${CLAUDE_PWA_DEPLOY_UNIT:-claude-pwa-deploy}"
LOG="${CLAUDE_PWA_DEPLOY_LOG:-$HOME/.cache/clarp/deploy.log}"
STATE_DB="${CLAUDE_PWA_STATE_DB:-$HOME/.local/share/clarp/state.sqlite}"
RESUME_FILE="${CLAUDE_PWA_DEPLOY_RESUME_FILE:-$HOME/.cache/clarp/deploy-resume-agents.txt}"
PYTHON_BIN="${PYTHON:-python3}"

usage() {
    cat <<EOF
Usage: $0 [start|status|log|wait]

start   Start a detached make deploy, then send "continue" to active agents.
status  Show the deploy unit status and the latest log lines.
log     Print the latest deploy log lines.
wait    Wait for the deploy unit to exit, then show status.
EOF
}

ensure_log_dir() {
    mkdir -p "$(dirname -- "$LOG")"
    chmod 700 "$(dirname -- "$LOG")"
}

unit_active() {
    systemctl --user is-active --quiet "$UNIT.service"
}

service_main_pid() {
    systemctl --user show clarp.service -p MainPID --value 2>/dev/null || true
}

descendants_of() {
    local parent="$1"
    local child
    while read -r child; do
        [[ -n "$child" ]] || continue
        printf '%s\n' "$child"
        descendants_of "$child"
    done < <(pgrep -P "$parent" 2>/dev/null || true)
}

active_agent_processes() {
    local main_pid
    main_pid="$(service_main_pid)"
    if [[ -z "$main_pid" || "$main_pid" == "0" ]]; then
        return 0
    fi

    local pid args
    while read -r pid; do
        [[ -n "$pid" ]] || continue
        args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
        [[ -n "$args" ]] || continue
        case "$args" in
            *"codex exec"*|*"clarp "*|*"clarp-cli"*|*"agy "*|*"claude --"*|*"claude -p"*)
                printf '%s %s\n' "$pid" "$args"
                ;;
        esac
    done < <(descendants_of "$main_pid")
}

snapshot_resume_sessions() {
    mkdir -p "$(dirname -- "$RESUME_FILE")"
    chmod 700 "$(dirname -- "$RESUME_FILE")"
    : > "$RESUME_FILE"
    if [[ ! -f "$STATE_DB" ]] || ! command -v sqlite3 >/dev/null 2>&1; then
        return 0
    fi

    sqlite3 -noheader "$STATE_DB" <<'SQL' > "$RESUME_FILE.tmp" || true
WITH latest AS (
    SELECT agent_id, MAX(state_id) AS state_id
    FROM state_log
    GROUP BY agent_id
)
SELECT a.session
FROM latest l
JOIN state_log s ON s.state_id = l.state_id
JOIN agents a ON a.agent_id = s.agent_id
WHERE a.deleted_at IS NULL
  AND s.kind IN ('thinking', 'tool', 'compacting')
ORDER BY s.ts DESC;
SQL
    active_agent_processes > "$RESUME_FILE.procs" || true
    "$PYTHON_BIN" - "$STATE_DB" "$RESUME_FILE.procs" >> "$RESUME_FILE.tmp" <<'PY' || true
import sqlite3
import sys
from pathlib import Path

db_path = sys.argv[1]
process_lines = Path(sys.argv[2]).read_text().splitlines()
if not process_lines:
    raise SystemExit(0)

conn = sqlite3.connect(db_path)
try:
    rows = conn.execute("""
        SELECT a.session, r.backend_session_id
          FROM agents a
          JOIN runtimes r ON r.agent_id = a.agent_id
         WHERE a.deleted_at IS NULL
           AND r.ended_at IS NULL
           AND r.backend_session_id IS NOT NULL
           AND r.backend_session_id != ''
    """).fetchall()
finally:
    conn.close()

for line in process_lines:
    for session, backend_session_id in rows:
        if backend_session_id and backend_session_id in line:
            print(session)
PY
    sed '/^[[:space:]]*$/d' "$RESUME_FILE.tmp" | awk '!seen[$0]++' > "$RESUME_FILE"
    rm -f "$RESUME_FILE.tmp" "$RESUME_FILE.procs"
}

config_value() {
    local key="$1"
    "$PYTHON_BIN" - "$key" <<'PY'
import os
import pathlib
import sys
import tomllib

key = sys.argv[1]
path = pathlib.Path(os.environ.get(
    "CLAUDE_PWA_CONFIG",
    pathlib.Path.home() / ".config" / "clarp" / "config.toml",
))
data = {}
try:
    data = tomllib.loads(path.read_text())
except Exception:
    pass
server = data.get("server", {}) if isinstance(data, dict) else {}
if key == "base_url":
    bind = str(server.get("bind_addr", "127.0.0.1")).strip() or "127.0.0.1"
    if bind in {"0.0.0.0", "::"}:
        bind = "127.0.0.1"
    if ":" in bind and not bind.startswith("["):
        bind = f"[{bind}]"
    port = int(server.get("port", 7682))
    print(f"http://{bind}:{port}")
elif key == "auth_token":
    print(str(server.get("auth_token", "")))
PY
}

wait_for_server() {
    local base token auth_args=()
    base="$(config_value base_url)"
    token="$(config_value auth_token)"
    if [[ -n "$token" ]]; then
        auth_args=(-H "Authorization: Bearer $token")
    fi
    for _ in {1..30}; do
        if curl -fsS --max-time 2 "${auth_args[@]}" "$base/server-info" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

resume_sessions() {
    local file="${1:-$RESUME_FILE}"
    [[ -f "$file" ]] || return 0
    mapfile -t sessions < "$file"
    [[ "${#sessions[@]}" -gt 0 ]] || return 0
    if ! wait_for_server; then
        echo "WARNING: server did not become ready; skipped post-deploy resume" >&2
        return 1
    fi

    local base token session payload auth_args=()
    base="$(config_value base_url)"
    token="$(config_value auth_token)"
    if [[ -n "$token" ]]; then
        auth_args=(-H "Authorization: Bearer $token")
    fi

    echo "Resuming ${#sessions[@]} active agent(s): ${sessions[*]}"
    for session in "${sessions[@]}"; do
        [[ -n "$session" ]] || continue
        payload="$("$PYTHON_BIN" - "$session" <<'PY'
import json
import sys

print(json.dumps({
    "session": sys.argv[1],
    "text": "continue",
    "force_session": True,
    "hands_free": False,
    "origin": "automation",
    "synthesize_audio": False,
}))
PY
)"
        if response="$(curl -fsS --max-time 10 -X POST \
            -H "Content-Type: application/json" "${auth_args[@]}" \
            --data "$payload" "$base/send")"; then
            echo "resumed $session response=$response"
        else
            echo "WARNING: failed to resume $session" >&2
        fi
    done
}

start_deploy() {
    ensure_log_dir
    if unit_active; then
        echo "Deploy already running as $UNIT.service"
        echo "Log: $LOG"
        exit 0
    fi
    if ! command -v systemd-run >/dev/null 2>&1; then
        echo "systemd-run is required for detached deploys" >&2
        exit 127
    fi

    snapshot_resume_sessions
    {
        printf '\n=== claude-pwa deploy started %s ===\n' "$(date -Is)"
        printf 'repo=%s\n' "$REPO_DIR"
        if [[ -s "$RESUME_FILE" ]]; then
            printf 'will_resume_sessions=%s\n' "$(tr '\n' ' ' < "$RESUME_FILE")"
            active_agent_processes | sed 's/^/active_process=/' || true
        else
            printf 'will_resume_sessions=\n'
        fi
    } >> "$LOG"

    local cmd
    printf -v cmd \
        'cd %q && make deploy >> %q 2>&1; rc=$?; if [[ $rc -eq 0 ]]; then %q resume %q >> %q 2>&1 || true; fi; exit $rc' \
        "$REPO_DIR" "$LOG" "$REPO_DIR/scripts/deploy_detached.sh" "$RESUME_FILE" "$LOG"
    systemd-run --user --unit="$UNIT" --collect /usr/bin/env bash -lc "$cmd"
    echo "Started $UNIT.service"
    echo "Log: $LOG"
    if [[ -s "$RESUME_FILE" ]]; then
        echo "Will resume active agents after restart: $(tr '\n' ' ' < "$RESUME_FILE")"
    fi
}

show_status() {
    systemctl --user status "$UNIT.service" --no-pager || true
    echo
    show_log 60
}

show_log() {
    local lines="${1:-120}"
    if [[ -f "$LOG" ]]; then
        tail -n "$lines" "$LOG"
    else
        echo "No deploy log yet: $LOG"
    fi
}

wait_deploy() {
    while unit_active; do
        sleep 1
    done
    show_status
}

case "${1:-start}" in
    start) start_deploy ;;
    status) show_status ;;
    log) show_log "${2:-120}" ;;
    wait) wait_deploy ;;
    resume) resume_sessions "${2:-$RESUME_FILE}" ;;
    -h|--help|help) usage ;;
    *)
        usage >&2
        exit 2
        ;;
esac
