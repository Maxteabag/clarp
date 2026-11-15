# The Clarp Host in Rust: the brief

Read this whole file before you touch `server-rs/`. It is the single source of truth for every agent in the
program. The lead is **Maximus** (Clarp session `maximus-6c37`); the owner is **Peter**. Ask the lead, not Peter.
Only the lead edits this file.

## 1. What Peter asked for

On 14 Nov 2026, after the architecture video and report (`docs/design.md` here is that report's proposal):

> "I agree with your video 100% and report. I agree with all your steps. Can you get started so I can expect a
> new version of a Rust server that's battle tested and so I can start migrating agents over one by one."

> "Also if there's any other refactoring you want to do (ugly code, code smells) you can rewrite the code exactly
> as you want since it's your rebuild. Maybe make it really easy to swap out implementations (e.g. which DB,
> which compressor) etc."

On 15 Nov, on how to build it (dictated):

> "I think I would like to have rapid development. And I think a bottleneck would be, if you were to create a lot of
> different sub-agents and they all would compile in parallel and do a lot of tests, then there would be a lot of
> dead time instead of actual compute and writing of code. [...] The Google Build Box is kind of expensive to keep
> running continually. And maybe we can just compile it on the XPS 15 so that neither the EliteBook or the laptop
> studio is interrupted. [...] If you want to have parallel agents, that's fine. The only thing I'm concerned about
> is when we start running tests and compiling multiple times at the same time."

(His typing corrected; the words are his.)

**Outcome.** A new Clarp Host, written in Rust, that joins the fleet as one more Host on today's client contract.
Peter moves agents to it one at a time. The Python server keeps running until its last agent has left.

**This is a rebuild, not a port.** Do not translate Python modules. Build the design in `docs/design.md` and use
the Python server as the reference for behaviour the clients can observe.

## 2. How we prove it

Done is evidence, in this order:

1. **Conformance.** One suite for the core client contract (`contract/` schemas and fixtures, the replay corpus of
   `tests/replay/`). It must pass against today's Python Host first. It counts only once it has been shown to fail:
   every planted difference (a response field, an event order, a missing event, a state transition) is caught,
   including plants written by the reviewer, not only by the author.
2. **The five rules, under failure.** The kernel runs in a simulator with a fake clock and scripted backends. It is
   killed between any two steps, restarted, and checked:
   1. an accepted message is never lost and never runs twice;
   2. a running turn survives any restart of the server;
   3. every fact has exactly one owner;
   4. every change is an event with a number, and a client can ask what happened since a number;
   5. the cost of an action depends on that one agent's data, never on the size of the whole.
3. **On a real Host.** A restart during ten running turns interrupts none. Kill tests at every step of a turn.
   One week of real agents with no lost or doubled message.

Every difference from the Python Host is exactly one of: our bug (fix it); an intended difference (registered in
`docs/differences.md`, checked by the reviewer, signed by the lead); a defect in the reference (registered in
`docs/reference-defects.md`, reported to the release owner).

## 3. Hard rules

1. **Rust compiles in two places only: the XPS 15, through `server-rs/tools/build`, and GitHub Actions.** Never
   run `cargo` on elitebook or laptopstudio, and never by hand on the XPS 15: the script is the queue that keeps it
   to one build at a time, at lowest priority (that machine runs a Clarp Host of its own). The Google build machine
   is not used.
2. **The live Hosts are not ours.** Never deploy, update or restart `clarp*` units. Never run code against the
   live `state.sqlite`; read it only with `sqlite3 -readonly` or `mode=ro`. A disposable Host must not be able to
   reach `service_manager` (Clarp issue 322). Tools print structure, counts and codes, never conversation text.
3. **The client contract does not change** in stages 1 to 3: `contract/`, `HOST_CONTRACT`, routes, payloads and
   event types are frozen for this program. The Rust Host leaves out of `FEATURES` what it does not have yet.
