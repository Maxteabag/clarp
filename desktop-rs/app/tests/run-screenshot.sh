#!/usr/bin/env bash
# Offscreen screenshot of the real Main.qml against the fake Host: the Rust
# app connects, loads the roster, opens a chat and saves the window. Fails
# on a non-zero exit, a missing PNG, or any Clarp QML warning.
#   app/tests/run-screenshot.sh [out.png] [session]
set -uo pipefail
cd "$(dirname "$0")/../.."
out=${1:-/var/tmp/clarp-rust-screenshot.png}
session=${2:-rachel}
scratch=$(mktemp -d /var/tmp/clarp-screenshot.XXXXXX)
trap 'rm -rf "$scratch"' EXIT
python3 app/tests/fake_host.py --port-file "$scratch/port" --log "$scratch/host.log" &
host=$!
for _ in $(seq 50); do [ -s "$scratch/port" ] && break; sleep 0.1; done
rm -f "$out"
env CLARP_BASE_URL="http://127.0.0.1:$(cat "$scratch/port")" CLARP_TOKEN=probe-token CLARP_SETTINGS=off \
    XDG_CONFIG_HOME="$scratch/config" XDG_CACHE_HOME="$scratch/cache" XDG_DATA_HOME="$scratch/data" CLARP_AUDIO_OUTPUT=null \
    QT_FORCE_STDERR_LOGGING=1 QT_QPA_PLATFORM=offscreen CLARP_SCREENSHOT_PATH="$out" \
    CLARP_SCREENSHOT_SELECT_SESSION="$session" CLARP_SCREENSHOT_SIZE=1280x800 \
    timeout 60 dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- \
    target/debug/clarp-desktop >"$scratch/out.txt" 2>&1
code=$?
kill "$host" 2>/dev/null; wait "$host" 2>/dev/null
if grep -E 'qrc:/qt/qml/Clarp/Desktop|qrc:/clarp-rust' "$scratch/out.txt"; then
    echo "FAIL: QML warnings"; exit 1
fi
if [ "$code" -ne 0 ] || ! file "$out" | grep -q 'PNG image data'; then
    echo "FAIL: exit $code"; cat "$scratch/out.txt"; exit 1
fi
echo "screenshot $out ($(file -b "$out" | cut -d, -f2))"
