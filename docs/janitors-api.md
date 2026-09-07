# Janitors API contract

Implementation contract for Host and native clients. Janitors retain an ordinary
agent identity and runtime; `is_janitor` defaults to false on existing agents.

All routes require existing Host authentication and full device access for
mutations. Times use Unix milliseconds. Unknown Janitor routes on older Hosts
mean unsupported, not an empty successful configuration.

## Read

`GET /janitors` returns `{janitors: [Janitor], templates: [Template], triggers:
[TriggerDefinition]}`. `GET /janitors/{session}` returns `{janitor: Janitor}`.
`GET /janitors/{session}/runs?limit=30` returns `{runs: [Run]}`.
`GET /janitor-triggers` returns `{triggers: [TriggerDefinition]}`.
`GET /janitor-triggers/preview?cron=...&timezone=...&count=3` returns
`{next_runs: [Unix-milliseconds], timezone: "Europe/Oslo"}` without saving or
starting work. Invalid cron/timezone values return HTTP 400.

Janitor:

```json
{
  "agent_id": "stable-id", "session": "sam-042d", "name": "Sam",
  "is_janitor": true, "enabled": false, "revision": 1, "generation": 1,
  "template_id": "task-labels", "model": "", "effort": "",
  "scope": {"agent_ids": [], "exclude_agent_ids": []},
  "health": "paused", "last_error": "", "last_run_at": null,
  "last_change_at": null, "current_run_id": null,
  "attachments": [{
    "attachment_id": "attachment-id", "trigger_id": "agent-work-completed",
    "trigger_version": 1, "enabled": true,
    "config": {"coalesce_seconds": 8, "max_targets": 3},
    "next_run_at": null
  }]
}
```

An empty `scope.agent_ids` watches all non-Janitor, nonarchived task agents on
this Host except explicit exclusions. Model and effort belong to the ordinary
agent record. Health: `paused`, `ready`, `running`, `needs_attention` (Host
availability is also evaluated by the client). Pausing invalidates effects
immediately when acknowledged, even if cancellation of model work takes longer.

Template: `{id, name, description, allowed_effects}`. Initial ID `task-labels`.
Templates also advertise `recommended_backend`, `recommended_model` and
`recommended_effort`. Task labels recommends Codex / gpt-5.3-codex-spark / low;
new Codex identities use that preset when model/effort are omitted. Explicit
settings and existing-agent conversion preserve the selected overrides. Native
creation confirms the recommendation exists in the actual model catalog and
requires a choice if unavailable, rather than silently selecting another model.
TriggerDefinition: `{trigger_id, version, name, kind, defaults}`. Built-ins:
`agent-work-completed@1` (kind `event`) and `schedule@1` (kind `schedule`). Schedule
configuration: `{cron: "30 8 * * 1-5", timezone: "Europe/Oslo"}`. Existing legacy
schedules retain UTC; Janitor schedule configuration carries an explicit zone.

Run: `{run_id, agent_id, session, attachment_id, generation, status, outcome,
created_at, started_at, finished_at, trace_id, error, results}`. Terminal results
include `changed`, `same_task`, `insufficient_context`, `skipped`, `error`, and
`cancelled`; queued/running are execution states. Each result carries
`target_session`, `before`, `after`, `outcome`, and `reason` where available.
Only actual effect receipts support a claim that a label changed.

## Configure

`POST /janitors` accepts existing `{session, template_id, scope, attachments}`
or new `{name, backend, cwd, model?, effort?, template_id, scope, attachments}`.
New creation also requires `request_id`, a client-generated UUID retained until
the request is resolved. Identical retries return the recorded identity/config;
reusing that ID with changed input returns 409. Identity provenance is linked
atomically, so retrying after a lost response cannot create a second agent.
It creates or explicitly converts an ordinary agent without changing identity,
and returns `{janitor: Janitor}`. Creation/conversion is paused. Repeated conversion
of an existing Janitor reads its current state; it does not silently overwrite
configuration. New identity creation uses the existing agent creation service.

`POST /janitors/{session}/configure` accepts `{expected_revision, template_id,
scope, attachments, model?, effort?}` and returns `{janitor: Janitor}`. A save
pauses the configuration and advances revision/generation. Old effects are fenced.
Attachments contain the fields above; missing IDs create new attachments, existing
IDs retain per-attachment progress when compatible. Omitting a settings field
preserves its value; attachments supplied as a list replace that configuration.

`POST /janitors/{session}/enabled` accepts `{expected_revision, enabled}` and
returns `{janitor: Janitor}`. Revision mismatch is HTTP 409; the client refreshes
and asks the user to repeat their intent rather than blindly resubmitting. Pause
cancels only this configuration's queued/running maintenance, never observed work.
Enable reconciles current state once; it does not replay a historical backlog.
The response Janitor includes `cancellation_pending` when an old model run is
still stopping. GET /janitors includes `runtime_available`: older agent runtimes
can finish existing work during an HTTP update, but creating/enabling Janitors
requires the runtime's `janitor_runs` capability and otherwise returns 503.

`DELETE /janitors/{session}?expected_revision=N` disables/fences and archives the
Janitor. Agent ID, conversation and run history survive. Response `{ok: true}`.

Errors use `{error: "readable description", code?: "stable_code"}` with 400
validation, 404 missing, 409 stale/conflicting ownership and 503 unavailable.

## Worker effects and dispatch

An internal admitted run owns a stable trace/request ID, frozen generation and
observed targets. Only registered runs may dispatch to a Janitor; a user-provided
origin string cannot grant this permission. Use `queue_if_busy=true` and preserve
the existing runtime bridge and durable queue recovery.

`GET /janitor-runs/{run_id}/context` returns frozen/bounded candidates for the
validated active run. `POST /janitor-runs/{run_id}/review` accepts
`{target_session, observed_state_id, outcome, label?, reason}`. Validate active
configuration generation, candidate membership, fresh task evidence and label
ownership before committing effects and receipts atomically. Accepted outcomes:
`changed`, `same_task`, `insufficient_context`, `error`. A changed label must be
2–3 short words and at most 20 characters. No-op writes become `same_task`.

Snapshots expose `is_janitor` plus `interaction_capabilities` with
`can_chat`, `can_voice_target`, `can_restart` false for Janitors and
`can_inspect` true. The iOS inspector must not use select/focus/conversation
navigation to load its read-only transcript.
