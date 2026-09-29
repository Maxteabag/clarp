#!/usr/bin/env bash
# The tray and notifications of the real window against a fake tray host and
# notification daemon, all on a private session bus: nothing reaches the
# user's panel, notifications or settings.
set -uo pipefail
cd "$(dirname "$0")/../.."
scratch=$(mktemp -d /var/tmp/clarp-desktop-services.XXXXXX)
trap 'rm -rf "$scratch"' EXIT
python3 app/tests/fake_host.py --port-file "$scratch/port" --log "$scratch/host.log" &
host=$!
for _ in $(seq 50); do [ -s "$scratch/port" ] && break; sleep 0.1; done
base="http://127.0.0.1:$(cat "$scratch/port")"
env CLARP_BASE_URL="$base" CLARP_TOKEN=probe-token CLARP_SETTINGS="$scratch/settings.json" \
    XDG_CONFIG_HOME="$scratch/config" XDG_CACHE_HOME="$scratch/cache" XDG_DATA_HOME="$scratch/data" \
    CLARP_AUDIO_OUTPUT=none CLARP_AUDIO_INPUT=none CLARP_TEST_OPEN_URL="$scratch/urls" CLARP_TEST_CLIPBOARD="$scratch/clip" \
    QT_FORCE_STDERR_LOGGING=1 QT_QPA_PLATFORM=offscreen CLARP_APP_LOG="$scratch/app.log" CLARP_PRIVATE_BUS=1 \
    timeout 90 dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- \
    python3 app/tests/desktop_services_check.py "$base" "$scratch/settings.json" "$PWD/target/debug/clarp-desktop"
code=$?
kill "$host" 2>/dev/null; wait "$host" 2>/dev/null
[ "$code" -eq 0 ] || grep -v 'dbind\|portal' "$scratch/app.log" | tail -20
exit $code
