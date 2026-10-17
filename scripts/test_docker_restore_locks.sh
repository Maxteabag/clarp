#!/usr/bin/env bash
# Restore exclusivity against the built image, its real entrypoint and owned named volumes (CI only).
#
# Uses the image the container smoke test already built (CLARP_TEST_IMAGE, default clarp:test); it
# never builds. Every container of it runs on the owned internal network with the loopback-only
# resolv.conf (scripts/ci_docker_offline.sh), proven offline before the first one starts. Only
# synthetic data: disposable volumes this run creates, archives made from them, probe files.
#
# K1 the Host's locks are held for the life of its stack and seen from another container; exactly
#    the entrypoint shell (inheritable) and the sealed server and runtime (close-on-exec) hold them
# K2 a second Host on a live volume exits 75
# K3 a maintenance command beside a live Host holds only the shared lock
# K4 a restore stages only while a lock-aware Host lives; without one it exits 78, archive unchanged
# K5 a staged restore is refused (76) while another container uses the volume, data and archive
#    unchanged; it is applied once the Host owns the volume alone
# K6 a PTY login shell of the Host (/server-terminal) holds no lock file
# K7 synthetic marker states, the files an interrupted apply leaves (not a real crash): marker and
#    archive make maintenance refuse (76) and the Host start re-apply; a marker alone is recovered
# K8 the Host command with an extra argument exits 64 (never a server without the Host's locks)
#
# The instance generation token (/data/clarp/backups/instance.generation, lib.host_generation), read
# only by probe containers (no entrypoint, no bootstrap) or by an exec into a Host this run started:
# G1  the first Host start creates it: a regular 0600 file of the image user, one link, a canonical UUID
# G2  the entrypoint shell, the server and the runtime all run as that uid (the same-uid invariant)
# G3  a refused second Host, a maintenance command and an extended Host command leave it unchanged
# G4  an ordinary restart (nothing staged) keeps it
# G5  a backup archive carries nothing under clarp/backups, so a restore can never bring a token back
# G6  a refused stage (78) and a refused apply (76) leave it unchanged
# G7  a tampered staged archive is refused inside the apply (78) before rotation: unchanged; the staged
#     archive's exact bytes and mtime are then restored, so K5 proceeds as before
# G8  the exclusive restore rotates it, and the new one survives the swap
# G9  the K7a re-apply of an interrupted restore (synthetic marker state) rotates it again
# G10 K7b marker recovery is not a restore and leaves it unchanged
# G11 a group-writable clarp/backups never stops the Host: no token, exactly three bootstrap refusals
#     in the logs (entrypoint, server, runtime); once fixed, the next start creates it
# Not observable here: the bootstrap's order against the seal and the first database open, and the
# rotation's order inside the apply (both proven by the unit tests); no real crash.
#
# Names are unique to this run and every resource carries its label. A name is recorded only after
# its create succeeded, and cleanup removes only recorded resources whose label still matches.
# Any unexpected result stops the run (set -e); nothing is retried.
set -euo pipefail

IMAGE="${CLARP_TEST_IMAGE:-clarp:test}"
NS="clarp-rlock-${GITHUB_RUN_ID:-local}-$$"
LABEL_KEY="clarp.test.rlock"
NETWORK="$NS-net"
TOTAL_LIMIT=840            # seconds for the whole script; the workflow step has its own bound
OWNED_CONTAINERS=()
OWNED_VOLUMES=()
PROBES=0
OUT="$(mktemp -d)"
# shellcheck source=scripts/ci_docker_offline.sh
. "$(dirname "$0")/ci_docker_offline.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
phase() {
    (( SECONDS <= TOTAL_LIMIT )) || fail "total time limit ${TOTAL_LIMIT}s exceeded"
    echo "== $*"
}

owned_label() {  # $1 kind (container|volume), $2 name: the label value, or empty if absent
    if [[ "$1" == container ]]; then
        docker inspect -f "{{index .Config.Labels \"$LABEL_KEY\"}}" "$2" 2>/dev/null || true
    else
        docker volume inspect -f "{{index .Labels \"$LABEL_KEY\"}}" "$2" 2>/dev/null || true
    fi
}

