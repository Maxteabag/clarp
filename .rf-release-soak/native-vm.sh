#!/usr/bin/env bash
# Runs ON THE BUILDBOX in a heavy-remote copy of a Clarp checkout (no .git). Synthetic data only.
#   canary-native-vm.sh base <sha>      fresh systemd container, install.sh of <sha>
#   canary-native-vm.sh release <sha> [--plant-fail]   install.sh of <sha> under release-check watch,
#                                       then follow-runtime through the natural handover
#   canary-native-vm.sh handover-turns <sha>   fake-Codex agents with turns in flight and queued
#                                       across a runtime-rolling release of <sha>
#   canary-native-vm.sh exec <cmd...>   run a command as the canary user (diagnostics)
#   canary-native-vm.sh down            remove container and image
set -uo pipefail
C=${CLARP_SOAK_CONTAINER:?owned soak container required}; IMG=clarp-native-canary-os
: "${HEAVY_SOAK_REMOTE_JOB_ID:?admission job required}" "${HEAVY_SOAK_NONCE:?admission nonce required}" "${HEAVY_SOAK_BOOT_ID:?admission boot required}"
[[ $C =~ ^clarp-soak-rf-releasec-[a-zA-Z0-9-]+$ ]] || { echo 'invalid owned container name' >&2; exit 2; }
owned_remove() {
    local ids label job nonce
    ids=$(docker container ls -aq --filter "name=^/$C$") || { echo 'container ownership query failed' >&2; return 1; }
    [[ -n $ids ]] || return 0
    [[ $ids =~ ^[0-9a-f]{12,64}$ ]] || { echo 'ambiguous container identity' >&2; return 1; }
    label=$(docker inspect --format '{{index .Config.Labels "clarp.soak-owner"}}' "$ids") || return 1
    [[ $label == rf-releasec-29c5 ]] || { echo 'container ownership mismatch; refusing removal' >&2; return 1; }
    job=$(docker inspect --format '{{index .Config.Labels "clarp.soak-job"}}' "$ids") || return 1
    nonce=$(docker inspect --format '{{index .Config.Labels "clarp.soak-nonce"}}' "$ids") || return 1
    [[ $job == "$HEAVY_SOAK_REMOTE_JOB_ID" && $nonce == "$HEAVY_SOAK_NONCE" ]] || { echo 'container admission differs; refusing removal' >&2; return 1; }
    docker rm -f "$ids" >/dev/null || { echo 'owned container removal failed' >&2; return 1; }
}
log() { printf '%s native-canary: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
as_canary() { docker exec -u canary -e XDG_RUNTIME_DIR=/run/user/1000 \
    -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus -e HOME=/home/canary -w /home/canary "$C" "$@"; }
stage() {  # stage <sha> [fail|lib]: copy this tree into the container as a git repo at /home/canary/src-<short>[-variant]
    local short=${1:0:8} dir=/home/canary/src-${1:0:8}${2:+-$2}
    chmod -R a+rX .
    as_canary rm -rf "$dir"
    tar --exclude=./canary-bundle --exclude=./canary-native --exclude=./canary-native-vm.sh --exclude=./release-check.py -cf - . | docker exec -i -u canary "$C" bash -c "mkdir -p $dir && tar -xf - -C $dir"
    docker exec -i -u canary "$C" bash -c "mkdir -p $dir/static/app" < /dev/null
    tar -C canary-bundle -cf - . | docker exec -i -u canary "$C" tar -xf - -C "$dir/static/app"
    if [[ ${2:-} == lib || ${2:-} == latelib ]]; then  # a comment in server/lib: changes RUNTIME_RELEASE_ID only
        as_canary bash -c "printf '\\n# canary runtime-roll marker %s\\n' \"\$(date +%s)\" >> $dir/server/lib/health.py"
    fi
    if [[ ${2:-} == late || ${2:-} == latelib ]]; then  # planted late failure: the server answers, then dies after 45 s
        docker exec -i -u canary "$C" python3 - "$dir/server/server.py" <<'PY2'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); lines = p.read_text().splitlines(keepends=True)
at = max([i + 1 for i, l in enumerate(lines) if l.startswith("from __future__")] or [0])
lines.insert(at, "import os as _o, threading as _t, time as _m\n_t.Thread(target=lambda: (_m.sleep(45), _o._exit(1)), daemon=True).start()\n")
p.write_text("".join(lines))
PY2
        as_canary grep -q "_o._exit(1)" "$dir/server/server.py" || { log "late plant not applied"; return 1; }
    fi
    if [[ ${2:-} == fail ]]; then  # planted failure: the server exits at import
        as_canary bash -c "printf 'raise SystemExit(\"planted canary failure\")\n' | cat - $dir/server/server.py > $dir/server/server.py.x && mv $dir/server/server.py.x $dir/server/server.py"
    fi
    as_canary bash -c "cd $dir && git init -q && git add -A && git commit -qm 'canary of $1${2:+ ($2)}' && git rev-parse --short HEAD" | tail -1
}
install_from() {  # install_from <dir> [done-file]
    as_canary bash -c "cd $1 && CLARP_TOOLCHAIN_MODE=none ./install.sh > install.log 2>&1; rc=\$?; ${2:+touch $2;} echo \$rc"; }
