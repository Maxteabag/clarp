#!/usr/bin/env bash
# End-to-end: the Rust desktop against a real Clarp Host, offline and headless.
# The Host is tests/qa/host.py (production server, deterministic Codex
# provider, every path inside a scratch directory). Host and desktop run
# together in a private network namespace with only loopback, so nothing
# reaches the network, with their own HOME (no real CLI credentials) and a
# system-only PATH (no real agent CLIs). The desktop runs offscreen on a
# private session bus. Screenshots of each stage land in OUT.
# Usage (from desktop-rs/): app/tests/run-e2e.sh [OUT]
set -uo pipefail
cd "$(dirname "$0")/../.."
repo=$(cd .. && pwd)
out=$(realpath -m "${1:-/var/tmp/clarp-e2e}")
venv=${CLARP_E2E_VENV:-/var/tmp/clarp-e2e-venv}
mkdir -p "$out"
scratch=$(mktemp -d /var/tmp/clarp-e2e.XXXXXX)
trap 'rm -rf "$scratch"' EXIT
# The Host's locked dependencies, in a disposable environment (the only step
# that uses the network, and only when the environment is missing).
if [ ! -x "$venv/bin/python" ]; then
    (cd "$repo" && UV_PROJECT_ENVIRONMENT="$venv" uv sync --frozen --no-dev --quiet) || exit 1
fi
mkdir -p "$scratch/home" "$scratch/run" && chmod 700 "$scratch/run"
export E2E_SCRATCH="$scratch" E2E_REPO="$repo" E2E_VENV="$venv" E2E_OUT="$out" E2E_APP="$PWD"
timeout 180 unshare --user --map-root-user --net bash -c '
    set -uo pipefail
    ip link set lo up || exit 1
    s=$E2E_SCRATCH
    # The provider streams its reply over ~3 s so the desktop can be seen
    # showing it mid-stream.
    env -i HOME="$s/home" PATH=/usr/bin:/bin LANG=C.UTF-8 CLARP_QA_STREAM_DELAY=1.0 \
        "$E2E_VENV/bin/python" "$E2E_REPO/tests/qa/host.py" --state-dir "$s/host" --port 0 > "$s/host.log" 2>&1 &
    host=$!
    for _ in $(seq 100); do [ -s "$s/host/host.json" ] && break; sleep 0.1; done
    base=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[\"url\"])" "$s/host/host.json") || { kill $host; exit 1; }
    case "$base" in http://127.0.0.1:*) ;; *) echo "refusing a Host that is not on loopback: $base"; kill $host; exit 1 ;; esac
    env -i HOME="$s/home" PATH=/usr/bin:/bin LANG=C.UTF-8 \
        CLARP_BASE_URL="$base" CLARP_TOKEN=qa-host-test CLARP_SETTINGS="$s/settings.json" \
        XDG_CONFIG_HOME="$s/config" XDG_CACHE_HOME="$s/cache" XDG_DATA_HOME="$s/data" \
        XDG_STATE_HOME="$s/state" XDG_RUNTIME_DIR="$s/run" CLARP_WORKSPACE_STORE="$s/workspaces.json" \
        CLARP_AUDIO_OUTPUT=null CLARP_AUDIO_INPUT=none CLARP_TEST_OPEN_URL="$s/urls" CLARP_TEST_CLIPBOARD="$s/clip" \
        QT_QPA_PLATFORM=offscreen QT_FORCE_STDERR_LOGGING=1 CLARP_RS_QML="$E2E_APP/app/tests/e2e/e2e_main.qml" \
        timeout 120 dbus-run-session --config-file="$E2E_APP/tests/private-bus.conf" -- \
        "$E2E_APP/target/debug/clarp-desktop" "--e2e-out=$E2E_OUT" > "$s/app.log" 2>&1
    code=$?
    kill $host; wait $host 2>/dev/null
    exit $code
'
code=$?
grep -E "^qml: (ok|FAIL|E2E|streaming)" "$scratch/app.log" | sed 's/^qml: //'
if grep -q "codexUsageFetched used=\|chatgpt.com" "$scratch/host.log"; then
    echo "FAIL the Host reached a provider account"; code=1
fi
if [ "$code" -ne 0 ] || ! grep -q "E2E_PASS" "$scratch/app.log"; then
    echo "--- app log"; grep -v "dbind\|portal" "$scratch/app.log" | tail -30
    echo "--- host log"; tail -30 "$scratch/host.log"
    exit 1
fi
ls "$out"/*.png
