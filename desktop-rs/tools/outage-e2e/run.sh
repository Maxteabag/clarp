#!/usr/bin/env bash
# End-to-end proof on ai-swarm: a separate disposable Rust Clarp Host (own dirs, port, token, name),
# a gateway in front of it, the desktop engine headless (outage_probe) reading through the gateway.
# S1: the gateway answers 502 for ~5 s while the Host runs and a turn happens.
# S2: the Host is stopped as for an update (SIGTERM), the gateway answers 502 for ~44.5 s in all,
#     the Host starts again on the same data, a turn happens while the gateway still says 502.
# S3: a wrong device token: said at once.
set -euo pipefail
E2E=$(cd "$(dirname "$0")" && pwd)
HOSTBIN=${HOSTBIN:?}; SHIMBIN=${SHIMBIN:?}; PROBE=${PROBE:?}
FIX=dr502-e2e-$(date -u +%Y%m%dT%H%M%SZ)
D=/var/tmp/$FIX
mkdir -p "$D"/{home,tmp,data,run,config,work,nobin}
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
HOST_PORT=$(free_port); PROXY_PORT=$(free_port)
TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
# The guard exits 7 on a live target; it is written for a shell without errexit.
set +eo pipefail
source "${GUARD:-$E2E/guard_not_live.sh}" "http://127.0.0.1:$HOST_PORT" "$TOKEN"
source "${GUARD:-$E2E/guard_not_live.sh}" "http://127.0.0.1:$PROXY_PORT" "$TOKEN"
set -eo pipefail
pgrep -af "clarp-host .*--listen [^ ]*:7690" >/dev/null && echo "note: a :7690 Host runs on this machine; this fixture uses $HOST_PORT/$PROXY_PORT only" >&2
printf '#!%s\n' "$(command -v python3)" > "$D/claude-standin"; cat "$E2E/standin.py" >> "$D/claude-standin"; chmod 755 "$D/claude-standin"
echo pass > "$D/proxy-mode"
now_ms() { date +%s%3N; }
mark() { printf '{"wall_ms": %s, "mark": "%s"}\n' "$(now_ms)" "$1" >> "$D/marks.jsonl"; echo "$(date -u +%T) $1"; }
direct() { curl -sS -m 10 -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; }
start_host() {
  env -i HOME="$D/home" TMPDIR="$D/tmp" PATH="$D/nobin:/usr/bin:/bin" CLARP_HEARTBEATS_DISABLED=1 \
    CLARP_CLAUDE_PROGRAM="$D/claude-standin" CLARP_HOST_TOKEN="$TOKEN" XDG_CONFIG_HOME="$D/home/.config" \
    CLARP_CONFIG_DIR="$D/config" CLARP_SHARE_DIR="$D/home/share" CLARP_CACHE_DIR="$D/home/cache" \
    "$HOSTBIN" --listen "127.0.0.1:$HOST_PORT" --data "$D/data" --run "$D/run" --cwd "$D/work" --engine sqlite \
    --accounts none --launcher direct --shim "$SHIMBIN" --name "$FIX" --config "$D/config" >> "$D/host.log" 2>&1 &
  echo $! > "$D/host.pid"
  for _ in $(seq 1 200); do direct "http://127.0.0.1:$HOST_PORT/server-info" > "$D/server-info.$1.json" 2>/dev/null && return 0; sleep 0.1; done
  echo "Host did not start" >&2; tail -20 "$D/host.log" >&2; exit 1
}
send() { direct -X POST "http://127.0.0.1:$HOST_PORT/send" -d "{\"session\": \"$SESSION\", \"text\": \"$1\", \"client_msg_id\": \"$FIX-$2\"}" > /dev/null; }
replied() { direct "http://127.0.0.1:$HOST_PORT/log?session=$SESSION&limit=50" | python3 -c "import json,sys; t=json.load(sys.stdin).get('turns',[]); sys.exit(0 if any(r.get('role')=='assistant' and '$1' in (r.get('text') or '') for r in t) else 1)"; }
wait_reply() { for _ in $(seq 1 150); do replied "$1" && return 0; sleep 0.2; done; echo "no reply to $1" >&2; return 1; }
cleanup() { kill "$(cat "$D/proxy.pid" 2>/dev/null)" 2>/dev/null || true; kill -INT "$(cat "$D/host.pid" 2>/dev/null)" 2>/dev/null || true; kill "$(cat "$D/probe.pid" 2>/dev/null)" 2>/dev/null || true; }
trap cleanup EXIT