rc_in() { as_canary python3 /home/canary/release-check "$@"; }
boot_wait() {
    local target=$1 until=$((SECONDS+300))
    while (( SECONDS < until )); do
        if rc_in --ignore-stt status | python3 -c 'import json,sys;d=json.load(sys.stdin);p=d["probe"];sys.exit(0 if p.get("status_code")==200 and p.get("health_ready") is True and p.get("runtime_available") is True and p.get("runtime_release_id")==sys.argv[1] and d["runtime_on_installed"] else 1)' "$target"; then
            (( SECONDS < until )) || return 1
            return 0
        fi
        sleep 2
    done
    log 'supervised runtime boot health deadline 300s exceeded'; return 1
}
restart_once() {
    local target=$1 old_pid new_pid
    old_pid=$(as_canary systemctl --user show clarp-runtime.service -p MainPID --value) || return 1
    as_canary systemctl --user restart clarp-runtime.service || { log 'restart command failed; no retry'; return 1; }
    new_pid=$(as_canary systemctl --user show clarp-runtime.service -p MainPID --value) || return 1
    [[ $new_pid =~ ^[0-9]+$ && $new_pid != 0 && $new_pid != "$old_pid" ]] || { log 'runtime process replacement not proven'; return 1; }
    boot_wait "$target"
}
restore_both() {
    local previous=$1 target=$2
    # Previous release own interpreter/admin, exactly the official rollback boundary.
    as_canary bash -c 'old="$HOME/.local/share/clarp/releases/$1"; py=$(cat "$old/SERVICE_PYTHON"); CLARP_CODE_ROOT="$old" "$py" "$old/bin/clarp-admin.py" rollback "$1"' bash "$previous" || return 1
    restart_once "$target"
}
case "${1:-}" in
  base)
    sha=$2
    docker image inspect "$IMG" >/dev/null 2>&1 || { log "build OS image"; docker build -q -t "$IMG" canary-native >/dev/null || exit 1; }
    owned_remove || exit 1
    # systemd as PID 1 needs a writable cgroup: privileged (tested 3 Nov; the cap-add variant
    # exits 255). No host mounts, no published ports; only this checkout runs inside.
    docker run -d --name "$C" --hostname native-canary -e container=docker --cgroupns=private \
        --privileged --label clarp.soak-owner=rf-releasec-29c5 --label "clarp.soak-job=$HEAVY_SOAK_REMOTE_JOB_ID" --label "clarp.soak-nonce=$HEAVY_SOAK_NONCE" -e HEAVY_SOAK_NONCE -e HEAVY_SOAK_REMOTE_JOB_ID -e HEAVY_SOAK_BOOT_ID --tmpfs /run --tmpfs /run/lock "$IMG" >/dev/null || exit 1
    for _ in $(seq 1 60); do docker exec "$C" test -S /run/user/1000/bus 2>/dev/null && break; sleep 1; done
    docker exec "$C" test -S /run/user/1000/bus || { log "user manager did not start"; docker logs --tail 20 "$C"; exit 1; }
    docker cp release-check.py "$C":/home/canary/release-check && docker exec "$C" chown canary /home/canary/release-check
    docker cp tests/qa/fake_codex.py "$C":/usr/local/lib/fake_codex.py || exit 1
    docker exec "$C" bash -c 'printf "#!/bin/sh\nexport CLARP_QA_PROVIDER_ROOT=\$HOME/.codex CLARP_QA_STREAM_DELAY=1\nmkdir -p \$HOME/.codex/sessions\nexec python3 /usr/local/lib/fake_codex.py \"\$@\"\n" > /usr/local/bin/codex && chmod 755 /usr/local/bin/codex /usr/local/lib/fake_codex.py' || exit 1
    v=$(stage "$sha"); log "staged $sha as $v"
    rc=$(install_from /home/canary/src-${sha:0:8}); log "install.sh exit $rc"
    [[ $rc == 0 ]] || { as_canary tail -20 /home/canary/src-${sha:0:8}/install.log; exit 1; }
    base_rt=$(as_canary cat /home/canary/.local/share/clarp/current/RUNTIME_RELEASE_ID)
    boot_wait "$base_rt" || exit 1
    rc_in --no-units status | python3 -c 'import json,sys; d=json.load(sys.stdin); p=d["probe"]; print("base:", p["status_code"], p.get("release_id"), "runtime", p.get("runtime_release_id"), "on installed", d["runtime_on_installed"])'
    as_canary systemctl --user is-active clarp.service clarp-runtime.service | tr '\n' ' '; echo
    ;;
  release)
    sha=$2; variant=""; net=""
    # --no-safety-net (4th arg): the watcher never restarts the Host itself, so only the
    # installer's own failed-activation restore is exercised (OPS-N2 evidence).
    if [[ ${4:-} == --no-safety-net ]]; then net="--recover-after 100000 --switch-timeout 150 --max-down 100000"; fi
    case ${3:-} in --plant-fail) variant=fail;; --plant-late-fail) variant=late;; --touch-lib) variant=lib;;
        --plant-late-fail-lib) variant=latelib;; "") ;; *) log "unknown flag $3"; exit 2;; esac
    docker cp release-check.py "$C":/home/canary/release-check && docker exec "$C" chown canary /home/canary/release-check
    if ! as_canary systemctl --user is-active --quiet clarp.service; then  # canary housekeeping after a failed run
        log "canary Host was down before the release: reset-failed + restart"
        as_canary bash -c 'systemctl --user reset-failed clarp.service; systemctl --user restart clarp.service; sleep 5'
    fi
    prev=$(as_canary bash -c 'basename $(readlink -f ~/.local/share/clarp/current)')
    prev_rt=$(as_canary cat /home/canary/.local/share/clarp/current/RUNTIME_RELEASE_ID)
    v=$(stage "$sha" $variant); log "staged $sha${variant:+ ($variant)} as $v; previous release $prev"
    dir=/home/canary/src-${sha:0:8}${variant:+-$variant}
    docker exec -d -u canary -e XDG_RUNTIME_DIR=/run/user/1000 -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
        -e HOME=/home/canary -w /home/canary "$C" bash -c "python3 /home/canary/release-check --observe-runtime --ignore-stt watch --expect-release-id '$v-*' --previous $prev \
        --minutes ${CANARY_MINUTES:-2} --interval 5 --switch-timeout 600 --max-down 120 --install-done-file /home/canary/install-done-$v $net --previous-runtime-id $prev_rt --rollback-follow-hours 0.25 --log /home/canary/watch-$v.jsonl > /home/canary/watch-$v.out 2>&1"
    sleep 1
    as_canary rm -f /home/canary/install-done-$v
    rc=$(install_from "$dir" /home/canary/install-done-$v); log "install.sh exit $rc"
    for _ in $(seq 1 120); do as_canary grep -q watch_end /home/canary/watch-$v.out && break; sleep 5; done
    as_canary grep -E '"event": "(switched|installer_finished|restart_previous_start|restart_previous_command|rollback_start|rollback_command|host_restored|runtime_restore_wait|watch_end)"' /home/canary/watch-$v.out | python3 -c '
