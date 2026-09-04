#!/usr/bin/env bash
# Run a Clarp deployment independently of the shell/agent that requested it.
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="${CLAUDE_PWA_DEPLOY_UNIT:-clarp-deploy}"
LOG="${CLAUDE_PWA_DEPLOY_LOG:-$HOME/.cache/clarp/deploy.log}"

usage() {
    cat <<EOF
Usage: $0 [start|status|log|wait]

start   Start a detached make deploy. Agent turns remain in clarp-runtime.
status  Show the deploy unit status and the latest log lines.
log     Print the latest deploy log lines.
wait    Wait for the deploy unit to exit, then show status.
EOF
}

unit_active() {
    systemctl --user is-active --quiet "$UNIT.service"
}

show_log() {
    local lines="${1:-120}"
    if [[ -f "$LOG" ]]; then
        tail -n "$lines" "$LOG"
    else
        echo "No deploy log yet: $LOG"
    fi
}

show_status() {
    systemctl --user status "$UNIT.service" --no-pager || true
    echo
    show_log 60
}

start_deploy() {
    mkdir -p "$(dirname -- "$LOG")"
    chmod 700 "$(dirname -- "$LOG")"
    if unit_active; then
        echo "Deploy already running as $UNIT.service"
        echo "Log: $LOG"
        return
    fi
    if ! command -v systemd-run >/dev/null 2>&1; then
        echo "systemd-run is required for detached deploys" >&2
        return 127
    fi
    {
        printf '\n=== Clarp deploy started %s ===\n' "$(date -Is)"
        printf 'repo=%s\n' "$REPO_DIR"
        printf 'agent_runtime=restart-independent\n'
    } >> "$LOG"
    local command
    printf -v command 'cd %q && make deploy >> %q 2>&1' "$REPO_DIR" "$LOG"
    systemd-run --user --unit="$UNIT" --collect /usr/bin/env bash -lc "$command"
    echo "Started $UNIT.service"
    echo "Log: $LOG"
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
    -h|--help|help) usage ;;
    *) usage >&2; exit 2 ;;
esac
