#!/usr/bin/env bash
# Run the Playwright browser suite against a throwaway Clarp container.
#
# The container gets a fresh data volume, a paired-device token, and the built-in
# roster, then is thrown away. Nothing here touches a real Clarp install.
#
#   scripts/test_e2e_docker.sh                 # build image, run every spec
#   scripts/test_e2e_docker.sh basic.spec.js   # pass-through Playwright args
#   CLARP_E2E_SKIP_BUILD=1 scripts/test_e2e_docker.sh   # reuse clarp:test
set -euo pipefail

IMAGE="${CLARP_TEST_IMAGE:-clarp:test}"
NAME="clarp-e2e-$$"
VOLUME="clarp-e2e-$$"
NETWORK="clarp-e2e-net-$$"
# shellcheck source=scripts/ci_docker_offline.sh
. "$(dirname "$0")/ci_docker_offline.sh"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
    ci_offline_cleanup
}
trap cleanup EXIT

start_node() {
    # The run's own internal network, no published port: several runs can coexist, nothing else on
    # the machine (least of all a live Clarp on 7682) is ever addressed, and the container has no
    # route out.
    docker run -d --name "$NAME" "${OFFLINE[@]}" \
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
ci_offline_create "$NETWORK"
ci_offline_preflight "$IMAGE"
mapfile -t OFFLINE < <(ci_offline_run_args)
docker volume create "$VOLUME" >/dev/null
start_node
wait_healthy
ci_offline_assert_inside "$NAME"

# Seed the test roster while retaining the generated local administrator token.
docker exec -i "$NAME" python3 - <<'PY'
import pathlib
import sys

from lib.roster_seed import seed_defaults

seed_defaults("claude", cwd=pathlib.Path("/data/workspace"))
PY
docker restart "$NAME" >/dev/null
wait_healthy
ci_offline_assert_inside "$NAME"

ci_offline_forward "$NAME"
CLARP_BASE_URL="$CI_OFFLINE_URL"
export CLARP_BASE_URL
export CLARP_E2E_TOKEN="$(python3 scripts/pair_docker_test_client.py "$NAME" "$CLARP_BASE_URL")"
echo "playwright -> $CLARP_BASE_URL (container $NAME)"
npx playwright test "$@"
