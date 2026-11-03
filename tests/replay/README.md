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
# On the laptop (git only, starts nothing): extract the builds the VM needs.
python3 tests/replay/run.py prepare <base-sha> <head-sha> --prune
# On the buildbox: a slice, base commit against the slice head.
/var/tmp/clarp-refactor-program/bin/heavy-remote uv run --frozen --group dev \
  python tests/replay/run.py --jobs 8 compare --base <base-sha> --head <head-sha>
# The harness proof on one commit (prepare it first).
/var/tmp/clarp-refactor-program/bin/heavy-remote uv run --frozen --group dev \
  python tests/replay/run.py --jobs 8 self-check --ref <sha>
# Remove cached worktree builds (laptop or CI).
python3 tests/replay/run.py clean
```

heavy-remote copies the worktree without `.git`, so builds come from
`.replay-builds/<sha>/`, which `prepare` makes with `git archive` (server/
and static/, files read-only, a stamp with a SHA-256 over every file).
`.replay-builds/` is untracked on purpose so heavy-remote ships it; never
commit it. In CI, where `.git` exists, builds are `git worktree add --detach`
checkouts as below.

`compare` exits 0 with no unexplained difference, 1 with differences, 2 when a
scenario could not run. `--intended FILE` takes registered difference ids
(one per line, see "Classifying differences"). `--scenarios a,b` limits the
corpus. Scratch space is `/var/tmp/rfh` (`--scratch` or `CLARP_REPLAY_SCRATCH`; short
on purpose, because each Host makes AF_UNIX sockets under its state dir).
Every run claims its own directory `<scratch>/r/<stamp>-<pid>-<n>` with an
exclusive mkdir and holds an exclusive flock on it until it ends; all its
Host state dirs (`<label-index>/s<scenario-index>`, mapped in `labels.json`),
planted copies and raw captures live there, and a scenario root that already
exists is refused, never deleted. Concurrent runs therefore never share or
remove each other's state. Only the SHA build cache `<scratch>/builds` is
shared (created under a lock). A clean run removes its directory; a failed
run, or one with `--keep`, keeps it and prints its path. `clean` removes run
directories no job holds and refuses anything it did not make. Nothing is
written to `~/.local/share/clarp` or `~/.config/clarp`.

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

- **Two builds, one harness.** A build is a prepared tree whose stamp still
  matches (checked before and after the replay), or, where git exists, a
  commit added with `git worktree add --detach` under
  `scratch/builds/<full sha>`, made read-only, and reused only while it is
  still at that SHA and clean (otherwise the run stops with a build error).
  Every build's `static/` mtimes are pinned to one constant: the Host's
  `server-version` is the newest static mtime, which otherwise records when a
  checkout or sync happened. `--allow-path` accepts an
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
  The fake provider runs under the harness's own interpreter (a generated
  wrapper in the state dir). The Host is a real `build_server` on a loopback port with a bearer token;
  turns go through the real dispatcher, runner and provider process (the
  fake). Only the codex backend can spawn; anything else raises. This is
  not an OS sandbox: the executor provides that (CI runs inside
  `scripts/ci_contained.sh`: no network but loopback, no credentials, empty
  HOME; the buildbox's `heavy-remote` must give the equivalent).
- **Waits poll state, never the clock**: a turn settled in `turns`, a provider
  `turn/start` in the fake's request log, state rows in `state_log`, `/status`
  busy, and quiescence (every listed session idle, SSE quiet for 1 s), which
  every scenario reaches before its final reads so they never race the Host's
  background import.
- **compare runs the base twice.** If the base differs from itself, the run
  fails as `base is not reproducible` (exit 2): a comparison with head would
  prove nothing.
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
| `sse` | every complete `/events?live=<sessions>` frame except heartbeats: event name, whether it carried an `id:`, data (multi-line data joined) | per producer lane, an exact sequence aligned by signature (event name, type, kind, status, state, phase, action, live op names), then field by field; see "SSE order" |
| `sse_log` | the `sse_events` table (the replay buffer behind `Last-Event-ID`), by event id | like `sse` |
| `schema` | `sqlite_master` (type, name, table, SQL), raw | exact |
| `table` | every non-empty table except `sse_events`, all rows, with its primary key columns | multiset per table (exact counts); leftover rows paired by primary key and compared field by field, else reported as added or removed |
| `files` | every file under the state directory except logs and the databases: path, and the parsed JSON (`.json`, `.jsonl`), the text (other UTF-8 files up to 64 KiB) or size and SHA-256 (anything else) | by path, then field by field |
| `cli` | reserved for CLI scenarios (`clarp-admin` against the replay Host) | exact |

**SSE order.** The Host broadcasts from several threads (the state
watcher, the dispatcher, the live hub, the transcript streamer, the
notifier), so their interleaving differs between runs of unchanged code. A
frame's lane is its producer: event name, type, payload key set and session,
except that the state watcher's `agent-state` (it always carries
`status_text` and never `client_msg_id`) and `agent-activity` share one lane,
because `state_watcher._poll_once` emits them in sequence from one thread.
Each lane is compared as an exact sequence, so a reorder within a producer, a
missing or extra event and any field change are differences. Order between
independent producers is not a contract and is not compared;
`--cross-lane-order` reports, as a diagnostic, cross-lane orders that both
base runs agreed on and head broke.

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
- inside the SSE channels, an event id is named after the frame that
  carries it, its lane and position (`<eid:agent-state#2@1a2b3c>`), so every
  reference to the same event gets the same name whatever the interleaving;
  elsewhere a field called `event_id` (for example `voice_events`) is an
  ordinary value;
