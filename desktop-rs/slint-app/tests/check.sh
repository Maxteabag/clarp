#!/usr/bin/env bash
# A headless behaviour check of the Slint app against the fake Host (no
# display, private bus, scratch config): slint-app/tests/check.sh NAME [OUT]
set -uo pipefail
cd "$(dirname "$0")/../.."
# pair runs the command line, not the window (and runs on macOS too).
[ "$1" = pair ] && exec slint-app/tests/pair-check.sh "${2:-slint-app/docs/checks/pair}"
name=$1; out=$(realpath -m "${2:-slint-app/docs/checks}"); mkdir -p "$out"
# Every artifact type and its interactions run in one long check.
case $name in artifacts|scroll-jump|live|row-overlap) limit=300 ;; artifact-keys|scrollbar) limit=600 ;; *) limit=120 ;; esac
# desktop reports presence, which needs an unlocked login session: its own,
# not the machine's (a CI runner has none).
[ "$name" = desktop ] && export CLARP_TEST_LOGIN=unlocked
# The null sink "plays" a clip this long, so a check can pause it.
case $name in artifacts|artifact-keys) export CLARP_TEST_SILENT_CLIP_MS=4000 ;; esac
# roster-stale lets a slow snapshot time out in 1.5 s, not a minute.
[ "$name" = roster-stale ] && export CLARP_SNAPSHOT_TIMEOUT_MS=1500
scratch=$(mktemp -d /var/tmp/clarp-slint-check.XXXXXX)
trap 'kill "$host" 2>/dev/null; wait "$host" 2>/dev/null; rm -rf "$scratch"' EXIT
host_args=(); [ "$name" = startup ] && host_args=(--roster 100)
cp slint-app/docs/screens/markdown-paper.png "$scratch/photo.png"
# The "microphone": a second of tone, never the user's device.
/usr/bin/python3 -c "import math,struct,wave,sys; w=wave.open(sys.argv[1],'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(i/8))) for i in range(16000))); w.close()" "$scratch/voice.wav"
mkdir -p "$scratch/home" && mkdir -m 700 "$scratch/run"
# The native CLI and terminal launcher an agent terminal needs; never run,
# since CLARP_TEST_TERMINAL_LOG records the launch instead.
mkdir -p "$scratch/bin"
for program in claude xdg-terminal-exec; do printf '#!/bin/sh\nexit 1\n' > "$scratch/bin/$program"; chmod +x "$scratch/bin/$program"; done
/usr/bin/python3 tests/fake_host.py --port-file "$scratch/port" --log "$scratch/host.log" "${host_args[@]}" &
host=$!
for _ in $(seq 50); do [ -s "$scratch/port" ] && break; sleep 0.1; done
# A check runs the app once; startup runs it twice, with an empty portrait
# cache and then with the one the first run filled (a usual launch).
# CLARP_CHECK_PROFILE=release runs the release build.
passes=(once); [ "$name" = startup ] && passes=(cold warm)
# keymap saves its bindings (fonts its font) to a scratch settings file and
# starts again (restart) to find them; the others keep settings in memory.
settings=off; case $name in keymap|fonts) passes=(once restart); settings="$scratch/settings.json" ;; esac
# customize edits its settings file by hand, as a person would.
[ "$name" = customize ] && settings="$scratch/settings.json"
# chat-zoom zooms, then starts again (restart) to find the zoom kept.
[ "$name" = chat-zoom ] && { passes=(once restart); settings="$scratch/settings.json"; }
# form-events runs it twice too: what the first run queued is delivered by
# the second (the scratch data folder and the fake Host outlive the app).
[ "$name" = form-events ] && passes=(first second)
for pass in "${passes[@]}"; do
[ "$pass" = once ] || echo "perf pass: $pass"
CLARP_CHECK_PASS=$pass env -u WAYLAND_DISPLAY -u DISPLAY -u XDG_SESSION_ID CLARP_BASE_URL="http://127.0.0.1:$(cat "$scratch/port")" CLARP_TOKEN=probe-token CLARP_SETTINGS="$settings" \
    HOME="$scratch/home" XDG_RUNTIME_DIR="$scratch/run" \
    XDG_CONFIG_HOME="$scratch/c" XDG_CACHE_HOME="$scratch/k" XDG_DATA_HOME="$scratch/d" XDG_STATE_HOME="$scratch/s" \
    CLARP_AUDIO_OUTPUT=null CLARP_AUDIO_INPUT="file:$scratch/voice.wav" CLARP_KEYRING=off \
    CLARP_TEST_OPEN_URL="$scratch/urls" CLARP_TEST_HOST_LOG="$scratch/host.log" \
    CLARP_TEST_ATTACH_FILE="$scratch/photo.png" CLARP_TEST_NOTIFY_LOG="$scratch/notifications" \
    CLARP_TEST_FOREGROUND=1 CLARP_TEST_CLIPBOARD="$scratch/clipboard" \
    CLARP_TEST_TERMINAL_LOG="$scratch/terminal.jsonl" PATH="$scratch/bin:$PATH" \
    timeout "$limit" dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- \
    "target/${CLARP_CHECK_PROFILE:-debug}/clarp-slint" --check "$name" --out "$out" 2>&1 | tee -a "$scratch/run.log" | grep -E "^(ok|FAIL|E2E|perf)|panicked"
done
# The app's whole log beside the shots, to read when a stage times out.
cp "$scratch/run.log" "$out/app.log"
grep -q "^E2E_PASS" "$scratch/run.log" && ! grep -q "^E2E_FAIL" "$scratch/run.log"
