#!/usr/bin/env bash
# Startup and memory of the Slint app, offscreen against the fake Host with
# scratch config and a private bus (never the user's Host, keyring or
# speakers). For each run: milliseconds from launch to the first chat's /log
# request (started, connected, roster loaded, chat opened), then VmRSS and
# VmHWM five seconds in. Prints medians.
#   slint-app/tests/perf.sh [RUNS]
set -uo pipefail
cd "$(dirname "$0")/../.."
runs=${1:-5}
slint=$PWD/target/release/clarp-slint
[ -x "$slint" ] || { echo "build it first: cargo build --release -p clarp-slint"; exit 1; }

measure() { # label binary args...
    local label=$1 bin=$2; shift 2
    local scratch; scratch=$(mktemp -d /var/tmp/clarp-perf.XXXXXX)
    mkdir -p "$scratch/home" && mkdir -m 700 "$scratch/run"
    /usr/bin/python3 tests/fake_host.py --port-file "$scratch/port" --log "$scratch/host.log" &
    local host=$!
    for _ in $(seq 50); do [ -s "$scratch/port" ] && break; sleep 0.05; done
    local start; start=$(date +%s%N)
    env -i PATH=/usr/bin:/bin LANG=C.UTF-8 HOME="$scratch/home" XDG_RUNTIME_DIR="$scratch/run" \
        XDG_CONFIG_HOME="$scratch/c" XDG_CACHE_HOME="$scratch/k" XDG_DATA_HOME="$scratch/d" XDG_STATE_HOME="$scratch/s" \
        CLARP_BASE_URL="http://127.0.0.1:$(cat "$scratch/port")" CLARP_TOKEN=probe-token CLARP_SETTINGS=off CLARP_KEYRING=off \
        CLARP_SEPARATE_PROCESS=1 CLARP_WORKSPACE_STORE=off CLARP_AUDIO_OUTPUT=null CLARP_AUDIO_INPUT=none \
        CLARP_TEST_OPEN_URL="$scratch/urls" CLARP_TEST_CLIPBOARD="$scratch/clip" QT_QPA_PLATFORM=offscreen \
        timeout 30 dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- "$bin" "$@" > "$scratch/app.log" 2>&1 &
    local runner=$!
    local ready=""
    for _ in $(seq 400); do
        if grep -q '"path": "/log"' "$scratch/host.log" 2>/dev/null; then ready=$(( ($(date +%s%N) - start) / 1000000 )); break; fi
        sleep 0.01
    done
    sleep $(( 5 - (($(date +%s%N) - start) / 1000000000) )) 2>/dev/null
    local pid; pid=$(pgrep -n -f "^$bin")
    local rss hwm
    rss=$(awk '/VmRSS/ {print int($2/1024)}' "/proc/$pid/status" 2>/dev/null)
    hwm=$(awk '/VmHWM/ {print int($2/1024)}' "/proc/$pid/status" 2>/dev/null)
    kill "$pid" 2>/dev/null; wait "$runner" 2>/dev/null
    kill "$host" 2>/dev/null; wait "$host" 2>/dev/null
    rm -rf "$scratch"
    echo "$label ${ready:-timeout} ${rss:-?} ${hwm:-?}"
}

median() { sort -n | awk '{a[NR]=$1} END {print a[int((NR+1)/2)]}'; }
results=$(for _ in $(seq "$runs"); do measure slint "$slint" --headless; done)
echo "$results" > /var/tmp/clarp-perf-runs.txt
for app in slint; do
    echo "$app: ready $(echo "$results" | awk -v a=$app '$1==a {print $2}' | median) ms," \
        "RSS $(echo "$results" | awk -v a=$app '$1==a {print $3}' | median) MB," \
        "peak $(echo "$results" | awk -v a=$app '$1==a {print $4}' | median) MB"
done
