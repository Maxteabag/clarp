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
`/var/tmp/clarp-refactor-program/bin/heavy-remote` once that exists, or in
GitHub Actions. Locally only `tests/replay/test_report.py` may run (one file,
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
  host.py (subprocess, own process group, own state dir)  <──┘  drives HTTP, reads SSE,
    ├─ server/ and static/ from the BUILD under test               waits on observable state,
    └─ fakes from the HARNESS tree:                                dumps the database
       tests/qa/fake_codex.py (deterministic provider process),
       FakeTTSEngine, StubSTT, no provider CLI resolvable
normalize.py ── rewrites clock readings, generated ids, pids, ports, scratch paths
diff.py ────── aligns and compares, prints the report
planted.py ─── deliberate behaviour changes for the proof
```

- **Two builds, one harness.** A build is a checkout: a commit is added with
  `git worktree add --detach` under the scratch directory and cached by SHA;
  an existing checkout path is used as is. The launcher, fakes, corpus and
  normalizer always come from the harness tree, so base and head differ only
  in their server code.
- **Per scenario a fresh Host.** Each scenario starts its own Host process on
  a fresh state directory (`HOME`, `XDG_*`, `CLAUDE_PWA_DB`, `CLARP_*` dirs all
  inside it), with two codex-backed agents, `rachel` and `mike`. The Host is
  a real `build_server` on a loopback port with a bearer token; turns go
  through the real dispatcher, runner and provider process (the fake). Only
  the codex backend can spawn; anything else raises.
- **Waits poll state, never the clock**: a turn settled in `turns`, a provider
  `turn/start` in the fake's request log, `/status` busy, and finally
  quiescence (every scenario session idle, SSE quiet for 0.4 s).
- **Shutdown.** The capture reads everything it needs at quiescence, then
  kills the Host's own process group (a graceful shutdown costs about 10 s).
  Shutdown behaviour is therefore not compared; restart scenarios belong to
  the nightly corpus.

## What is compared

| Channel | Captured | Compared as |
|---|---|---|
| `http` | every request a scenario makes: method, path, status, JSON body | sequence, then field by field |
| `sse` | every `/events?live=<sessions>` event except heartbeats | sequence aligned by signature (type, kind, status, state, phase, action, live op names), then field by field |
| `schema` | `sqlite_master` (type, name, table, SQL) | exact, after normalization |
| `table` | every non-empty table, all rows | multiset per table; leftover rows paired by first column and compared field by field |
| `files` | every file under the state directory except logs and the databases: path and size | exact |
| `cli` | reserved for CLI scenarios (`clarp-admin` against the replay Host) | exact |

JSON stored in text columns is parsed and normalized like the wire.

**Normalization** (`normalize.py`) rewrites only what differs by nature
between runs: values under clock-like keys (`ts`, `*_at`, `*_ms`, `since`,
durations) become `<time>`; epoch numbers and ISO timestamps anywhere;
UUIDs, hex tokens and the random tail of prefixed ids (`pn-…`, `live-…`)
become ordinal surrogates `<id:n>` by first appearance, so the same id keeps
the same surrogate within a run; pids, ports and the scratch, build and
harness paths. Event ids become `<event_id>` (their order is the sequence).
Everything else, including counts, codes, ordering and presence of fields, is
compared as is. Each rule exists because the unchanged-code check showed that
noise; a rule must never hide a planted change.

## The report

Prints structure, counts and codes, **never message text**. A difference is
`id class scenario / channel / locator / kind: base -> head`. A value is
printed only when its key is a code key (`type`, `kind`, `status`, `op`,
`outcome`, `state`, `method`, `path`, `reason`, …) and it looks like a code
(`[\w./:<>?=&-]{1,48}`), or it is a number, boolean or null. Anything else is
printed as `type[length]#hash8`, enough to tell values apart without showing
them. Kinds: `changed`, `field-added`, `field-removed`, `row-added`,
`row-removed`, `sequence-insert|delete|replace` (reorders show as
replace/insert+delete in the aligned sequence).

The id is a hash of scenario, channel, locator and kind, so the same
difference has the same id on every run.

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

`run.py self-check --ref <sha>` replays the corpus three times on the same
build and once per planted change, then reports:

- `unchanged run1 vs run2` and `run1 vs run3`: must be **0 differences**;
- every planted change (`planted.py`): must be **CAUGHT**, meaning at least
  one difference in the channel the change should show in;
- `PROOF HOLDS` only when both hold and no scenario failed to run.

Planted changes are exact text replacements in a copy of the build's
`server/` (never in a checkout); an anchor that no longer matches fails the
run loudly so a planted change can never silently plant nothing.

| id | change | expected channel |
|---|---|---|
| `response-field` | `POST /send` stops returning `queue_depth` | http |
| `db-write` | a finished turn no longer writes its `turn_usage` row | table |
| `status-transition` | a user stop settles the turn `failed` instead of `interrupted` | table |
| `sse-order` | two events of a turn are broadcast in swapped order | sse |

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
