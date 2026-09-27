#!/usr/bin/env bash
# Lab-only variant of tools/lab/second-launch.sh for wrapper launchers that exec
# a resident binary. It keeps the same owned-session safety model, but finds and
# accounts the resident executable rather than the short-lived launcher path.
set -euo pipefail

L=${LAB:-/var/tmp/clarp-startlab}
PORT=${PORT:-7791}
WL=${WL:-lab-wl}
LAB_TOOLS=${LAB_TOOLS:-${T:-/var/tmp/clarp-startlab/tools}}
LAUNCHER=${1:?launcher binary}
RESIDENT=${2:-$LAUNCHER}

. "$LAB_TOOLS/owned.sh"

tok=$(
  if [[ -s ${LAB:-/nonexistent}/token ]]; then
    cat "$LAB/token"
  else
    grep -oP '^\s*auth_token\s*=\s*"\K[^"]*' ~/.config/clarp/config.toml
  fi
)

rm -rf "$L/data/"*
envs=(
  HOME="$HOME" PATH="$PATH" USER="$USER"
  CLARP_INSTANCE_NAME=com.maxteabag.Clarp.StartLab
  XDG_RUNTIME_DIR=$L/run XDG_CONFIG_HOME=$L/config XDG_DATA_HOME=$L/data XDG_CACHE_HOME=$L/cache
  QT_QPA_PLATFORM=wayland WAYLAND_DISPLAY=$WL QT_FORCE_STDERR_LOGGING=1
  CLARP_BASE_URL=http://127.0.0.1:$PORT CLARP_TOKEN="$tok"
  CLARP_SHARED_FILESYSTEM_HOST=http://127.0.0.1:$PORT CLARP_STARTUP_TRACE=1 CLARP_STALL_LOG=$L/stalls.log
  DBUS_SESSION_BUS_ADDRESS=unix:path=$L/run/lab-bus
)
if [[ -n ${LAB_ENV:-} ]]; then
  envs+=("$LAB_ENV")
fi

rm -f "$L/run/lab-bus"
owned_start /dev/null dbus-daemon --session --address="unix:path=$L/run/lab-bus" --nofork --nopidfile \
  || { echo "bus launch failed"; exit 1; }
bus_sid=$OWNED_SID

sleep 0.3
owned_start "$L/a.log" env -i "${envs[@]}" "$LAUNCHER" --no-new-agent \
  || { echo "launch failed"; owned_stop "$bus_sid"; exit 1; }
app_sid=$OWNED_SID

cleanup() {
  owned_stop "$app_sid"
  owned_stop "$bus_sid"
}
trap cleanup EXIT INT TERM

sleep 6
pid=$(owned_pid "$app_sid" "$RESIDENT" 5) || {
  echo "app not running in its own session as $RESIDENT"
  exit 1
}
before=$(awk '/^Pss:/{s+=$2} END{print int(s/1024)}' "/proc/$pid/smaps")

start=$(date +%s%3N)
env -i "${envs[@]}" timeout 10 "$LAUNCHER" --no-new-agent > "$L/b.log" 2>&1
client_exit=$(( $(date +%s%3N) - start ))

sleep 3
after=$(awk '/^Pss:/{s+=$2} END{print int(s/1024)}' "/proc/$pid/smaps")
echo "client exited after ${client_exit} ms: $(grep -o 'startup [a-z-]* ms=[0-9]*' "$L/b.log" | tr '\n' ' ')"
echo "server: $(grep -oE 'startup (window-[a-z-]+) ms=[0-9]+' "$L/a.log" | tr '\n' ' ')"

count=0
resident_path=$(readlink -f "$RESIDENT")
for member in $(owned_members "$app_sid"); do
  [[ $(readlink -f "/proc/$member/exe" 2>/dev/null) == "$resident_path" ]] && count=$((count + 1))
done
echo "processes: $count  pss before=${before}MB after=${after}MB"