import json,sys
for l in sys.stdin:
    d=json.loads(l)
    for k in ("sample","final","rollback","command","stdout_tail","installed"): d.pop(k,None)
    print(json.dumps(d))'
    wrc=$(as_canary python3 -c "import json;print([json.loads(l) for l in open('/home/canary/watch-$v.out') if 'watch_end' in l][-1]['exit'])" 2>/dev/null || echo 99)
    log "watch exit $wrc"
    if [[ $wrc == 0 ]]; then
        rid=$(as_canary cat /home/canary/.local/share/clarp/current/DEPLOYED_RELEASE_ID)
        rrt=$(as_canary cat /home/canary/.local/share/clarp/current/RUNTIME_RELEASE_ID)
        # 803 supervised runtime activation, no legacy runtime-outage Host rollback.
        restart_once "$rrt"; frc=$?
        if [[ $frc != 0 ]]; then
            log 'candidate boot failed: official BOTH Host/runtime rollback'
            restore_both "$prev" "$prev_rt" || { log 'BOTH rollback health unresolved'; exit 11; }
            exit 11
        fi
        log "supervised runtime target healthy $rrt"
    elif [[ $wrc == 10 ]]; then
        # Late-failure drill: verify the previous runtime as well as the restored Host.
        restart_once "$prev_rt" || { log 'rollback runtime health unresolved'; exit 11; }
    fi
    # A green release needs the Host watch AND the runtime follow to pass: the follow's result
    # is the release's result when the watch passed.
    if [[ $wrc == 0 && ${frc:-0} != 0 ]]; then log "follow-runtime did not pass (exit $frc): release NOT green"; wrc=$frc; fi
    as_canary bash -c 'basename $(readlink -f ~/.local/share/clarp/current)'
    as_canary systemctl --user is-active clarp.service clarp-runtime.service | tr '\n' ' '; echo
    as_canary systemctl --user show clarp.service -p Result -p NRestarts | tr '\n' ' '; echo
    as_canary python3 /home/canary/release-check --ignore-stt --no-units status | python3 -c 'import json,sys; d=json.load(sys.stdin)["probe"]; print("host after:", d["status_code"], d.get("release_id"), "ready", d.get("health_ready"))'
    exit "$wrc"
    ;;
  handover-turns)
    # Turns in flight across a runtime-rolling release (TP-04 exposure). Needs a base install.
    sha=$2
    docker cp tests/qa/fake_codex.py "$C":/usr/local/lib/fake_codex.py
    # The fake writes its rollouts under CLARP_QA_PROVIDER_ROOT/sessions, which must be where
    # the runtime looks (CODEX_HOME/sessions), or a restart starts fresh conversations.
    # The delay paces the fake reply (3 steps): ~60 s turns, so they are in flight at the
    # release. Not via config [env] (CLARP_* names are managed by Clarp there).
    docker exec "$C" bash -c 'printf "#!/bin/sh\nexport CLARP_QA_PROVIDER_ROOT=\$HOME/.codex CLARP_QA_STREAM_DELAY=20\nmkdir -p \$HOME/.codex/sessions\nexec python3 /usr/local/lib/fake_codex.py \"\$@\"\n" > /usr/local/bin/codex && chmod 755 /usr/local/bin/codex /usr/local/lib/fake_codex.py'
    docker cp canary-native/turns-driver.py "$C":/home/canary/turns-driver.py && docker exec "$C" chown canary /home/canary/turns-driver.py
    as_canary bash -c "sed -i '/^\[env\]\$/d; /^CLARP_QA_STREAM_DELAY/d' ~/.config/clarp/config.toml"
    # canary only: pick up the fake codex and [env] before the scenario
    as_canary bash -c 'systemctl --user restart clarp-runtime.service clarp.service; sleep 8'
    as_canary python3 /home/canary/turns-driver.py setup /home/canary/turns.json || exit 1
    bash "$0" release "$sha" --touch-lib; rrc=$?
    as_canary python3 /home/canary/turns-driver.py check /home/canary/turns.json 600; trc=$?
    log "release exit $rrc, turns check exit $trc"
    as_canary bash -c 'journalctl --user -u clarp-runtime.service --since "-15min" --no-pager | grep -c -i -E "interrupt" || true' | sed 's/^/runtime journal lines mentioning interrupt: /'
    [[ $rrc == 0 && $trc == 0 ]]
    ;;
  exec) shift; as_canary "$@" ;;
  down) owned_remove || exit 1; log "owned container down; OS image retained" ;;
  *) sed -n 2,8p "$0"; exit 2 ;;
esac
