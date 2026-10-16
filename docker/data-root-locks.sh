# Restore exclusivity for the Clarp container entrypoint (sourced; POSIX sh).
#
# The entrypoint shell opens and holds two flock(2) locks on the data volume for the whole life
# of what it starts, then a staged restore is applied only with exclusive ownership. See
# server/lib/data_root_locks.py and docs/docker.md ("Backup and restore").
#
#   fd 8  <data_root>/clarp/backups/host-owner.lock      EXCLUSIVE for the one Host stack
#   fd 9  <data_root>/clarp/backups/data-root-users.lock SHARED by every user; EXCLUSIVE to apply
#
# Exit codes: 75 data root owned by a live Host; 76 restore refused, data root in use;
# 77 locking unsupported or unverifiable, restore refused; 78 restore refused, archive/provenance.
#
# CONTENTION refuses. A LOCKING ERROR (flock unsupported, or a lock a fresh open could still take)
# refuses only a pending restore; with nothing to apply the Host still starts, with a warning that
# the single-Host guarantee is unavailable on this filesystem.

clarp_is_server_cmd() {
    [ "$#" -eq 2 ] && [ "$1" = "python3" ] && [ "$2" = "/opt/clarp/server.py" ]
}

clarp_locks_py() {
    PYTHONPATH="$CLARP_SHARE_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m lib.data_root_locks "$@"
}

clarp_lock_warn() {
    echo "clarp: warning: $1; the single-Host guarantee is unavailable on this filesystem" >&2
}

clarp_data_root_locks() {
    CLARP_BACKUPS_DIR="$CLARP_DATA_DIR/clarp/backups"
    clarp_locks=held
    if ! clarp_locks_py prepare; then
        clarp_locks=unavailable
    fi
    if [ "$clarp_locks" = held ]; then
        # Append mode: never truncates, and the descriptions stay with this shell and its
        # direct children. Python validates them by identity before anything trusts them.
        exec 8>>"$CLARP_BACKUPS_DIR/host-owner.lock" 9>>"$CLARP_BACKUPS_DIR/data-root-users.lock"
        clarp_locks_py validate-fds || clarp_locks=unavailable
    fi
    clarp_pending=0
    [ -f "$CLARP_BACKUPS_DIR/restore.pending.tar.gz" ] && clarp_pending=1

    if clarp_is_server_cmd "$@"; then
        CLARP_ENTRYPOINT_ROLE=server
        if [ "$clarp_locks" = held ]; then
            clarp_rc=0; flock -x -n -E 75 8 || clarp_rc=$?
            if [ "$clarp_rc" = 75 ]; then
                echo "clarp: the data root is owned by a live Host (exit 75)" >&2
                exit 75
            fi
            if [ "$clarp_rc" != 0 ] || ! clarp_locks_py probe-held owner; then
                clarp_locks=unavailable
            fi
        fi
        if [ "$clarp_pending" = 1 ]; then
            if [ "$clarp_locks" != held ]; then
                echo "clarp: locking is unsupported here: restore refused, the staged archive is kept (exit 77)" >&2
                exit 77
            fi
            clarp_rc=0; flock -x -n -E 76 9 || clarp_rc=$?
            if [ "$clarp_rc" = 76 ]; then
                echo "clarp: restore refused: the data root is in use (exit 76)" >&2
                exit 76
            fi
            if [ "$clarp_rc" != 0 ]; then
                echo "clarp: locking is unsupported here: restore refused, the staged archive is kept (exit 77)" >&2
                exit 77
            fi
            clarp_locks_py probe-held users || exit 77
            clarp_rc=0; clarp_locks_py apply || clarp_rc=$?
            [ "$clarp_rc" = 0 ] || exit "$clarp_rc"
            flock -s 9
        else
            if [ "$clarp_locks" = held ]; then
                clarp_rc=0; flock -s -w 30 -E 76 9 || clarp_rc=$?
                if [ "$clarp_rc" = 76 ]; then
                    echo "clarp: the data root is held exclusively by a restore elsewhere (exit 76)" >&2
                    exit 76
                fi
                [ "$clarp_rc" = 0 ] || clarp_lock_warn "the users lock could not be taken"
            else
                clarp_lock_warn "the data-root locks could not be taken"
            fi
            # Marker recovery happens here, before the stack starts or admits any data.
            clarp_locks_py recover-marker || exit $?
        fi
    else
        CLARP_ENTRYPOINT_ROLE=maintenance
        if [ "$clarp_locks" = held ]; then
            clarp_rc=0; flock -s -w 30 -E 76 9 || clarp_rc=$?
            if [ "$clarp_rc" = 76 ]; then
                echo "clarp: a restore is being applied on this data root (exit 76)" >&2
                exit 76
            fi
            [ "$clarp_rc" = 0 ] || clarp_lock_warn "the users lock could not be taken"
        fi
        # A maintenance command never holds the Host's owner file open.
        exec 8>&-
        # Only after holding SHARED: an apply holds fd 9 EXCLUSIVE from before its marker exists
        # until after it is removed, so a present marker means a half-swapped root.
        clarp_locks_py check-marker || exit 76
    fi
    export CLARP_ENTRYPOINT_ROLE
}
