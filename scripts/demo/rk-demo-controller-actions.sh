#!/usr/bin/env bash
# Seed the Ringerikskraft demo's controller actions on this Host. Safe to rerun:
# an existing action is updated in place. Mapping gestures is an explicit
# opt-in because it replaces their voice behaviour (swipe up replays, swipe
# down skips):
#   scripts/demo/rk-demo-controller-actions.sh            # actions only
#   scripts/demo/rk-demo-controller-actions.sh --map      # + small swipe up/down
set -euo pipefail
url="${RK_SCENARIO_URL:-http://127.0.0.1:8765/api/scenario}"
feed="${RK_FEED:-demo-cam-02}"
read -r -a admin <<< "${CLARP_ADMIN:-clarp-admin}"

seed() {
    local id=$1 title=$2 clip=$3 body
    body=$(printf '{"feed":"%s","clip":"%s"}' "$feed" "$clip")
    "${admin[@]}" controller-action add "$id" --title "$title" --http-post "$url" --body "$body" 2>/dev/null \
        || "${admin[@]}" controller-action update "$id" --title "$title" --http-post "$url" --body "$body" --enable
}

seed rk-demo-leak "Demo: leak" leak
seed rk-demo-reset "Demo: reset" normal
if [[ "${1:-}" == "--map" ]]; then
    "${admin[@]}" controller-action map --button secondary --gesture swipe-up --action rk-demo-leak
    "${admin[@]}" controller-action map --button secondary --gesture swipe-down --action rk-demo-reset
fi
"${admin[@]}" controller-action list
