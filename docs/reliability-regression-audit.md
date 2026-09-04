# Clarp reliability and regression audit

Status: draft umbrella; historical findings reclassified
Audience: Clarp maintainers
Started: 2026-09-04
Original baseline: `a95d9b0` (`v0.6.4`)
Current rebase: `eeecc72` (#34)

**Read the [runtime-boundary reassessment](reliability-audit-runtime.md) first.**
It runs the full 53-case failure inventory, distinguishes defects from invalid
contracts, links priority fixes, and defines focused draft TDD slices. The
findings below are historical hypotheses and do not all remain accepted.

## Direct answer so far

The six repository issues opened by Bjorn Wolstad (#8 and #10-#14) are useful
probes of a smaller set of systemic weaknesses rather than six unrelated
mistakes:

1. **Split truth**: the same fact is represented independently in multiple
   places and the reader and writer do not share one resolution path (#12).
2. **Conflated states**: an HTTP response is treated as network failure (#10),
   and UI intent flags double as recorder lifecycle state (#13).
3. **Ephemeral work without durable reconciliation**: a process can die while
   work is in flight, leaving no terminal user-visible outcome (#11).
4. **Implicit capabilities**: text markup is sent based on assumptions about a
   provider instead of an explicit negotiated capability (#14).
5. **Packaging is not tested as the user consumes it**: source-tree success did
   not imply that an archive-based install contained every staged input (#8).
6. **Observability begins too late**: several failure exits occur before the
   trace or diagnostic record that would explain them (#10, #11, #13).

All six issues are closed on the current baseline. This audit is not assuming
their fixes are complete: it will test adjacent states, failure boundaries,
restart behavior, retries, stale clients, archive/install modes, and protocol
compatibility.

## Source corpus

Primary evidence is the issue report, current code, fixing commit, and current
test suite for each report:

| Issue | Original failure | Fix lineage | Adjacent risk under test |
|---|---|---|---|
| [#8](https://github.com/Maxteabag/clarp/issues/8) | release staging required an absent toolchain lockfile | `39ec6f0` and installer coverage | archive completeness; optional-mode isolation; update rollback |
| [#10](https://github.com/Maxteabag/clarp/issues/10) | repeated 401 responses rendered as no server response | `209a461` | all HTTP/auth outcomes; stale roster; recovery after token replacement |
| [#11](https://github.com/Maxteabag/clarp/issues/11) | restart-killed turn stayed silently open | `d466528` | subagents and other durable work; idempotent reconciliation; crash windows |
| [#12](https://github.com/Maxteabag/clarp/issues/12) | update ignored the persisted `SOURCE_REMOTE` | `209a461` | metadata precedence; corrupt/blank state; archive version identity |
| [#13](https://github.com/Maxteabag/clarp/issues/13) | explicit mic stop cleared the guard before recorder stop | `5badec1` | every recorder terminal event; duplicate clients; recorder exceptions |
| [#14](https://github.com/Maxteabag/clarp/issues/14) | non-SSML adapters spoke markup aloud | `a4b5e1b` | safe capability defaults; malformed markup; fallback/provider switches |

## Working reliability axioms

These are hypotheses to turn into executable contracts or revise with evidence:

- Every accepted unit of user work eventually has exactly one durable terminal
  state: succeeded, failed, cancelled, interrupted, or explicitly still live.
- Startup reconciliation is idempotent and covers every externally visible
  in-flight entity, not only top-level turns.
- A liveness signal records attempted time, completed time, outcome, and owner;
  failed heartbeat work must not masquerade as dormancy or success.
- Network reachability, authentication, authorization, server readiness, and
  application success are different states with different recovery actions.
- A retry is safe only when its idempotency key and acknowledgement boundary are
  explicit; accepted does not mean durably dispatched or rendered.
- One canonical resolver owns each persisted configuration fact. Secondary
  copies are caches with declared precedence and validation.
- Provider-specific syntax crosses a provider boundary only after capability
  negotiation; an absent capability is treated conservatively.
- Every lifecycle transition has one owner. UI presentation booleans do not
  control resource cleanup or durable delivery.
- Install and update tests exercise the same archive, permissions, service
  manager, paths, and restart behavior used by a first-time operator.
- No error path may occur before correlation metadata is created; silent exits
  are observable outcomes too.

## Investigation matrix

| Area | Evidence sought | Candidate regression tests | State |
|---|---|---|---|
| Restart/subagent survival | process registry, subagent hooks, startup reconciliation | stale child PID; parent death; restart during subagent; duplicate stop hook | queued |
| Heartbeats | scheduler state, dispatch failures, database contention | failed dispatch retry; interrupted cooldown; restart; overlapping tick | queued |
| Database writes | transaction boundaries and lock diagnostics | cancelled writer; exception rollback; lock owner context; busy timeout | queued |
| Send/delivery | idempotency and accepted/durable boundaries | restart after accept; retry same id; stale optimistic row; queue failure | queued |
| SSE/snapshots | cursor, replay, reset and auth behavior | event gap; old server epoch; 401 recovery; duplicate event | queued |
| Installation/update | archive and rollback invariants | clean archive install; missing optional inputs; corrupt metadata; interrupted flip | queued |
| Recording/transcription | recorder/VAD lifecycle and diagnostics | stop/error/inactive/zero-byte; duplicate client; offline upload retry | queued |
| TTS/audio | capability, fallback, acknowledgement and replay | unknown capability; fallback mismatch; ack-before-play; partial stream | queued |
| First-run UX | discoverability and diagnostics | no token; no runtime; permissions; doctor remediation contract | queued |
| iOS/server contract | current private client assumptions vs server schema | compatibility fixtures; additive fields; restart and replay | queued |

## Open questions

- What is the durable identity and terminal-state model for a spawned subagent?
- Which component owns proof that an accepted message reached a backend process?
- Can a server restart be distinguished from a stale SSE connection without a
  server-instance epoch in every snapshot/event?
- Does heartbeat mean the scheduler tried, the backend accepted, the agent
  completed, or the user-visible state was refreshed?
- Which install metadata is authoritative after a source checkout disappears?
- Which failures should self-heal, which should retry, and which must stop and
  ask the operator?
- What is the minimum server/client compatibility window, especially for an iOS
  release that cannot be updated atomically with the server?

## Historical red regressions (reclassified above)

These are executable failures on the baseline, not brainstorming items. The
tests are intentionally red in this draft audit PR until maintainers decide
which fixes belong together.

### Restart and process lifetime

- **Production startup defeats issue #11's fix.** `server/server.py` calls
  `reconcile.reconcile_all()` before `build_server(..., restart_recovery=True)`.
  The first call rewrites stale busy state to `idle`; the later
  `recover_after_restart()` therefore finds no orphaned turn and writes no
  marker. Existing tests call recovery without reproducing production order.
- **Stopping a turn does not stop its process tree.** `ProcessRegistry` sends
  `terminate()` only to the registered top-level CLI. A deterministic Linux
  test spawns one child, stops the registered parent, and proves the child is
  still alive. Claude/Codex subagents are descendants and are not separately
  registered. Custom adapter processes already use the safer
  `start_new_session=True` plus `killpg` pattern.
- **The durable turn ledger never reaches a terminal state.** Dispatch calls
  `agents.open_turn()`, but no production caller invokes `agents.close_turn()`.
  Clean completion and restart recovery both leave `ended_at IS NULL` rows.
  Trace gates, source/audio policy, snapshots, and reconciliation all query
  those supposedly active rows.
- **Claude can open the same logical turn twice.** Server dispatch opens a row,
  then the UserPromptSubmit hook opens another with the same trace. There is no
  uniqueness constraint or idempotent open operation.

### Heartbeats and scheduled autonomy

- **A failed periodic heartbeat consumes its cadence.** `last_started` is
  persisted before dispatch. An exception is logged, but an immediate retry is
  skipped for minimum spacing and then the configured interval.
- **A failed restart heartbeat is never retried.** The once-only latch is set
  before per-agent dispatch. One transient lock or backend error permanently
  skips continuity recovery for that agent in the current server process.
- **A scheduled run advances before dispatch.** If dispatch raises, `last_run`
  and `next_run` already moved forward; a daily job is silently lost for a day.
- **Two scheduler workers can double-dispatch one occurrence.** Selection and
  advance are separate autocommit statements with no atomic claim. A barrier
  test makes two workers read the same due row and both dispatch it.
- **Deleting an agent leaves its schedule enabled and due.** Soft deletion
  cancels task plans and artifacts, but not `agent_schedules`.
- **Leap-day cron becomes permanently unscheduled.** The next-run search stops
  after 366 days, so `0 0 29 2 *` starting in March 2025 returns `None` instead
  of 2028-02-29.

### Installation and update

- **Quick-start source cleanup never runs.** `get.sh` installs an EXIT trap and
  then `exec`s `setup.sh`; successful `exec` discards the parent shell and its
  trap. The downloaded source tree remains, and setup persists that temporary
  path as `source_repo`.
- **The advertised interactive pipe cannot reach the wizard.** Even in a real
  terminal, `curl ... | bash` gives the installer process piped stdin. `get.sh`
  passes the exhausted pipe to `setup.sh`, whose `-t 0` check rejects it as a
  non-terminal invocation. A pseudo-terminal regression exits with the
  fixture's non-TTY failure code.
- **Clean archive releases identify as `unknown-dirty`.** `install.sh` derives
  both version and dirty state exclusively from Git metadata. It ignores a
  supplied release identity even though archive installs intentionally have no
  `.git` directory.
- **A pre-activation environment failure leaves an orphan release directory.**
  The staging directory is moved into `releases/` and its cleanup trap removed
  before `uv lock`/`uv sync`; the activation rollback trap is installed later.
  A failure in between leaves an incomplete unmarked directory. It is correctly
  excluded from `clarp-admin rollback` because it lacks `INSTALL_OK`, but it is
  never removed and repeated failures accumulate disk state.

### Client state and voice boundaries

- **HTTP errors become fake network silence after any prior success.** Error
  responses advance `lastResponseAt`, but `assess()` uses only last successful
  fetch/SSE time once a success exists. Continuous 503 responses eventually
  render as `no server contact`, the same state-conflation family as #10.
- **A fast second mic tap starts a second permission request.** While the first
  `getUserMedia()` is pending, the intent state still says stopped; the second
  tap neither cancels nor coalesces and can leak a stream/start recording after
  the user tried to stop.
- **Mid-capture MediaRecorder errors have no handler.** There is no
  `recorder.onerror`, so the state can remain capturing without a diagnostic or
  resource reset.
- **Transcription HTTP failures are not recorded in client diagnostics.** They
  flash briefly but emit no `clog`, leaving no durable evidence after the UI
  message disappears.
- **`"ssml": "false"` enables SSML.** Manifest loading coerces arbitrary JSON
  values with `bool(...)`; a truthy string opts a plain engine into receiving
  raw markup.
- **No-SSML mode strips only four tags.** `strip_ssml_for_plain_tts()` promises
  to remove every SSML tag but leaves standard `prosody`, `emphasis`, `say-as`,
  `p`, and `s` tags to be spoken aloud. This is the same boundary failure as
  #14 with a wider vocabulary.

### Delivery, replay, and database availability

- **A retry after synchronous spawn failure is silently discarded.** The user
  row is committed before the CLI is spawned. If spawn then fails, iOS/PWA
  retries the same `client_msg_id`; the existing user row is treated as proof
  of completed admission, so the server returns a deduplicated success without
  starting a backend. The message remains permanently unanswered.
- **SSE's replay/live handoff duplicates overlap events.** The handler
  subscribes first, then queries durable replay. An event arriving in that
  window is returned by both SQLite and the subscriber queue. This is mostly
  cosmetic for idempotent state events, but a native calendar/location event is
  a one-shot action and can execute twice.
- **Paired-device authentication turns every request into a write.** Token
  validation reads the device row and immediately updates `last_seen_at`.
  Holding an unrelated `BEGIN IMMEDIATE` proves the valid device can no longer
  authenticate even a WAL-readable GET: the audit receives `database is
  locked`. This explains why one writer can make the iOS-facing server appear
  wholly unavailable rather than merely read-only degraded.
- **Deleting an agent leaves its durable background jobs running.** Agent
  deletion cancels task plans and artifacts, but neither the background-job
  rows nor their exact worker identities are terminalized.
- **Older queued TTS overwrites newer turn ownership.** Before synthesis, the
  worker copies the queued row's trace into the agent-global current trace. If
  a newer turn is live, its terminal callback then sees the old trace and
  discards its own result as superseded.
- **TTS restart recovery can duplicate speech and provider cost.** A crash after
  clip creation/broadcast but before `tts_queue.mark_done()` leaves a
  `synthesizing` row. Startup blindly requeues it although the completed clip
  already exists.
- **Server-herald-held replies are process-local only.** The real clip is kept
  in `HeraldManager._buffers` without a durable `held` status or audio SSE row.
  Restart loses the only reference, so neither replay nor `/clips/recoverable`
  can return it.
- **The audio janitor deletes durably held MP3s.** It uses only file mtime and
  ignores clip status, while the recovery query intentionally treats held
  clips as longer-lived. The database can advertise a clip whose file is gone.
- **Playback status can move backward after success.** A late second-client
  `queued` acknowledgement overwrites `play-ok`, making an already consumed
  clip recoverable again.
- **TTS error classification is provider-blind.** Any `401` is presented as
  “ElevenLabs quota exceeded,” including a Cartesia invalid-key response.

### Connection, resource, and test-system failures

- **PWA reconnects discard their durable SSE cursor.** The code reads neither
  `MessageEvent.lastEventId` nor a persisted cursor before constructing a new
  `EventSource`; the comment claiming Last-Event-ID replay therefore does not
  hold after the app explicitly replaces the object.
- **Reconnect timers are not coalesced.** Two error/reconnect signals schedule
  two future `connectSSE()` calls; the second does not close the first newly
  opened stream. This is a direct mechanism for duplicate live clients and
  repeated remote actions.
- **Known authentication rejection does not stop SSE retries.** The banner is
  corrected by #10, but the client continues opening connections and adding
  401 traffic instead of waiting for a replacement credential.
- **Request concurrency remains unbounded.** The listen backlog is 128, but
  `ThreadingHTTPServer` still creates one thread per accepted slow/SSE client.
  A 48-client regression creates 48 simultaneous subscribers, preserving the
  resource-exhaustion mechanism seen in the prior 380-thread/1,024-FD incident.
- **Repeated active model-install requests create repeated monitor threads.**
  Twenty idempotent calls against one running generation start twenty monitor
  loops.
- **A stopped TTS worker cannot be restarted.** Unlike the other worker
  classes, `TTSWorker.start()` does not clear its stop event.
- **Heartbeat failure is absent from subsystem health.** Errors are printed,
  but `/diagnostics/health` has no heartbeat entry or failed-at timestamp.
- **Fast regression suites are not required on pull requests.** Current CI
  builds/runs Docker and a path-filtered installer subset, but does not run the
  full Python and Vitest suites. Most tests in this audit would not gate a PR.
- **The in-app updater repeats #12's split resolver.** `clarp-admin update` now
  falls back to the canonical repository, but `server_update._update_remote()`
  still returns empty when metadata is absent.

### Security and error-contract failures

- **A limited paired token can read arbitrary host files and escalate.** Every
  GET is currently allowed for limited devices. `/agent-file` accepts an
  arbitrary absolute root and intentionally reads anything accessible to the
  server user, including the Clarp configuration containing the administrator
  token. The integration reproduction receives the chosen secret with HTTP
  200; this endpoint must require full scope (and the scope model needs a full
  GET allowlist audit).
- **Provider error strings are interpolated into JSON.** Quotes and newlines in
  an STT error produce an invalid `application/json` response. Several other
  handlers use the same hand-built pattern instead of `json.dumps`.

Current red count: **46 tests** across Python and JavaScript. One additional
green archive-install guard pins the original #8 non-managed lockfile fix.

## Native-client reference findings

The private `clarp-ios` `origin/main` tree was inspected read-only; the dirty
local checkout was not updated or modified. This is protocol evidence, not a
request to copy native build/signing material into this repository.

- The native outbox correctly retries ambiguous `/send` failures with the same
  stable message id. That makes the server's persisted-before-spawn hole above
  user-visible and deterministic.
- The native SSE client persists `Last-Event-ID`, but it does not suppress a
  duplicate id delivered twice within one connection. Calendar handling calls
  the native writer for each event, so the replay/live overlap needs an
  exactly-once fence on at least one side.
- `/teams` now caches a successful result for ten seconds, but calls made while
  the first slow request is still in flight are not coalesced. This can still
  amplify a slow/locked server before any response populates the cache—the same
  traffic family observed during the EliteBook overload incident.
- The native client validates the stable server instance identity before
  accepting snapshots. That is a sound boundary and should be extended to SSE
  cursor epochs so a restored/reinstalled server cannot inherit an unrelated
  event cursor solely because it uses the same URL.
- Clip acknowledgement is server-global rather than per device/consumer. One
  PWA or phone marking a clip `play-ok` prevents another client from recovering
  it; late acknowledgements from the second client can also regress the shared
  status. A delivery ledger needs `(clip_id, consumer_id)` identity.

## Research method and stop condition

The audit uses repository issues and pull requests as incident evidence, then
traces each failure class through current code, tests, install artifacts, and
the private `clarp-ios` client where its protocol behavior is relevant. A claim
is considered strong only when backed by an executable reproduction or a
specific code path. The investigation stops when every matrix row has been
traced, high-impact hypotheses have either a failing test or documented
disproof, the focused and broad test suites have been run, and another targeted
search produces only duplicate or lower-value variants.

## Investigation log

- 2026-09-04: fetched `origin/main`; confirmed six issues exist and all six were
  opened by `bjowol` (Bjoern Wolstad). Captured full issue bodies and comments.
- 2026-09-04: created isolated worktree
  `/home/peter/GIT/Clarp-bjowol-audit` from `origin/main` at `a95d9b0`.
- 2026-09-04: found a separate dirty, ownership-uncertain
  `/home/peter/GIT/Clarp-hardening` worktree and left it untouched.
- 2026-09-04: ran the pre-existing focused Python suites for restart recovery,
  reconciliation, heartbeat, scheduling, process registry, and custom TTS;
  114 tests passed before adding the new regressions.
- 2026-09-04: added and reproduced 17 red regressions covering production boot
  order, process descendants, heartbeat retry, schedule loss/duplication and
  calendar range, installer archive/failure boundaries, client health, mic
  lifecycle diagnostics, and no-SSML capability enforcement.
- 2026-09-04: repaired the existing mic test mock to provide the newly required
  `addConditionSource` dependency; the three pre-existing #13 regression tests
  are green, while the three new mic lifecycle cases fail for their intended
  assertions.
- 2026-09-04: inspected the already-fetched private `clarp-ios` `origin/main`
  without touching its dirty checkout. Used its outbox, SSE, one-shot native
  action, server-identity, and `/teams` behavior to test the server at the
  protocol boundaries it actually serves today.
- 2026-09-04: expanded the reproduced red set from 17 to 25 with the
  persisted-before-spawn retry hole, never-closed/duplicate turn rows, SSE
  replay overlap, paired-auth write coupling, interactive-pipe bootstrap,
  and orphaned background work after agent deletion.
- 2026-09-04: expanded from 25 to 46 red regressions across audio ownership and
  recovery, dead subagent presentation, heartbeat admission/health, stale
  schedule dispatch, model-monitor and request-thread amplification, PWA SSE
  cursor/timer/auth behavior, in-app update fallback, limited-token file read,
  and invalid JSON error responses. Added a green archive test for #8.

- Runtime rebase: ported the five audit commits onto `eeecc72`; corrected runtime,
  process, SSE, timer and doctor fixtures. Original run: 53 red plus one guard.
  Adapted inventory: 51 red and 3 green, with a separate per-case relevance review.
  Priority fixes are PRs #35 and #36; all remaining work is tracked in draft
  TDD slices linked from the reassessment. No audit red tests were merged.
