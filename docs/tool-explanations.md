# Shared tool explanations

## Viewport demand

New clients request only tool views intersecting the conversation viewport,
after 180ms dwell. Mounted offscreen delegates are not demand. This first version
has **no speculative prefetch buffer**. Cached answers remain available when a
row returns; raw data is still hidden while its explanation is pending.

Each request item may include a random `demand_id`. Clients refresh it while
polling; the Host expires queued demand after five seconds without renewal.
Send `items: []` and `release: ["demand-id"]` to release a departed view. Release
affects only that token. Queued work disappears only after every requesting view
has released/expired. Running model batches finish and populate the shared cache.
Two panes on desktop share a local reference count; phones use separate UUIDs.
Late requests for released tokens are fenced by bounded two-minute tombstones.
Legacy clients without demand IDs retain work for up to 24 hours after their last poll.

Desktop checks mapped card coordinates against the transcript ListView every
80ms while narration is enabled, including minimized-window suppression. iOS uses
one shared 100ms visibility sampler for mounted labels, intersects every clipping
ancestor, and suppresses requests outside the active scene. It cancels the label
task on exit and sends a separate best-effort release; Host expiry covers loss
of connectivity. Visibility changes do not rewrite transcripts or move scroll.

Tests cover offscreen/dwell/re-entry geometry, shared view ownership, queued
expiry, released-token races, and completion of already running batches.

Translated levels use the user-selected **refined-low** instructions from the
three-column Spark experiment (lab commit `ae97244`). The exact combined prompts
are in `server/lib/tool_explanation_prompts.json`, with regression SHA-256 checks
against the recorded experiment. Each audience has its own examples and wording
rules; Developer remains raw and never starts inference. The model stays
`gpt-5.3-codex-spark` with low reasoning, not medium. Cache prompt version is 2.
This is a shared Host policy: desktop/iOS keep their existing per-device detail
settings. Updating source on main does not itself update a running Host.

Opt-in presentation only. Developer (0) never invokes a model. Technical (1),
Balanced (2), Plain English (3), and Grandma (4) use distinct audience policies
with `gpt-5.3-codex-spark`, low effort. Client preferences are per device, not a
global Host preference. Raw transcripts are never rewritten.

Hosts advertise `tool_explanations` in `/server-info` capabilities. Authenticated
full-access clients POST `/tool-explanations`:

```json
{"session":"agent-session","detail_level":3,"items":[{"id":"1","activity":{"name":"Bash","command":"ls src"}}]}
```

At most eight activities; IDs must be unique strings (1–128 characters). The
response echoes `detail_level`, reports `model`, and returns `items` containing
each ID with `status`: `disabled`, `pending`, `ready` (with `text`), `failed`
(with a safe reason), or `busy`. Clients poll pending/busy items only, approximately
every 600–700ms, with a bounded overall wait. Switching Host, activity or audience
must cancel/ignore stale client responses. Never reveal raw activity while pending.

## SQLite state and retention

SQLite is authoritative for the whole explanation system, in the Host database
reported by `clarp-admin paths` (normally `~/.local/share/clarp/state.sqlite`).
Schema v72 adds four tables:

- `tool_explanation_cache`: lookup hash, completed text, creation and expiry time.
- `tool_explanation_jobs`: queued/running work, bounded normalized payload,
  audience, claim owner, lease expiry, and temporary failure state.
- `tool_explanation_demands`: per-view ownership and demand expiry.
- `tool_explanation_releases`: short-lived fences against late cancelled requests.

Ready explanations expire exactly **24 hours after generation**; reads do not
extend expiry. Reads reject expired answers and worker/request maintenance removes
expired rows. Cleanup resumes after downtime; this is logical retention, not secure
erasure of SQLite WAL pages or backups. The previous 512-entry RAM limit is gone.
No Python in-memory queue or answer cache is authoritative. Client render caches
remain transient copies; this migration does not add client-side databases.

The queue holds at most 64 waiting jobs plus one batch of at most eight running
jobs, with the existing 180ms debounce. Workers claim atomically using a unique
owner token and a 60-second lease, longer than the 45-second model timeout. A
crashed worker's expired claim can be recovered if viewer demand remains; stale
completions cannot overwrite a newer claim. Recovery is at-least-once model work,
not a guarantee against duplicate model usage after a crash. No described command
is executed. A clean shutdown releases owned jobs for recovery.

Pending payloads, including bounded script excerpts, are stored only while needed
for queued/running work. Success deletes the job and demand rows, retaining only
the answer/hash/timestamps. Cancellation deletes abandoned queued jobs. Failure
clears the payload and persists a safe failure reason for 60 seconds to prevent
retry storms. Every state survives HTTP process restarts until its own expiry.

Only selected bounded metadata and labeled operations are accepted; tool output,
diffs, arbitrary nested payloads, history and client-provided script excerpts are
excluded. For a known agent, directly referenced regular scripts on the Host
(relative paths resolve from its workspace) can contribute at most two
6,000-character excerpts from files no
larger than 64KiB. Hidden files, symlinks and nonregular files are excluded. Script
content participates in cache identity. Common credentials are redacted best-effort;
this is not a general secret scanner. This data is sent to the signed-in Codex
provider. The translator never executes the described command or follows imports.

Each isolated Codex invocation has disabled action integrations, no user/project
instructions, a private temporary directory, a strict JSON output schema with
short enumerated IDs, and a 45-second timeout. Shutdown kills the owned process
group. A Host needs Codex installed and signed in; there is no client-side fallback.

Diagnostics: query the Host journal/event log for `toolExplanationsBatch`.
Fields include model, audience, count, outcome, elapsed_ms and queue_wait_ms.
Commands, excerpts, model text, stderr and exception messages are not logged.

Headless regression gates:

```sh
uv run --group dev pytest tests/unit/test_tool_explanations.py tests/integration/test_tool_explanations_endpoint.py
uv run --group dev pytest tests/unit/test_tool_explanation_cache.py tests/unit/test_tool_explanation_queue.py
ctest --test-dir desktop/build/release -R 'tool-narrator|activity-layout' --output-on-failure
```
