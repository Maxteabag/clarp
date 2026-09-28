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
`gpt-5.3-codex-spark` with low reasoning, not medium. Cache prompt version is 3:
the model also returns a parameterised template (see Tiers and learning).
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

## Tiers and learning

Each call is split into parts: a compound shell command at top-level `&&`,
`||`, `;`, `&`, `|` and newlines, anything else as one part. `cd DIR` becomes
the directory of the parts after it, and `| head -n N`, `true` and `:` are
dropped. `bash -c '...'` is the parts of its argument. A heredoc read by an
interpreter (`python3 - <<'EOF'`, `node -`, `bash`) is that program running
an inline script, and one fed to `cat > PATH` or `tee PATH` is a file write
(the `write_file` template). The heredoc's body is withheld before anything
else sees the call: it is never a slot, never stored, and never sent to Jev
or the model, so every such call reads `python3 - <<'EOF'` / `…` / `EOF` and
one explanation holds for any body. A `$(...)` that only reads (`date`,
`git rev-parse`, `basename`, …, piped only into filters) is one `expr` slot.
Quotes are respected; any other command substitution, subshells, heredocs
read by anything else (a database shell, `ssh`) or whose unquoted body runs a
command, process substitution, shell keywords, and pipelines into anything
but a plain output filter (`grep`, `sort`, `wc`, `jq`, …) keep the whole
command as one opaque part. Each part goes through the tiers, cheapest first:

1. **Template.** The typed scripted templates (`tool_explanation_templates`),
   including mappings a reviewer approved. Synchronous.
2. **Learned.** The part's *shape* is its words with every dynamic value
   replaced by a typed slot: numbers, commit IDs, URLs, paths, `$` expressions,
   identifiers with digits or mixed case, and free text. Programs,
   subcommands, flags and plain lowercase or upper-case words stay literal,
   because they choose what a program does (`filectl list` versus `filectl
   delete`). The signature is `sh:<program> #<keyed hash of the shape>`; the
   rules are in the `tool_explanation_shapes` docstring. One indexed lookup in
   `tool_explanation_learned` either renders a stored sentence with this
   call's values (`{path1}`, `{path1_name}`, `{num1}`, …) or returns an
   exact-only answer for identical text. Synchronous.
3. **Jev chooses from the learned table**, for every part that tiers 1 and 2
   miss, when the `explanations` judgment site is on. Its options are the
   closest entries of the whole, permanently growing
   `tool_explanation_learned` table; the built-in templates are extras
   after them. Jev picks one or abstains; it never writes text. The answer
   starts with "Likely", and a pick with at least 0.90 confidence is
   learned under this part's own shape (producer `jev`, `source_signature`
   naming the row it came from), so the next call with that shape is a
   tier-2 hit and every pick widens what later calls can reuse. Choosing
   the options:

   - **Retrieval** (`learning.library`, `learning.rank`): one read per batch
     of the model's rows at this audience (parameterised and exact-only;
     Jev's own copies left out; at most 5,000, most used first), ranked per
     part: same program and action, then same program, then any other row
     whose sentence shares a word stem with the part, more shared words
     first. No embeddings, no network. Up to five are offered. The program
     is the first word, looking through wrappers (`timeout N`, `env A=1`,
     `nice`, `nohup`, `sudo`), so `timeout 60 dotnet test` is a `dotnet`
     call; `sudo` stays `privileged` all the same.
   - **Safety**: a row is offered only if this part fills every placeholder
     from its own slots, and it is shown to Jev as it would read for this
     call. A sentence that states a value the call does not contain as a
     whole token (a number, an identifier with digits, `_`, `.` or three
     segments such as `oracle-mike-call`) is dropped: that value belonged to
     another call, and offering it would invent a parameter. The "unknown"
     choice reads "none of these states exactly what the call does, or its
     effect is unclear", so Jev abstains when nothing fits.
   - **Template extras**, only for an unknown program or tool (`jev_offer` in
     `tool_explanation_templates`): the read/list/search templates that can
     render from the part's own arguments. A template that acts on a file or
     folder needs a usable argument; without one only templates true of the
     current directory, or needing no value, are offered; a search is never
     offered because the call cannot supply its pattern. Candidate arguments
     exclude shell punctuation (`]`), `$` expressions, flags, bare numbers
     and text with spaces. Templates claim a read with no change, so they
     are never offered to a mutating, script, privileged or upload call.

   A part with nothing to offer is not sent to Jev. Its ledger reason says
   why: its own route reason (`mutating_program`, `truncated`,
   `shell_builtin`, …), `jev_no_target` (a tool with no argument, such as a
   `done`/`idle` status row or a delegation), `jev_unsafe_arguments`
   (arguments, but none usable) or `jev_no_candidates` (an unknown program
   with nothing that renders). A `jev_*` reason otherwise means Jev was
   asked and abstained: `jev_unknown`, `jev_low_confidence` (below 0.80),
   `jev_no_argument` (a file template but no argument), `jev_invalid_parameters`
   (the pick failed validation or rendering). An answered part's reason is
   `jev_learned:<same_action|same_program|lexical>` or `jev_template:<id>`,
   with the confidence in `jev_confidence`. A Codex row whose tool name is a
   `/usr/bin/bash -lc "…"` display label is explained as that shell command.
   That label is clipped at 80 characters, so the Codex backend remembers
   each agent's recent whole commands in memory (`tool_explanation_commands`,
   256 per agent) and the explainer uses the one the clipped label starts;
   the apps' display and request are unchanged. A label the Host cannot
   complete (after a restart, or two recent commands with that start that
   differ in more than a withheld heredoc body) is `truncated` and exact. A grouped exploration row with a single
   `Read: path` is the `read_file` template; other exploration rows are
   `exploration` or `multiple_targets`.
