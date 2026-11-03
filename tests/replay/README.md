# Replay harness (refactor program slice S0)

The oracle for the server refactor program (`REFACTOR.md` section 1): replay
the same corpus against a slice's **base** commit and its **head**, and report
every difference in what the server did. A refactor slice must produce zero
unexplained differences. The harness owner (rf-harness) is the only editor of
this directory.

## How to run

Replay starts real test Hosts, so it **never runs on laptopstudio** (Peter's
standing order of 2026-10-12: no test suites or test servers on the machine that
runs his live Host). It runs on the buildbox through
`/var/tmp/clarp-refactor-program/bin/heavy-remote` (development and the
go/no-go proof runs) and in GitHub Actions as the permanent CI gate:
`.github/workflows/replay.yml` (`workflow_dispatch` and `workflow_call`, inputs
`mode` = `self-check`|`compare`, `ref`, `base`, `head`), contained like
`tests.yml` through `scripts/ci_contained.sh`; it uploads the report as the
`replay-report` artifact. Locally only `tests/replay/test_report.py` may run (one file,
`-n 0`, through `heavy`); it starts no Host.

```bash
# A slice: base commit on main against the slice head.
uv run --frozen python tests/replay/run.py compare --base <base-sha> --head <head-sha>
# The harness proof: unchanged code replayed against itself (twice) gives zero
# differences, and every planted change is caught.
uv run --frozen python tests/replay/run.py self-check --ref <sha>
# Remove the cached build worktrees.
uv run --frozen python tests/replay/run.py clean
```

`compare` exits 0 with no unexplained difference, 1 with differences, 2 when a
scenario could not run. `--intended FILE` takes registered difference ids
(one per line, see "Classifying differences"). `--scenarios a,b` limits the
corpus. Scratch space is `/var/tmp/clarp-refactor-program/rf-harness/scratch`
(`--scratch` or `CLARP_REPLAY_SCRATCH` to move it); nothing is written to
`~/.local/share/clarp` or `~/.config/clarp`.

## What runs

```
run.py ── for each build × scenario (thread pool, --jobs 4) ──┐
                                                             capture.py
  host.py (subprocess, own process group, own state dir)  <──┘  drives HTTP, reads SSE frames,
    ├─ server/ and static/ from the BUILD under test               waits on observable state,
    └─ fakes from the HARNESS tree:                                freezes, dumps db and files
       tests/qa/fake_codex.py (deterministic provider process),
       FakeTTSEngine, StubSTT, no provider CLI resolvable
normalize.py ── rewrites clock readings, generated ids, pids, ports, scratch paths
diff.py ────── aligns and compares, prints the report
planted.py ─── deliberate behaviour changes for the proof
```

- **Two builds, one harness.** A build is a commit added with
  `git worktree add --detach` under `scratch/builds/<full sha>`, made
  read-only, and reused only while it is still at that SHA and clean
  (otherwise the run stops with a build error). `--allow-path` accepts an
  existing clean checkout for development; such a run is diagnostic. The
  launcher, fakes, corpus and normalizer always come from the harness tree,
  so base and head differ only in their server code. Every report starts
  with a manifest: target SHA(s), harness commit (and `DIRTY` if
  `tests/replay` has local changes), a digest of the harness files, the
  scenario ids and the planted change ids.
- **Per scenario a fresh Host.** Each scenario starts its own Host process
  on a fresh state directory the harness created and marked; nothing is
  deleted unless it carries that marker. The child gets a minimal
  environment (`PATH=/usr/local/bin:/usr/bin:/bin`, locale, nothing else
  inherited), and `host.py` points `HOME`, `TMPDIR`, `XDG_*`,
  `CLAUDE_PWA_DB`, the `CLARP_*` dirs, `CODEX_HOME` and `CLAUDE_CONFIG_DIR`
  into the state directory. Two codex-backed agents, `rachel` and `mike`.
  The Host is a real `build_server` on a loopback port with a bearer token;
  turns go through the real dispatcher, runner and provider process (the
  fake). Only the codex backend can spawn; anything else raises. This is
  not an OS sandbox: the executor provides that (CI runs inside
  `scripts/ci_contained.sh`: no network but loopback, no credentials, empty
  HOME; the buildbox's `heavy-remote` must give the equivalent).
- **Waits poll state, never the clock**: a turn settled in `turns`, a provider
  `turn/start` in the fake's request log, `/status` busy, and finally
  quiescence (every scenario session idle, SSE quiet for 0.4 s).
- **A frozen cut.** At quiescence the SSE reader is stopped and joined, then
  the Host's own process group is killed (which takes the fake provider
  with it), and only then are the database (one read transaction) and the
  files read: no writer runs during the dump. A stream that errors, sends a
  non-JSON frame or ends early makes the run fail; it is never read as
  quiet. Graceful shutdown (about 10 s) is not compared; restart scenarios
  belong to the nightly corpus.