4. **Main moves by fast-forward, after the gates:** the repository's gates in `AGENTS.md`, plus a green
   `Server (Rust)` run, started by hand, on the exact commit. No force-push, no rewriting main, no secrets in commits.
5. **Fail safe.** Unknown, partial or contradictory means stop and report. Never retry a write that may have
   landed; read back first.
6. **Never `pkill -f` or `pgrep -f`.** Kill only process ids you captured.
7. **Spending and capacity are Peter's.** No new paid service, no build-machine time beyond his cap, no helper on a
   model or account he has not approved. His own agents come first.

## 4. Decisions already made (do not reopen without the lead)

| # | Decision | Why |
|---|---|---|
| D1 | One agent, one actor, one log. An agent's state is owned by one task; its history is an append-only log only it writes | The domain is already this; rules 1, 3 and 5 hold by construction |
| D2 | One server process, `clarp-host`, plus one `clarp-shim` per running turn that owns the CLI child and records its output to a spool file | A restart never interrupts a turn; one writing process |
| D3 | Storage by life of the data: a small fleet database; one log per agent in `agents/<id>/`; large bodies as files; spools; a journal by day | Cost per agent; an agent is a directory that can be moved, archived, deleted |
| D4 | **Everything that has a plausible second implementation sits behind a port** (a trait in `clarp-ports`) with one conformance suite in `clarp-ports::testkit` that every implementation must pass, and is chosen by configuration: log engine, compressor, clock, later speech providers and backends | Peter's request; and the in-memory implementations the simulator needs are the second implementation that keeps each port honest |
| D5 | A stored body carries the tag of the codec that wrote it. Changing the configured compressor changes what new writes use and never requires rewriting old data | Swapping must be cheap in production, not only in code |
| D6 | SQLite (through `rusqlite`, bundled) is the first log and fleet engine; `zstd` with a shared dictionary the first production codec | The report's measurements; Turso and DuckDB were weighed (design, section "SQLite, Turso or DuckDB") |
| D7 | The wire contract is one crate, `clarp-protocol`; the desktop client will use it; Swift and Kotlin types are generated from it | Four clients, 99 contract versions, prose that drifted from code |
| D8 | Bounded contexts are crates: `fleet`, `conversation`, `execution`, `voice`, `autonomy`, `presence`, `artifacts`, `operations`, over `clarp-kernel`, under `clarp-edge` | The compiler enforces what guard tests check today |
| D9 | Triggers: one durable mechanism (when, what, to whom, under which admission policy) for every scheduler | Seven schedulers and five polling watchers become rows and subscriptions |
| D10 | Migration moves agents, not modules: the Rust Host is a new Host in the fleet | No shared-database coupling with the Python server, no big-bang switch |
| D11 | The binary is `clarp-host`. (`clarpd` is taken: it is the desktop installer in dotfiles.) | Avoids a name collision the report missed |
| D12 | Rust stable, edition 2024; Tokio; Axum; no message broker, no external database, no orchestrator | Plain, widely used, one binary and one directory of data |
| D13 | **Write big, compile rarely.** The lead keeps a compiled skeleton: every crate, its public traits and message types, no logic. A builder writes a whole crate against it, then compiles: `tools/build check` until clean, then `tools/build test`. No compile after every function, and no one-shot of the whole server | Peter's concern is dead time and parallel builds; the compiler is still the design feedback, so the batch is a crate, not the application |

## 5. Code rules (this is where "exactly as you want" is spent)

- **A port has two implementations or it is not a port.** Do not add a trait for something with one
  implementation and no simulator need.
- **Illegal states do not compile.** State machines are enums whose transitions consume the old state. No boolean
  flags that must agree with each other, no `Option` that is "always set after init".
- **One owner per fact.** State lives in the actor that owns it. No global mutable state, no `lazy_static` caches,
  no `Arc<Mutex<...>>` shared between contexts: send a message.
- **No polling.** Something that waits, awaits: a channel, a notification, a timer owned by the trigger engine.
- **Errors are values with a cause.** No `unwrap` or `expect` outside tests and `main`; no catch-all that hides
  an error. Every refusal a client can see has a stable code.
