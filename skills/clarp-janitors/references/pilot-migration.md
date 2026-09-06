# Migrate an existing task-label pilot

Prerequisites: the new Host API is installed and healthy, the exact existing
session and stable agent ID are known, the old pilot is idle with no ambiguous
delivery, and any existing replacement configuration is paused. This operation
does not deploy a Host, change a model, or rewrite retained conversation history.

Use the pilot's `session.json`, generation-specific `event-watch-job.json`, exact
systemd user service and exact legacy five-minute schedule ID. No process-name
search or broad cron cleanup is permitted. The helper checks the Host identity,
service command/working directory/PID, schedule owner and paused state.

```bash
clarp-admin janitor migrate-pilot --session SESSION --agent-id STABLE_ID \
  --pilot-dir /absolute/private/pilot --service EXACT.service --cron-id SCHEDULE_ID
```

This default dry run makes only read requests and service inspection. It reports
the cursor, bounded receipt and ownership counts, and omitted ownership/pending
work. Historical claims whose label changed or whose target left the scope are
omitted from active ownership; pending work outside the current scope is omitted
too. The original bounded source is preserved in the private backup. Historical
receipts remain history. A human's replacement label is never reclaimed and
there is no force switch.

When authorized, repeat with `--apply --backup /private/new-backup.json`.
The backup must be a new file; it is created with mode 0600 before any mutation.
The helper disables and stops only the verified old service, confirms its PID
is zero, disables the exact matching old cron if necessary, and checks the
agent is still idle. It then reads the final frozen cursor, creates/converts
the existing agent paused if needed, exports current Host migration state into
the backup, and imports progress/ownership/receipts through the guarded Host API.
Current label values must match before ownership is imported. Import never
rewrites a target's caption. The replacement remains paused after verification.

Imports are bounded to 1 MiB, 1000 targets/receipts and 256 remembered trace IDs.
The retained source journal stays untouched; only its bounded tail is imported.
No transcript, prompt, token, or command history is part of the imported payload.
`import_id` is a stable content hash, so an ambiguous import response can be
checked/retried against the same frozen payload. Repeat the exact migration
identity arguments with `--resume-backup /private/backup.json` to preview recovery,
then `--apply` when authorized. Recovery requires the old service and cron off
and the replacement paused; it reuses the saved import ID and expected revision,
never re-filters changing labels or silently rebases a stale configuration.
The Host fences generation and
requires the exact expected agent ID and revision. Use
`clarp-admin janitor export SESSION` to inspect the Host state.

If anything fails after stopping the pilot, both sides should remain off. Keep
the private backup and inspect the exact error and Host export. Do not enable
the replacement or restart the old service until it is clear which import was
committed. There is no automatic rollback that could create duplicate listeners.
After successful migration, explicitly enable the saved revision only within
authorization, and verify actual run/effect receipts. Runtime process existence
does not prove useful maintenance occurred.
