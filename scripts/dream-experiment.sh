#!/usr/bin/env bash
# Start one dream per seeding arm, now.
#
# The nightly scheduler resolves 03:00 from the phone's last known position,
# which is the right default and the wrong thing to depend on when a specific
# comparison has to run on a specific night. This pins one strategy per agent
# so a single night covers every arm, and logs what it started.
set -uo pipefail

HOST="${CLARP_HOST:-http://127.0.0.1:7682}"
LOG="${CLARP_DREAM_LOG:-$HOME/.cache/clarp/dream-experiment.log}"
mkdir -p "$(dirname "$LOG")"

TOKEN="$(python3 - <<'PY'
import pathlib, tomllib
p = pathlib.Path.home() / ".config/clarp/config.toml"
print(tomllib.loads(p.read_text()).get("server", {}).get("auth_token", "")
      if p.exists() else "")
PY
)"

# session:strategy:dose  — dose empty means "let the strategy's own default win"
ARMS=(
  "mike-5f22:control:full"
  "dream-lenses:lenses:fragments"
  "dream-foreign:foreign:none"
  "dream-roleplay:roleplay:none"
)

echo "=== dream experiment $(date -Is) ===" >>"$LOG"
for arm in "${ARMS[@]}"; do
  IFS=: read -r session strategy dose <<<"$arm"
  body=$(printf '{"session":"%s","seed_strategy":"%s","context_dose":"%s"}' \
    "$session" "$strategy" "$dose")
  response=$(curl -sS -m 60 -X POST \
    ${TOKEN:+-H "Authorization: Bearer $TOKEN"} \
    -H "Content-Type: application/json" \
    -d "$body" "$HOST/dreaming/run" 2>&1)
  echo "$session $strategy/$dose -> $response" >>"$LOG"
  # Stagger: four Codex sessions starting in the same second contend for the
  # same CLI auth and app-server startup for no benefit — the night is long.
  sleep 20
done
echo "=== done $(date -Is) ===" >>"$LOG"
