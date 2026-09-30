#!/usr/bin/env bash
# A headless screenshot of the Slint app against the fake Host (no display,
# private bus, scratch config): slint-app/tests/shot.sh OUT.png SESSION [THEME]
set -uo pipefail
cd "$(dirname "$0")/../.."
out=$(realpath -m "$1"); session=${2:-rachel}; theme=${3:-terminal}
scratch=$(mktemp -d /var/tmp/clarp-slint-shot.XXXXXX)
trap 'kill "$host" 2>/dev/null; wait "$host" 2>/dev/null; rm -rf "$scratch"' EXIT
# The "microphone": a second of tone, never the user's device.
/usr/bin/python3 -c "import math,struct,wave,sys; w=wave.open(sys.argv[1],'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(i/8))) for i in range(16000))); w.close()" "$scratch/voice.wav"
mkdir -p "$scratch/home" && mkdir -m 700 "$scratch/run"
/usr/bin/python3 app/tests/fake_host.py --port-file "$scratch/port" --log "$scratch/host.log" &
host=$!
for _ in $(seq 50); do [ -s "$scratch/port" ] && break; sleep 0.1; done
env CLARP_BASE_URL="http://127.0.0.1:$(cat "$scratch/port")" CLARP_TOKEN=probe-token CLARP_SETTINGS=off \
    HOME="$scratch/home" XDG_RUNTIME_DIR="$scratch/run" \
    XDG_CONFIG_HOME="$scratch/c" XDG_CACHE_HOME="$scratch/k" XDG_DATA_HOME="$scratch/d" XDG_STATE_HOME="$scratch/s" \
    CLARP_AUDIO_OUTPUT=null CLARP_AUDIO_INPUT="file:$scratch/voice.wav" CLARP_KEYRING=off \
    CLARP_TEST_OPEN_URL="$scratch/urls" \
    timeout 60 dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- \
    target/debug/clarp-slint --shot "$out" --select "$session" --theme "$theme" 2>&1 | grep -E "^(ok|FAIL|E2E)"
