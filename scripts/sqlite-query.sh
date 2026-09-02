#!/usr/bin/env bash
# Query the live Clarp SQLite stores: state.sqlite (main) with telemetry.sqlite
# attached as `telemetry`.
#
#   ./scripts/sqlite-query.sh
#   ./scripts/sqlite-query.sh "SELECT * FROM telemetry.trace_paths ORDER BY last_at DESC LIMIT 10"
set -euo pipefail

DB="${CLAUDE_PWA_DB:-$HOME/.local/share/clarp/state.sqlite}"
TELEMETRY="${CLARP_TELEMETRY_DB:-$HOME/.local/share/clarp/telemetry.sqlite}"
mkdir -p "$(dirname "$DB")"

exec sqlite3 -cmd ".headers on" -cmd ".mode column" \
  -cmd "ATTACH DATABASE '$TELEMETRY' AS telemetry" "$DB" "$@"