cleanup() {
    local name
    for name in "${OWNED_CONTAINERS[@]}"; do
        [[ "$(owned_label container "$name")" == "$NS" ]] && docker rm -f "$name" >/dev/null 2>&1 || true
    done
    for name in "${OWNED_VOLUMES[@]}"; do
        [[ "$(owned_label volume "$name")" == "$NS" ]] && docker volume rm "$name" >/dev/null 2>&1 || true
    done
    ci_offline_cleanup
    rm -rf "$OUT"
}
trap cleanup EXIT

# Every helper that creates something runs in this shell (never in $(...) or a pipe), so the
# record of what this run owns is never lost in a subshell.
new_volume() {  # $1 suffix; sets VOL
    local name="$NS-$1"
    timeout 30 docker volume create --label "$LABEL_KEY=$NS" "$name" >/dev/null
    OWNED_VOLUMES+=("$name")
    VOL="$name"
}

new_container() {  # $1 suffix, rest: docker create arguments after the common ones; sets NEW
    local name="$NS-$1"; shift
    timeout 60 docker create --name "$name" --label "$LABEL_KEY=$NS" "${OFFLINE[@]}" "$@" >/dev/null
    OWNED_CONTAINERS+=("$name")
    NEW="$name"
}

remove_container() {  # only a recorded container that still carries this run's label
    [[ "$(owned_label container "$1")" == "$NS" ]] || fail "refusing to remove unowned container $1"
    timeout 60 docker rm -f "$1" >/dev/null
}

run_to_exit() {  # $1 container: start attached; output to $OUT/$1.log; sets RC (bounded)
    RC=0
    timeout 150 docker start -a "$1" >"$OUT/$1.log" 2>&1 || RC=$?
    (( RC != 124 )) || fail "$1 did not exit within 150s"
}

expect_exit() {  # $1 container, $2 expected exit code, $3 expected output substring
    run_to_exit "$1"
    if [[ "$RC" != "$2" ]] || ! grep -qF -- "$3" "$OUT/$1.log"; then
        cat "$OUT/$1.log" >&2
        fail "$1 exited $RC (expected $2 with '$3')"
    fi
    remove_container "$1"
}

wait_healthy() {  # $1 container, at most 90s; a container that stops is a failure
    local status
    for _ in $(seq 1 90); do
        status="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$1")"
        [[ "$status" == "running healthy" ]] && return 0
        [[ "$status" == running* ]] || { docker logs "$1" >&2 || true; fail "$1 stopped: $status"; }
        sleep 1
    done
    fail "$1 not healthy within 90s"
}

start_host() {  # $1 suffix, $2 volume; a Host with the image's own command, healthy and offline
    new_container "$1" -v "$2:/data" "$IMAGE"
    timeout 60 docker start "$NEW" >/dev/null
    wait_healthy "$NEW"
    ci_offline_assert_inside "$NEW"
    HOST="$NEW"
}

stop_host() {
    timeout 60 docker stop "$1" >/dev/null
    remove_container "$1"
}

probe() {  # $1 volume, $2 python source run as the image user with the volume at /data, no locks;
           # its output stays in $PROBE_LOG
    PROBES=$((PROBES + 1))
    new_container "probe-$PROBES" --entrypoint python3 -v "$1:/data" "$IMAGE" -c "$2"
    run_to_exit "$NEW"
    [[ "$RC" == 0 ]] || { cat "$OUT/$NEW.log" >&2; fail "probe $PROBES failed ($RC)"; }
    PROBE_LOG="$OUT/$NEW.log"
    cat "$PROBE_LOG"
    remove_container "$NEW"
}

# State of the test root as one line: the probe file, and each restore file's sha256@mtime_ns or
# "absent" (the DB is not compared: maintenance runs open it by design).
STATE_PY='
import hashlib, pathlib
b = pathlib.Path("/data/clarp/backups")
def h(p): return f"{hashlib.sha256(p.read_bytes()).hexdigest()}@{p.stat().st_mtime_ns}" if p.exists() else "absent"
m = pathlib.Path("/data/workspace/probe/marker")
print("STATE", m.read_text() if m.exists() else "absent",
      h(b / "restore.pending.tar.gz"), h(b / "restore.pending.owner"), h(b / "restore.applying"))
