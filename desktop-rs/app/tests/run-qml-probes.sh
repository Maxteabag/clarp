#!/bin/sh
# Run every offscreen QML probe against the built Rust desktop binary.
# Usage (from desktop-rs/): app/tests/run-qml-probes.sh [target/debug/clarp-desktop]
set -u
binary=${1:-target/debug/clarp-desktop}
status=0
scratch=$(mktemp -d /var/tmp/clarp-qml-probes.XXXXXX)
trap 'rm -rf "$scratch"' EXIT
for probe in app/tests/qml/*_probe.qml; do
    # Each probe gets its own workspace store, never the user's layout.
    store="$scratch/$(basename "$probe" .qml)/workspaces.json"
    output=$(QT_FORCE_STDERR_LOGGING=1 QT_QPA_PLATFORM=offscreen CLARP_RS_QML="$PWD/$probe" \
        CLARP_WORKSPACE_STORE="$store" QML_XHR_ALLOW_FILE_READ=1 QML_XHR_ALLOW_FILE_WRITE=1 \
        timeout 60 "$binary" "--probe-store=$store" 2>&1)
    code=$?
    if [ "$code" -eq 0 ] && printf '%s\n' "$output" | grep -q PROBE_PASS; then
        echo "pass $probe"
    else
        echo "FAIL $probe (exit $code)"
        printf '%s\n' "$output" | grep -E 'FAIL|rror' | head -20
        status=1
    fi
done
exit $status
