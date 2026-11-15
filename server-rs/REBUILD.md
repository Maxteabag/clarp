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

And when the work was slowed by gate runs: "Why are you running tests and not working".

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
   **Main moves at milestones, not per commit** (Peter, 15 Nov 2026): work on your branch, push it often, and
   pay for the gates when a stage or a whole piece is done.
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
| D14 | The kernel admits a message in one of four ways. **Queue**: it was said; it is in the conversation at once and the agent comes to it when it is free. **Later**: someone left it for later (`queue_if_busy`); it waits like the first, but in the queue, and enters the conversation when its turn starts. **Background**: left for later by something that is not a person; a hold holds it. **Direct**: it ends the live turn and runs ahead of what waits. A Stop holds everything that waits at that moment; what a person says or leaves afterwards is not held by it, and when the agent is free it runs past what is held. The edge admits no message direct: the Python Host queues a user's plain send to a busy agent (`turn_claim._enqueue_if_busy`), whatever `docs/protocol.md` section 4 says | The conformance runs of 15 Nov and the Python Host's admission policy (`policies/admission.py`): the apps were built against what the Host does. A hold that also held what the user says next would make Stop a trap. Direct stays in the kernel, tested, for the day something asks for it |
| D19 | The Host names the tool's session before the tool starts (Claude: `--session-id`), with the agent's conversation id | A conversation must be called the same from its first message on: a client drops what it cached when the name changes |
| D20 | How a turn ended is a conversation item of its own (`TURN_END`), written once, and told on the row that closed the turn; that row then carries the end's revision | The apps read `turn` on the closing row. Storing it once keeps the log append-only and lets a client that asks "what is newer" get the row again |
| D21 | The edge writes the Python Host's shapes field for field, as JSON values built in `clarp-host/src/wire.rs` from what `conformance/golden` recorded, empty fields included. `clarp-protocol` stays the typed reading side | An app's decoder may count on a field being there. The recorded answers are the specification; the conformance run fails when the two drift |
| D22 | A message that waits outside the conversation is a log item of its own kind (`WAITING`). When its turn starts, the same message is written as a conversation row in the write that starts the turn; the journal does this, not the actor. The row that closed the turn before is told its end after that row | The conversation is what was said to the agent, in the order it reached it. One write, so a crash cannot start a turn whose message is not in the conversation. The order is the reference's, at no cost |
| D23 | A conversation's head revision is the revision of its newest change (its newest item in the log, or a reply being written), not of the log's newest item | A client compares the head with what it holds to know whether to ask. Bookkeeping must not make it ask |
| D24 | Every turn's end is told to the apps, also when the next turn begins in the same write: first how the one ended, then that the next began. The answer a turn ended on is found by the row that closed it, not by being the newest row | An app that waits for an agent to be done must hear it, and the user is notified of every answer |
| D25 | `busy` and `turn_started_at` in the snapshot are what an app counts a running turn from (issue 354). `busy` is true exactly while a turn holds the agent. `turn_started_at` is then the moment that turn was taken up: not when its message was accepted, not when one of its tools started, never an earlier turn's, and the same for as long as the turn runs. At rest it is 0, or the start of the last turn when that turn did not complete, as the Python Host keeps it | The timer in the apps is the turn's time. Held by the conformance scenarios `turn-timer` and `turn-timer-after-stop` |
| D15 | A conversation's revision is `seq * 2^20 + n`: the log position, with room in between for the changes of a reply that is shown while it is written and not stored. A restart that rejoins a turn writes once, so later revisions are above earlier ones | Clients sync by revision; the log gives it for free; a growing reply needs revisions of its own |
| D16 | A turn starts at most once by construction: the shim creates the turn's spool exclusively; "spawned" is durable before the tool is given the message; the shim prints `ready`; a second shim for the turn leaves with exit 125 | Rule 1 must not depend on the server remembering what it started |
| D17 | The hub forgets. It keeps five minutes of events for clients that reconnect; after longer, or after a restart, a client is sent `agent-roster` and syncs from the snapshot and the conversations' revisions | An event is a wake-up, not the data; the logs are the durable record. Events that are themselves the data (a clip, a notification) get a durable home in stage 3 |
| D18 | The conformance suite is `tests/replay` run across implementations: its scenarios, its fake Claude and its differ, with the Rust Host as the second build, comparing the `http` and `sse` channels. It runs in GitHub Actions, contained, never on a machine with a live Host | The harness exists, is proven to catch planted changes, and already isolates a Python Host safely |
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
  crates/
    clarp-ports/        the traits and their conformance suites: log engine, key-value store, compressor, clock
    clarp-codec/        compressors: none, zstd, zstd with a dictionary
    clarp-store-mem/    in-memory log engine and tables (tests, the simulator)
    clarp-store-sqlite/ SQLite log engine (`agents/<id>/log.db`) and tables (`fleet.db`)
    clarp-protocol/     the client contract as types: events, log, snapshot, server-info, requests, responses
    clarp-kernel/       turn state machine, the agent's pure core, the journal (write first, then change),
                        the conversation's items
    clarp-spool/        the spool: what a tool printed, as the shim recorded it
    clarp-shim/         `clarp-shim` (owns one tool process) and `clarp-hook`
    clarp-backend/      the backend port; Claude Code (stream-json), a scripted tool
    clarp-actor/        one agent's actor without its thread: send, stop, the spool pump, rejoin after a restart
    clarp-host/         `clarp-host`: agent threads, launcher (direct, systemd scope), fleet, pairing, hub,
                        HTTP edge, and `wire.rs`, where the Host's answers get the Python Host's shapes
  conformance/          the Rust Host against the Python Host on what a client sees (README.md there)
  docs/differences.md   what differs from the Python Host on purpose
  docs/reference-defects.md   what the conformance runs showed to be wrong in the Python Host
  tools/build           the build queue: check, test, clippy, fmt, conform on the XPS 15, one job at a time
  tools/repo-gates      the repository's Python gates, on the same machine and queue
