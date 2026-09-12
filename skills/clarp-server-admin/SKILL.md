---
name: clarp-server-admin
description: Diagnose, update, roll back, and repair a Clarp installation. Use for server health, installation, upgrade, or managed-skill problems.
---

# Clarp Server Administration

Prefer the supported commands:

```bash
clarp-admin doctor
clarp-admin update
clarp-admin rollback
clarp-admin skills repair-links
```

Never hand-edit the active generated release. Use `clarp-admin paths` to find
the platform-native release, configuration, database, cache, log, service, and
toolchain paths.

## Rehearse an additive state upgrade

Before deploying a migration, compare the candidate's `_SCHEMA_VERSION` with
the actual database's `PRAGMA user_version` and required columns. A database
previously opened by another feature branch can have a higher marker without
the new feature's columns. Do not lower the marker or assume version order alone
proves compatibility. Fix and test the candidate migration first.

### Choose the version number against every source, not just main

`_migrate` returns early when `PRAGMA user_version >= _SCHEMA_VERSION`, so a
number another branch already used means your migration **silently never runs**
on that host: the deploy succeeds, the tables are missing, and the feature fails
at runtime. Several agents work this repo in parallel and deploy feature
branches to the live host, so `origin/main` alone does not tell you what is
taken. Read all four before picking:

```bash
git fetch origin
grep -n '_SCHEMA_VERSION = ' server/lib/db.py                       # your branch
git show origin/main:server/lib/db.py | grep -n '_SCHEMA_VERSION = ' # merged
grep -n '_SCHEMA_VERSION = ' ~/.local/share/clarp/current/lib/db.py  # deployed
sqlite3 ~/.local/share/clarp/state.sqlite 'pragma user_version;'     # live DB
```

Take `max(all four) + 1`, add a `_migrate_to_vNN` guarded by
`if version < NN:`, and keep every statement `CREATE TABLE IF NOT EXISTS` so
re-applying is harmless whichever branch lands first. Re-check after every
rebase: a merge that lands mid-task can take your number. Verify after
deploying that `pragma user_version` advanced *and* the new tables exist --
a matching version number alone does not prove the migration ran.

Use the helper from this skill to migrate a new private backup, never the live
database. Choose an existing private directory for the output:

```bash
python3 scripts/rehearse_state_upgrade.py \
  --source /path/from/clarp-admin-paths/state.sqlite \
  --server-root /candidate/checkout/server \
  --output /private/backups/new-rehearsal.sqlite
```

It uses SQLite online backup, refuses to overwrite output, and checks every
existing table's original columns and values after migration. Exit zero proves
an additive upgrade on that snapshot, not a deployment. Intentional data
transformations need their own validation; this helper reports them as changes.

For a migration that intentionally inserts catalog or seed rows, opt in only
the expected existing tables with repeatable `--allow-added-rows TABLE` flags:

```bash
python3 scripts/rehearse_state_upgrade.py \
  --source /path/from/clarp-admin-paths/state.sqlite \
  --server-root /candidate/checkout/server \
  --output /private/backups/new-catalog-rehearsal.sqlite \
  --allow-added-rows janitor_trigger_definitions
```

The default remains strict. Each allowed table retains a complete row multiset
in memory across all original columns, including generated columns, and must
preserve every original value, type and duplicate count. Only additional rows
are accepted; changed/deleted rows and removed tables/columns still fail.
Unknown table names fail before migration. The JSON `allowed_added_rows` field
reports `before`, `after`, `added` and `missing` counts per allowed table, or a
schema error if comparison is impossible. It never prints the row contents.

Keep backups private. Release rollback does not automatically undo a database
migration. Prefer the supported `clarp-admin update --ref FULL_SHA` once the
candidate is verified; do not hand-edit generated releases.

For an authorized update that must survive the HTTP connection restarting, run
`bash scripts/update_with_job.sh SESSION FULL_SHA PRIVATE_STATE_DIR` in an owned
`systemd-run --user` unit with a private append log and the normal CLI PATH.
Use `--dry-run` first to inspect its target without starting a job. Keep the
script in a permanent location outside the generated release being switched.
Invoke it with `/usr/bin/bash` in the unit too: managed copies may not have an
executable bit. Resolve symlinks before choosing the helper path; a dotfiles
skill link can still point into the generated release.
It heartbeats a process-fenced job and records `installer-exit`; job completion
means the installer finished, not that phone/runtime verification is complete.
Once installation starts, the installer owns rollback; cancelling the tracking
job is not an emergency stop for the installer. Verify the deployed SHA, schema,
runtime availability, and `clarp-admin doctor` afterwards.

## Verify HTTP and runner adoption separately

The installer restarts the HTTP service. An already running agent runtime can
continue on its previous release while turns are active. Current `runtime.py`
wires `RuntimeReleaseMonitor`: after `RUNTIME_READY` appears with a different
release identity, it calls `begin_drain_if_idle`, writes a clean handoff marker
and shuts down for the service manager to start the new release. It does not
interrupt busy turns merely because installation completed. The behavior is
covered by `tests/unit/test_runtime_release.py` and the runtime bridge tests.

Check the installed and running versions rather than assuming every Host has
this mechanism. Older runtimes without the monitor need a separate restart.
HTTP-only features can be verified through their endpoints immediately; runner
changes require proof that the running runtime adopted the candidate release.

Never force a shared runtime restart while agent turns are active. Inspect its
status and allow supported idle adoption. If a manual restart is required,
explain the interruption and obtain explicit approval before doing it; a queued
or installed release does not prove new runner behavior is active.

## Docker Container Administration

When diagnosing or managing Clarp inside a Docker container:

- Run diagnostics inside the container:
  ```bash
  docker compose -p clarp exec clarp clarp-admin doctor
  ```

- Authenticating AI providers inside headless containers:
  * For terminal/interactive use, pass `-it` to keep stdin open for OAuth codes:
    ```bash
    docker compose -p clarp exec -it clarp claude auth login --claudeai
    docker compose -p clarp exec -it clarp codex login --device-auth
    ```
  * When automating or scripting the login flow via agent/subprocess, use `-T` and keep the standard input stream connected to feed the callback code to the active prompt without terminating the session or expiring challenge parameters.
  * Ensure the container has working outbound DNS (configured in `compose.yaml`) before initiating OAuth token exchanges.

- Tailscale and phone connectivity:
  * Prefer the `compose.tailscale.yaml` sidecar mode for isolated Tailnet identity, auto-HTTPS, and zero host firewall conflicts.
  * If publishing ports on the host directly (`CLARP_PORT=...`), ensure host firewalls (`ufw` / `DOCKER-USER`) allow Tailscale CGNAT traffic (`100.64.0.0/10` / `tailscale0`) to forward to Docker bridge networks.
