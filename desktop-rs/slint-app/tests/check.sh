#!/usr/bin/env bash
# A headless behaviour check of the Slint app against the fake Host (no
# display, private bus, scratch config): slint-app/tests/check.sh NAME [OUT]
set -uo pipefail
cd "$(dirname "$0")/../.."
name=$1; out=$(realpath -m "${2:-slint-app/docs/checks}"); mkdir -p "$out"
scratch=$(mktemp -d /var/tmp/clarp-slint-check.XXXXXX)
trap 'kill "$host" 2>/dev/null; wait "$host" 2>/dev/null; rm -rf "$scratch"' EXIT
cp slint-app/docs/screens/markdown-paper.png "$scratch/photo.png"
/usr/bin/python3 app/tests/fake_host.py --port-file "$scratch/port" --log "$scratch/host.log" &
host=$!
for _ in $(seq 50); do [ -s "$scratch/port" ] && break; sleep 0.1; done
env CLARP_BASE_URL="http://127.0.0.1:$(cat "$scratch/port")" CLARP_TOKEN=probe-token CLARP_SETTINGS=off \
    XDG_CONFIG_HOME="$scratch/c" XDG_CACHE_HOME="$scratch/k" XDG_DATA_HOME="$scratch/d" XDG_STATE_HOME="$scratch/s" \
    CLARP_TEST_OPEN_URL="$scratch/urls" CLARP_TEST_HOST_LOG="$scratch/host.log" \
    CLARP_TEST_ATTACH_FILE="$scratch/photo.png" \
    timeout 120 dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- \
    target/debug/clarp-slint --check "$name" --out "$out" 2>&1 | tee "$scratch/run.log" | grep -E "^(ok|FAIL|E2E)"
grep -q "^E2E_PASS" "$scratch/run.log"
