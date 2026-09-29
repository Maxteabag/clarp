#!/usr/bin/env bash
# Runs net/tests/credentials.rs against a private D-Bus session: once with a
# throwaway gnome-keyring in a temporary HOME, once with no keyring at all.
# tests/private-bus.conf has no service directories, so nothing auto-starts.
# The user's own session bus and keyring are never touched.
set -euo pipefail
cd "$(dirname "$0")/../.."
cargo test -p clarp-net --test credentials --no-run -q
home=$(mktemp -d /var/tmp/clarp-keyring-test.XXXXXX)
trap 'rm -rf "$home"' EXIT
mkdir -p "$home/run" && chmod 700 "$home/run"
isolated=(env -u DBUS_SESSION_BUS_ADDRESS HOME="$home" XDG_RUNTIME_DIR="$home/run"
          XDG_DATA_HOME="$home/share" XDG_CONFIG_HOME="$home/config")
"${isolated[@]}" CLARP_TEST_SECRET_SERVICE=absent timeout 60 dbus-run-session --config-file=tests/private-bus.conf -- \
    cargo test -p clarp-net --test credentials -q -- --nocapture
"${isolated[@]}" CLARP_TEST_SECRET_SERVICE=isolated timeout 60 dbus-run-session --config-file=tests/private-bus.conf -- sh -c '
    printf probe-pass | gnome-keyring-daemon --unlock --components=secrets --daemonize >/dev/null
    cargo test -p clarp-net --test credentials -q -- --nocapture'
