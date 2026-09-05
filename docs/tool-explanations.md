# Shared tool explanations

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
Failure entries are cached too, preventing polling retry storms. Cache entries
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