- **Blocking work never runs on the async runtime.** A store has its own writer thread; native or CPU-heavy work
  runs in a worker process.
- **No `unsafe`** without the lead's signature in the decision log.
- **Small files.** A module over about 500 lines or a function over about 60 is a signal to split, not a law.
- **A comment says why**, and when it records an incident it carries the date, as the Python server's best
  comments do.
- **Tests are fast and need no server.** Unit and simulation tests run in `cargo test`; anything that needs a
  running Host is a conformance or soak job.

## 6. Layout

```
server-rs/
  REBUILD.md            this brief (lead only)
  docs/design.md        the design (from the report of 14 Nov 2026)
  docs/log.md           the decision log, append-only (lead only)
  docs/differences.md   intended differences from the Python Host (lead signs)
  crates/
    clarp-ports/        the traits and their conformance suites (testkit)
    clarp-codec/        compressors: none, zstd, zstd with a dictionary
    clarp-store-mem/    in-memory log engine (simulator, tests)
    clarp-store-sqlite/ SQLite log engine
    ...                 clarp-protocol, clarp-kernel, clarp-shim, clarp-edge and the contexts follow
  tools/build           the build queue: check, test, clippy, fmt on the XPS 15, one job at a time
```

Ownership is by crate. A crate has one owner at a time, named in section 8. Shared files (`Cargo.toml` of the
workspace, the CI workflow, `clarp-ports`) belong to the lead.

## 7. Stages and gates

| Stage | Content | Gate |
|---|---|---|
| 0 Foundation | this brief, the build queue and the CI gate, the ports with two implementations each, then the compiled skeleton of every crate | green run on main |
| 1 Contract | `clarp-protocol` for the core contract; the conformance suite, proven against the Python Host and proven able to fail; the event model and spool format written down; the behaviour list from the 66 dated notes and the review registers | the suite passes on today's Host and catches every plant |
| 2 Core | kernel, shim, stores, edge for the core contract (agents, send, queue, stop, log, events, snapshot), Claude and Codex adapters, the importer for one agent | section 2, all three kinds of evidence; then the first agent moves |
| 3 Daily use | speech in and out, push, artifacts, goals and their wakes, helpers, background jobs | Peter uses it for a week with agents he chooses |
| 4 Everything | autonomy on the trigger engine, live calls, remaining backends, accounts, install and update | the last agent leaves the Python server |

## 8. Roles

| Role | Session | Owns |
|---|---|---|
| Lead | `maximus-6c37` | this brief, the ports, the workspace, decisions, sign-offs |

Builders and reviewers are added here when Peter has decided models and accounts. Reviewers use a different model
family from the authors and never edit sources.

## 9. How to work

- One worktree per agent under `~/GIT/clarp-worktrees/`, one branch per slice. Commit after every meaningful step.
- Your compiler is `server-rs/tools/build check` (seconds once warm), then `test`, `clippy`, `fmt`; `status` shows
  who holds the queue. It works from any checkout on elitebook or laptopstudio and runs on the XPS 15. Write the
  crate first (D13); a queue with one slot is fast only if nobody compiles after every function.
- The merge gate is the `Server (Rust)` workflow, and it never starts by itself: Peter does not want tests that
  run automatically (his word of 10 Nov 2026). Whoever lands a commit starts it on that commit:
  `gh workflow run server-rs.yml -R Maxteabag/clarp --ref <branch>`, then `gh run list --workflow server-rs.yml
  --branch <branch>` and `gh run view <id> --log-failed`. Start a watcher that wakes your goal instead of waiting
  in a turn (`clarp-background-jobs`, `run_detached_job.sh --goal ...`).
- Report to the lead with `clarp-admin prompt --to maximus-6c37 --from <you> --text "..."`: branch, head, done,
  next, blockers. One message per decision. No acknowledgements.
- A slice is ready for review when its CI run is green on the exact commit and its tests show the behaviour, the
  failure cases included.
