#!/bin/sh
# Run every offscreen QML probe against the built Rust desktop binary.
# Usage (from desktop-rs/): app/tests/run-qml-probes.sh [target/debug/clarp-desktop]
set -u
binary=${1:-target/debug/clarp-desktop}
status=0
# Probes must never reach the user's real Host (default 127.0.0.1:7682, token
# from ~/.config/clarp/config.toml): point at a refused port with a dummy
# token and keep settings in memory unless a probe starts its own fake Host.
export CLARP_BASE_URL="${CLARP_BASE_URL:-http://127.0.0.1:9}"
export CLARP_TOKEN="${CLARP_TOKEN:-probe-token}"
export CLARP_SETTINGS="${CLARP_SETTINGS:-off}"
scratch=$(mktemp -d /var/tmp/clarp-qml-probes.XXXXXX)
trap 'rm -rf "$scratch"' EXIT
for probe in app/tests/qml/*_probe.qml; do
    # Each probe gets its own workspace store, never the user's layout.
    name=$(basename "$probe" .qml)
    store="$scratch/$name/workspaces.json"
    mkdir -p "$scratch/$name"
    base_url="$CLARP_BASE_URL"
    host_pid=""
    if grep -q 'needs: fake-host' "$probe"; then
        python3 app/tests/fake_host.py --port-file "$scratch/$name/port" --log "$scratch/$name/host.log" &
        host_pid=$!
        for _ in $(seq 50); do [ -s "$scratch/$name/port" ] && break; sleep 0.1; done
        base_url="http://127.0.0.1:$(cat "$scratch/$name/port")"
    fi
    # "// env: KEY=VALUE" lines set per-probe variables; $SCRATCH is the
    # probe's scratch directory.
    probe_env=$(sed -n 's#^// env: ##p' "$probe" | sed "s#\$SCRATCH#$scratch/$name#g")
    output=$(env $probe_env QT_FORCE_STDERR_LOGGING=1 QT_QPA_PLATFORM=offscreen CLARP_RS_QML="$PWD/$probe" \
        CLARP_WORKSPACE_STORE="$store" QML_XHR_ALLOW_FILE_READ=1 QML_XHR_ALLOW_FILE_WRITE=1 \
        CLARP_BASE_URL="$base_url" \
        timeout 60 "$binary" "--probe-store=$store" "--probe-host-log=$scratch/$name/host.log" 2>&1)
    code=$?
    if [ -n "$host_pid" ]; then kill "$host_pid" 2>/dev/null; wait "$host_pid" 2>/dev/null; fi
    if [ "$code" -eq 0 ] && printf '%s\n' "$output" | grep -q PROBE_PASS; then
        echo "pass $probe"
    else
        echo "FAIL $probe (exit $code)"
        printf '%s\n' "$output" | grep -E 'FAIL|rror' | head -20
        status=1
    fi
done
exit $status