4. **Model.** The language model gets each unanswered part with its `slots`
   and returns `text` and a `template` with placeholders. The template is
   learned for the shape only if it renders back to exactly `text`, uses only
   known slots, and repeats none of this call's values (nor any number, when
   the call has a numeric slot). Otherwise the text is learned exact-only for
   that identical part, and the decision's reason says `exact:<why>`.

A compound call is ready when all its parts are, and its text joins them in
order with "Then". Learned rows have no expiry; they are ignored when
`PROMPT_VERSION` or the template library version changes, and `clarp-admin
explanations revoke SIGNATURE` deletes one (`clarp-admin explanations learned`
lists them). Approving or rejecting a mapping also deletes a Jev pick learned
for that shape. Learned rows are shared by every Janitor configuration and
target on the Host; the 24-hour exact cache below stays per configuration.
Maintenance deletes rows of dead versions after 30 days and keeps at most
100,000 exact-only rows (least recently used go first).

Every row names its `program`. A whole command that is one opaque part
(substitution, heredoc, loop, a pipe into a non-filter), a malformed one and a
clipped Codex label take the program of their first real simple command,
skipping `cd`, env assignments, builtins, keywords, wrappers and heredoc
bodies, and looking inside `(...)`, `{ ...; }` and `$(...)`. Before schema v99 such
rows were keyed `x:? #hash` with no program, and `timeout N cmd` parts were
keyed by `timeout`. v99 fills `program` wherever the stored signature names
one; the others cannot be backfilled because the command was never stored, so
the next identical call still finds the old key and the next ledger write
moves that row to the current key and program. Once after the upgrade,
maintenance also shapes every shell command the Host still holds in
`messages` (tool input, Codex label and its clip, command row) and moves each
`x:? #…` row whose key matches (`recover_programs`; it writes only the new
key and program, and records that it ran in the `tool_explanations.program_recovery`
setting). Only keyed hashes and
placeholder text are stored for parameterised rows; exact-only rows hold the
explanation a client was shown.

## Decisions and hit rate

Every explained call is one row in `tool_explanation_decisions`: time, agent,
signature, `tier` (`template`, `learned`, `exact_cache`, `jev`, `llm`,
`failed`, `miss`, `disabled`), audience, part count, Jev confidence, latency,
model and a short reason. A compound call counts once, in its most expensive
part's tier. Polls of queued work add nothing: the worker records the answer,
and the poll that collects it is not counted again. A busy or disabled item
is recorded at most once a minute per call. Work abandoned before the worker
ran, and cancelled rows, are not decisions. Rows are buffered in memory and
written with the worker's batch transaction, or once a second when idle; a
crash can lose the last second. Maintenance deletes rows older than 90 days
and beyond 500,000.

`GET /tool-explanations/stats?window=24h|7d|30d&bucket=hour|day` (see
`docs/protocol.md`) and `clarp-admin explanations stats [--window 7d]
[--bucket day] [--json]` report counts per bucket and `hit_rate = (template +
learned + exact_cache) / lookups`, where lookups exclude `disabled`. Read it
as: `template` is the fixed floor from shipped templates; `learned` is what
learning adds and should grow while `llm` shrinks as shapes repeat; a high
`exact_cache` share would mean learning is not storing answers; `jev` and
`llm` are the calls that still cost a request. `totals.learned_exact_only`
against `learned_parameterised` shows how often the model's templates could be
reused.

## SQLite state and retention

SQLite is authoritative for the whole explanation system, in the Host database
reported by `clarp-admin paths` (normally `~/.local/share/clarp/state.sqlite`).
Schema v72 added four tables, v97 adds `tool_explanation_learned` and
`tool_explanation_decisions` (see above), and v99 adds the learned row's
`source_signature` and backfills `program`:

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
uv run --group dev pytest tests/unit/test_tool_explanation_learning.py tests/unit/test_tool_explanation_hybrid.py
uv run --group dev pytest tests/unit/test_tool_explanation_jev_similar.py tests/unit/test_tool_explanation_jev_picks.py
uv run --group dev pytest tests/unit/test_tool_explanation_full_commands.py
ctest --test-dir desktop/build/release -R 'tool-narrator|activity-layout' --output-on-failure
```