mark host-start-1; start_host 1
direct "http://127.0.0.1:$HOST_PORT/peers" > "$D/peers.json"
python3 - "$D" "$FIX" <<'PY'
import json, sys
d, fix = sys.argv[1], sys.argv[2]
info = json.load(open(f"{d}/server-info.1.json")); peers = json.load(open(f"{d}/peers.json"))
assert info.get("name") == fix, ("not the fixture Host", info.get("name"))
assert peers in ([], {"peers": []}) or not (peers.get("peers") if isinstance(peers, dict) else peers), ("peers", peers)
print("identity:", info.get("name"), info.get("server_id"), info.get("clarp_version"), "peers:", peers)
PY
SESSION=$(direct -X POST "http://127.0.0.1:$HOST_PORT/agents" -d '{"name": "Probe", "backend": "claude"}' | python3 -c 'import json,sys; o=json.load(sys.stdin); print(o.get("session") or o.get("agent",{}).get("session"))')
echo "session=$SESSION"
send "history before" h0; wait_reply "history before"
CODE=$(direct -X POST "http://127.0.0.1:$HOST_PORT/pairing/issue" -d '{}' | python3 -c 'import json,sys; o=json.load(sys.stdin); print(o.get("code") or o.get("pairing_code"))')
DEVICE=$(curl -sS -m 10 -H 'Content-Type: application/json' -X POST "http://127.0.0.1:$HOST_PORT/pairing/exchange" -d "{\"code\": \"$CODE\", \"device_name\": \"$FIX probe\"}" | python3 -c 'import json,sys; o=json.load(sys.stdin); print((o.get("device") or {}).get("token") or o)')
case "$DEVICE" in cld_*) ;; *) echo "pairing failed: $DEVICE" >&2; exit 1;; esac
python3 "$E2E/gateway.py" "$PROXY_PORT" "$HOST_PORT" "$D/proxy-mode" "$D/gateway.jsonl" & echo $! > "$D/proxy.pid"
sleep 0.5
CLARP_PROBE_URL="http://127.0.0.1:$PROXY_PORT" CLARP_PROBE_TOKEN="$DEVICE" CLARP_PROBE_SESSION="$SESSION" CLARP_PROBE_SECONDS=160 \
  HOME="$D/home" XDG_CONFIG_HOME="$D/home/.config" XDG_DATA_HOME="$D/home/.local/share" XDG_CACHE_HOME="$D/home/.cache" \
  "$PROBE" > "$D/timeline.jsonl" 2> "$D/probe.log" & echo $! > "$D/probe.pid"
sleep 8; mark baseline

# S1: a short 502 from the gateway while the Host runs; a turn happens meanwhile.
mark s1-502-on; echo 502 > "$D/proxy-mode"; sleep 0.5
send "gap one" g1; wait_reply "gap one"; sleep 1
mark s1-502-off; echo pass > "$D/proxy-mode"
sleep 20

# S2: a planned restart (SIGTERM) with ~44.5 s of 502 at the gateway, then 6 s more of 502 for reads only.
mark s2-sigterm; T0=$(now_ms); kill -TERM "$(cat "$D/host.pid")"
while kill -0 "$(cat "$D/host.pid")" 2>/dev/null; do sleep 0.05; done; mark s2-host-exited
while [ $(( $(now_ms) - T0 )) -lt 36000 ]; do sleep 0.1; done
echo 502 > "$D/proxy-mode"; mark s2-gateway-holds-502
mark host-start-2; start_host 2; mark s2-host-up
send "gap two" g2; wait_reply "gap two"; mark s2-turn-done
while [ $(( $(now_ms) - T0 )) -lt 44500 ]; do sleep 0.05; done
# The stream may come back now, but the reads a catch-up needs still fail for 6 s: a reopened
# stream alone must not end the outage.
mark s2-502-off; echo reads > "$D/proxy-mode"
while [ $(( $(now_ms) - T0 )) -lt 50500 ]; do sleep 0.05; done
mark s2-reads-pass; echo pass > "$D/proxy-mode"
sleep 25; mark s2-end
kill "$(cat "$D/probe.pid")" 2>/dev/null || true; sleep 0.5

# S3: a refusal is said at once (a token this Host never issued).
mark s3-start
CLARP_PROBE_URL="http://127.0.0.1:$PROXY_PORT" CLARP_PROBE_TOKEN="cld_not_issued_by_this_host" CLARP_PROBE_SESSION="$SESSION" CLARP_PROBE_SECONDS=6 \
  HOME="$D/home" "$PROBE" > "$D/timeline-auth.jsonl" 2> "$D/probe-auth.log"
direct "http://127.0.0.1:$HOST_PORT/log?session=$SESSION&limit=50" > "$D/host-log-final.json"
echo "$FIX $D host=$HOST_PORT gateway=$PROXY_PORT session=$SESSION" > "$D/fixture.txt"
cat "$D/fixture.txt"