'
state() {  # $1 volume; sets STATE
    probe "$1" "$STATE_PY" >/dev/null
    STATE="$(grep '^STATE ' "$PROBE_LOG")"
}

# The token as one line: TOKEN <sha256>@<inode> <mode> <uid> <links> <canonical 0|1> <regular 0|1>,
# or "TOKEN absent". Probes run python3 directly, so they never bootstrap one.
TOKEN_PY='
import hashlib, os, stat, uuid
p = "/data/clarp/backups/instance.generation"
try:
    st = os.lstat(p)
except FileNotFoundError:
    print("TOKEN absent")
    raise SystemExit(0)
regular = stat.S_ISREG(st.st_mode)
raw = open(p, "rb").read() if regular else b""
text = raw.decode("ascii", "replace")
try:
    canonical = int(text.endswith("\n") and str(uuid.UUID(text[:-1])) == text[:-1])
except ValueError:
    canonical = 0
print("TOKEN", f"{hashlib.sha256(raw).hexdigest()}@{st.st_ino}", oct(stat.S_IMODE(st.st_mode)),
      st.st_uid, st.st_nlink, canonical, int(regular))
'
token() {  # $1 volume; sets TOKEN
    probe "$1" "$TOKEN_PY" >/dev/null
    TOKEN="$(grep '^TOKEN ' "$PROBE_LOG")"
}
host_token() {  # $1 a running Host this run started; sets TOKEN
    TOKEN="$(timeout 60 docker exec "$1" python3 -c "$TOKEN_PY" | grep '^TOKEN ')"
}
token_valid() {  # $1 a TOKEN line: regular, 0600, image uid, one link, canonical
    local _t _id mode uid links canonical regular
    read -r _t _id mode uid links canonical regular <<<"$1"
    [[ "$mode" == 0o600 && "$uid" == 10001 && "$links" == 1 && "$canonical" == 1 && "$regular" == 1 ]]
}
token_sha() {  # $1 a TOKEN line: its content sha256
    local _t id
    read -r _t id _ <<<"$1"
    echo "${id%%@*}"
}