```

Ownership is by crate. A crate has one owner at a time, named in section 8. Shared files (`Cargo.toml` of the
workspace, the CI workflow, `clarp-ports`) belong to the lead.

**Where it stands (15 Nov 2026).** The Host runs end to end: a message over HTTP becomes a turn in a shim, the
conversation, snapshot and events are served, and a Host killed with `kill -9` in the middle of ten running turns
comes back and finishes all ten (`crates/clarp-host/tests/host.rs`). A real Claude Code turn has been through
it, with the Host killed while Claude's command ran; the next turn continued Claude's session. That test runs
only when asked, because it spends a real account:

```
CLARP_BUILD_ENV="CLARP_TEST_CLAUDE=1" server-rs/tools/build test --test host -- --ignored
```

It needs a logged-in Claude Code on the machine that runs it. On 15 Nov the XPS 15's was not; the test was run on
elitebook with the binaries the XPS had built (running a built binary is not compiling).

**The Rust Host conforms (15 Nov 2026, run 43683008178 and after).** `conformance/` holds the corpus, the planted
changes and the driver; the workflow `Conformance (Rust Host)` records the Python Host twice, proves that every
planted change is caught, and compares the Rust Host. Eight differences are left, all registered with their
reason in `docs/differences.md`; what the runs showed to be wrong in the Python Host is in
`docs/reference-defects.md` (nine entries, none reported yet). `tools/build conform` is the everyday loop against
the recorded answers (about 20 seconds). Read `conformance/README.md` before you change anything a client can
see: the recorded answers are the specification. Still open on this proof: planted changes written by someone
other than the author, and a corpus for Codex agents (the Python Host runs Codex through its app server, the
harness has no fake for it).

**Codex runs** (`crates/clarp-backend/src/codex.rs`): one `codex exec --json` per turn, written against
recordings of codex-cli 0.167.1. A real Codex turn has been through the Host, killed while its command ran; the
next turn resumed Codex's thread (`CLARP_TEST_CODEX=1`, run the same way as the Claude test above).

**Pairing is there** (`clarp-host pair --url ...`, `POST /pairing/exchange`, scopes, revoke), the Python Host's
scheme word for word.

**An agent moves** (`clarp-host import`, `docs/moving-an-agent.md`): rehearsed on 15 Nov against the live Python
Host, reads only, into a scratch Rust Host. A move that is kept waits for the owner: he chooses the agent, and
the Rust Host has to run on his machine as a service of its own first.

Not there yet: the first kept move (stage 2's gate), and stage 3, which is what makes an agent livable on this
Host. The iOS app names about 150 endpoints; the Rust Host answers the 15 of the core contract and leaves every
other feature out of `capabilities.features`. The owner declined an install on 15 Nov: a Host an agent cannot
live on is not put on his machine. It is proposed again when stage 3's first surfaces are there.

**Stage 3 goes in the order the load says.** Counted on the live elitebook Host on 15 Nov 2026, reads only, over
the seven days before:

| Surface | What it carried in seven days | Order |
|---|---|---|
| Notifications to the owner (and the push behind them) | 5,907 notifications | 1 |
| Background jobs | 1,183 jobs, 3,188 job events | 2 |
| Goals and their wakes | 88 plans, 4,347 goal events | 3 |
| Sub-agents | 233 agents made, nearly all helpers | 4 |
| Artifacts, media, decision cards | 172 artifacts, 203 media assets, 16 decisions | 5 |
| Speech out and in | 45 clips, 709 voice events | 6 |

How much that is, by the size of what the Python Host has for it (lines of Python without tests, files sorted
by name, 15 Nov 2026; the whole is 147,900 and the Rust Host stood at 19,200 lines):

| Area | Lines of Python | Share |
|---|---|---|
| Core: turns, queue, messages, transcript, events, snapshot, agents (what the Rust Host has) | 31,000 | 21% |
| Goals, task plans, background jobs, sub-agents | 7,800 | 5% |
| Artifacts, media, decisions, forms, attention | 5,100 | 4% |
| Push, notifications, devices, pairing | 4,700 | 3% |
| Speech and calls (speech out and in, voice catalogue, Oracle, rings, group calls) | 33,800 | 23% |
| Janitors, heartbeat, dreaming, leaders, teams | 17,400 | 12% |
| Accounts, usage, limits, the other backends | 13,000 | 9% |
| Everything else (settings, install, update, admin, fleet map) | 35,000 | 24% |

What a worker agent needs to live on the Rust Host is the second to fourth row: about 17,600 lines of Python,
a little over half of what the core was. Speech out and in is a part of the fifth row; calls and the Oracle
are stage 4.

Speech was planned first and is last: on this Host it carries the least. What is built of it stays: the crate
`clarp-speech` holds the voice markup (what of a reply is spoken, what is shown), ported from
`server/lib/voice_markup.py` and held to it by vectors written from the Python module. Its design, for when its
turn comes: a speech engine is a program outside the Host (text in, audio out), a clip is a file that grows and
is announced before its first byte, as the Python Host's chunked-file delivery does.

**The agent's side of the contract is command lines.** An agent reaches its Host through tools that skills call
by name: `clarp-goal`, `clarp-agent-bg`, `clarp-agent-artifacts`, `clarp-sub-agent`, `clarp-media-publish`. On
the Python Host most of them are not clients of an API: `clarp-goal` and `clarp-agent-bg` import the Host's
library and write its database, and their wrappers set the Python Host's paths themselves, whatever the
environment says (read on 15 Nov in `~/.local/share/clarp/bin` and `scripts/agent_bg.py`,
`scripts/agent_tasks.py`); `clarp-agent-artifacts` speaks HTTP. So for the Rust Host each tool is a program of
its own with the same name, arguments, output and exit codes, which speaks to the Rust Host. `tools/install-host`
puts the Rust Host's own directory of tools first on the path of an agent's commands, and fills it with one tool
of the same name, which refuses, for every tool of the Python Host that is not built yet (they are also linked
from `~/.local/bin`, so taking a directory off the path would not keep them away): a tool that is not there yet
says so, and is never an action on the other Host.

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

**Stage 1 is built by the lead alone** (Peter's decision of 15 Nov 2026, from three options: "Maximus alone
through stage 1"). Builders and reviewers are staffed for stage 2, on the models and accounts Peter decides then.
The one exception to "alone" is the proof that the conformance suite can fail: its plants must also come from a
reviewer who did not write the suite, so that review is requested from Peter when stage 1 reaches its gate.
Reviewers use a different model family from the authors and never edit sources.

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