- id surrogates are assigned in a deterministic order: the HTTP sequence,
  then tables by name, then files, then the streams;
- pids and ports under `PID_KEYS`, the loopback port in URLs, and the
  scratch, build and harness paths;
- in small text files, UUIDs (generated tokens such as the instance
  generation); `settings.key` and `settings.value` hold generated ids
  (launch receipts) and get id surrogates.

Rules for behaviour that is nondeterministic in the Host itself. Each was
reported to the lead as a pre-existing nondeterminism; each is as narrow as
the evidence:

- **Invalidation signals** (`transcript-updated`): unchanged replays send one
  to three per turn, sometimes naming the backend session; they are compared
  by presence, one per (type, session, agent).
- **Two writers of one transition**: a turn's `thinking` is written by the
  dispatcher's spawn and again when the provider turn starts, in racing
  order. Consecutive state rows (and watcher `agent-state` frames) of one
  agent with the same kind and trace are compared with their details sorted.
- **Tied sort**: `/agents/snapshot` orders personas by `created_at`, and every
  built-in is seeded in the same millisecond; `personas` and `roster` lists
  are compared sorted.

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

`run.py self-check --ref <sha>` replays the full corpus four times on the
same build and once per planted change, then reports:

- `unchanged run1 vs run2`, `run1 vs run3` and `run1 vs run4`: each must be
  **0 differences**;
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
| `sse-order` | `agent-activity` broadcast before `agent-state` for a state change | sse watcher-lane sequence difference |
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
| `stop-mid-turn` | user Stop while a turn runs (after both thinking rows) | done |
| `retry-same-client-id` | a resend after a lost acknowledgement: one message, one turn | done |
| `two-agents` | two agents take a turn each; nothing crosses | done |
| `usage-limit` | the provider reports a usage limit and fails the turn | done |
| `steer-into-running` | a send without `queue_if_busy` steered into a running turn | done |
| `focus-and-reads` | `/select`, `/focus`, `/agents/snapshot`, `/live`, `/turn-queue`, `/attention`, `/status` | done |
| `voice-clip-turn` | `/transcribe` (stub STT) then `/send` with its trace and transcription ids, `/voice-events` | done |
| tool use, group call, helper spawn, goal turn, failover to another account, interrupt by a new prompt, queue cancel and resume, delete and restore, CLI (`clarp-admin`) against the replay Host | | next |
| text-dependent paths (voice routing, command parsing, TTS segmentation) | synthetic-text scenarios, since scrubbed text cannot exercise them | next |

Timing on the buildbox (`--jobs 8`): `compare` with 9 scenarios takes about
50 s (27 Host runs); `self-check` takes about 150 s (90 Host runs).

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
| stop at admission | `/stop` between a turn's two `thinking` rows: today the late row overwrites `interrupted` and the agent stays busy (reported pre-existing race); the scenario pins the fix |
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
- Order between independent SSE producers is not compared (see "SSE
  order"); a refactor that changes it is not caught unless it also changes a
  lane, a row or a response.
- Process cleanup kills the Host's process group. A backend launcher that
  starts its own session (`backend/stream_json.py`) would escape it; the
  corpus only runs the codex app-server, which stays in the group. As the
  corpus grows, the executor's namespace or cgroup teardown is the backstop,
  and the run should check that nothing it started survives.
- Isolation inside `host.py` is a convention, not a boundary; the executor
  (CI containment, `heavy-remote`) is the boundary. The CI workflow's
  containment is source-reviewed, not yet exercised.
