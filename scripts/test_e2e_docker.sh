#!/usr/bin/env bash
# Run the Playwright browser suite against a throwaway Clarp container.
#
# The container gets a fresh data volume, a known auth token, and the built-in
# roster, then is thrown away. Nothing here touches a real Clarp install.
#
#   scripts/test_e2e_docker.sh                 # build image, run every spec
#   scripts/test_e2e_docker.sh basic.spec.js   # pass-through Playwright args
#   CLARP_E2E_SKIP_BUILD=1 scripts/test_e2e_docker.sh   # reuse clarp:test
set -euo pipefail

IMAGE="${CLARP_TEST_IMAGE:-clarp:test}"
TOKEN="${CLARP_E2E_TOKEN:-test}"
NAME="clarp-e2e-$$"
VOLUME="clarp-e2e-$$"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

start_node() {
    # An ephemeral loopback port: several runs can coexist, and nothing else
    # on the machine (least of all a live Clarp on 7682) is ever addressed.
    docker run -d --name "$NAME" -p "127.0.0.1::7682" \
        -v "$VOLUME:/data" "$IMAGE" >/dev/null
}

wait_healthy() {
    for _ in $(seq 1 90); do
        status="$(docker inspect "$NAME" \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}')"
        [[ "$status" == healthy ]] && return 0
        if [[ "$(docker inspect "$NAME" --format '{{.State.Running}}')" != true ]]; then
            docker logs "$NAME"
            echo "clarp container exited before becoming healthy" >&2
            exit 1
        fi
        sleep 1
    done
    docker logs "$NAME"
    echo "clarp container never became healthy" >&2
    exit 1
}

if [[ "${CLARP_E2E_SKIP_BUILD:-0}" != 1 ]]; then
    docker build -t "$IMAGE" .
fi
docker volume create "$VOLUME" >/dev/null
start_node
wait_healthy

# The specs sign in with a fixed token and expect the built-in roster.
docker exec -i "$NAME" python3 - "$TOKEN" <<'PY'
import pathlib
import sys

from lib.config_writer import set_toml_value
from lib.deployment import LAYOUT
from lib.roster_seed import seed_defaults

set_toml_value(LAYOUT.config_file, "server", "auth_token", sys.argv[1])
seed_defaults("claude", cwd=pathlib.Path("/data/workspace"))
PY
docker restart "$NAME" >/dev/null
wait_healthy

port="$(docker port "$NAME" 7682/tcp | head -1 | sed 's/.*://')"
export CLARP_BASE_URL="http://127.0.0.1:${port}"
export CLARP_E2E_TOKEN="$TOKEN"
echo "playwright -> $CLARP_BASE_URL (container $NAME)"
npx playwright test "$@"