## What is compared

| Channel | Captured | Compared as |
|---|---|---|
| `http` | every request a scenario makes: method, route, status, contract headers (`Content-Type`, `Location`, `Content-Range`, `Accept-Ranges`, `Content-Encoding`, `Cache-Control`, `Content-Disposition`, `ETag`, `WWW-Authenticate`, `Retry-After`), JSON body; a non-JSON body as length and SHA-256 | sequence, then field by field |
| `sse` | every complete `/events?live=<sessions>` frame except heartbeats: event name, whether it carried an `id:`, data (multi-line data joined) | sequence aligned by signature (event name, type, kind, status, state, phase, action, live op names), then field by field |
| `schema` | `sqlite_master` (type, name, table, SQL), raw | exact |
| `table` | every non-empty table, all rows, with its primary key columns | multiset per table (exact counts); leftover rows paired by primary key and compared field by field, else reported as added or removed |
| `files` | every file under the state directory except logs and the databases: path, size, and either the parsed JSON (`.json`, `.jsonl`) or a SHA-256 of the bytes | by path, then field by field |
| `cli` | reserved for CLI scenarios (`clarp-admin` against the replay Host) | exact |

**Normalization** (`normalize.py`) follows an explicit contract and rewrites
only what differs by nature between runs:

- a number in the epoch range (seconds or milliseconds) or an ISO timestamp
  string becomes `<time>`; a setting such as `timeout_ms: 30000` is not in
  that range and is compared as is;
- the Host's measured durations, only under the keys in `DURATION_KEYS`;
- generated ids, only where they are known to occur: a UUID or a 12–64
  character hex token under an id key (`id`, `*_id`, `*_ids`, `ID_KEYS`) or in
  a scratch path, and the random tail of a prefixed id (`pn-…`, `live-…`)
  under an id key. Each becomes an ordinal surrogate by first appearance
  (`<id:n>`), so the same id keeps the same surrogate within a run and an id
  that moves shows up. Hex in other fields (a checksum, free text) is content
  and compared;
- event ids become `<eid:n>` by first appearance, keeping which frames name
  the same event;
- pids and ports under `PID_KEYS`, the loopback port in URLs, and the
  scratch, build and harness paths.

