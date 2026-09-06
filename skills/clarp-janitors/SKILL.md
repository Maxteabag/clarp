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
```

Enable only within the user's existing authorization or explicit UI action.
Configuration saves pause the Janitor; enabling again is deliberate. An omitted
expected revision is read immediately before the write. A 409 is never retried:
inspect the current configuration and obtain renewed intent for any changed scope.
`remove` archives the setup and retains identity and run history; it is not a
conversation-history deletion command.

`release` returns a paused, idle, custom Janitor to ordinary chat, preserving
its identity, model, conversation, original portrait and maintenance history.
Built-in workers cannot be released. For an explicitly authorized replacement,
create the successor paused, then transfer matching maintained labels atomically:

```bash
clarp-admin janitor release SOURCE --expected-revision SOURCE_REVISION \
  --successor REPLACEMENT --successor-revision REPLACEMENT_REVISION --dry-run
clarp-admin janitor release SOURCE --expected-revision SOURCE_REVISION \
  --successor REPLACEMENT --successor-revision REPLACEMENT_REVISION
```

Both configurations must be paused and idle with equal watched scope. The
handoff preserves original label receipts and user-edited text. Read the new
successor revision before separately enabling it within existing authorization.

New identity creation requires `--name NAME --backend BACKEND --cwd PATH`.
The CLI generates one UUID request ID and prints it before sending. If delivery
is ambiguous, repeat the identical creation input with `--request-id THAT_UUID`;
never retry without the original ID or reuse it for changed settings. Supply an
ID before `--dry-run` when the subsequent real request must match that preview.
Structured creation JSON may also contain `request_id`. Existing-session
conversion does not use this new-identity creation ledger.

For structured configuration use `--config @configuration.json`; `--scope` also
accepts JSON or `@file`. `configure` accepts `template_id`, `scope`, `attachments`,
`model`, `effort`, `backend`, `execution`, and `options`. Scope uses stable agent IDs, with empty `agent_ids` meaning
all eligible task agents on this Host except `exclude_agent_ids`. Supplied
attachments replace that configuration; `attach` retains existing attachments.
Pin `trigger_id` and `trigger_version`. Scheduled attachments use `schedule@1`
with `{ "cron": "30 8 * * 1-5", "timezone": "Europe/Oslo" }` in their config.
Do not silently convert an existing ordinary-agent UTC schedule.

## Built-in demand workers

Message delegation and tool explanation are persisted Janitor identities. Their
model, provider, enabled state and typed job options belong to that identity.
Pause the Janitor to disable its feature; do not maintain another enable flag
or independent model/policy configuration in the calling feature.

The catalog provides each job's supported providers, versioned triggers and
typed options. Use these descriptors rather than a hardcoded settings screen.
`routing-requested@1` and `tool-explanation-requested@1` are demand triggers:
the relevant request invokes the selected compatible subscriber. A custom
Janitor can replace a paused built-in, including a non-overlapping watched scope.
These workers do not enter ordinary chat or the task-label scheduling loop.

For the developer adapter contract, read the Host's
`docs/janitor-demand-workers.md`. Do not ask task agents for bookkeeping or
additional context reports. Scope comes from the authenticated request's
resolved source agent, never a caller-supplied target ID.

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
