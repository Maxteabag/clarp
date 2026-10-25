# Muse Code backend

Status: in progress on `feat/muse-backend`. Follows
[backend-strategy.md](backend-strategy.md): one `MuseBackend` class, no
`if backend == "muse"` outside the registry.

Muse Code is Meta's terminal coding agent (`muse`, launcher in
`~/.local/bin/muse`, the binary self-updates). Observed on 1.4.13-R6996.1.

## Turn runner

One headless process per turn, like Grok:

```
muse exec --json --yolo --session-id <uuid> [--model M] [--reasoning-effort E] -- <prompt>
```

* `--session-id` both creates and continues a session: a second `exec` with
  the same id appends to it (its sequence numbers continue). Clarp therefore
  pre-mints the UUID for a new agent and binds it before the process starts,
  and resumes by passing the same id.
* `--yolo` turns approval and the sandbox off and trusts the workspace for
  the run, the same posture the other backends run with.
* `--json` prints one JSON envelope per line on stdout:
  `{schema_version, stream, sequence, record_type, payload_type, payload}`.
  The runner reads `payload_type`:

| payload_type | meaning | runner action |
|---|---|---|
| `run.model.configured` | model actually used (`model_id`) | record |
| `run.lifecycle.started` | the turn began | `SPAWN_STARTED` |
| `run.output.delta` | assistant text delta (`text`) | live text, `<speak>` |
| `tool.result` | a tool finished (`call_id`, `correlation_facts.tool_name`, `outcome`) | flush live segment, tool phase, refresh transcript |
| `run.usage` | tokens per model and the subscription allowance (`window`, `weekly`) | usage, quota |
| `run.terminal.<state>` | `completed`, or `failed` / `cancelled` / `interrupted` / `aborted` with `reason` | result or error |

Everything else (`task.lifecycle.*` for reminder sub-agents, workspace
facts) is ignored.

## Transcript

`~/.local/share/muse/sessions/YYYY/MM/DD/<session-id>/session.jsonl` is the
durable log. Rows with `payload_type == "runtime.session"` and
`payload.kind == "run"` carry `payload.event`:

* `started` → user row (`prompt`)
* `assistant_message_committed` → assistant row (`text`)
* `assistant_tool_calls_committed` → tool calls (`name`, JSON `args`, `call_id`)
* `tool_result_batch_committed` → their results (`tool_call_id`, `text`)
* `reasoning_committed` → encrypted; never shown
* `terminal` → a non-`completed` state becomes an error row

`runtime.session.metadata` / `route_facts` give the workspace root and cwd
for `list_sessions`.

## Catalogue

`id = "muse"`, label "Muse Code", binary `muse`, terminal
`muse --yolo resume <id>`. Sign-in happens outside Clarp (`muse login`).

Models come from Muse's own catalogue cache,
`~/.local/share/muse/model-catalog/*.json`: visible rows only, each with its
own effort tiers (`effort_scope = "model"`, default `high`); without a cache
the bundled `muse-wonder-1.0` row is offered.

Not supported (yet): compaction (Muse compacts its own context; a typed
`/compact` in `muse resume` is unverified), forks, steering, goals, and the
quota card (`run.usage` carries the allowance windows, which the runner
records but does not yet publish).

## Fixtures

`tests/fixtures/muse/` holds compacted real output: one `exec --json` turn
with a `read_file` tool call, one echo-provider turn, and the matching
`session.jsonl` rows (encrypted reasoning redacted, rules text dropped).
