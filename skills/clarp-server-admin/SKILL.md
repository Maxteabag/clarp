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
grep -n '_SCHEMA_VERSION = ' server/lib/db_schema.py                       # your branch
git show origin/main:server/lib/db_schema.py | grep -n '_SCHEMA_VERSION = ' # merged
grep -n '_SCHEMA_VERSION = ' ~/.local/share/clarp/current/lib/db_schema.py  # deployed
sqlite3 ~/.local/share/clarp/state.sqlite 'pragma user_version;'     # live DB
```

When first assigning a migration, choose a number above the versions already
used by these sources. An already-assigned candidate number can be retained if
no other source has used it; do not repeatedly increment it merely because the
candidate itself appears in the comparison. Add a migration guarded by
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

## Host, runtime and plugin activation are separate

The supported installer restarts the HTTP Host and enables the runtime service;
it does not force-restart an already-running runtime. Current `runtime.py` starts
`RuntimeReleaseMonitor`, which watches the installed `RUNTIME_RELEASE_ID` only
when `RUNTIME_READY` exists. It hands off to the new release after
`begin_drain_if_idle()` succeeds. Busy turns remain on their existing runtime.
Verify this mechanism in the candidate and deployed source rather than applying
older advice that no idle handoff exists.

HTTP endpoints and schema migrations activate with the Host restart. Runner
code becomes active after the natural idle handoff; installer success alone
is not proof of that transition. Read the runtime status `release_id` and
`draining` fields and compare with the installed release.

The standard Claude runner launches one CLI process per turn. Its plugin path
resolves through `share/plugin -> current/plugin` on each launch, so the next
naturally admitted turn can load the new plugin even while the runtime process
still uses the predecessor's compatible runner code. Hook subprocesses resolve
the library colocated with their plugin, and managed helper wrappers resolve
`current` rather than retaining an inherited old code root. Do not claim that
an already-running Claude turn hot-reloaded its plugin; verify an actual
provenance receipt on subsequent natural work. Directory-plugin caching and
manual persistent CLI sessions need their own verification.

Never force a runtime or fleet restart to make a rollout look complete. A manual
restart interrupts in-flight turns and requires explicit authorization. Preserve
an existing recovery pause, active conversations, account selection, and rollback.

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

## Vendor CLI version blocks a resumed agent

A successful Host restart is not proof that a native agent resumed. Inspect its
actual transcript for errors such as a model requiring a newer Claude Code CLI.
Read the runtime process's PATH and resolve the executable using that PATH; a
user-local version check can differ from the managed one, and an earlier Host
`bin/claude` entry can shadow a hash-pinned toolchain wrapper. Run only `--version`
for this check; it does not prove a successful model response.

The canonical managed pins and upgrade procedure are in `toolchain/README.md`.
Build a new hash-addressed toolchain with `scripts/install_agent_toolchain.py`;
never run `npm ci` in an active prefix to repair one vendor, because it also
replaces the other vendor's packages. Keep running processes, credentials and
unrelated CLI versions intact. Coordinate ownership before switching executable
routing, recheck its current target immediately before mutation, and retain a
rollback receipt. A newer competing route is a reason to reconcile, not overwrite.
A runtime restart is not needed merely to make a future child resolve a changed
executable path, but existing child processes keep their current binary. Verify
an actual resumed response/tool activity separately, in the exact conversation.
