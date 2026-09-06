---
name: clarp-janitors
description: Create, configure, pause and inspect Clarp Janitors and their triggers; review admitted maintenance runs or migrate the task-label pilot safely.
---

# Clarp Janitors

Use `clarp-admin janitor` against the configured local Host. It reuses Host
authentication; never print the token or write the database directly. Older Hosts
may not support Janitors: an unavailable route is not an empty successful result.

Janitors are inspected and configured rather than addressed as ordinary chat or
voice recipients. An existing agent can be converted explicitly, preserving its
identity, model and conversation. New and converted Janitors are always paused.
Use `clarp-admin sessions` to resolve the exact session and stable agent ID first.

## Visual identity

Janitors are rusty metal maintenance robots, not ordinary human portraits.
Use weathered iron, warm oxidized copper/rust, visible rivets and small practical
sensor eyes; friendly, useful and slightly worn rather than threatening. Keep
each robot distinguishable while retaining this shared visual family. Use the
persona/avatar APIs for custom art, never overwrite existing conversation data.
Native Janitor lists use a scalable rusty-robot badge for consistent recognition.

```bash
clarp-admin janitor list
clarp-admin janitor templates
clarp-admin trigger list
clarp-admin janitor create --agent SESSION --template task-labels --paused --dry-run
clarp-admin janitor create --agent SESSION --template task-labels --paused
clarp-admin janitor attach SESSION --trigger agent-work-completed@1 --dry-run
clarp-admin janitor attach SESSION --trigger agent-work-completed@1
clarp-admin janitor inspect SESSION
clarp-admin janitor enable SESSION --expected-revision REVISION
clarp-admin janitor runs SESSION --limit 30
clarp-admin janitor pause SESSION --expected-revision REVISION
clarp-admin janitor reset-defaults SESSION --expected-revision REVISION --dry-run
```

Enable only within the user's existing authorization or explicit UI action.
Configuration saves pause the Janitor; enabling again is deliberate. An omitted
expected revision is read immediately before the write. A 409 is never retried:
inspect the current configuration and obtain renewed intent for any changed scope.
`remove` archives the setup and retains identity and run history; it is not a
conversation-history deletion command.

New identity creation requires `--name NAME --backend BACKEND --cwd PATH`.
The CLI generates one UUID request ID and prints it before sending. If delivery
is ambiguous, repeat the identical creation input with `--request-id THAT_UUID`;
never retry without the original ID or reuse it for changed settings. Supply an
ID before `--dry-run` when the subsequent real request must match that preview.
Structured creation JSON may also contain `request_id`. Existing-session
conversion does not use this new-identity creation ledger.

For structured configuration use `--config @configuration.json`; `--scope` also
accepts JSON or `@file`. `configure` accepts `template_id`, `scope`, `attachments`,
`model`, and `effort`. Scope uses stable agent IDs, with empty `agent_ids` meaning
all eligible task agents on this Host except `exclude_agent_ids`. Supplied
attachments replace that configuration; `attach` retains existing attachments.
Pin `trigger_id` and `trigger_version`. Scheduled attachments use `schedule@1`
with `{ "cron": "30 8 * * 1-5", "timezone": "Europe/Oslo" }` in their config.
Do not silently convert an existing ordinary-agent UTC schedule.

`active-interval@1` accepts `interval_seconds` (default 900),
`idle_timeout_seconds` (default 300), and `run_on_resume` (default true).
Intervals/timeouts must be 60–86400 seconds. Foreground/input leases from the
apps gate admission; no valid lease means no new maintenance. Missed intervals
are not replayed. Returning checks once when due, not on every focus change.
`reset-defaults` restores each attached trigger's parameters and compatible
template model defaults, preserves watched scope/identity/history, and pauses.
Review before deliberately enabling again; never retry a revision conflict.

## During an admitted maintenance run

Use the run ID supplied by the Host. Read bounded evidence once, then submit one
guarded review for every candidate. These are Host effects with atomic receipts;
an accepted model turn is not evidence that any label changed.

```bash
clarp-admin janitor run-context RUN_ID
clarp-admin janitor review RUN_ID --session TARGET --state-id OBSERVED_STATE_ID \
  --outcome changed --label 'Car mode UX' --reason 'The current task concerns the voice interface.'
clarp-admin janitor review RUN_ID --session TARGET --state-id OBSERVED_STATE_ID \
  --outcome same_task --reason 'The existing label still describes the task.'
```

For insufficient evidence use `insufficient_context`; for a failed review use
`error` with a concise reason. Changed labels have 2–3 words and at most 20
characters. Preserve user-owned text. Never infer completion, accept approvals,
change runtime busy state, or announce a deployment from a label review. Do not
ask workers to write additional reports. A paused/stale run cannot apply effects;
do not bypass it with a direct status command, database write or invented run ID.

The companion `scripts/janitor_run.py` accepts `context RUN_ID` or
`review RUN_ID` with the same review flags and delegates to the installed CLI.

## Pilot migration and recovery

Read [references/pilot-migration.md](references/pilot-migration.md). The helper
defaults to read-only preview. Migration is a separately authorized operation;
it never automatically enables the replacement or restarts a failed cutover.

For developer lifecycle integration, admission/backoff and timezone tests, read
the Host's `docs/janitor-runner.md`. Reuse that implementation and its focused
tests rather than starting a second listener or custom scheduler.
