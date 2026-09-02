#!/usr/bin/env bash
# Open a DuckDB shell with the claude-pwa eventlog views pre-loaded.
#
#   ./scripts/query.sh                      # interactive shell
#   ./scripts/query.sh "SELECT * FROM heralds LIMIT 10"
#
# Inside the shell, .help / .tables / .schema view_name all work.
set -euo pipefail

LOG_DIR="${CLAUDE_PWA_LOG_DIR:-$HOME/.cache/clarp/logs}"
mkdir -p "$LOG_DIR"

# At least one JSONL file must exist for DuckDB's read_json_auto to bind.
shopt -s nullglob
if ! compgen -G "$LOG_DIR/*.jsonl" > /dev/null; then
  touch "$LOG_DIR/$(date -u +%F).jsonl"
fi

VIEWS_FILE="$(dirname "$(readlink -f "$0")")/views.sql"

CLAUDE_PWA_LOG_DIR="$LOG_DIR" duckdb \
  -cmd "SET enable_progress_bar=false;" \
  -cmd ".read '$VIEWS_FILE'" \
  ${1:+-c "$1"}
