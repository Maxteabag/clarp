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
Keep backups private. Release rollback does not automatically undo a database
migration. Prefer the supported `clarp-admin update --ref FULL_SHA` once the
candidate is verified; do not hand-edit generated releases.

For an authorized update that must survive the HTTP connection restarting, run
`scripts/update_with_job.sh SESSION FULL_SHA PRIVATE_STATE_DIR` in an owned
`systemd-run --user` unit with a private append log and the normal CLI PATH.
Use `--dry-run` first to inspect its target without starting a job. Keep the
script in a permanent location outside the generated release being switched.
It heartbeats a process-fenced job and records `installer-exit`; job completion
means the installer finished, not that phone/runtime verification is complete.
Once installation starts, the installer owns rollback; cancelling the tracking
job is not an emergency stop for the installer. Verify the deployed SHA, schema,
runtime availability, and `clarp-admin doctor` afterwards. The split runtime
drains to a new release when idle; do not restart it manually while turns run.

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
