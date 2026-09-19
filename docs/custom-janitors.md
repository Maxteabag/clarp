# Custom Janitors

`custom-task` lets an owner create a maintenance identity with instructions and
local-time schedule or activity-gated interval, using the existing Janitor controls,
quiet dispatch, generation fencing and run history. It uses the configured agent
backend and tools. No extra scheduler, process service or database migration is
needed. This initial implementation is available through the Host API and CLI;
client editors must support the catalog's multiline `text` option to edit instructions.

For example, save this generic configuration to a private file:

```json
{
  "template_id": "custom-task",
  "options": {
    "instructions": "Read the installed maintenance skill and its private configuration. Check only newly reported feedback. Create issues only in the configured repository, deduplicate using source IDs, and report actual verified effects. Do not change schedules or application code."
  },
  "attachments": [{
    "trigger_id": "schedule",
    "trigger_version": 1,
    "config": {"cron": "*/15 * * * *", "timezone": "Europe/Oslo"}
  }]
}
```

```sh
clarp-admin janitor create --name 'Feedback watcher' --backend codex \
  --cwd /absolute/private/working-directory --config @/private/configuration.json --paused
clarp-admin janitor inspect SESSION
clarp-admin janitor enable SESSION --expected-revision REVISION
clarp-admin janitor runs SESSION
```

Use an available backend/model. Creating a Janitor does not validate provider quota.
There is no implicit permission for external actions: instructions must describe
what the user has authorized. Prefer parameterized, guarded helper scripts for
mechanical changes such as workspace renaming or duplicate-safe issue creation.

Instructions are frozen into each run and configurable up to 16,000 characters.
Multiple custom tasks can coexist because they do not own the built-in task-label
or routing effects. Owners must avoid overlapping external writes themselves.
Scope agent IDs do not act as a filesystem or tool-access sandbox.

Schedule enable waits for the next occurrence. No catch-up burst runs after a long
pause. Only one run per identity is admitted at a time, and an ambiguous dispatch
retries the same durable ID. A completed or failed run waits for the next future
occurrence. Active intervals use the existing app-activity lease policy.

Run completion records terminal execution only. It does not invent a count of
issues created or workspace changes. Verify those through the task's helper
receipts. Pause and edits fence future dispatch and context access, while external
effects already in flight require task-specific guards.

To migrate an ordinary scheduled agent, explicitly pause its old schedule, wait
until its current work ends, then convert with `janitor create --agent SESSION`.
Preserve the old UTC schedule's meaning when choosing a timezone. Enable only one
writer. Migration is deliberately not automatic.