JSON stored in a text column or a file is parsed and compared as JSON (the
stored string's key order and whitespace are not compared). Schema SQL is
compared raw. Every rule exists because a replay of unchanged code showed
that noise; a rule is added only with that evidence and never one that
hides a planted change.

## The report

Prints structure, counts and codes, **never message text**. A difference is
`id class scenario / channel / locator / kind: base -> head`.

- A value prints only if it is a number, boolean or null, a surrogate the
  normalizer made (`<id:3>`, `<time>`), or a **code**: a string under a code
  key (`type`, `kind`, `status`, `op`, `outcome`, `state`, `method`, `phase`,
  `role`, `action`, `mode`, `event`, `field`, `backend`, `code`, …) that is
  also an identifier-shaped string literal in the server source shipped with
  the harness (the codebook). Text a user or agent wrote is not in the
  source, so it prints as `str[length]#hash8`, enough to tell values apart
  without showing them.
- A key in a locator prints only if it is in the codebook or one of the
  capture's own envelope keys; any other key (a map keyed by user text)
  prints as `k#hash8`. Request paths print as their route shape with unknown
  segments hashed; file paths print as a hash.
- Run errors print as the build label, scenario, step index, op and what it
  waited for; never Host output.

Kinds: `changed`, `field-added`, `field-removed`, `row-added`, `row-removed`,
`file-added`, `file-removed`, `run-failed`, `sequence-insert|delete|replace`
(a reorder shows as replace, or insert plus delete, in the aligned
sequence; both sides' signatures are printed).

The id is a hash of scenario, channel, locator, kind **and both values**, so
the same difference has the same id on every run, and a registered intended
difference matches only that exact change: another change at the same place
gets a new id and is unexplained.

## Classifying differences

Every difference is exactly one of the three classes of `REFACTOR.md`
section 1:

1. **Refactor bug**: fix it in the slice and rerun.
2. **Intended difference**: send the id and one line of why to the lead for
   `registers/differences.md`; once signed, the slice passes `--intended` with
   a file of signed ids.
3. **Pre-existing bug**: send to the lead for `registers/bugs.md`; it is fixed
   in the fix lane, never inside a move slice. If it makes replay
   nondeterministic, the harness owner adds a normalization rule or scenario
   wait and says so.

The report marks every unregistered difference `UNEXPLAINED`; the harness
does not guess the class.

## The proof (go/no-go 1)

`run.py self-check --ref <sha>` replays the full corpus three times on the
same build and once per planted change, then reports:

- `unchanged run1 vs run2` and `run1 vs run3`: must be **0 differences**;
- every planted change (`planted.py`): must be **CAUGHT**, meaning at least
  one difference matching that change's own expectation (channel, locator
  pattern and kinds), not just any difference;
- `PROOF HOLDS` only with the full scenario set, the full planted set, a
  commit build, both of the above, and no run error.

`--scenarios`, `--planted` or `--allow-path` make the run **diagnostic**: it
prints `DIAGNOSTIC CLEAN|FAILED (not a proof)` and exits 1. Unknown scenario
or planted ids are refused.

Planted changes are exact text replacements (or a swap of two adjacent
blocks) in a fresh copy of the build's `server/` (never in a checkout); an
anchor that does not occur exactly the expected number of times fails the
run, so a planted change can never silently plant nothing.
`test_report.py` checks every anchor against the current tree.

| id | change | must show as |
|---|---|---|
| `response-field` | `POST /send` stops returning `queue_depth` | http `….body.queue_depth` field-removed |
| `db-write` | a finished turn no longer writes its `turn_usage` row | table `turn_usage…` |
| `status-transition` | a user stop settles the turn `failed` instead of `interrupted` | table `turns[…].outcome` changed |
| `sse-order` | `agent-activity` broadcast before `agent-state` for a state change | sse sequence difference |
| `header-only` | `Cache-Control: no-cache` instead of `no-store`, body unchanged | http `….headers.cache-control` changed |
| `sse-envelope` | SSE frames lose their `id:` line, data unchanged | sse `….has_id` changed |

Planned after the first full run (they need the observed data to pick
anchors): a same-size file content change, a changed setting value, a
schema default, and an event-id aliasing change.

## The CI corpus

Synthetic turns only, all text invented here (`scenarios.py`). Target about
30 scenarios, under 2 minutes for both builds. Status:

| Scenario | Covers | State |
|---|---|---|
| `text-turn` | one plain text turn, transcript and status reads | done |
| `queue-while-busy` | a queued send behind a slow turn, then drained | done |
| `stop-mid-turn` | user Stop while a turn runs | done |
| tool use, steer into a running turn, retry by client id, two agents, focus/select, failover on usage limit, voice clip (`/transcribe` with the stub STT), group call, helper spawn, goal turn, read-model reads (`/agents/snapshot`, `/live`, `/turn-queue`, `/attention`) | | next |
| text-dependent paths (voice routing, command parsing, TTS segmentation) | synthetic-text scenarios, since scrubbed text cannot exercise them | next |

Corpus hygiene: the CI corpus is synthetic. If anything is ever recorded from
real traffic it is scrubbed deterministically before it reaches this
directory: every text field is replaced by an HMAC-SHA256 surrogate under a
key kept outside the repo (stable, so the same text maps to the same
surrogate), ids are remapped the same way, and the harness owner never reads
raw text into tool output.

## Nightly fault corpus (design; built after the CI gate)

On the Docker shadow node owned by the release owner (`make docker-test`
image), never on this machine's live Host. The same runner, with a broad
scenario pack and fault hooks in the fakes:

| Fault | How it is injected |
|---|---|
| backend crash | the fake provider exits non-zero mid-stream (control file in the provider root) |
| usage limit | the fake's `quota-mode` (`blocked`, `stale`, `partial`) already exists |
| stop or cancel mid-turn | `/stop` and turn-queue cancel at each turn phase |
| restart mid-turn | SIGKILL the Host at a named phase, start a new Host on the same state dir (`restart_recovery=True`), then compare recovery |
| preemption | a second send without `queue_if_busy` while a turn runs, and an agent switch |
| voice-claim failure | the Oracle/voice claim path made to fail early (RTO-01), the claim must be released |

Each fault scenario compares base and head like the CI corpus; the nightly
also runs the proof. Results go to the lead as the report above.

## Limits (today)

- The Host runs in a subprocess with the harness's interpreter and
  dependencies; a slice that changes Python dependencies needs a build-local
  interpreter (`--python`, not yet).
- `host.py` calls a few server internals to start (`agents.create_agent`,
  `ServerContext`, `build_server`, `backends.spawn_turn`). A slice that moves
  them must publish its move map; the harness owner updates `host.py` with an
  import fallback so both builds still start.
- Graceful shutdown and restart are not in the CI corpus (see the nightly).
- Process cleanup kills the Host's process group. A backend launcher that
  starts its own session (`backend/stream_json.py`) would escape it; the
  corpus only runs the codex app-server, which stays in the group. As the
  corpus grows, the executor's namespace or cgroup teardown is the backstop,
  and the run should check that nothing it started survives.
- Isolation inside `host.py` is a convention, not a boundary; the executor
  (CI containment, `heavy-remote`) is the boundary. The CI workflow's
  containment is source-reviewed, not yet exercised.
