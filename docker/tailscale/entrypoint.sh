#!/bin/sh
set -eu

socket=/var/run/tailscale/tailscaled.sock
state=/var/lib/tailscale/tailscaled.state

/usr/local/bin/tailscaled \
  --state="$state" \
  --socket="$socket" \
  --tun=userspace-networking &
daemon_pid=$!

shutdown() {
  kill -TERM "$daemon_pid" 2>/dev/null || true
  wait "$daemon_pid" 2>/dev/null || true
}
trap shutdown INT TERM EXIT

until /usr/local/bin/tailscale --socket="$socket" status >/dev/null 2>&1; do
  kill -0 "$daemon_pid" 2>/dev/null || exit 1
  sleep 0.2
done

set -- --socket="$socket" up \
  --hostname="${TS_HOSTNAME:-clarp-docker}" \
  --accept-dns=false
if [ -n "${TS_AUTHKEY:-}" ]; then
  set -- "$@" --auth-key="$TS_AUTHKEY"
fi
/usr/local/bin/tailscale "$@"

/usr/local/bin/tailscale --socket="$socket" serve --bg \
  http://127.0.0.1:7682

wait "$daemon_pid"
