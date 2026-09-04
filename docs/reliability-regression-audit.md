# Clarp reliability and regression audit

Status: active investigation  
Audience: Clarp maintainers  
Started: 2026-09-04  
Baseline: `origin/main` at `a95d9b0` (`v0.6.4`)

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
