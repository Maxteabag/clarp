# The design of the Clarp Host in Rust

This is the proposal part of the report "The Clarp server: what it is, where it bends, and what I would build"
(Maximus, 14 November 2026, written against the Python server at commit `2d467c2e`), kept here as the design the
rebuild follows. The report's first three sections (the map of the Python server, its six limits with their
evidence, and its history) are in the Clarp report `report-clarp-architecture-maximus`. Marks: [read] in code or a
document, [measured] by a command, [inferred] from the two. Where this file and `REBUILD.md` disagree, the brief
wins: it carries the decisions taken since, such as the binary's name.

## 4. The architecture I would build

### 4.1 The load, and five rules

I design from the load the system carries, as measured or as you have described it: one owner; two or three machines; hundreds of agents per Host (292 rows on elitebook, 165 not archived) of which a few work at any moment (six when measured); millions of messages (2.26 million on laptopstudio); voice in real time; and a codebase changed by several agents every day (37 pull requests merged in two days by three agents). It is not a multi-tenant service and it does not need to scale out. It needs to be exact, cheap per agent, and safe to change.

1.  **An accepted message is never lost and never runs twice.**
2.  **A running turn survives any restart of the server.**
3.  **Every fact has exactly one owner.**
4.  **Every change is an event with a number,** so a client can always ask what happened since a number.
5.  **The cost of an action depends on that one agent's data,** never on the size of the whole.

### 4.2 The idea: one agent, one actor, one log

