# Verification and performance implementation

Paired work: `clarp-ios` branch `refactor/verification-performance`.
Host baseline: `eeecc72`. iOS baseline: `b5aca38`.

## Outcomes

- [x] Reliable local entrypoints and full PR test gates; release depends on tests.
- [x] Bounded host snapshot queries, transcript usage caching, clip ownership cleanup.
- [x] One portable iOS core target, behavioral coverage replacing source-text checks and duplicate tests.
- [x] Off-main transcript persistence, indexed roster ordering, explicit task ownership.
- [x] Shared versioned contract fixtures exercised by both clients.
- [x] Disposable end-to-end turn scenarios, immutable build identities, QA evidence bundles.
- [x] Safe worktree closeout tooling and reproducible setup/debug documentation.
- [ ] Full tests, measured performance comparison, simulator QA, structured review and handoff.

## Evidence requirements

Keep canonical identity, revision ordering, durable retries, queue recovery and
reconciliation semantics. Compare snapshot values against the existing individual
queries. Benchmark query counts and elapsed time using disposable databases.
Use dedicated simulator instances and worktree-local build output. Device-only
checks remain explicitly pending until the paired phone is reachable.

The source checkouts have unrelated ongoing work; all changes belong in these
paired worktrees. Do not deploy a service from either worktree.

## Measured evidence

- Idle snapshots: 100 agents now issue 11 statements, versus 1,105 before.
  Warm synthetic median about 4.4 ms (prior single warm sample about 22 ms).
- Populated comparison against the original snapshot: 10 agents, 200 messages
  per agent, 16 KiB per message, 20 repetitions: median 38.11 ms before,
  33.99 ms after. Candidate sorting carries identifiers, not full message text.
- Native UI round trip through real HTTP/SSE/dispatch/persistence passed,
  including relaunch; screenshots were inspected.
- Device-only checks remain pending: the paired iPhone reports unavailable.