# ---- preflight: image, tokens, offline network (before any Host container) ----
phase "preflight"
[[ -z "${ACTIONS_RUNTIME_TOKEN:-}${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]] \
    || fail "job tokens must be unset for test containers"
timeout 30 docker image inspect "$IMAGE" >/dev/null || fail "image $IMAGE missing (built by the smoke step)"
ci_offline_create "$NETWORK"
ci_offline_preflight "$IMAGE"
mapfile -t OFFLINE < <(ci_offline_run_args)
new_volume v1; V1="$VOL"

# ---- K1 ----
phase "K1 Host lock lifetime"
start_host host-a "$V1"; A="$HOST"
timeout 60 docker exec "$A" sh -c 'mkdir -p /data/workspace/probe && printf original >/data/workspace/probe/marker'
probe "$V1" '
import fcntl, os
d = "/data/clarp/backups/"
owner = os.open(d + "host-owner.lock", os.O_RDONLY)
try:
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB); raise SystemExit("owner lock is free")
except BlockingIOError:
    pass
users = os.open(d + "data-root-users.lock", os.O_RDONLY)
try:
    fcntl.flock(users, fcntl.LOCK_EX | fcntl.LOCK_NB); raise SystemExit("users lock is not shared-held")
except BlockingIOError:
    pass
fcntl.flock(users, fcntl.LOCK_SH | fcntl.LOCK_NB)
print("K1 cross-container: owner EX held, users SH held")
'
timeout 60 docker exec "$A" python3 -c '
import glob, os
want = {"/data/clarp/backups/host-owner.lock", "/data/clarp/backups/data-root-users.lock"}
O_CLOEXEC = 0o2000000
roles = {"clarp-entrypoint": "entrypoint", "/opt/clarp/server.py": "server", "/opt/clarp/runtime.py": "runtime"}
held = {}                                   # role -> {target: close-on-exec}
for link in glob.glob("/proc/[0-9]*/fd/*"):
    try:
        target = os.readlink(link)
    except OSError:
        continue
    if target not in want:
        continue
    _, _, pid, _, fd = link.split("/")
    cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
    role = next((r for k, r in roles.items() if k in cmd), None)
    assert role is not None, ("an unexpected process holds a lock file", pid, cmd, target)
    assert role not in held or target not in held[role], ("two processes share a role", role, cmd)
    flags = next(int(l.split()[1], 8) for l in open(f"/proc/{pid}/fdinfo/{fd}") if l.startswith("flags:"))
    held.setdefault(role, {})[target] = bool(flags & O_CLOEXEC)
assert set(held) == {"entrypoint", "server", "runtime"}, held
for role, files in held.items():
    assert set(files) == want, (role, files)
    sealed = set(files.values())
    assert sealed == ({False} if role == "entrypoint" else {True}), (role, files)
print("K1 holders: entrypoint (inheritable), server and runtime (close-on-exec)")
'

# ---- G1 / G2 ----
phase "G1 the first start created the instance generation"
host_token "$A"; T1="$TOKEN"
token_valid "$T1" || fail "G1 token: $T1"
echo "G1 token valid (0600, uid 10001, one link, canonical)"
phase "G2 entrypoint, server and runtime run as the token owner"
timeout 60 docker exec "$A" python3 -c '
import glob, os
token_uid = os.lstat("/data/clarp/backups/instance.generation").st_uid
found = {}
for status in glob.glob("/proc/[0-9]*/status"):
    pid = int(status.split("/")[2])
    if pid == os.getpid():
        continue
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        uids = next(line.split()[1:] for line in open(status) if line.startswith("Uid:"))
    except (OSError, StopIteration):
        continue
    argv = [a.decode() for a in argv if a]
    if not argv:
        continue
    role = None
    if os.path.basename(argv[0]) in ("sh", "dash", "bash") and "/usr/local/bin/clarp-entrypoint" in argv[1:2]:
        role = "entrypoint"
    elif "python" in os.path.basename(argv[0]) and argv[1:] == ["/opt/clarp/server.py"]:
        role = "server"
    elif "python" in os.path.basename(argv[0]) and argv[1:] == ["/opt/clarp/runtime.py"]:
        role = "runtime"
    if role is None:
        continue
    assert role not in found, ("two processes for one role", role, argv)
    found[role] = {int(u) for u in uids}
assert set(found) == {"entrypoint", "server", "runtime"}, found
for role, uids in found.items():
    assert uids == {token_uid} == {10001}, (role, uids, token_uid)
print("G2 entrypoint, server and runtime run as uid", token_uid, "= the token owner")
'

# ---- K2 ----
phase "K2 second Host refused"
new_container host-b -v "$V1:/data" "$IMAGE"
expect_exit "$NEW" 75 "the data root is owned by a live Host"
wait_healthy "$A"
state "$V1"; [[ "$STATE" == "STATE original absent absent absent" ]] || fail "K2 changed the test root: $STATE"

# ---- K3 ----
phase "K3 maintenance beside a live Host"
new_container maint-doctor -v "$V1:/data" "$IMAGE" sh -c 'clarp-admin doctor >/dev/null && ls -l /proc/$$/fd'
run_to_exit "$NEW"
[[ "$RC" == 0 ]] || { cat "$OUT/$NEW.log" >&2; fail "K3 maintenance exited $RC"; }
grep -qF data-root-users.lock "$OUT/$NEW.log" || fail "K3 maintenance does not hold the users lock"
! grep -qF host-owner.lock "$OUT/$NEW.log" || fail "K3 maintenance holds the owner lock"
remove_container "$NEW"

# ---- K8 ----
phase "K8 extended Host command refused"
new_container host-extra -v "$V1:/data" "$IMAGE" python3 /opt/clarp/server.py --extra
expect_exit "$NEW" 64 "the Host command takes no extra arguments"
wait_healthy "$A"

# ---- G3 / G4 ----
phase "G3 refused and maintenance entrypoints left the token unchanged"
host_token "$A"; [[ "$TOKEN" == "$T1" ]] || fail "G3 token changed: $TOKEN"
phase "G4 an ordinary restart keeps the token"
stop_host "$A"
start_host host-a-restart "$V1"; A="$HOST"
host_token "$A"; [[ "$TOKEN" == "$T1" ]] || fail "G4 token changed across an ordinary restart: $TOKEN"

# ---- K4 ----
phase "K4 staging needs a live Host"
backup="$(timeout 120 docker exec "$A" clarp-admin backup create | tail -1)"
[[ "$backup" == /data/clarp/backups/clarp-*.tar.gz ]] || fail "K4 backup path: $backup"
probe "$V1" "
import tarfile
names = tarfile.open('$backup', 'r:gz').getnames()
inside = [n for n in names if n.lstrip('./').startswith('clarp/backups')]
assert not inside, inside
print('G5 archive members exclude clarp/backups:', len(names))
" >/dev/null
timeout 60 docker exec "$A" sh -c 'printf changed >/data/workspace/probe/marker'
timeout 120 docker exec "$A" clarp-admin backup restore "$backup" >/dev/null
state "$V1"; staged="$STATE"
read -r _ file pending sidecar marker <<<"$staged"
[[ "$file" == changed && "$pending" != absent && "$sidecar" != absent && "$marker" == absent ]] \
    || fail "K4 staging state: $staged"
stop_host "$A"
new_container maint-stage -v "$V1:/data" "$IMAGE" clarp-admin backup restore "$backup"
expect_exit "$NEW" 78 "restore can only be staged inside the running Host"
state "$V1"; [[ "$STATE" == "$staged" ]] || fail "K4 refusal changed the test root or the staged archive: $STATE"
token "$V1"; [[ "$TOKEN" == "$T1" ]] || fail "G6 the refused stage changed the token: $TOKEN"

# ---- K5 ----
phase "K5 apply only with exclusive ownership"
new_container maint-hold -v "$V1:/data" "$IMAGE" sleep 300
M="$NEW"
timeout 60 docker start "$M" >/dev/null
held=""
deadline=$((SECONDS + 60))
while (( SECONDS < deadline )); do
    probe "$V1" '
import fcntl, os
users = os.open("/data/clarp/backups/data-root-users.lock", os.O_RDONLY)
try:
    fcntl.flock(users, fcntl.LOCK_EX | fcntl.LOCK_NB); print("FREE")
except BlockingIOError:
    print("HELD")
' >/dev/null
    if grep -qx HELD "$PROBE_LOG"; then held=1; break; fi
    sleep 1
done
[[ -n "$held" ]] || fail "K5 holder did not take the users lock within 60s"
new_container host-a2 -v "$V1:/data" "$IMAGE"
expect_exit "$NEW" 76 "restore refused: the data root is in use"
state "$V1"; [[ "$STATE" == "$staged" ]] || fail "K5 refusal changed the test root or the staged archive: $STATE"
token "$V1"; [[ "$TOKEN" == "$T1" ]] || fail "G6 the refused apply changed the token: $TOKEN"
timeout 60 docker stop "$M" >/dev/null
remove_container "$M"

# ---- G7 (after K5's refusal checks; K5's apply below sees the staged archive byte- and mtime-exact) ----
phase "G7 a tampered staged archive is refused inside the apply, before rotation"
probe "$V1" '
import os, shutil
pending = "/data/clarp/backups/restore.pending.tar.gz"
keep = "/data/workspace/probe/"
shutil.copyfile(pending, keep + "pending.copy")
st = os.stat(pending)
open(keep + "pending.times", "w").write(f"{st.st_atime_ns} {st.st_mtime_ns}")
with open(pending, "ab") as handle:
    handle.write(b"tampered")
print("G7 staged archive tampered (a copy kept)")
' >/dev/null
new_container host-g7 -v "$V1:/data" "$IMAGE"
expect_exit "$NEW" 78 "the staged archive does not match its staging record"
token "$V1"; [[ "$TOKEN" == "$T1" ]] || fail "G7 the refused apply rotated the token: $TOKEN"
probe "$V1" '
import os, shutil
pending = "/data/clarp/backups/restore.pending.tar.gz"
keep = "/data/workspace/probe/"
shutil.copyfile(keep + "pending.copy", pending)
atime, mtime = map(int, open(keep + "pending.times").read().split())
os.utime(pending, ns=(atime, mtime))
os.remove(keep + "pending.copy")
os.remove(keep + "pending.times")
print("G7 staged archive restored from the copy")
' >/dev/null
state "$V1"; [[ "$STATE" == "$staged" ]] || fail "G7 the staged archive or its record is not exactly as staged: $STATE"

start_host host-a3 "$V1"; A3="$HOST"
[[ "$(timeout 60 docker exec "$A3" python3 -c "$STATE_PY")" == "STATE original absent absent absent" ]] \
    || fail "K5 apply did not restore the snapshot and clear the restore files"

# ---- G8 ----
phase "G8 the exclusive restore rotated the token, which survived the swap"
host_token "$A3"; T2="$TOKEN"
token_valid "$T2" || fail "G8 token: $T2"
[[ "$(token_sha "$T2")" != "$(token_sha "$T1")" ]] || fail "G8 the restore did not rotate the token"

# ---- K6 ----
phase "K6 PTY shell holds no lock file"
timeout 60 docker exec -i "$A3" python3 - <<'PY'
import base64, os, socket, struct, time, tomllib
token = tomllib.load(open("/data/clarp/config.toml", "rb"))["server"]["auth_token"]
sock = socket.create_connection(("127.0.0.1", 7682), timeout=20)
key = base64.b64encode(os.urandom(16)).decode()
sock.sendall((f"GET /server-terminal HTTP/1.1\r\nHost: 127.0.0.1:7682\r\nUpgrade: websocket\r\n"
              f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
              f"Authorization: Bearer {token}\r\n\r\n").encode())
head = b""
while b"\r\n\r\n" not in head:
    chunk = sock.recv(4096)
    assert chunk, head
    head += chunk
assert head.split(b"\r\n", 1)[0].endswith(b"101 Switching Protocols"), head[:200]
buffer = head.split(b"\r\n\r\n", 1)[1]

def send(opcode, payload):
    mask = os.urandom(4)
    size = len(payload)
    header = bytes([0x80 | opcode]) + (bytes([0x80 | size]) if size < 126
                                       else bytes([0x80 | 126]) + struct.pack(">H", size))
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

def fill(n):
    global buffer
    while len(buffer) < n:
        chunk = sock.recv(4096)          # the socket timeout (20s) bounds every read
        assert chunk, "the Host closed the terminal socket"
        buffer += chunk

def frames():
    global buffer
    while True:
        fill(2)
        size = buffer[1] & 0x7F
        offset = 2
        if size == 126:
            fill(4)
            size, offset = struct.unpack(">H", buffer[2:4])[0], 4
        elif size == 127:
            fill(10)
            size, offset = struct.unpack(">Q", buffer[2:10])[0], 10
        fill(offset + size)
        yield buffer[0] & 0x0F, buffer[offset:offset + size]
        buffer = buffer[offset + size:]

# The marker is split in the typed line, so only the shell's output contains it whole.
send(0x2, b'ls -l /proc/$$/fd; echo __PTY_""DONE__\n')   # the shell joins __PTY_""DONE__
output, deadline = b"", time.monotonic() + 20
for opcode, payload in frames():
    assert time.monotonic() < deadline, output[-2000:]
    if opcode == 0x8:
        break
    output += payload
    if b"__PTY_DONE__" in output:
        break
send(0x8, struct.pack(">H", 1000))
text = output.decode("utf-8", "replace")
assert "__PTY_DONE__" in text, text[-2000:]
assert "/dev/pts/" in text, text[-2000:]                   # the shell runs on the server's PTY
assert "host-owner.lock" not in text and "data-root-users.lock" not in text, text[-2000:]
print("K6 PTY shell fds hold no lock file")
PY

# ---- K7 (synthetic marker states; not a real crash) ----
phase "K7a marker and archive: maintenance refuses, the Host re-applies"
timeout 60 docker exec "$A3" sh -c 'printf k7base >/data/workspace/probe/marker'
backup="$(timeout 120 docker exec "$A3" clarp-admin backup create | tail -1)"
timeout 60 docker exec "$A3" sh -c 'printf k7changed >/data/workspace/probe/marker'
timeout 120 docker exec "$A3" clarp-admin backup restore "$backup" >/dev/null
stop_host "$A3"
probe "$V1" '
import os
fd = os.open("/data/clarp/backups/restore.applying", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, b"1"); os.close(fd)
print("K7a synthetic marker written beside the staged archive")
'
state "$V1"; before="$STATE"
read -r _ file pending sidecar marker <<<"$before"
[[ "$file" == k7changed && "$pending" != absent && "$sidecar" != absent && "$marker" != absent ]] \
    || fail "K7a fixture: $before"
new_container maint-k7 -v "$V1:/data" "$IMAGE" true
expect_exit "$NEW" 76 "a restore is in progress or was interrupted"
state "$V1"; [[ "$STATE" == "$before" ]] || fail "K7a maintenance changed the test root: $STATE"
start_host host-a4 "$V1"; A4="$HOST"
[[ "$(timeout 60 docker exec "$A4" python3 -c "$STATE_PY")" == "STATE k7base absent absent absent" ]] \
    || fail "K7a the Host start did not re-apply"
host_token "$A4"; T3="$TOKEN"
token_valid "$T3" || fail "G9 token: $T3"
[[ "$(token_sha "$T3")" != "$(token_sha "$T2")" ]] || fail "G9 the re-applied restore did not rotate the token"
echo "G9 the re-applied restore rotated the token again"
stop_host "$A4"

phase "K7b marker alone: recovered as completed"
probe "$V1" '
import os
for name in ("restore.applying", "restore.pending.owner"):
    fd = os.open("/data/clarp/backups/" + name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b"{}"); os.close(fd)
print("K7b synthetic marker and leftover record written, no archive")
'
start_host host-a5 "$V1"; A5="$HOST"
[[ "$(timeout 60 docker exec "$A5" python3 -c "$STATE_PY")" == "STATE k7base absent absent absent" ]] \
    || fail "K7b the Host start did not recover the marker"
host_token "$A5"; [[ "$TOKEN" == "$T3" ]] || fail "G10 marker recovery changed the token: $TOKEN"
echo "G10 marker recovery left the token unchanged"
stop_host "$A5"

# ---- G11 (a second owned volume) ----
phase "G11 a group-writable clarp/backups never stops the Host"
new_volume v2; V2="$VOL"
probe "$V2" '
import os
os.makedirs("/data/clarp/backups", exist_ok=True)
os.chmod("/data/clarp/backups", 0o770)
print("G11 clarp/backups is group-writable")
' >/dev/null
start_host host-g11 "$V2"; G11="$HOST"
G11_LINE=' hostGenerationInvalid backups is group or world writable$'
refusals=0
deadline=$((SECONDS + 60))
while (( SECONDS < deadline )); do
    refusals="$(timeout 30 docker logs "$G11" 2>&1 | grep -c -- "$G11_LINE" || true)"
    (( refusals >= 3 )) && break
    sleep 1
done
(( refusals >= 3 )) || fail "G11 expected three bootstrap refusals within 60s, saw $refusals"
sleep 5
refusals="$(timeout 30 docker logs "$G11" 2>&1 | grep -c -- "$G11_LINE" || true)"
[[ "$refusals" == 3 ]] || fail "G11 expected exactly three bootstrap refusals, saw $refusals"
host_token "$G11"; [[ "$TOKEN" == "TOKEN absent" ]] || fail "G11 a token exists despite the refusal: $TOKEN"
echo "G11 healthy without a token; three refusals (entrypoint, server, runtime)"
stop_host "$G11"
probe "$V2" '
import os
os.chmod("/data/clarp/backups", 0o700)
print("G11 clarp/backups fixed")
' >/dev/null
start_host host-g11-fixed "$V2"; G11="$HOST"
host_token "$G11"; token_valid "$TOKEN" || fail "G11 the fixed start did not create a valid token: $TOKEN"
echo "G11 the next start created the token"
stop_host "$G11"

phase "done in ${SECONDS}s"
echo "docker restore exclusivity ok"
