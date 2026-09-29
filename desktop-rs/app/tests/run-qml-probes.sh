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
# Probes never play through the speakers; a probe opts into a test output
# with "// env: CLARP_AUDIO_OUTPUT=null" or "=decode".
export CLARP_AUDIO_OUTPUT="${CLARP_AUDIO_OUTPUT:-none}"
# Nor the microphone: "// env: CLARP_AUDIO_INPUT=file:$FIXTURES/dictation.wav".
export CLARP_AUDIO_INPUT="${CLARP_AUDIO_INPUT:-none}"
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
    # "// fixture: NAME TEXT" lines write files into the probe's scratch
    # directory (next to host.log), for probes that need real files.
    sed -n 's#^// fixture: ##p' "$probe" | while read -r fixture text; do
        printf '%s' "$text" > "$scratch/$name/$fixture"
    done
    # "// env: KEY=VALUE" lines set per-probe variables; $SCRATCH is the
    # probe's scratch directory, $FIXTURES app/tests/fixtures.
    probe_env=$(sed -n 's#^// env: ##p' "$probe" | sed "s#\$SCRATCH#$scratch/$name#g; s#\$FIXTURES#$PWD/app/tests/fixtures#g")
    # Every probe gets a private session bus with nothing activatable, so no
    # probe can reach the user's keyring. "// needs: keyring" starts a
    # throwaway gnome-keyring on that bus, its files in the scratch directory.
    keyring=""
    if grep -q 'needs: keyring' "$probe"; then
        mkdir -p "$scratch/$name/keyring-run" && chmod 700 "$scratch/$name/keyring-run"
        keyring="printf probe-pass | XDG_DATA_HOME='$scratch/$name/keyring' XDG_RUNTIME_DIR='$scratch/$name/keyring-run' \
            gnome-keyring-daemon --unlock --components=secrets --daemonize >/dev/null;"
    fi
    output=$(env $probe_env QT_FORCE_STDERR_LOGGING=1 QT_QPA_PLATFORM=offscreen CLARP_RS_QML="$PWD/$probe" \
        CLARP_WORKSPACE_STORE="$store" QML_XHR_ALLOW_FILE_READ=1 QML_XHR_ALLOW_FILE_WRITE=1 \
        CLARP_BASE_URL="$base_url" XDG_CONFIG_HOME="$scratch/$name/config" XDG_CACHE_HOME="$scratch/$name/cache" \
        timeout 60 dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- \
        sh -c "$keyring"' exec "$@"' probe "$binary" "--probe-store=$store" "--probe-host-log=$scratch/$name/host.log" 2>&1)
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
