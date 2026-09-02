#!/usr/bin/env bash
# Find Claude Code backend session IDs by agent persona name or session name.
#
# Usage:
#   ./scripts/find-session.sh [agent_name]
#
set -euo pipefail

DB="${CLAUDE_PWA_DB:-$HOME/.local/share/clarp/state.sqlite}"

if [[ ! -f "$DB" ]]; then
  echo "Error: Database not found at $DB" >&2
  exit 1
fi

QUERY="SELECT 
    r.runtime_id,
    a.persona,
    r.session AS app_session,
    r.backend_session_id,
    datetime(r.started_at/1000, 'unixepoch', 'localtime') AS started_at,
    CASE WHEN r.ended_at IS NULL THEN 'ACTIVE' ELSE datetime(r.ended_at/1000, 'unixepoch', 'localtime') END AS status
FROM runtimes r
JOIN agents a USING (agent_id)"

if [[ $# -gt 0 ]]; then
  FILTER="$1"
  QUERY="$QUERY WHERE a.persona LIKE '%$FILTER%' OR r.session LIKE '%$FILTER%'"
fi

QUERY="$QUERY ORDER BY r.started_at DESC;"

exec sqlite3 -header -column "$DB" "$QUERY"
