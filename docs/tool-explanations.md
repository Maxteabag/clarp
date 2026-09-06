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
Legacy clients without demand IDs retain their existing behavior.

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

The Host coalesces equivalent metadata at each audience level. A serial worker
batches eight requests after a 180ms debounce; the queue holds at most 64 and the
in-memory cache at most 512 entries. Restarting the HTTP service clears the cache.
Failure entries are cached for 60 seconds, preventing polling retry storms; a new
request after that can retry. Clients stop polling terminal failures. Cache entries
are not permanent records; raw payloads and script excerpts are not persisted.

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
ctest --test-dir desktop/build/release -R 'tool-narrator|activity-layout' --output-on-failure
```
