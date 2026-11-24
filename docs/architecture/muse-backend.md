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

* Not passed yet, `--disable-reminders` (waits for the owner's answer to
  decision-eb180055fd7e840f3f96e7c8): Muse's reminder agents (skill-, goal- and
  verify-reminder) are model calls in child sessions, mid-turn and after the
  answer. An accepted one puts a short developer `<system-reminder>` into the
  agent's next step (28 of 1,453 proposals on one machine); none writes
  memory. The one after the answer runs before the process exits, and the
  session stays locked until then. They held every
  turn's end, and the start of a message waiting behind it: 3 to 7.5 s from
  the last text to exit with them, 0.6 to 2.7 s without, in direct runs, and
  8 to 19 s through a Host (measured 24 Nov 2026 on 1.4.25).
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

## Speed (measured 24 Nov 2026, Muse 1.4.25, muse-spark-1.3)

* The Host adds little: queue and launch under 0.05 s, a waiting message
  starts 0.3 s after the turn before it ends (`server-rs/tools/turn-timings
  --backends muse`).
* Each `exec` pays Muse's own start against Meta's API, 1.3 to 4 s (0.4 s
  with the echo provider), and without `--disable-reminders` the reminder
  agents' model call at the end.
* A spoken turn is the slow one. The voice rules at the head of the prompt
  make Muse write the whole answer in its reasoning before the first word,
  and weigh them against a short request ("reply only BANANA"): first text
  after 48 to 68 s for a 50-item list against 7 to 13 s for the bare prompt,
  and 11 to 32 s of reasoning for a one-word reply.
* Steering needs a long-lived process: `exec` sessions refuse
  `muse session-message` (`target_surface_excluded`). `muse serve` speaks
  MSP over stdio (`session/start`/`session/resume`, `turn/start`,
  `turn/steer` with `expectedTurnId`, `turn/interrupt`, `item/delta`
  notifications) and would also keep the start off every turn.

Grok, the other one-process-per-turn tool here, uploads telemetry after its
last line and when it is stopped: 1 to 1.6 s from `end` to exit and 1.5 to
3 s from a Stop to its output closing. Its turns run with
`DISABLE_TELEMETRY=1`: 0.02 to 0.33 s and 0.1 s.