Each agent is an independent actor: a task with its own mailbox (today's queue), one turn at a time, and exclusive ownership of its state. Its history is an append-only log that only it writes. Everything else talks to it by sending it a message and learns about it by reading its events. This is what the domain already is: agents do not share state, each runs one turn at a time, and an agent's conversation is naturally a log. The current code expresses the same idea with shared tables, a global turn lock and repair passes; the proposal expresses it in the structure, so rules 1, 3 and 5 hold by construction.

### 4.3 The process model

<div class="tw">

| Process | How many | Owns | Changes how often |
|----|----|----|----|
| `clarp-host` | one per Host | the API edge, the kernel (agents, mailboxes, turn state machines, timers), the stores, the feature contexts | daily; a restart takes about a second and interrupts no turn |
| `clarp-shim` | one per running turn (and one for the shared Codex app-server) | the CLI child process, its stdin, and a spool file with everything it printed | rarely: it knows nothing about any backend |
| Whisper worker | one | the local speech model | rarely |
| `clarp-hook` | one short run per hook event | nothing: appends one line to the turn's spool and exits | rarely |

</div>

**The shim** is the piece that lets everything else be simple. It is the pattern container runtimes use to restart their daemon without killing containers. The shim starts the CLI, holds its pipes, appends each output line to `spool/<turn>.jsonl` with a sequence number, and listens on a small Unix socket for input and for stop. It puts the spool's path into the CLI's environment, so a hook (which the CLI starts as its child) can append its one line to the same file. The server tails the spool with a durable byte offset. If the server restarts, the offset tells the new process where to continue; if the shim dies, the server sees the socket close and records the turn as interrupted, as today. Backend-specific parsing stays in the server, which reads the raw spool, so a fix to an adapter applies to turns that are already running.

**Why one server process and not two.** Today's split exists so that a Host restart does not kill turns. With shims that property no longer needs a second service, a private RPC protocol, a handoff at idle boundaries, release fencing or account-recovery carry files. One process also means one writer, which is rule 3 for free. **Cost:** a crash anywhere in the server restarts all of it (about a second; apps reconnect by cursor, shims are untouched), and feature code runs in the same address space as the kernel, so a feature that blocks the async runtime hurts everyone. Mitigations: each feature context is a supervised task that is restarted on panic; anything CPU-heavy or native runs in a worker process; the kernel has its own runtime threads.

### 4.4 The kernel

- **State machines as types.** A turn is an enum (`Queued`, `Admitted`, `Running`, `Settled(Outcome)`) and a transition is a function that consumes one state and returns the next; the 147-line `transition` table of `turn_lifecycle.py` becomes what the compiler checks.
- **Write before acknowledge.** An actor handles a command by producing events; the events are appended to its log (one transaction, group-committed), and only then is the command acknowledged and the in-memory state updated. That is rule 1.
- **Memory holds the current state.** Agents, queues, turn states, the last page of each active conversation. `/agents/snapshot` is a serialisation of memory, not a query plus a repair pass. On start the kernel loads the small fleet database and opens agent logs lazily.
- **The log is the bus.** Each event gets a per-agent sequence number and a global one. Subscribers (the SSE edge, push, voice, triggers) receive events from an in-process broadcast and catch up from the logs by number. Nothing polls. That is rule 4.
- **Admission stays a pure policy.** Today's `policies/admission.py` is the right shape and carries over as a specification.

### 4.5 Storage: fitted to how the data lives

<div class="tw">

| Store | Holds | Shape | Why |
|----|----|----|----|
| `fleet.db` (SQLite, WAL) | agents, mailboxes, turn heads, timers and triggers, devices, settings, accounts, per-agent previews | small rows, current state, megabytes | hot, mirrored in memory, backed up in full every time |
| `agents/<id>/log.db` (SQLite) | that agent's conversation items and events | `item(seq INTEGER PRIMARY KEY, turn, kind, ts, body BLOB)`: append-only, clustered by `seq`, bodies compressed with Zstandard and a shared dictionary | the newest page is the end of one small file; cost depends on one conversation; an agent is one directory |
| `agents/<id>/blobs/` | large tool outputs, attachments, artifacts, audio | files named by content hash | keeps the log small and the page cache useful |
| `agents/<id>/spool/` | the raw output of running turns | one append-only file per turn, deleted when the turn is settled and ingested | the hand-over between shim and server; also the recovery source |
| `journal/` | fleet-level events for client catch-up | append-only segments by day, pruned by age | bounded; replaces `sse_events` and `state_log` |
| `telemetry` | diagnostics | its own file, lossy, byte budget | never competes with state (as today's `telemetry.sqlite`) |

</div>

**What makes an agent's directory complete.** Besides its log, blobs and spool, the directory holds a small manifest (identity, persona, backend, working directory, settings), so the agent's row in `fleet.db` can be rebuilt from the directory, and a copy of the CLI's own session file, refreshed when a turn settles. Without that copy a moved agent would keep its conversation in Clarp and lose the tool's memory of it; today's hand-over and fork already copy that file for the same reason.

**Why SQLite stays.** It is crash-safe, it is the right size for one owner on one machine, and agents can inspect it with tools they already know. What changes is how it is used: one writing process, small files, rows physically in the order they are read.

**Why a file per agent, and not one table clustered by agent.** Both satisfy rule 5. A file per agent also makes deleting, archiving and backing up an agent a file operation, confines corruption to one agent, and makes handing an agent to another Host a directory copy. **Cost:** no SQL joins across agents (the fleet list and previews become projections that the kernel maintains in `fleet.db`); no single transaction across the fleet database and an agent log, so the order is fixed (the log first, then the projection, which is rebuildable from the log by sequence number); hundreds of small files and their write-ahead logs to manage (opened lazily, closed when idle); a global search needs its own index.

#### The storage experiment \[measured\]

A helper rebuilt elitebook's 224,589 messages in the proposed layout on a copy of the database (report in `/var/tmp/maximus/reports/storage-experiment.md`). Read costs are SQLite page-cache misses in a fresh process with the memory map off and an 8 MiB cache, so they do not depend on the operating system's cache.

<div class="tw">

|  | Today | Proposed | Ratio |
|----|----|----|----|
| Conversation data on disk | 389.4 MB (table 284.7, ten indexes 104.7) | 114.6 MB with the dictionary | 0.29 |
| the same, no compression |  | 340.6 MB | 0.87 |
| the same, Zstandard without a dictionary |  | 205.1 MB | 0.53 |
| Newest page of the largest conversation (12,755 rows) | 84 pages | 15 pages | 0.18 |
| The whole largest conversation | 4,794 pages, 19.6 MB | 1,586 pages, 6.5 MB | 0.33 |
| 200 point lookups in it | 442 pages | 205 pages | 0.46 |
| Appending 20 rows, bytes written to the WAL | 284 to 309 KB | 20.6 to 24.7 KB | 0.07 |
| Decompression, one thread |  | about 325,000 rows a second |  |

</div>

<div class="card warn">

**What these numbers do not say.** The proposed files have no secondary index; the real design needs at least one small one (client message id to `seq`). The dictionary was trained on a sample of the same rows. Nothing was measured at laptopstudio's size, where today's costs are far higher than here because the file no longer fits the memory map; the experiment therefore understates the difference on that machine. Durable commit latency, concurrent writers and checkpoints were not measured. And the experiment measures a layout, not the absence of the importer, which is the larger saving.

</div>

### 4.6 The edge and the protocol

- **One library, `clarp-protocol`.** Every request, response and event is a Rust type with serde. The server and the Rust desktop use the crate directly (`desktop-rs/core` already has the protocol types and reducers on the client side); Swift and Kotlin types and the JSON Schemas are generated from it. Today `docs/protocol.md` and the Host disagree in places (issue 280) because the prose and the code are two things.
- **Sync by number.** `GET /events` with `Last-Event-ID` stays; what changes is that an event carries the data or a precise range, and `/log?after=<seq>` is a clustered range read. An app away for a week costs one range read per conversation it opens.
- **Compatibility layer.** The new Host serves contract 99 as it is: the same routes, payloads and event types, mapped onto the kernel. Features it does not have yet are absent from `FEATURES`, which is how the apps already decide what to show.
- **Stack.** Axum on Tokio and Hyper for HTTP, SSE and WebSocket; systemd socket activation so connections queue during a restart instead of being refused.

### 4.7 Triggers: one mechanism for autonomy

Heartbeats, dreaming, janitors, the team leader, schedules, goal continuations and decision delivery are seven workers today, each with its own loop, tables and admission adapter (`dispatch_adapters.py`). In the proposal each is a row: *when* (a time, an event pattern, or a dependency such as "job X ended"), *what* (a message template and an origin), *to whom*, *under which admission policy*. One engine in the autonomy context keeps them in `fleet.db`, fires them, and records what became of each firing. A goal's wake, a janitor's schedule and a "remind me in ten minutes" are the same thing. The watchers that poll tables (`state-log-watcher`, `runtime-event-watcher`, `background-job-watcher`, `decision-delivery`, `tts-worker`) become subscriptions. **Cost:** each feature's particular semantics (idle-only heartbeats, janitor quotas, goal recovery backoff) must be expressed as policy and re-tested; this is where most of stage 4's work is.

### 4.8 Voice

Two traits, `SpeechToText` and `TextToSpeech`, each with a streaming interface and one implementation per vendor; audio moves through bounded channels so a slow provider or a slow phone applies back-pressure instead of filling a queue table. One clip delivery mechanism. One live-call bridge with provider adapters instead of two engines. The local Whisper model stays in a worker process, as it is today: a native model must not be able to take the server down, and it holds 1.1 GB that should not be in the server's address space.

### 4.9 Bounded contexts

You mentioned domain-driven design. I take two of its parts: bounded contexts and aggregates. The agent is the aggregate (it is also the actor and the unit of storage). The contexts are crates, so the compiler enforces that one reaches another only through its public messages.

<div class="tw">

| Crate | Owns | Speaks |
|----|----|----|
| `clarp-protocol` | wire types, generated schemas | to every client |
| `clarp-kernel` | actors, mailboxes, turn state machines, the journal, timers | commands in, events out |
| `fleet` | agents, personas, teams, helpers, focus | agent created, archived, moved |
| `conversation` | items, logs, previews, search | item appended, range read |
| `execution` | backend adapters, shims, spools, accounts and seats, usage | turn started, output, ended; account exhausted |
| `voice` | speech in and out, clips, live calls | utterance, clip ready |
| `autonomy` | triggers, goals, janitors, background jobs | trigger fired, goal changed |
| `presence` | devices, pairing, push, attention, live activities | device seen, notification sent |
| `artifacts` | documents, media, forms, reports | artifact published, form answered |
| `operations` | install, update, backup, diagnostics, config | release installed, backup taken |
| `clarp-edge` | HTTP, SSE, WebSocket, auth, the contract 99 mapping | the outside |

</div>

### 4.10 Technology: each choice, its reason and its cost

<div class="tw">

| Choice | Reason | Cost | What I weighed against it |
|----|----|----|----|
| **Rust** for the server | The code is changed daily by several agents; the compiler is the cheapest reviewer. Sum types make illegal turn states unrepresentable. Ownership is "one owner per fact". One static binary. No interpreter start per hook. The desktop client is already Rust (227 files, 75,996 lines since 29 September), so one protocol crate serves both. | Builds are heavy: the repository already forbids `cargo` on your machines because parallel builds pushed the load to about 97. Every agent's edit-compile-test loop goes through the build machine or CI, which costs money and minutes. Async Rust is easy to get subtly wrong (cancellation, blocking in async code). | **Go**: fast builds, good at exactly this kind of daemon (containerd is Go), but no sum types and no ownership, so the two properties I want from the compiler are missing, and nothing is shared with the clients. **Elixir/OTP**: the actor and supervision model is this problem's model, but it is dynamically typed, shares nothing with the clients, and is awkward at owning OS child processes. **Python with asyncio**: fixes the architecture without changing language and would be the fastest to write, but keeps the weight, keeps guard tests in the place of a compiler, and keeps the interpreter in every hook. |
| **Actors and an event log** as the design style | Matches the domain; gives rules 1, 3, 4 and 5 by construction | Projections must be maintained and versioned; cross-agent views are eventually consistent (by milliseconds, in one process) | Keeping shared tables with stricter stores: that is today's design after the refactor |
| **One server process plus shims** | Turns survive every restart; one writer; one service to operate | A spool format and a shim protocol that must stay compatible across versions; one more small process per running turn | Keeping Host and runtime as two services: keeps the hand-off machinery and the second writer |
| **SQLite, split by life cycle** | Section 4.5 | Section 4.5 | One clustered table; a custom log format (smaller, but agents could not inspect it and crash recovery would be ours to write); Postgres (an extra service to operate for one owner) |
| **Zstandard with a shared dictionary** | 0.29 of today's size on real data; 325,000 rows a second to decompress | The dictionary is part of the data format: it must be versioned and kept for ever | No compression (0.87), Zstandard without a dictionary (0.53) |
| **Tokio, Axum, rusqlite, serde** | The plain, widely used choices; agents know them well | None particular | Custom runtimes or frameworks: no reason |
| **No broker, no external database, no orchestrator** | One owner, one machine per Host; every extra service is something that can be down | A Host does not scale out. That is not a load this system has. | NATS, Redis, Postgres: all solve problems of scale or of many writers that the single-writer design does not have |

</div>

#### SQLite, Turso or DuckDB (added on 14 November, after Peter asked)

I had weighed SQLite against one clustered table, a custom log format and Postgres, and not against these two. Their state as their own pages describe it on 14 November 2026 \[read\]:

<div class="tw">

| Engine | What it is | For Clarp | Verdict |
|----|----|----|----|
| **DuckDB** | An in-process column store for analysis. Its documentation says one process writes, and that "executing many small transactions is not a primary design goal": it is optimised for bulk operations. | Clarp's writes are the opposite: a few rows per event, each acknowledged only when durable, and reads of the last page of one conversation. | Not the store. A good second engine beside it: it can attach SQLite files, so it answers the cross-agent questions that one log per agent makes harder (usage and cost per agent, fleet statistics, telemetry). Message bodies are compressed, so questions about content need a periodic export, for example to Parquet. |
| **Turso Database** (the Rust rewrite of SQLite, formerly Limbo) | SQLite's dialect and file format in Rust, with asynchronous I/O, concurrent writers through MVCC (`BEGIN CONCURRENT`), built-in change capture, and sync to embedded replicas. Its README: "we have not yet reached 1.0", "we are not at 100% yet" on compatibility, and encryption, full-text search and multi-process access are marked experimental. Turso was acquired by Supabase (announced 2 October 2026); the announcement says the database stays open source and actively developed. | Its main advantage is many writers at once. The design has one writing process and one writer per agent log, so that advantage buys nothing here. Rule 1 (an accepted message is never lost) rests on this component, and SQLite is the most tested storage engine there is. What Turso would add that I do want one day: no C in the binary, and sync, so that a phone could hold a replica of its conversations and a Host could replicate an agent's log to another Host. | Not for stages 1 to 3. The named successor: re-evaluate at its 1.0, or earlier if device sync becomes a goal (that would replace part of the client contract, which the migration plan keeps fixed). The choice is reversible because Turso keeps SQLite's file format: the stores sit behind one small interface in the kernel (append, read a range, read the tail), so changing engine means changing one crate, and the files stay. |
| **SQLite** (through `rusqlite`) | The C library, compiled into the binary | One writer, small files, rows in the order they are read (section 4.5) | The choice for the fleet database and the agent logs |

</div>

One observation in the design's favour: a database per agent, which is the layout I chose for the logs, is the pattern Turso itself is built around.

Pages read: duckdb.org/docs/current/connect/concurrency; github.com/tursodatabase/turso (README); docs.turso.tech/sql-reference/statements/transactions; turso.tech/blog/turso-is-joining-supabase (2 October 2026).

**On memory.** The video states a target of under 200 MB for the server. That is a design target, not a measurement (today's Host and runtime held between 1.27 and 2.04 GB together in three readings): the kernel's state for a few hundred agents is tens of megabytes; the rest is SQLite page caches and buffers. The Whisper worker's 1.1 GB stays, and each `claude -p` keeps its 0.3 GB: those are not ours to shrink.

### 4.11 Testing and operations

- **Simulation.** The kernel takes its clock, its storage and its backends as parameters, so a test can run it with a fake clock and scripted agents, kill it between any two steps, restart it and check the five rules. This is the kind of test that today needs a running Host and a replay corpus, and that the repository forbids on your laptops for load reasons.
- **Conformance.** The contract fixtures, the JSON Schemas and the replay corpus become the acceptance suite: the new Host must produce the same responses and event orders as the old one for the core contract. The refactor program proved this harness can catch planted differences.
- **Deploy.** Copy one binary, restart one unit. Rollback is the previous binary. Schema changes are per store and small.
- **Observability.** Structured tracing with the turn's id through every hop; counters for the five rules (events appended, commands refused, spool lag).

### 4.12 Failure cases

<div class="tw">

| What fails | Today | In the proposal |
|----|----|----|
| The server is restarted or crashes | Host: turns continue, pushed events may be missed. Runtime: turns are interrupted and marked; a clean release waits for an idle boundary. | Turns continue under their shims; the server resumes each spool at its stored offset; apps reconnect with their last event number. |
| A CLI hangs | Holds its slot until someone preempts it | The actor owns a deadline per turn state; the trigger engine fires it; the shim is told to stop |
| A shim dies | (no equivalent) | Its socket closes; the spool is complete up to the last line; the turn is recorded as interrupted |
| Power loss | WAL recovery; reconcile passes on boot and on read | WAL recovery per file; a log entry is either there or not; projections are rebuilt from the log by number; a torn spool tail is cut at the last complete line |
| An account runs out | Failover coordinators in runtime memory, carried across restarts in a JSON file | An event from the execution context; parked turns are mailbox state in `fleet.db` |
| A client is away for days | Replay of durable events for one day, then snapshot and deltas | The same contract; the delta is one range read per conversation |
| The disk fills | Writes fail somewhere | The kernel refuses new turns before it cannot append; spools and blobs have budgets |

</div>

### 4.13 What this design does not try to do

It is not multi-tenant, it does not replicate between Hosts (an agent lives on one Host and can be moved), it has no plugin system beyond triggers and custom speech adapters, and it does not change the apps. If one of those becomes a goal, say so before stage 2: tenancy and replication in particular would change the storage design.

## 5. What a migration would take

<div class="card key">

**Migrate agents, not modules.** The seam between the old server's Host and runtime is not clean enough to replace one of them (both write the same tables), and replacing modules inside the Python server would inherit its storage. The fleet, however, already consists of several Hosts behind one contract, and the apps already talk to more than one. So the new server is a new Host. It starts with one agent. Agents move to it one at a time. The old server keeps running and keeps receiving fixes until its last agent has left.

</div>

### Stages and gates

<div class="tw">

| Stage | Content | Gate to pass | My estimate |
|----|----|----|----|
| **1. The contract** | `clarp-protocol` for the core contract; a conformance suite built from the fixtures, the schemas and the replay corpus, proven against the current Host; the event model and the spool format written down; a read of all 66 dated notes and the review registers into a list of behaviours the new server must have | The suite passes against today's Host and fails on planted differences | 1 to 2 weeks |
| **2. The core** | Kernel, shim, stores, the edge for the core contract (agents, send, queue, stop, log, events, snapshot), Claude and Codex adapters, an importer from a `state.sqlite` agent to an agent directory; runs as a third Host on a spare machine or the Docker node | Conformance green; a week of real agents with no lost or doubled message; kill tests at every step; a restart during ten running turns interrupts none | 3 to 5 weeks |
| **3. Daily use** | Speech in and out, clips, push, artifacts and forms, goals with their wakes, helpers and background jobs; the skills' commands working against the new Host | You use it for a week with the agents you choose, on phone and desktop, with voice | 3 to 5 weeks |
| **4. Everything** | Autonomy on the trigger engine (heartbeat, dreaming, janitors, Hotseat), live calls, the remaining backends, accounts and failover, install and update, Docker; laptopstudio's agents move | The last agent leaves the old server; it runs empty for two weeks, then it is retired | 4 to 8 weeks |

</div>

**On the estimate: 11 to 20 weeks.** \[inferred\] It is an estimate with one calibration point from this project: the Rust desktop client went from nothing on 29 September to 75,996 lines of Rust and 10,702 of Slint on 14 November, written by agents alongside everything else. The new server's core is smaller than what it replaces, because the importer, the polling loops, the reconcile passes, the second service and the duplicated generations are not ported. The long tail is stage 4: 87,000 lines of features hold many small behaviours.

### What is not rewritten, and what is not ported

- **Carried over as specification:** the turn state machine's transition table, the pure policies, the contract, the fixtures, the tests' intent, the incident comments.
- **Not ported:** the transcript importer and its caches (about 5,000 lines; replaced by parse-once, plus a one-time import when a terminal session is adopted), the runtime bridge and release hand-off, reconcile and repair passes, the single-process mode, the older of each doubled generation, the polling workers.
- **Yours to decide:** which features are still wanted. The PWA was set aside during the refactor program; there are three desktop shells in the tree; each feature left behind is weeks not spent.

### What it costs and what it risks

<div class="tw">

| Cost or risk | How I would contain it |
|----|----|
| Two servers to maintain for three to five months | The old one gets fixes only; new features wait or go to the new one from stage 3 |
| Build-machine spending and slower iteration | A workspace of small crates, a shared build cache, CI as the gate as for the desktop today. A daily cap, as the refactor program had |
| Second-system effect: the rewrite loses lessons the old code learned | Stage 1 turns the lessons into tests before any kernel code exists |
| Stage 4 never ends | Agents move per feature: an agent that needs no live calls can move in stage 3. A feature nobody moves for is a feature to delete |
| CLI behaviour I have not verified (section 6) breaks parse-once | Stage 2 proves it on Claude and Codex first; the spool keeps raw output, so the transcript file remains a fallback source |
| The iPhone rewrite and the Android app are in flight against the same contract | The contract does not move; the new Host is one more Host to them |

</div>

### Decisions I need from you

1.  **Go or no-go on stage 1.** About two weeks, no effect on the running fleet, and its product (the contract as code and a conformance suite) is useful even if you stop there.
2.  **A spare Host and a build budget** for stage 2: where the new server runs first, and what the build machine may cost per day.
3.  **Who owns it.** I would run it as the refactor program was run: one lead, authors of one model family, reviewers of another, a merge gate. I can lead it or hand this report to whoever does.

Nothing in this report has been started. I changed nothing on the server, installed and restarted nothing, and read the live database only in read-only mode.

## 6. What I could not verify

- **laptopstudio.** Every number from that machine is Arnold's (his checkpoints and logs). I did not read its database or its `FINAL-HANDOFF.md` from the refactor program.
- **The 1.5 to 2 GB per full import** is a counted figure from Arnold's attribution; the mechanism behind it is read from the code and inferred. Elitebook's database fits the memory map and cannot reproduce it.
- **That a CLI's output stream carries everything its transcript file does.** The design's parse-once rests on it. The runtime already builds live rows from Claude's stream, which is encouraging, but I did not compare the two for sub-agents, compaction or every backend.
- **The shim under real load.** No prototype was built. The pattern is proven elsewhere, not here.
- **Moving an agent by copying its directory.** It depends on each CLI accepting its session file put back in place on another machine. Today's hand-over does this for Claude; I did not check it for the other backends.
- **The memory target and the restart time** of the new server are design targets.
- **Why each historical decision was taken.** Dates and commit subjects are facts; the reasons in section 3 are my reading, supported by `ARCHITECTURE.md` and code comments where they speak.
- **Whether the refactor program met its own exit criteria** (the 29 high findings). I say only what it did that the tree shows.
- **Thread-to-worker mapping** in the live Host (all threads are named `python`), the cadence of the adaptive janitor loop, query time per polling pass, the AGY, Grok, OpenCode and Muse drains line by line.
- **The grouping behind "core 60,500, features 86,700" and "54 core modules import features back".** Both rest on one helper's assignment of files to subsystems by name; the independent checker did not recount them. The same holds for "17 timer loops, 15 of which query the database": each loop is cited with file and line in `edges.md`, and the checker read six of them.
- **Routes per subsystem** and the table lists in section 1.5: the SQL was found by regular expression, so the lists are a lower bound.
- **The split of authorship** between you and agents in the 4,820 commits: every commit carries your name.
- **Rust as a language for this team of agents on the server.** The desktop is evidence that they write it at volume; whether their async server code is sound is something stage 2's kill tests must show.
