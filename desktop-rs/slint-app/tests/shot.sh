#!/usr/bin/env bash
# A headless screenshot of the Slint app against the fake Host (no display,
# private bus, scratch config): slint-app/tests/shot.sh OUT.png SESSION [THEME]
set -uo pipefail
cd "$(dirname "$0")/../.."
out=$(realpath -m "$1"); session=${2:-rachel}; theme=${3:-terminal}
scratch=$(mktemp -d /var/tmp/clarp-slint-shot.XXXXXX)
trap 'kill "$host" 2>/dev/null; wait "$host" 2>/dev/null; rm -rf "$scratch"' EXIT
/usr/bin/python3 app/tests/fake_host.py --port-file "$scratch/port" --log "$scratch/host.log" &
host=$!
for _ in $(seq 50); do [ -s "$scratch/port" ] && break; sleep 0.1; done
env CLARP_BASE_URL="http://127.0.0.1:$(cat "$scratch/port")" CLARP_TOKEN=probe-token CLARP_SETTINGS=off \
    XDG_CONFIG_HOME="$scratch/c" XDG_CACHE_HOME="$scratch/k" XDG_DATA_HOME="$scratch/d" XDG_STATE_HOME="$scratch/s" \
    CLARP_TEST_OPEN_URL="$scratch/urls" \
    timeout 60 dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- \
    target/debug/clarp-slint --shot "$out" --select "$session" --theme "$theme" 2>&1 | grep -E "^(ok|FAIL|E2E)"
