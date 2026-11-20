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
8. **A live test agent on the Host is a Codex agent** (Peter, 16 Nov 2026: "if he's going to test live agents on
   the host use codex. Use Claude only if testing Claude. But use sonnet."). The soak, the restart exercises and
   the checks that make agents of their own run on Codex. A Claude agent is used only where Claude Code itself is
   what is tested (what its tool is handed, how its session is taken up again), and then on Sonnet: not on Haiku,
   Fable or Opus. A test that cannot be done so is a question for Peter, never an exception.
9. **Two Hosts never dial the connection relay under one name.** The relay gives a name's phones to whoever
   dialled last. The Rust Host takes its relay from the file `--relay-config` names and from nowhere else; the
   installed battle-test Host has a name of its own there, or none. `~/.config/clarp/relay.json` is the Python
   Host's until the day of the switch. Never print or copy a `relay.json`, and never log the address a
   connector dials: both carry the key.

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
| D26 | A shim never waits for the Host, and asks a tool to stop in the tool's own way through one signal to its direct child, bounded, before it signals the group | A restart must never hold a turn. The reference aborts a served tool before it ends it. OpenCode gives its abort request three seconds |
| D25 | `busy` and `turn_started_at` in the snapshot are what an app counts a running turn from (issue 354). `busy` is true exactly while a turn holds the agent. `turn_started_at` is then the moment that turn was taken up: not when its message was accepted, not when one of its tools started, never an earlier turn's, and the same for as long as the turn runs. At rest it is 0, however the last turn ended (the Python Host kept the start of a turn that did not complete until its fix of 15 Nov 2026, PR 357) | The timer in the apps is the turn's time. Held by the conformance scenarios `turn-timer` and `turn-timer-after-stop` |
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
    clarp-ports/        the traits and their conformance suites: log engine, key-value store, compressor,
                        whole files, clock
    clarp-codec/        compressors: none, zstd, zstd with a dictionary
    clarp-store-mem/    in-memory log engine, tables and whole files (tests, the simulator)
    clarp-store-sqlite/ SQLite log engine (`agents/<id>/log.db`) and tables (`fleet.db`)
    clarp-store-files/  whole files on disk (`media/<first two signs>/<name>`): what agents publish
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
the Rust Host has to run on his machine as a service of its own first (it does, since 15 Nov 17:14). Since the
evening of 15 Nov an agent brings more than its conversation: the files it published, under the ids that name
them (its gallery, and the files its recordings, films and documents are made of, as its own; a picture of
another agent that it shows comes along without becoming its own), so that an image in an old reply still
shows and a film in its library still plays; its library, each artifact as it was
shown, a report with every version it had, a request to the user as open or as answered as it was; and its
plans and goals, each as it stood, at its revision, with its steps, its history, how its work goes on and the
newest of its documents (most of the owner's agents work toward a goal: 88 plans in the week that was
counted). Nothing of it is made anew or judged anew here: the kernel takes what was shown
(`GoalBook::taken_over`, `artifacts/moved.rs`), and what one book shows another shows the same after it. It
arrives with what it made and what it is committed to or not at all. And it arrives held: what its user says to
it runs, and what would start by itself waits for a resume, because until it is archived where it came from it
is on both Hosts and both would wake it. Not moving yet: its portrait and voice.

**Stage 3 has begun, on the agent's side first** (15 Nov 2026). An agent on this Host has its background jobs
(`clarp-agent-bg`, the apps' view of them and the cancel), its own status label, and its goals: `clarp-goal`
with the Python Host's rules and words (`crates/clarp-kernel/src/goals.rs`, held to
`scripts/agent_tasks.py` by 75 recorded steps), kept in the agent's own log as what each change set, and the
wakes (`goals/wake.rs`): a goal that is due wakes its owner when the owner rests, a goal that waits for a job
is woken when the job ends, a wake no checkpoint follows is tried again later each time, a Stop pauses the
agent's goals. The message is the Python Host's word for word (55 recorded cases). The apps have their side of
it (`plans.rs`): the plan a session works, its plans, a goal's documents, and what the user does to a goal,
which is what its owner may do and one thing more, setting a criterion aside (101 recorded steps in all, 13
of them the user's). Not there of goals: the ledger and subgoals (`ledger`, `subgoal` and `bookkeeping`
answer that they are not here yet), and what an account's usage limit does to a wake (accounts come later).
The Goal keeper and its receipts are there: below.

**Developer diagnostics** (19 Nov 2026). A computer's capture preferences live in one fleet
setting. A limited device may read settings and health; changing the setting requires full access.
Capture goes to one private directory and uses a bounded nonblocking queue. Its writer drops new
records at capacity and expires hour files even with capture off. Its resource sampler runs only
when selected. The public retention numbers are upper bounds; this Host keeps no hourly rollups.
The policy and its reference recordings are in `clarp-kernel/src/diagnostics.rs` and
`tools/diagnostics-vectors.py`. The file implementation of the diagnostic store port owns expiry
and the 256 MiB bound. Its two GET routes never open the capture store.

**Background Bash tasks in Claude's transcript** (19 Nov 2026). The agent reads a background Bash
launch, its tool result and the provider's eventual task notice from its bound parent transcript.
The agent's job book gives that task a stable id and shows it in the ordinary job list and
timeline. A progress update is provider evidence. The Host owns no worker PID for it. An
unmatched result creates nothing. A matching late completion can settle an inferred stop.

The live reader shares a bounded file window with the helper cells. The Host's existing runner
visits two eligible idle bindings in agent and session order. Each visit reads at most 256 KiB and
keeps its place within a record with more than 64 actions. The record ceiling is 4 MiB including
the newline. The actor writes the cursor, the launching attempt and the job records together.
On rejoin it drains the final byte boundary and reconciles the ended attempt before publishing
its job book. An interactive resume has no managed attempt to stop its jobs.

The rules and binding rotation are in `clarp-kernel/src/provider_tasks.rs`. The generators
`tools/provider-background-tasks-vectors.py` and `tools/provider-observer-vectors.py` record
the Python Host's own functions, including 1,142 lifecycle steps and 320 binding selections.

The feature `provider_background_tasks` is listed at contract 24. Managed Claude turns receive
one Bash PreToolUse hook through inline settings. The shim reads at most 64 KiB of JSON, returns
no permission decision and never contacts the Host. The hook attaches the exact agent, native
session and tool-use id to a worker registration. A registration with that binding takes the native
row's place in the list; its own detail and timeline remain available. Full devices can read
the last 64 KiB of the receipted task's output with secrets redacted. A limited device sees only
progress and history. Cancelling a native job gives a 409 receipt and leaves its state intact.

The hook's turn advice is held by `tools/provider-hook-vectors.py` and the Host test that runs
the repository's `run_detached_job.sh --goal` against its own tools. A worker awaiting a refused
checkpoint never started the command. A successful launch stored the worker and goal dependency
before returning and woke the agent when it finished. A separate setsid child answered after
the managed turn ended and after the Host restarted. Registration and output are held by
`tools/provider-registration-vectors.py`.

**What agents make and ask is there, for the plain kinds** (15 Nov 2026). The rules of `server/lib/artifacts.py`
are in the kernel (`artifacts.rs`, `artifacts/decisions.rs`, held to the Python module by 264 recorded calls,
123 of them refusals): documents, research, data, code changes, releases, directories and workflow runs; a
plan as an artifact that follows its plan; requests to the user (an approval, a choice, a text) and every way
one ends; the inbox in the order the apps show it. An agent keeps its artifacts in its own log. The Host
serves them as the Python Host does (`library.rs`: `/artifacts`, `/pinned-artifacts`, `/decisions`,
`/attention`, the events `artifact-updated` and `attention-updated`), tells an agent its answer as a message
written in one append with the mark that it was told, and gives the agent its tool (`clarp-agent-artifacts`,
`artifact_tool.rs`). Four conformance scenarios hold the routes to the Python Host. Not there: countdowns and
podcast episodes (each says it is not available on this Host yet). Recordings, films, files, reports, forms
and the paged list the apps call Updates (`GET /attention/inbox`, `artifacts/updates.rs`: what failed first,
then what is ready, then what is in the making, with a cursor that holds as long as nothing shown has
changed; 54 recorded calls and the scenario `updates`) came later, below.

**The user is told, also when the phone is in a pocket** (15 Nov 2026). Who is told when a turn ends is the
Python Host's rule (`clarp-kernel/src/notify.rs`, 86,016 recorded combinations): a reply for the user tells
the user; what agents say to each other and what a schedule began tells nobody; a user who asked at the desk
is not paged. A phone registers the token its push service gave it (`devices.rs`, 49 recorded calls;
`POST`/`DELETE /devices`, `GET /devices/push-tokens`): a token belongs to the paired device that registered
it. A push is handed to the relay (`relay.rs`, Clarp Audio Central) by the phone's own grant, for which the
Host needs no key and no account, or by the Host's credential there; what is sent is the Python Host's
payload, field for field (`push.rs`, 341 recorded payloads). A request an agent makes is pushed when it is
made; while a desktop app says its user sits in front of it (`presence.rs`, `POST /desktop-presence`), no
phone is paged; a token the push service calls dead is not sent to again. A Host pushes nothing until it is
given a relay (`--push-relay`), which `tools/install-host` does. Held end to end by a Host with a paired
phone and a stand-in relay. Not there: Android phones (FCM), Live Activities, the silent push that asks an
app to sync, an agent's portrait in a push.

**Agents write to each other, also across Hosts** (15 Nov 2026). The skill tells an agent to hand work to
another with `clarp-admin prompt --to SESSION --from ME`; on this Host the same command is answered by the Host
(`admin_tool.rs`). With `--server NAME` the message goes to an agent of another Host the owner named
(`peers.rs`, `clarp-host peer add NAME URL`, the Python Host's scheme): that Host's own `POST /send`. This is
what lets agents move one by one: an agent that lives here still reaches the ones on the Python Host beside
it, and they reach it once the Python Host has this one as a peer (`clarp-admin peers add`, the release
owner's to do at an install). A request that comes back is there too (`clarp-admin request` and `reply`,
`request_tool.rs`, the words held by 97 recorded cases): the agent that is asked is told how to answer, the
asker's goal waits for the result as a dependency with a deadline and its owner is woken with the answer, and
an update is a message, at most five to a request. Not there: a message for later (`--delay`), and a request
to an agent of another Host (the Python Host has none either).

**The user changes who an agent is** (15 Nov 2026). Its name (`POST /agent-rename`), whether what it says
pages (`/agent-mute`: marked unread, nobody paged, also when it asks something), whether it is put away
(`/agent-archive`), and which model it runs with how much effort (`/agent-llm`, from its next turn on). The
change is the fleet's (`fleet.rs`, `Amend`): one writer for who an agent is, and the agent's own thread has
the change before the request is answered. One agent has a name at a time, at a rename and when an agent is
made, as on the Python Host: who is written to by name is one agent. An agent that is put away keeps
everything and can still be written to; the apps read the snapshot without the ones put away
(`?include_archived=0`) and page those (`GET /agents/archived`, the Python Host's cursor byte for byte). An
agent reads the roster and tunes itself with `clarp-admin sessions` and `clarp-admin model` (the skill
`clarp-sessions`). The rules are `clarp-kernel/src/roster.rs`; held to the Python Host by the scenario
`roster-edits`. Not there: the list of models a tool offers (any name is taken, and the tool says at its next
turn whether it has it), an agent's voice, portrait and MCP servers.

**Agents make agents** (15 Nov 2026). An agent is made with the agent that makes it and a role (`POST /agents`
with `parent` and `role`, answered as the Python Host answers). A helper's work has a state
(`clarp-kernel/src/helpers.rs`, 72 recorded moves): it is working; it has reported when it writes to its maker
or answers its request, or sat idle for half an hour; it has more work when its maker writes back; its maker or
the user marks it done, failed or working again (`/agent-helper-state`); it is abandoned when its maker is
removed or put away, and nothing is ever removed with its maker; a helper that has been done for a day is put
away. The lineage is the fleet's, like every fact about who an agent is. A maker's row counts what it made
(`child_count`, `running_children`, `background_jobs.sub_agents`), shows it as at work while a helper or a
process of its own is, and says in a line what they are doing; a job that only mirrors a helper is not counted
twice. A helper's replies page nobody: it reports to its maker. An agent makes one with `clarp-admin agent
create` and follows it with `clarp-admin agent helper-state`: the two commands the program `clarp-sub-agent`
is built on. Held to the Python Host by the scenario `helpers`. Not there: a helper that starts from its
maker's conversation (`agent fork`), and the program `clarp-sub-agent` itself on this Host's path.

**A turn that fails says why, as the Python Host says it** (15 Nov 2026). How a turn ended is read off the
tool's own words into a kind (`clarp-kernel/src/failures.rs`, the Python Host's wordings, 668 texts): a
connection that dropped, an account that is out, a sign-in that ran out, a plan without the model, a tool
that died. The kind is kept with the turn's end in the log as it was read when it happened: a log is replayed,
never read again, because wordings change and what happened does not. The user is told in the Python Host's
words (`failures::told`, 764 tellings): the agent's state carries the kind, the sentence for it, the tool's
and the provider's words, and that the message is kept; under the message that caused it the Host says why
in a row of its own (`marker-<message>`); and the turn's end is told on the last thing the turn itself put
into the conversation, which is the message when the turn said nothing. The gauge of how full the
conversation is, when a turn last ran to its end and which model answered last are kept through turns that
do not finish: a tool that dies says nothing. Held to the Python Host by the scenario `failed-turns`.

**A turn has attempts** (15 Nov 2026). A connection that drops under a turn, or a tool that stalls, does not
end the turn: the turn waits a moment and is tried again, three attempts in all, a second after the first
and two after the second (`clarp-kernel/src/recovery.rs`, the Python Host's rule). Between two attempts a
turn is in a state of its own in the kernel (`Parked`): it holds the agent like a turn that runs, and has no
tool process, so a Stop ends it there and then, and what its last attempt's shim still says is late news.
Every attempt has a spool, a socket and an interpreter of its own (`7.jsonl`, then `7.2.jsonl`); what a
shim's follower reports names its attempt. The next attempt is given the same message and is started on the
session the tool named before. The wait is in the log: a Host that is killed while a turn waits takes the
wait up, and a crash at any step of it leaves the agent where it was (`journal.rs`, the crash script). While
it waits the apps are told that the turn reconnects, as busy as before; when the last attempt fails too, the
turn ends on that failure and says after how many attempts. A turn that is tried once more and then works
is held to the Python Host by the scenario `retried-turns`. A turn whose connection drops every time is held
by this Host's own test: the reference did not end such a turn within the fifteen seconds the harness waits,
in four recordings of four (runs 43741865162 and 43744222881), and a scenario the reference does not finish
cannot be in the corpus. That is told to the release owner as something seen, not as a defect that is read.

**A turn whose account is out waits, where someone else changes the login** (15 Nov 2026). Two Hosts on one
machine run their tools with the same logins. The Python Host's selector puts another account in place when
one is out, and stops its own running turns before it does. A second Host that did the same would change the
login under the first one's turns. So the Rust Host has a mode for a machine it shares (`--accounts follow`,
which `tools/install-host` chooses when the Python Host there has a selector): it never changes a login and
never stops a turn for another turn's account. A turn that fails on a limit that was reached or a sign-in
that ran out waits (`Wait::Account` in the kernel, the same `Parked` state): it keeps its place, and the
apps are told what it waits for in the Python Host's words. It is tried again when the tool's login has
changed (`logins.rs` looks at the file each tool keeps its login in, keeps a digest of it in memory and
nothing else), and now and then meanwhile: after a minute, then twice as long each time, eight minutes at
most. After half an hour in all it ends, and says that the account is out. The tool, which has the turn in
its own session, is not given the message again: it is told that the turn is taken up, in the Python Host's
words and envelope. Without the mode (`--accounts none`, a Host whose tools have one login) a turn whose
account is out ends at once and says so, which is what the Python Host does without a selector and what
the scenario `failed-turns` holds. Not there: a selector of this Host's own, for a machine where it is the
only Host. Not held by a scenario: what the apps are told while a turn waits for an account is read from
the Python Host's code (`activity.py`, `live_core.py`, `turn_launcher._pause_for_account`), not recorded.

**The account pool, for a Host that is alone on its machine** (17 Nov 2026). Where no Python Host runs beside it, nobody changes the login when an account is out: the Rust
Host has to run the owner's selector itself. What that takes is the Python Host's coordinator
(`server/lib/account_failover.py`, 785 lines, and the plan it asks, `policies/failover.py`), whose every rule
was paid for by an incident there: the selector changes the login the whole machine uses, so every running
turn of the tool is stopped first (a tool that runs writes the old account back over the new login when it
refreshes its token) and taken up after; a selector that gives no answer has said nothing about the
accounts; a turn waits half an hour at most and then runs and fails where its user sees it; an answer for
some models takes up those turns and leaves the others waiting while the pool works, one of which, let go
at its own half hour, is the trial for the rest; a turn that meets the limit again soon after it was taken
up is asked for later each time (one, two, four, eight minutes) and ends at the fifth; a plan that does not
offer a model stops nobody else and ends its turn after three answers without that model; a recovery that
was under way when a Host stopped is taken up by the one that starts, with the time it began. Here it is a
machine that does nothing by itself (`clarp-kernel` `accounts.rs`, `Pool`): it is told what happened (a
turn is about to start, a turn failed on its account, a timer came due, a turn has stopped, the selector
answered, a turn was stopped by its user or is over, the login was changed by someone else) and answers
what is to be done (pause, hold, stop, take up, end, ask the selector for these models, tell me again
after so long). It is held to the Python coordinator itself: `tools/account-pool-vectors.py` drives that
code through 37 made-up situations (431 steps, 457 things done) with a clock, a selector and timers the
script holds, and the pool has to answer and do the same at every step, in the same order, and stand the
same afterwards; the plan by itself over 1,080 cases; what a selector printed and how it ended over 30
(the selector there is a real process). One thing differs by nature and has tests of its own: the Python
coordinator stops a turn in place and goes on, and this pool asks for the stop and is told when it is
done. A pass that waits so goes on when the turn stops, ends by itself, is stopped by its user or is
replaced by its agent's next turn, and gives the wait up after half a minute (the others wait on for a
later pass, and are let go at their half hour all the same). That the tests can fail was tried: 23 wrong
rules were planted into the pool one at a time. The first round missed one (a trial whose turn its user
had stopped proved its model), which was a situation nobody had written; with it written, all 23 are
caught.

The Host uses it in a third mode (`--accounts select`, beside `none` and `follow`). The owner's selectors
are read from the owner's file (`--speech-keys`), under the Python Host's own names (`[agents]
claude_account_switch_command`, `codex_account_switch_command`); a tool without one has no pool, and a
turn of it that fails on its account ends and says so. For a tool that has one (`clarp-host` `pool.rs`):
before an attempt of a turn is launched the pool is asked whether it has to wait, and one that does is
not launched (the launch is held by the agent's thread and carried out when the pool takes the turn up;
the agent's row and state say that it waits for an account meanwhile); an attempt that fails on a limit
or a sign-in asks the pool whether the turn waits (`recovery::Next::Pool`), and waits like one that waits
where someone else changes the login; a running turn the pool wants stopped is stopped without being
ended (`Actor::stop_for_an_account`: its attempt is over, the turn is parked for an account, also when
its process went without a word in its spool), and the pool is told when nothing of it runs; a turn the
pool takes up is tried again, and the tool is told so in the Python Host's envelope; a turn that is over
or that its user stopped is taken out of the pool. The selector is run as a program of its own, without
a shell, in a group of its own, given the models on its standard input as the Python Host gives them,
for five minutes at most, after which it is ended with whatever it started; of what it prints nothing is
kept and nothing is logged, only what kind of answer it was. A turn that waited for an account when the
Host stopped is handed back to the pool when the Host starts, with the time its wait began; a turn that
ran on while the Host was down is the pool's again when it is found, and is stopped for its account if a
recovery is under way; the turn of an agent that is removed is let go. Held through
the Host with a stand-in tool and a stand-in selector
(`a_host_that_runs_the_selector_stops_what_runs_asks_it_once_and_takes_the_turns_up`): a slow turn of one
agent runs when another agent's turn meets the limit; the selector is asked once, for the models of
both, and no process of the tool ran while it did; both turns are taken up, each told so and neither
given its message a second time, and neither conversation says that anything failed; a limit met again
soon after is not asked for at once (the relapse rule); a turn of a third agent that is to start
meanwhile waits and says so; and a stop ends a waiting turn, also one that never began. That this test
can fail was tried with six wrong behaviours planted into the Host one at a time. `tools/install-host`
takes `--accounts select`, never chooses it by itself, and refuses it while the Python Host of the
machine runs with a selector: the logins are the machine's, and two Hosts must not both change them.
A plan that does not offer a turn's model is the pool's to say as well (`recovery::Next::Pool { plan }`):
where other turns of the tool run, the turn ends at once with what the provider said, nothing is stopped
and the selector is not asked; where nothing else runs, the selector is asked, since another account's
plan may offer the model, and the turn is taken up when one does; after three answers without the model
the pool ends the turn, and it ends with what the provider said and is not run again
(`Actor::end_waiting`). Held through the Host for the first two
(`a_plan_that_lacks_the_model_is_asked_of_the_selector_only_when_nothing_else_runs`) and at the actor for
the third, since three answers of a selector are two minutes apart
(`a_turn_the_pool_holds_for_its_plan_waits_and_ends_with_what_the_provider_said`, beside
`a_turn_that_is_stopped_for_its_account_waits_and_is_taken_up`; four faults planted into the actor were
each caught). A turn that waits for an account that serves its model, while the account now active serves
the other turns, says so in the Python Host's other wording ("Waiting for a claude account that serves
beta; the active one does not right now"), also a turn of that model that is to start meanwhile; held
through the Host with a selector that serves one model of two
(`a_selector_that_serves_one_model_takes_those_turns_up_and_the_others_say_what_they_wait_for`; two faults
planted against it were caught). Not there yet: the pool is not told when someone else changes the login
(`Pool::served`: a login file that changed says nothing of which models the new account serves, and a
tool that refreshes its token changes the file too; whoever changes the login knows, which will be the
desk of seats).

The reference was asked the same (17 Nov 2026, scenario `the-selector`): both Hosts are given an owner's
file that names a selector (a stand-in that says an account serves, `conformance/fake_selector.py`), and
the stand-in tool's account is out for the first process that is given the request and not for the next
(`[qa-limit:1]`). The Python Host ran it to its end in every recording, the same way each time, and the
Rust Host answered as it does in every body. Two things the Python Host told the apps and this Host did
not: when the turn was taken up, that the agent thinks again (this Host had told that the turn waits,
and nothing when the wait was over), and the send's own event once more. Both are told now, where a turn
that waited for an account is taken up, in either mode; with that the scenario conforms, with nothing
registered for it beyond two defects of the reference told by their shape (R6, R12).

**Who may ask has three answers** (15 Nov 2026). The administrator, with the Host's own token, from the
Host's machine. A paired device, with a token of its own and a scope. And an agent's own tools, with the token
their turn was given (`CLARP_TOOL_TOKEN`, `clarp_host::tool_token`): it follows from the administrator's token,
so a turn that went on while the Host was started again still reaches its Host; it does not give the
administrator's token away; and it opens `/tools/` and nothing else. What the Host starts never inherits the
Host's own secrets (`OWN_SECRETS`: the administrator's token, the credential at the push relay, the tokens of a
move and of a peer): a turn's environment is read by every command an agent runs, and what an agent prints is in
its conversation. The first install found it: until then a turn inherited the Host's environment whole. This is
not a wall between an agent and the administrator, who are the same user of the machine: the file the installer
writes for a worker that no turn told where its Host is (`~/.config/clarp-host/config.toml`) holds the
administrator's token, as the Python Host's file does.

A managed Claude turn carries `CLARP_RUST_MANAGED_TURN=1`. The Host clears its own secrets and
nine inherited identity names before applying a child's launch order. The names are
`CLAUDE_PWA_SESSION`, `CLARP_SESSION`, `CLARP_PROVIDER_TURN`, `CLARP_BACKGROUND_ORIGIN`,
`CLARP_RUST_MANAGED_TURN`, `CLARP_AGY_MANAGED_TURN`, `CLARP_BACKGROUND_WORKER_PID`,
`CLAUDE_CODE_SESSION_ID` and `CLARP_CODE_ROOT`. It also clears the Python store locations
`CLAUDE_PWA_DB`, `CLAUDE_PWA_LOG_DIR`, `CLARP_CACHE_DIR` and `CLARP_DATA_DIR`.
The Host takes `CLAUDE_PWA_CONFIG`, `CLARP_CONFIG_DIR` and `CLARP_SHARE_DIR` at startup and
explicitly supplies their configured values to its children. The first two let a tool find its
Host's configuration, and the third lets an installed wrapper find this Host's tools. A
one-off tool with a restricted environment still receives only the settings its order permits.
The Host process starts children through `clarp-child`, including the account selector, git
helpers, the speech decoder, the Gemini encoder and the transcription worker. The Host's
async and terminal adapters apply the same environment before adding launch-specific values.
The synchronous, asynchronous and PTY constructors use the same table. A Rust source test
walks every workspace crate, including the shim protocol library, and checks local dependency
coverage. The standalone shim binary launches its assigned tool. Its OpenCode module is
compiled into that binary alone. Both source exemptions are named with their reasons. The selector, a routing tool and
a terminal report their effective environments through stand-ins in the Host tests.
`tools/plants/one-door.py` exercises the table, caller overrides, the allowlist and the guard.

An enabled Python Host plugin can receive prompt, pre-tool,
post-tool, stop, pre-compact, notification and subagent-stop hooks from a Rust Claude turn.
When the Python registry resolves the same live or archived agent, those hooks can record
its turn and activity, show completion, and trigger notification classification. R111 is the
Python release owner's guard for the marker. Until the installed plugin honors it, an agent
moved here must no longer resolve on a Python Host whose plugin runs for every Claude process.

**What an agent publishes is there** (15 Nov 2026). An agent shows its user an image, or hands over a
recording or a document, by publishing a file: its tool sends the bytes, the Host decides what they are by
their first bytes and not by what the agent calls them, and takes only what a phone can show or play. The rules
are the Python Host's (`server/lib/media_store.py`), in `clarp_kernel::media`, held by 1,550 readings of made-up
files, 240 of `Range` headers and 33 of names written with `%XX`. A published file is two things, kept in this
order. Its bytes are kept whole under a name made of what they are (their SHA-256 and the extension of their
kind), so the same file published twice is kept once; that is a new port, `clarp_ports::blobs`, with one suite
and two stores that pass it (files on disk, memory), chosen with `--blobs`. What is known of it is one record
in the log of the agent that published it (kind 600): the agent's history is its log, a gallery is read off the
agent's own book, and what moves with an agent later is its log and the files its log names. A record never
names bytes that are not there; bytes without a record are in nobody's way. The routes answer as the Python
Host's do: `POST /media` (the file as the body, what it is called in the headers), `GET /media?session=` (the
agent's images, the newest first), `GET /media/<id>` (the bytes, whole or the part a `Range` asks for, since a
player asks for a recording in parts, with the headers that keep a browser from running anything in them),
read and sent a quarter of a megabyte at a time whatever the file's size. Publishing has a second door,
`POST /tools/media`, which the token of an agent's tools opens; `/media` stays the administrator's. The tool
is the repository's own `scripts/clarp-media-publish.py`, the program the skills were written against,
installed beside the Host: in a turn it is told where the door for tools is and given that token; a worker an
agent left running finds its Host in the file the installer wrote. It is not rewritten as a pipe of words
because a pipe of words does not carry a file. Held end to end with that program (`tests/host.rs`, an agent
publishes a file) and by the scenario `media`. A recording, a film and a file are artifacts that name a
published file, which is what the skills for audio, video and files make after they publish: the artifact
must name one of its agent's own files, where this Host serves it, as the kind and under the name the file
has (`artifacts.rs`, the Python Host's rule and words, 45 recorded calls with the files published through the
Python store itself). The rule reads the agent's own book of published files, on the agent's thread: nothing
is asked of another agent, and another agent's file is not found.

**An agent hands its user a report** (15 Nov 2026). The largest kind of artifact that was missing: on the live
Host, of the last week's 34 artifacts of the kind `html_form`, 30 were reports (a page of HTML that takes no
answers) and 4 were forms. A report is published with `POST /artifacts`; under the id it has it is revised and
must say the version it follows, and what it was stays on record under its version
(`server/lib/html_forms.py`, `html_report_revisions.py`; in the kernel `artifacts/reports.rs`, held by 116
vectors of the pure rules and 71 recorded calls of a report's whole life). What a page may reach is a list of
exact HTTPS origins, read strictly, which becomes the policy a browser is given; nothing the page says can
widen it. The page itself is 136 KB on average and the same from one change of its artifact to the next, so it
is not in what the log keeps of the artifact and not held in memory: it is kept in the store of files under its
digest, before the artifact is written, and the artifact names it. The rules see a page when it comes in;
afterwards they compare pages by their names. What the Host answers has the page put back; a summary names it
instead, with the Python Host's marker, which is exactly that digest and length. Served:
`GET /artifacts/<id>/html` (the page behind its policy; not for a device that may only read, and not with a
token in the address, which the page's own scripts could read) and `GET /artifacts/<id>/revisions`. The
agent's tool has its verbs: `clarp-agent-artifacts create-report` (the Host reads the page where the tool was
called; published again under its id, the tool finds the version the new one follows) and `report-history`.
Held end to end (`tests/host.rs`, an agent hands its user a report) and by the scenario `reports`. Not there
yet: countdowns and podcast episodes.

**Countdowns in Updates** (19 Nov 2026). A countdown names an offset timestamp and an IANA
time zone. Each page of Updates uses one clock reading. Ready and completed countdowns are
upcoming until their target, due for the first minute, then elapsed. Each stays for review under
the reference’s default policy. Failed countdowns need correction, active ones are working, and
draft, cancelled and expired countdowns stay out of Updates. Projection leaves the stored status
alone.

**A form’s events** (19 Nov 2026). The Host accepts 1–32 telemetry records per batch from a
full-access client. Each event object fits in 16 KiB of compact UTF-8 JSON. The immutable log
has no per-form quota. One new event reads three keys and commits four records in one fleet
transaction: the global counter, the form’s tail, its event-ID receipt and the event at its
ordinal. A retry compares the event with its receipt. Pages seek within one form and carry a
frozen upper sequence. The artifact tool can export that snapshot as private JSONL or set the
draft key. Recording telemetry never submits answers or starts a turn.

**An agent asks its user with a form** (15 Nov 2026). The last kind of artifact agents made in the measured
week that this Host refused: a page that asks for answers, with a JSON Schema that says what an answer is. The
Python Host asks a library whether the schema is one and whether answers fit, and tells the user the
library's own sentence. This Host reads the part of JSON Schema forms are written with itself
(`artifacts/answers.rs`: `type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `const`,
lengths and bounds) and says the sentence the library says first, by asking a schema's words in the order they
are written: 1,423 recorded pairs of a schema and answers, and 222 values as Python shows and writes them
(`python.rs`), since a sentence names the value that does not fit. A word the library reads and this Host does
not is refused by name when the form is made, so no form is taken whose answers would be judged otherwise; a
word that means nothing to the library means nothing here. The choice costs the forms that use the rest of
JSON Schema (`pattern`, `oneOf` and the like: none of the four of the measured week) and buys a Host without a
schema library and its dependencies. A form is made once and never changed; its page is kept beside the log
like a report's. A user's answers come under an id the app chose (`POST /artifacts/<id>/submit`): they are
held to the schema, kept on the form as a submission, and answered with a receipt; the same answers under the
same id are answered the same, on the whole Host, and anything else under a used id is refused. What the
form's agent is told is written into its log together with the mark that it was told, as what its user said
(`u-html-form-<id>`), so it is told once, also when the Host is killed in between. The rules and their order
are the Python Host's (`server/lib/html_forms.py`), held by 71 recorded calls of a form's whole life, end to
end (`tests/host.rs`, an agent asks its user with a form), and by the scenario `forms`. The vectors found a
fault that had nothing to do with forms: a fraction of seventeen digits could be read one step off
(`9.999999900000001e-05` as `9.9999999e-05`), so a number an agent published could come back as another;
fractions are read exactly now (`float_roundtrip`). The submission’s audio preference follows the queued message onto its own turn.
The ordinary output pipeline voices the agent’s marked reply and announces its clip with the
turn’s trace. Resending the submission only returns the receipt.

**What an app asks of any Host** (16 Nov 2026). The load was counted on the live Host's own log: in three
days its clients made 147,704 requests over 469 routes, and 69.3 per cent of them went to routes this Host
had. The phone app gates few of its calls on what a Host lists (read in its source: dreaming, the car
controller's actions, push grants, the archived list, live items and activities, one provider of speech, the
home terminal); everything else it asks of every Host it is paired with. So a phone paired with this Host
would have been answered "no such route" three times in ten. What it asks regardless is answered now
(`apps.rs`). With what is so, where this Host keeps it: `GET /status` (whether the Host is up and an agent
is in a turn), `POST /focus` (which agent the user is looking at: the focus the Host keeps already),
`POST /application-activity` (whether the app is in front and when it was last touched: held for 45 seconds
by the Python Host's rules, `activity.rs`), `POST /upload` (a file a user attaches to what they say: kept
under the agent's name, and once under an id the app gives; the names on the disk are the Python Host's,
held by 259 recorded names), `POST /clog` (the lines of the app's own log, which an app keeps and sends again
until a Host takes them: 26,442 calls in the three days; kept in one file that does not grow without end,
because they say why something did not work on a phone). And with a list that is empty, in the Python Host's
own fields, where this Host keeps nothing of the kind: teams, janitors, what the Host does by itself, what
transcribes, the voices, the catalogue of models. Held end to end (`tests/host.rs`) and by the scenario
`app-asks`, whose first run wrote down what the Python Host answers before any of this was written. Still
asked and not there: speech itself (`/transcribe`, the clips of a spoken reply), portraits, and the screens of
settings (usage, logins, updates).

**A message of the agent is one row, with a card for each tool it calls** (16 Nov 2026). The Python Host
shows a turn's tool work as cards on the rows of the agent's messages; this Host had a row of its own for every
use of a tool, with what the tool was given and gave back, whole. Now a conversation is rows of four kinds: a
message to the agent, a message of the agent, a word of the Host, a row an agent brought along. **A row's item
is written once and stands where the row stands. What happens to a row afterwards is a part that names it**
(`Entry::part_of`): the use of a tool its message calls (written when it starts and when it ends), that tool
work came after it in the same turn, how its turn ended. Three things follow, and each was wrong or costly
before. A page is counted in rows (a tool's use was two items and a page of fifty held thirty-eight rows). The
place of a row is found by its id, whatever happened to it since. And whoever asks what is newer is given a
row again whenever a part of it was written, as it is now: a row is read with its parts (`conversation.rs` in
`clarp-actor`), and carries the revision of the newest. The Claude reader tells a message when it is whole (it
stops, another begins, one of its calls is answered, the process ends) and its tools' uses after it; a message
that only calls tools is a row that says nothing. What was said before tool work was said along the way
(`phase: commentary`), what is said after it is the agent's final word, and a row that says nothing has no
phase: the Python Host's rule (`message_writes._row_phases`), written by the actor when the work comes, from
what the log says the turn has said, so that an attempt before or a restart in between loses nothing. A card
is the Python Host's (`tools::card`, held by 433 recorded cards and 16 results): the tool's name, what was
asked of it in the words of its kind, `recorded` while it runs, `ok` or `error` with the first 300 signs of
what came back, and `interrupted` when its turn was interrupted under it, which the Host writes, not each
tool's reader. What a tool was given whole and gave back whole stays on the Host. The short form an app asks
for (`include_tool_details=0`) and the details of one row (`/message-tool-details`) are made of the same
cards. With this every row a turn writes carries the turn's trace, and the feature `log_turn_summary` is
listed. Held by the scenario `tool-turns` (five messages with six uses: the first turn is the same on both
Hosts in every field), by the actor's own tests with Claude Code's stream (a restart in the middle of a
turn, a kill with a tool running) and by a recorded turn of the real CLI. Still rows by themselves: the calls
of a tool whose reader does not group them into messages (Codex, until its cells are built) and every call a
log already holds.

**A person speaks, and the Host turns the clip into text** (16 Nov 2026). The owner speaks to his agents:
on the live elitebook Host, in seven days, 198 clips were turned into text (127 by the Host's own engine, 71 by
ElevenLabs Scribe) and a fifth of what the user said was said aloud. An app records a clip and hands it to
`POST /transcribe`; the Host answers with the text and the trace the message will travel under, and the app
sends that text itself. The request and its answer are the Python Host's, rule for rule (`clarp-kernel`
`heard.rs`, held by 500 recorded answers of that Host's own functions): what a job may be called, when two
requests are the same job, which engine a clip goes to, which of an engine's pieces are kept, what an engine
invents on silence and is thrown away, the refusals in the same words. **A request that comes again is
answered with what was answered before**: the answer is kept under the job's name for thirty days, beside
the Host's data, and no engine runs twice for one thing said, also across a restart. Nothing of a clip is
kept. **The engines are behind one port** (`clarp-speech`, `listen.rs`: a clip in, pieces of text out, or
why not), with two behind it. One is reached over the network (ElevenLabs Scribe, `scribe.rs`: the Python
Host's request). The other is the Host's own (`whisper.rs`): a worker process that keeps a Whisper model
loaded and hears one clip at a time, on the Python Host's own protocol of lines, so that the model is asked
exactly as that Host asks it. It is a process of its own because hearing a long clip is minutes of processor
time and the model's library is not this Host's code; a worker that says why it failed stays, one that dies
is started again by the next clip, and none is left behind. The worker is this Host's own program
(`tools/whisper-worker.py`); what runs it is a Python that has the library, which on the owner's machines is
the Python Host's own, used where it is with the model's files that machine has already (`tools/install-host`
finds both; measured on elitebook: faster-whisper 1.2.1, the model `small.en`). That Python lives in a
directory the Python Host makes anew when it is updated, so the installer is run again after such an update,
as it is after that Host changed its tools; until then the engine says that its worker could not be started,
and a clip is refused with those words. **A key is read where the
owner keeps it**: the key of an engine over the network is looked up in the Python Host's own config each
time a clip is to be heard (`--speech-keys`), so a key that is changed there is the key used next, and no key
is copied into this Host or kept by it. The apps are told which engines there are and which one hears a clip
nobody named one for (`/transcription-capabilities`, `/transcription-providers`). Held end to end with a
stand-in worker and a stand-in for ElevenLabs on the loopback (`tests/host.rs`: no test reaches an engine
outside), by the scenario `speech-in`, and once by hand with the real model on a made-up clip. **A clip is
for the agent its user is looking at, and that agent can have a language and engines of its own**
(`clarp-kernel` `languages.rs`, the Python Host's `stt_language.py` by its recorded answers): one language or
up to three the speaker moves between, and engines in the order they are tried. The first engine hears the
clip; the next one only when the one before failed, and only the last one's failure is the answer, so one
provider's outage does not lose what was said. This is in use: on the owner's elitebook two agents are spoken
to in Norwegian through ElevenLabs Scribe with Google's Chirp after it, and when Scribe's account ran out of
credits on 15 Nov 2026 the Python Host went on with Chirp without a word. So Chirp is the third engine behind
the port (`chirp.rs`): it speaks as the owner's own account at Google, from the file `gcloud` wrote, read
where it is; a short-lived token is bought with it and kept in memory while it lasts; a clip is decoded once
and a long one sent in pieces. The owner makes these settings from an app (`POST /transcription-providers`,
by that Host's rules and in its words), and they are kept with the fleet (`Directory.settings`). Not there:
the vocabulary an engine is asked to listen for beyond what this Host's agents are called, the judge that
tells an invented "Okay." from one that was said (the list throws both away), a stronger engine for long
recordings, Cartesia as an engine that listens (Deepgram's came on 18 Nov), the record of each thing said (`/voice-events`),
and calls.

**An agent says something aloud, and the Host makes a clip of it** (16 Nov 2026). A reply marks what is for
the ear (`<speak>`), and only that is spoken, when the message it answers asked for it: what a person sends
is answered aloud unless the app says otherwise, anything else only when it says so. Each marked part, in
pieces an engine takes at once, is a **line**, and a line is written into the agent's log in the same append
as its reply (`clarp-kernel` `spoken.rs`): a line waits to be spoken exactly when its reply is in the
conversation, and a Host that is started again finds in its logs what it still owes the ear. A line goes one
way: it waits, it is taken, it has a clip, the clip is whole or the engine failed and says why. One line of an
agent at a time, in the order they were said, and four agents at once (`clarp-host` `voice.rs`). **Who speaks
is the owner's choice, read where he keeps it**: the engine for every agent and the one tried after it, an
entry for a single agent, the voices of his personas, the keys, all from the Python Host's own config each
time a line is to be said (`owner.rs`, `--speech-keys`; the rules are that Host's, `said.rs`, held by its own
functions' recorded answers). Two engines are behind the port (`clarp-speech` `speak.rs`): Gemini through
either of Google's doors, its sound piped through an encoder as it comes, and Cartesia. When the first cannot
begin, the next does; **what was begun in one voice is never begun again in another**. An engine that is not
built here, has no voice for the agent or has no key is passed over before anything is announced, and a line
nothing can say is over without a clip: no app waits for sound that never comes. **The clip is announced
before its first byte is there** (the event `audio`, with where it is fetched as a file, as a stream and
whole), so a listener hears the beginning of a long line while its end is still being made. A clip is a file
under the Host's data (`audio/<milliseconds>__<session>.mp3`, readable by its owner alone), and its number is
that moment: no two clips of a Host have the same, also across restarts. What lasts is in the agent's log
(the line, its clip, whether it is whole, what an app said of playing it); what the Host holds in memory is
only how far a growing clip has come, and the routes find a clip in what the agents publish of their logs,
without asking an agent. The routes are the Python Host's (`/audio/<name>`, `/clips/<n>/stream` in its three
shapes, `/clips/<n>/complete.mp3`, `/clips/ack`, `/clips/recoverable`, `/clips/message`): a stream for a
player that takes one, the part that was asked for as far as the clip has come with its length told as not
yet known, the file once it is whole. A user's Stop takes back what still waits; what is being said is said
to its end, as on that Host. After a restart, a line that was being said a moment ago waits again, one begun
long before is over, and one that has waited more than half an hour for a Host that was away is not spoken
any more. Clips are kept a week, 256 MiB at most, to be played again. Held end to end with a stand-in for
Cartesia on the loopback (`tests/host.rs`: the clip fetched while it grows, an engine that is down, a Host
killed and started again; no test reaches an engine outside). The corpus holds less of it than of speech in:
the reference speaks what a Claude agent says from the transcript Claude Code writes, the harness's fake
Claude writes none, and so the reference says nothing aloud there. The scenario `speech-out` compares what
both Hosts answer when nothing was said (no clip missed, what an app may not say of a clip, what is no clip);
what a clip is and how it is fetched is this Host's reading of `clip_stream.py`, not a recorded answer. Not
there:
ElevenLabs, Google's Chirp voices, a local voice and custom adapters as engines that speak; the raw and HLS
ways of delivering a clip; the herald that decides which of several agents' clips is played first; the record
of each thing said (`/voice-events` is taken and not kept); and calls. **What an app's voice picker reads is
the Python Host's catalogue** (`GET /voice-catalog`; `clarp-kernel` `voices.rs`, held by seven answers of that
Host's own function, recorded in rooms of their own by `tools/catalog-vectors.py`): the engines that speak,
whether each can be used, each voice with who has it and whether it is this agent's. Its lists of voices are
carried as data; Cartesia's is Cartesia's own, asked for with the owner's key and kept a quarter of an hour.
One thing is this Host's: an engine it has not built is told as one that cannot be used here. An agent is
given a voice with `POST /agent-voice`, kept with the agent; a voice another agent has is refused. A preview
of a voice is made once by its own engine and kept.

**A turn is shown as it runs** (16 Nov 2026). Counted on the owner's Host over a day, `GET /live` was asked
424 times and this Host had no answer: the apps build their status line, their tool rows and their thinking
rows on the open turn as items that appear, grow and settle, sent as small operations (`docs/live-items.md`).
`clarp-kernel` `live.rs` has the three things it takes, none with I/O or a clock: the reducer every client
runs (`View`), one agent's hub (`Hub`: what is seen of a turn becomes events numbered one by one, growth of a
text and of a tool's output held back for a moment, and every operation it sends applied to its own `View`,
so that the snapshot is by construction what a client that saw every event holds), and what a backend saw
of a turn in words every backend can say it in (`Glimpse`). The count of an edit's lines is Python's
`difflib`, ported by hand (`difflib.rs`): another matching counts another diff. All of it is held to the
Python Host by `tests/fixtures/live-vectors.json` (`tools/live-vectors.py`): the seven recorded streams of the
contract, nineteen scripted turns through the Python hub with a clock and a timer moved by hand, six of them
random walks, 668 events compared one by one, and the titles, previews, tails, edits and diffs beside them.
A planted fault was caught before the vectors were trusted. Claude Code's stream becomes glimpses in
`clarp-backend` (every block of a message an item of its own, a tool from the moment its name is known, what
each tool returned), held to the Python Host's reader by four recorded streams and checked on two turns
recorded from the real Claude Code. The agent's actor owns the hub, and what is shown follows the log: a turn
that holds the agent is begun for those who watch, one that holds it no longer is ended as it ended, one that
waits for an account says so. An item is stamped with the time of the record it came from. That is what a
restart needs: a Host that comes back in the middle of a turn builds the same items again from the turn's own
spool, under another `epoch`, which tells a client to ask for all of it anew. The Host sends the events to
the connections that asked (`/events?live=`): whole to one that follows the agent, the status line and the
turn alone to one that does not, nothing to one that did not ask; they have no number and are never kept.
`GET /live?session=` answers the whole view. The corpus has the scenario `live-items`: its stand-in for
Claude Code prints a turn piece by piece as the real one does (`[qa-stream]`), and the scenario is listened
to as an app that follows the agent. A Codex turn is shown from what `codex exec --json` says of each item
when it begins and when it is whole: a command from when it starts, with what it printed and the code it
ended with, what the model thought and said, and its plan. Not there: an explanation of a tool's use in
plain words (the setting answers off, and turning it on is refused in words); a plan from Claude Code's list
of tasks and the turn's diff as items of their own; and the pushes that keep the phone's "agents working"
activity current.

**An agent has a face, and an app is told who an agent can be** (16 Nov 2026). An agent's row says where its
picture is fetched (`avatar_url`): the one this Host ships for its name, else the one for its kind of tool; and,
for a name that has one, the picture drawn for the family of model it runs on (`model_avatar_url`). The rules
are the Python Host's (`clarp-kernel` `portraits.rs`, held by 338 faces, 1,140 faces on models and 33
addresses recorded from its own functions). A picture's address carries its content: `GET /static/avatars/...`
hands it to anybody, as the Python Host does, to be kept for a year under that address and checked each time
without it. The pictures are files beside the Host (`--static`); the installer copies the repository's own.
The snapshot lists the personas this Host has built in (`personas`: a name, its voices of three engines, how
it speaks, the kind of tool it belongs to) and offers their names before the agents' (`roster`). They are the
Python Host's lists, carried as data, with the owner's file adding to them by that Host's rules
(`personas.rs`, held by what the Python Host lists under three made-up owner's files, each asked in a room of
its own). Their order is this Host's own, the roster's. On the owner's Host that is 84 of his 93. The other
nine are the owner's own, and that is built too: he makes a persona (`POST /personas`), changes it
(`POST /personas/update`) and removes it (`DELETE /personas/<name>`), and it has a picture of its own
(`GET /persona-avatars/<id>`, with the token, as on the Python Host). A persona whose maker names no voice is
given the first voice of Cartesia's that no agent and no persona has. Of a persona that is built in only the
look can be changed. The rules are the kernel's (`personas.rs`, `Book`), held by 28 steps recorded from the
Python Host's own functions in two rooms: what it answers, what it refuses and in which words, what is made
or changed afterwards, and the voice the next persona would be given. The fleet keeps what the owner made
(tables `persona` and `persona-look`; a picture is kept once, under its digest, beside what agents publish)
and tells the apps (`agent-roster`: `persona-created`, `persona-updated`, `persona-deleted`). Not there: a
picture an agent chose itself (eleven of his 180 agents), the janitors' pictures, and the making of pictures.

**An agent's tool is told who the agent is, and how to speak** (16 Nov 2026). Found by reading for the
personas, not by a run: until this build the Host handed a tool the user's words and nothing else. An agent
here had no name to itself and no way of speaking, was never told to use the Host's own tools, and never
marked a part of an answer for the ear unless its user asked for the mark by hand, so a spoken turn stayed
silent. No test said so: the stand-in tools do not care who they are, and the check of speech out had asked
for the mark in the message. Now a tool is told three things, in the Python Host's own words
(`clarp-kernel` `told.rs`; the texts are carried as data, and held sign for sign to what that Host's own
functions say for nine agents and settings made up for it, each in a room of its own). Who the agent is: its
name and the way it speaks, which it has from the day it is made (asked for, else the persona's of its name)
and keeps under another name; as the tool's standing instruction (`--append-system-prompt` for Claude Code,
developer instructions for Codex). What holds for every turn: to use the Host's tools where they fit, and
whose turn this is. And for a turn whose answer is read aloud, how to speak: which part is heard, how human
it sounds at the agent's level, with tags of emotion only where the engine that speaks for the agent acts
them out, and how much it says while it works. The last two go with the prompt: for Claude Code after the
user's words in the same message, between the marks Claude Code puts around what a hook says (on the Python
Host a hook says it); for Codex before the user's words, between that Host's marks for it. It is worked out
when a message arrives (`clarp-host` `telling.rs`), from the agent, the owner's settings and the owner's
file as they are then, so a change holds for the next turn and nothing has to be started again. The owner's
settings for it are there: whether agents are told a way of speaking at all (`/personalities/settings`,
which the iOS app has a switch for), how human voices sound (`/voice-humanness`), how much an agent says
while it works (`/agent-voice-verbosity`). One thing was not copied: the Python Host never told Claude Code
the last of these (reference defect R21, mended there on 17 Nov 2026); here every tool is told. And when an app says that its user began to
speak before hearing what the agent last said aloud (`unheard_audio_sessions` of a send), the tool is told
so before the user's words, in that Host's words, while the conversation keeps the words as they were said.

**A spoken message finds its agent by its name** (16 Nov 2026). Found the same way, by reading what the
apps send: a message dictated hands-free carries `hands_free`, and unless it also says that it is for the
agent it names (`force_session`), the Python Host looks whom it is for. Until this build this Host gave
every message to the agent the app named, so "Rachel, check the branch", said while Mike was open, went to
Mike. Now it is as there with the router of spoken messages off, which is how that Host comes
(`clarp-kernel` `addressed.rs`, held to that Host's own functions for 94 messages, 35 pairs of words and 14
choices of where a message goes): the agent whose name the message begins with, within the first three words
after "hey", "ok" and the like, by the name as written or, for the first word, by one much like it ("Bell"
for Bella; the measure is Python's `difflib`, which the kernel already had for diffs); the address is taken
off; that agent is the open one from then on. Without a name the message stays with the agent that is open,
then with the one the app named. What was typed, or dictated for the open agent, is for the agent it names,
as before. The router that asks a model came on 17 Nov (below); off, which is how it comes, an app that asks
it (`/orchestrator/route-delegation`) is answered as by a Host whose router is off and asks its user. A name the message spells out is taken off
through its end where it ends as a word, all its words and its letters however they are composed, as the
Python Host does since its fixes of reference defects R23 and R27 (16 Nov 2026). One thing is not copied:
the word a name was found in is taken off whole, where the Python Host leaves the agent the end of a word
that spells the name another way or goes on after it.

**A phone says where it is** (16 Nov 2026). The first of the screens of what this Host had not that is
built instead of answered as nothing: the app says where its phone is for a session (`POST /location`), an
agent's tools read the last place said (`GET /location`), and an agent asks for a new one (`POST
/location/request`), which every listening app is told. It is asked a hundred times a week on the owner's
Python Host. The rules and their words are that Host's (`clarp-kernel` `located.rs`; the scenario `places`).
With it came a way to leave something small with the fleet under a key, in a table of its kind
(`FleetHandle::keep`, `kept`): one writer and one store as for everything else, without a new kind of
question to the fleet for each small thing an app or an agent leaves. What comes next of that sort (a
phone's tokens for calls and live activities) is kept the same way.

**An agent asks for an event in its user's calendar** (17 Nov 2026). Counted again on the owner's Python
Host, a week of its request log this time (`tools/count-load --days 7`): 79,413 requests that changed
something, over 100 routes, and eleven of them to six routes this Host had not. One is this: an agent's tool
names an event (`POST /calendar/request`: whose agent asks, a title, when it begins and ends, and what else
there is to say), the Host gives the request an id and tells the apps (`calendar-request`), and the app in
front asks its user and writes the event into the phone's own calendar. The Host keeps nothing of it and
reads no time. The rule is the Python Host's (`calendar_request.py`), held by what its own function makes of
fifty made-up requests (`tools/calendar-vectors.py`: what a request must say and the words it is refused
with, the zone under either of its two names, a number or a list where a word is expected written as Python
writes it, and `all_day` by what Python takes for something, so that the word "false" is a yes), by a Host
test and by the scenario `calendar` (11 steps, no difference at its first asking, run 44028268830). An
agent asks through a command, and so the commands are served too (`admin_tool.rs`): `clarp-admin calendar`,
and `clarp-admin location`, which gives the place the phone last said when it is new enough and otherwise
asks the phone and waits for one said since (the skill `location`; the route was there, the command was
not, so an agent here could not ask). Read in the owner's skills and the repository's: one script speaks to
a Host by itself and not through a command, the calendar skill's, which finds its Host by `CLARP_SERVER_URL`
and a token (`CLARP_AUTH_TOKEN`), and failing those by the Python Host's own settings. Left to itself it
would ask the Python Host of the machine for an agent of this one. So a turn of this Host is told this Host
under those two names, with the token of its agent's tools, and that token opens the calendar request beside
the tools and nothing else: held by the test of what a turn is given. The other ten of the eleven: an agent's slot given back by hand
(`POST /repair-slot`, five), and teams (three routes, three requests).

**The models an app's picker offers** (17 Nov 2026). The same week's log, read for what was asked and
not for what was done: among the screens this Host answered as having nothing, the one asked most was the
list of tools and models an app shows where an agent's model is chosen (`GET /agent-model-options`, 316
times), and with no tool listed there an app has nothing to choose from. Now the two tools this Host runs
are listed, each as the Python Host tells a tool (`provider_capabilities.py`): whether its program is on
the machine, the version it says, and its models. Codex is asked for them (`codex debug models`: the ones
it shows in a list, with the efforts it says each takes); of Claude Code the file is read in which it
keeps what the account that is signed in may run, since it has no command that lists them; and after what
a tool was seen to have come the models it is known to have anyway, each row saying where it is from. The
rules are in `clarp-backend` (`catalogue.rs`), pure, held by what the Python Host's own functions make of
eleven answers of Codex, eleven files of Claude Code and sixteen situations of a tool that is there, is not
there, or says nothing (`tools/model-catalogue-vectors.py`); what is declared of a tool (how it is shown,
the models it is known to have) is that Host's own table, written out by the same tool. The Host asks the
tools (`models.rs`): each has two and a half seconds, both are asked at once, and what was found is told
again for five minutes unless a login changed. Three things are this Host's own: it lists the tools it
runs and no other; what it cannot do with a tool yet it does not say it can, whatever is declared of the
tool; and it reads only what a tool prints, as the Python Host does where it runs for real.

**What the Host's own turns used** (17 Nov 2026). The usage screen (`GET /backend-usage`, asked 71 times
that week) was answered as unknown. One half of what the Python Host tells there needs nobody outside: what
each turn's tool said the turn took in and gave out, summed over the last five hours and the last seven days
(`turn_usage.py`); for Claude Code that is what its row is made of there. This Host wrote those numbers at
every turn's end already, in the agent's own log. Now it also counts them for the Host (`clarp-kernel`
`usage.rs`: the sums and the row an app is given, held by what the Python Host's accounting answers for 23
made-up turns asked at 13 moments, the edges of the windows to the millisecond among them): an agent's
thread says what a turn used when the turn ends, it is kept with the fleet under a name of the turn's own, so
that the turn a Host tells again when it starts is counted once, and what is older than seven days is let
go. The use belongs to the Host: an agent that is removed takes none of it along (a helper that did its work
and was removed has still spent what it spent). The other half is how much of an account is left, which the
Python Host asks the providers with the accounts' own logins: this Host asks nobody yet, and says that it
does not in the part of the answer that is for it. Codex's row is counted the same way here; the Python Host
has for it what Codex itself reports of its limits. What a turn cost is counted where the tool says so
(Claude Code does, to the millionth of a dollar here). The Python Host could not be asked for its usage
screen after a turn of the suite's stand-in: asked for it, that Host starts its tools to learn what is left
of the accounts, and with the suite's stand-ins for them its agent never came to rest again (run
44034994886; a scenario of fifteen steps, taken out of the corpus again). So this is held by what that
Host's own functions answer, and by this Host's tests, and by no scenario. Reading for this found a number this Host had wrong since the first turn it ran: what a turn
took in was counted as `input_tokens` alone, where the Python Host counts with it what the turn wrote to
the cache and read from it (`turn_stop._result_detail`), and a turn of Claude Code that goes on in a long
conversation reads nearly all of what it takes in from the cache. No scenario saw it, because the suite's
stand-in for Claude Code reports no cache; it is in a row's summary of its turn (`tokens_in`) as in the
tally. Now as there, held by the backend's own test. And one number hung on that one: how full an agent's
conversation is (`context_tokens`, the gauge an app shows) was taken from what the last turn took in. The
two are different things, as the Python Host's own note on it says: a turn's sum counts every step of the
turn anew, so a turn of thirty steps says thirty times what the conversation holds. How full it is, is what
the last answer was made from, which Claude Code says with every answer; that is now told by itself
(`Signal::Context`), kept with the turn's end, and what the gauge shows. A tool that says no such thing
has no gauge, as on the Python Host, which shows one for Claude Code alone: Codex keeps its conversation
within bounds by itself. This Host had shown what a Codex turn took in as its gauge. The whole corpus then
showed where the two Hosts part on the gauge (run 44033908756, and 44038730172 for the scenario alone):
after a turn that failed at the provider the Python Host shows 0. Claude Code writes the error where an
answer would be, as a message that used nothing, and that Host reads its zeros as the size of the
conversation (defect R38; in Claude Code's own transcripts on elitebook there are 8,256 such messages, each
with a usage of zero, 8,164 of them after an answer that held something). Here such a message says why the
turn failed and nothing of the conversation: the gauge stays what the last answer said. Three registered
differences, one cause. The Python Host was mended the same morning (pull request 402, two hours after
the report): asked again with the scenario alone (run 44058260247) the two Hosts show the same gauge, and
the three registrations are taken out.

One more number was wrong, and it was this Host's own: what a turn of Codex took in. `codex exec` says at
a turn's end what its whole thread has taken so far, not what the turn took (the two recordings of one
thread in the backend's fixtures: 87,566, then 131,412). The adapter now tells it as that (`Signal::Total`),
the turn's end keeps it with the thread it was said of, and the turn's own share is what the count grew by
since the end of the turn before in the same thread: nothing before a thread's first turn, and not known,
so not counted, where the thread was taken up or is older than this. Found on 17 Nov when the account the
tests ran on was used up: the usage screen said 61 million tokens for sixteen turns.

The Python Host counted both tools' turns from their running totals (defect R48) and mended it on 18 Nov
(its PR 413): a turn of Codex is the sum of its model calls, measured from where its first call says the turn
began; a turn of Antigravity is the sum of its steps, each step counted once by the first of its updates that
says what it took (an explicit nothing included), the result's own figure only where no step said anything.
Ported from that Host's own functions (`tools/turn-usage-vectors.py`): Antigravity's reader takes a step once
as there, drops a line told again word for word, reads the figures as `int()` reads them, and counts what was
read from the cache as what went in (29 streams); Codex's share is `usage::share`, held to 23 turns of 12
threads of that Host's client where its rule and this transport's mean the same.

**What is left of an account, as its tool says it** (17 Nov 2026). The other half of the usage screen is how
much of an account is left. The Python Host has it from Codex's app server as that runs, and from the
provider's usage endpoint, asked with the account's login. This Host runs Codex one process to a turn, and
`codex exec --json` prints nothing of limits. But Codex writes them into its conversation's record with every
count of tokens (`rate_limits`: each window with how much of it is used, how long it is and when it begins
anew), in the words of its endpoint. So when a turn of Codex ends, the newest such record of the turn's
rollout is read from the file's end (`clarp-host` `quota.rs`; in 37 conversations of elitebook it lay within
eight thousand bytes of the end), read as the Python Host reads one (`clarp-backend` `limits.rs`), and kept
and shown by that Host's rule (`clarp-kernel` `quota.rs`): a window of five hours and one of a week, fresh for
a quarter of an hour and until the window begins anew; a warning at four fifths of the five hours and a hard
limit, each told once; no longer known when the window begins anew or another login comes; over when the
account is back under the warning. Each of these is told to the apps as the event `provider-limit`, and what
is kept is shown in `GET /backend-usage`, with what this Host counted itself beside it. The rule is held to
the Python code's own answers over 33 made-up stories on four logins (146 steps, 83 askings, 39 limit
events); it matched at its first run, so 34 wrong rules were planted one at a time, and each was caught.
Through the Host it is held by a stand-in for Codex that writes a rollout: the windows are shown after a
turn; the warning and the hard limit are told once; a Host that is killed and started shows what it kept and
tells nothing again; another account's login shows nothing of the account before and ends what stood open of
it; and a limit whose window began anew is told as no longer known, to the app that asks and to those that
listen. That test too passed at its first run: 15 wrong rules planted into the Host's part were each caught.
Nobody is asked anything, so what is shown is as old as the last turn of Codex on this Host, and says so.

Whose account a record is of is the login Codex is on when the record is read. The login file is read for the
account and the token, and what is kept of them is a keyed digest (HMAC-SHA-256 under a key this Host draws
once), as the Python Host keeps one: neither is in what the Host keeps, and the test looks for them there. A
record written before the login file was last written is of the login before and is not taken; one that is
no newer than the newest kept was read before. Two things differ on purpose (`docs/differences.md`). The
tool's row says when its record was seen. And old windows are forgotten: a window is known by when it begins
anew, and Codex does not say that the same throughout (in 213 of 359 conversations of elitebook that held
more than one record it was said differently along the way; 4,516 of the 6,662 steps were of a minute or
less), so that most records are of another window; the Python Host on elitebook holds 755 rows of the week's
window from 48 days. That table also says what the owner's accounts have today: a week's window and no
five-hour one, and no limit was ever told there. So the warning and the hard limit are held by vectors and by
stand-ins, and by nothing live. Shown live on the installed Host the same evening
(`docs/battle-test.md`): after one turn of Codex its usage screen shows the account's week.

An agent's row then says of its tool that its account is used up (`backend_quota`): advice for whoever is
about to write to the agent, and no refusal. It says so while a window that was said to be all used, or that
stands at its hard limit, is fresh, and no turn of the tool completed since that was said: an account that
served a turn to its end was not used up. That is the Python Host's rule (`backend_usage.exhausted_backends`),
and its own answers were written down at each of the 108 askings of the stories (48 by now), with turns that
ended written into that Host's log of states: which window is named, the last reset where several are used
up, none where one of them does not say its own, and that a turn of another tool, or one that was cut off or
failed, proves nothing. The keeper is told when a turn of Codex completed, by the turn's own end, and keeps
it. The apps are told to read their agents' rows again when what they would show of it changes
(`agent-roster` of the kind `backend-quota`), never at the first look; and since what was said grows old
without anything happening, it is looked at every five minutes, as on the Python Host. Through the Host: a
turn that fails after Codex said that all is used marks every agent of Codex and none of Claude; a turn that
completes takes the mark away though Codex says the same in it; and a Host that is killed and started has
not forgotten that the turn completed. It matched at its first run again, kernel and Host. Of 17 wrong rules
planted, 15 were caught; one of the two others changes nothing by construction (a turn that completed in the
very moment the first of it was said), and the other showed a story that was missing (a window said again in
the moment a turn completed while an older one stands), which was written, after which it was caught. The
look every five minutes is held by no test.

A turn that fails for a usage limit is a limit too, whatever else is known: the Python Host records it as a
hard limit of which nothing more is known, neither which window nor until when
(`record_classified_usage_limit`), tells it once for as long as it stands, shows it as a window of the kind
`unknown`, and takes the account for used up while that is fresh. Here the same, by that function's own
answers (the stories are 59 now, 13 of their steps such failures): told once; kept under the login the Host
is on, or under none where no login can be read; ended as no longer known when another login comes. Where
the two Hosts part is how such a limit ends. The Python Host ends it when it asks the provider and is told
that all is well; this Host asks nobody, and ends it when a turn of Codex completes on the account, which
says the same. What ends then, and how it is told, is the Python Host's own ending
(`_resolve_unknown_terminals`), called in the stories where a turn completed: the limits of the login, of no
login, and of the login's account under an earlier login, and none of another account. The failed turn's own
state does not carry the limit's id, which the Python Host's recovery of goals reads; nothing here reads it.
Through the Host, with a stand-in for Codex that fails as Codex does when its account is used up: the limit is
told once and shown as a window of no known kind, the agent's row says used up, until nobody knows when; a
second such failure a second later is seen and not told again; while another account is logged in nothing of
it is shown as that account's; a turn that completes ends it, told as recovered, and its window is no more.
The kernel matched that Host's answers at its first run; the test through the Host failed twice for reasons
of its own (its stand-in failed by what it was asked, and a turn after turns that failed is given what they
were asked too; and a moment is told to the second), and then held. 17 wrong rules planted, each caught.

Claude Code says of its account as it runs: with each `rate_limit_event` of its stream it prints the five
hours and the week, each with how much of it is used and the second it begins anew (read off four raw
recordings of this machine: what is used is a part of the whole there, never a percentage, which is what the
provider's own endpoint answers the Python Host). The adapter tells it on (`Signal::Limits`), the actor keeps
the newest of a turn with when it was said and writes it with the turn's end, and when the turn ends the Host
reads it (`limits::claude`: in percent, to four places) and keeps it as the one word there is of that
account, in the place of the word before (`clarp-kernel` `standing.rs`). What is shown and what counts as
used up is the Python Host's rule for Claude, by that Host's own answers: its own function for asking the
provider was run with a made-up answer in the provider's place, and then asked as an app asks (26 stories, 46
askings): fresh for an hour, no warnings and no stretches, not shown to a Host on another account, used up
while a window of a fresh word is all used and no turn of Claude completed after it was said. Since a word
here comes out of a turn, a turn that completes after saying that all is used unmarks the account by the
same rule: it was served. Whose account it is, is who Claude Code's own files say is logged in, as that Host
finds it, kept as a keyed digest. Through the Host, with a stand-in for Claude Code: the windows are shown
after a turn; a turn that says all of the five hours is used and does not complete marks every agent of
Claude and none of Codex; one that completes unmarks them; a Host that is killed and started has kept both;
another account's login shows nothing of it; and nothing of who is logged in is in what the Host keeps. The
keeper matched at its first run; the test through the Host failed once for a reason of its own (its agent
was not an agent of Claude). 25 wrong rules planted into the keeper, the reader, the adapter, the actor and
the Host, each caught. With this the feature `backend_quota` is listed. Left: that an account's
credits are used up (the provider's own word), and asking a provider at all, which stays off unless the
owner turns it on.

**The seats of the machine's accounts: first, what is served** (17 Nov 2026). What the owner's apps ask the
Python Host for was counted off that Host's own request log on elitebook over three days (routes and counts
only, read through a pipe): 170,256 requests, every one to a route this Host has. Of what this Host had not
built they asked for teams 768 times, where the owner has none (the app's own polling, answered there with an
empty list, as here), and for the desk of seats about 180 times; for vocabulary, the Oracle, dreaming, the
terminal and the speech socket not once. So the seats come next, and the rest of the list after them. The
first piece is `GET /hotseat/status`, the strict and redacted answer: `clarp-kernel` `seats.rs` is the Python
Host's `hotseat_status.py`. Everything the owner's own tool says passes an allowlist; an alias that is an
address is shown by its first letters, and anything like a token, a path or a link as `account`; an account is
bound to a keyed digest of who it is, kept and never served, so that what was last known good is carried only
to the very same account; and serving reads what is kept and never the tool. The contract has shared fixtures
written by hand (`tests/fixtures/hotseat_status`: what the tool answered and what the switcher did, and the
exact body served). A script (`tools/seats-vectors.py`) gives each to the Python Host's own functions as that
Host's own test does, fails where the body is no longer what the fixture says, and writes them as vectors. The
port matched all 28 at its first compile. Of 39 wrong rules planted into it, 12 went unnoticed: the fixtures
were written for the shape of the body, and many a rule of the keeping is in none of them. One of the twelve
changes nothing (the marks of a path are refused by the rule for a plain alias anyway); for the other eleven,
sixteen cases were written and answered by the Python functions (a number above the whole, fresh and stale by
twice the switcher's cadence, an alias like a token, a failed row that keeps what was last known good, a
reading that says its age, the skips the switcher has and one it has not, a receipt that names the alias as
it is kept): 44 cases, and all 38 plants are caught. Nothing of the Host reads the owner's tool with it yet:
that is the desk, which is next.

**The seats of the machine's accounts: the desk** (17 Nov 2026). `GET /hotseat/desk` is the whole desk of the
owner's own tool, for an app with its owner's full rights: every saved account of Claude, Codex and Muse with
its windows, its state, its clocks of signing in, the work that stopped and the work that was done. The tool
is asked three things at once (its desk, its list, its work), each in a group of its own and with its time;
what they answered is made into one desk, kept, and served as it was read, with how old each number is. A
window whose time to begin anew has passed since it was read is served without its number, and an account
that was used up by such a window is no longer said to be. A read is one at a time, no sooner than a minute
after the last that worked and ten seconds after any attempt, and only when an app asks for one; a read of
which the tool's own desk failed makes none and the desk before stays, stale; one of which the list or the
work failed is made of what answered and says which did not. The list is also what the strict answer
(`GET /hotseat/status`) is made of, so a read here hands it over.

`clarp-kernel` `desk.rs` is the Python Host's `hotseat_desk.py` and `hotseat_desk_reading.py`, by that Host's
own answers: `tools/desk-vectors.py` runs its `HotseatDesk` through 18 stories with the clock held (119 steps,
58 reads asked for, 63 askings, 200 accounts served: every state of an account, the windows of each tool,
what is said oddly, reads that fail in every way one can and how soon the next may be, a Host started anew,
a window that begins anew while nobody reads, a desk that was never read) and writes two tables beside them
(204 labels of windows, 16 names of tools). The port matched all of it at its first compile, so 158 wrong
rules were planted into it one at a time: 152 were caught and six were not. Four for want of a case (a sign
between two words that is no space, a label that names thirty days and a week, a reason kept with its blanks,
an alias with blanks looked up in the list): the cases were written and answered by the Python Host. One
could never be seen: a list of codes a kept text was looked up in, where how a read failed is now one of
three things by its type (`Failed`). One changes nothing by construction (what the strict answer says is left
of a window is what was worked out from what is used). With this Host's own rule below there are 168 in the
list, and each is caught.

Three things are this Host's own, each in `docs/differences.md`:

- **Which tool is run.** A Host is told its owner's tool (`--seats-tool`; `tools/install-host` names the one
  it finds). One that was told none runs nothing, says `installed: false` and does not list `hotseat_desk`:
  no Host started bare, for a test or to try something, ever asks the providers about the owner's accounts.
  The Python Host runs `hotseat` wherever it finds it.
- **It reads and moves nothing.** Beside another Host the tool is told to renew no login as it reads
  (`HOTSEAT_NO_REFRESH=1`), and the Host's own token is never in the tool's environment. Switching and
  continuing (`POST /hotseat/switch`, `/hotseat/resume`) stay refused; there is no switcher here.
- **Of agents it says its own word.** What the tool's list says of a Clarp Host's agents and of a Host's
  stopped work, it has from the Host it is bridged to. Measured on elitebook: run in the environment this
  Host gives it, the tool listed six pieces of stopped work, and none was of an agent of this Host; its
  bridge reaches the Python Host of the machine. So this Host counts its own agents in that place, each on
  the account its tool is on (`desk::of_this_host`); of the work the tool says stopped it leaves a Host's
  own to that Host and shows a session that is no Host's, unless that is the session of one of its own
  agents. Its own agents that stopped are listed with what follows.

**The seats: work that stopped goes on from the desk** (18 Nov 2026). From the desk an app asks for one of
three things (`POST /hotseat/resume`): that an agent the desk lists as stopped continue, that an agent's
held queue be let go, or, for a session that is no Host's, the command a person types to take it up.
`clarp-kernel` `resume.rs` is the Python Host's `resume_work` and `resume_prompt`, by that Host's own
answers: `tools/resume-vectors.py` makes agents in an empty store, puts them away, makes them janitors,
holds their queues, says that they ended a turn or work, stores a desk with work that stopped as a live
read stores it, and asks, with a way to send that works, fails before or after it saved the prompt, cannot
confirm, or says that the prompt waits behind a turn (21 stories, 102 askings, 39 prompts sent, and 30
prompts for what a row says of why it stopped, what the tool noted, and how old the reading is; the same
file at every run). With each asking it writes what that Host knew of the agent, so that the port is given
the same facts. The rules: one stop gets one prompt, under an id made of the agent and the moment it
stopped, so that asking twice is the same send; an agent continues only while the desk's reading says it
could, it has not ended a turn since the stop, and it does not work now; an asking about a stop that got
its prompt is sent again whatever has happened since; a held queue is let go first. The prompt says only
what the Host knows, and the tool's own note on its reading comes last, announced as data, as one JSON
text nothing inside it can close. The port matched at its first compile; the cases its planted rules would
have gone unnoticed for were written before the plants were run (the order of two refusals, a first row
that counts, a reading from ahead), and all 64 were caught.

What the Host adds is what it knows of its own agents, and that is where it parts from the Python Host
(`docs/differences.md`). The desk lists this Host's own agents whose work stopped, first, each with why
and when and with whether it could go on by what the same reading says of the account its tool is on
(`desk::of_this_host`): a last turn that failed for a usage limit, listed for a week from then, a turn
that waits for an account, a queue that is held with turns waiting (34 more planted rules of the kernel,
each caught: its list for the desk is 190). Whether a stop's prompt was sent is asked of the agent's own log (a
row of that id in the conversation, or a message of that id in the queue), and the send is the Host's own
send, whose log takes a message once: so "one stop, one prompt" rests on the same rule as every other
message. A turn that waits for an account is not sent a prompt: it is tried at once on the account that is
there; a Host that puts the logins in place itself leaves it to its selector and says so. Through the Host,
with stand-ins for the owner's tool and for Codex and Claude Code: three agents whose turns fail for a
limit, one stopped with a message waiting, one stopped with none, one whose turn failed for something
else; one reading that lists them as they are; the queue let go and its message answered; one agent
continued by one prompt and asked again by name and by id; one that works and is sent nothing, and whose
prompt, waiting in its queue, counts as sent; one that ended a turn since; a janitor, an agent put away,
what is no asking; and a turn that waits for an account, in both ways a Host can stand to the logins.
Of 34 wrong rules planted into the Host's side each is caught. `hotseat_desk_actions` is not listed: switching the account in use is not built, and the
phone app sends a Host that does not list it none of its three actions.

An asking to make another account the one in use (`POST /hotseat/switch`) is read as the Python Host reads
it (`resume::switch_wanted`, by 39 askings that Host answered: the tools that have seats, what an alias may
be) and then answered by what this Host is: beside another Host it says in words, in the shape of that
Host's refusals, that the account in use is changed where the logins of the machine are kept; a Host that
keeps the logins itself says that switching from the desk is not built. Ten more planted rules of the
kernel and five of the Host, each caught.

**Sockets: one line, two carriers, and the Host's own terminal** (18 Nov 2026). Some of what an app asks
of a Host is a conversation and not a request: a terminal, the stream of speech, a ring's live socket, the
Oracle's. The app holds it over a WebSocket, and that reaches a Host at its door or, as a stream (`WS_OPEN`),
down the one socket the Host keeps to the relay. It is the first gate before a phone moves (section 7), and
it is built as one port so that no route is written twice. `line.rs`: a `Line` is what a route has of a
socket (what it hears, what it tells, a close with a code and a word, and the word that nobody is there any
more); a `Carrier` is its other end. Two things carry a line. The door carries it over the upgrade of a
request. The relay's connector carries it as frames (`connector.rs`, `conversation`): `WS_ACCEPT`, then
each thing the route tells as `WS_MSG` with its kind before it, then `WS_CLOSE` with the route's code and
the connector's own word. **A socket is asked for through the Host's own router either way**: the connector
asks with a request that carries the line, and a route takes its socket with `Socket`, which finds that line
or the upgrade. So the checks of who asks stand before every socket, once, and a route cannot tell its
carriers apart. Whether a request asks for a socket at all is read as the Python Host reads it
(`clarp-kernel` `socket.rs`, by `tools/socket-vectors.py`: that Host's `ws.is_websocket_upgrade` asked 1,323
times), and each route refuses what does not in its own words, as there.

**One rule where the Python Host has two**, decided on the way. That Host's connector lets a phone go when
sixteen messages wait, and at its door a client is made to wait. Built so at first, a test of this port
failed: a client that left while its route was not listening was never noticed, because a carrier that has
stopped reading cannot see its client go. The Python Host's terminal has that leak (the thread that types
is the thread that listens). So here a line holds what was said and not heard by its weight (two of the
largest messages; a thing weighs 256 bytes beside its own), a client that says more is let go by either
carrier (`ABORT` through the relay, a close with 1008 at the door), and a route is always told that its
client is gone, also while it waits for something else (`Line::gone`). The count is in bytes because
sixteen small things said at once must not end a stream: keys, or pieces of speech after a network stood
still. Registered in `docs/differences.md`.

**The Host's own terminal** (`GET /server-terminal`, `terminal.rs`, the feature `server_home_terminal`): a
login shell in the home of whoever the Host runs as, on a pseudo-terminal of its own that it leads, for the
administrator and a device of full scope. The two calls a terminal needs are calls this workspace does not
make itself (it forbids unsafe code): the crate `pty-process` makes them. It is refused as the Python Host
refuses it: a Host without a token opens none, since nobody there has said who they are; what asks for no
socket is told so (426) before anything of the machine is looked at; a shell that is not there or that
nobody may run is none (500). Bytes are keys, what is printed comes back in bytes, a text sets the size
and the program is told that its terminal changed. At its end the program's group is asked to end and
killed after a second, while its terminal is still there, as on the Python Host: a terminal that went
first would end a shell at once and not for the asking (found by a test, in this port's first form). A
device of limited scope is kept from an agent's terminal too (`GET /terminal/...`), though that route is
not built yet. Tested: the line by itself, the door against a real client, the relay's carriage by its frames (seven tests), and a real Host process at its door and through a stand-in relay (three). Of 83 wrong rules planted one at a time (16 into the kernel's reading of a request, 39 into the line, its carriers and the reading of a size, 28 into the terminal through a Host) each is caught.

**An agent's own terminal** (`GET /terminal/<session>`, 18 Nov 2026): the agent's tool, spoken to by hand,
taken up on the conversation the agent has with it. What each tool is started with is each backend's own
word in the Python Host (`terminal_argv`), read by the same vectors (`socket.rs`, `terminal_of`: 14 answers
of seven backends; Claude Code and Antigravity told not to ask before they act, each tool with its own word
for taking a session up, three tools that have no terminal and say so). It is started as the agent's turns
are: where the agent works (the home when that is gone), told the agent's name and where its tools find
their Host, never the Host's own secrets. Refused as there, in plain words: nobody of that name (404), a
janitor (403), a tool without a terminal (501), a tool that is not on the machine (500); a device of
limited scope is kept from it. **While a terminal is open on an agent, no turn of that agent is started**:
two processes on one conversation would spoil it. The Python Host keeps such a message in the agent's
queue. Here the agent's thread holds the launch of the turn, as it holds one for an account (`agent.rs`,
`carry_out`): the turn has begun, its tool starts when the last terminal on the agent is closed, however it
is closed, and a turn stopped meanwhile is over at once. The agent's state machine is not changed for it;
registered in `docs/differences.md`. Tested through a real Host process with a stand-in for Claude Code that is both the tool of the agent's turns and of its terminal. Of 36 wrong rules planted one at a time (12 into the kernel's word for each tool, 24 into the route and the agent's thread) each is caught. Four more were tried and showed what no test saw: one check got its case, two were redundant and are gone, and one changes nothing that can be seen (the launch a stopped turn left waiting, kept in place of dropped: carried out later, it fails where the turn's message was).

**The stream of speech** (`GET /stt/stream`, 18 Nov 2026, `listening.rs`). A phone finds the end of a turn
of speech itself and posts the clip, unless the engine that hears is one that finds the ends of turns: then
the phone streams what it hears and the Host carries the sound to the engine (Deepgram Flux) and tells the
phone what the engine says of the turns. The owner does not use it now (measured on both of his Hosts: no
stream in thirty days; he speaks in clips), and it is built because everything is. What is pure in the
Python Host's `stt_stream.py` is the kernel's, by that Host's own answers (`speech_stream.rs`,
`tools/stream-vectors.py`): the engine's address for every way a phone tunes the listening (138 requests,
four of which fail there), what a message of the engine means (55), the books of one stream through nine
stories of sound, events and time (which sound belongs to which turn, what the phone is told, what is
written down and kept when a turn ends: 78 steps), and the sound of a turn as a file (7). The port matched
at its first compile, so wrong rules were planted. The Host's part is the carrying. The engine is reached
before the phone's socket is opened, with the owner's key, which never leaves the Host, so that a phone is
told in words when it cannot be (no engine that finds turns, 409; no key, 503; the engine not reached,
502); then sound goes up and events come down until either side is done; a phone that is done says so, the
engine is told to say the rest and given four seconds for it; the phone is told last how many turns ended.
Refusals are JSON here, as on the Python Host, where a terminal's are plain: each route says it in its own
way, which is why the port leaves the saying to the route. With it comes what makes the engine one that can
be chosen: **Deepgram for clips** (`clarp-speech` `nova.rs`, by `deepgram_stt.py`: the clip posted as it
is, what is asked in the address), the key read where the owner keeps it, and the setting of how a turn's
end is found (`turn_taking`), kept and told as the Python Host keeps and tells it: the engine finds it only
while the engine chosen is one that can, and both are changed together. Not with it: the vocabulary (no
words to listen for, no runs written) and the keeping of sound, which this Host has not; both are
registered. Of 144 wrong rules planted one at a time (80 into the kernel's rules, 64 into the route, the engine for clips and the setting) each is caught. Two were missed at first and got their case (a run written as nothing is no run; what a phone says in words that is not its leave-taking ends nothing), one planting of mine did not compile and was planted again in other words, and two let a Host start that was to refuse, where its test waited without end and held the build machine's one slot until the run was ended by hand: every command of the Host's own program in the tests has a limit now.

On the line since: the socket of a live voice call ("A ring held as a live voice call" below). And the
socket of a call of the Oracle ("The Oracle: a call on this Host" below). Not built, and closed through the
relay as a socket that failed: the Oracle's call of the first kind.

**What is said of a tool's use, without asking a model** (17 Nov 2026). What the owner's Python Host does
most, counted over a week of its own log: its tool explainer ran 35,972 times, seventeen times what
every other janitor ran together (the goal keeper 1,647, the audio bookkeeper 444, one more 4). It puts a plain
sentence under a tool's row for a reader who chose not to read commands, at the level of detail the reader
chose. The Python Host has it in 3,900 lines over nine modules; here it is built in stages, and first the
layer that asks nobody, which answers most uses. `clarp-kernel` `explain.rs` is that layer. A use is read: a
shell command is split at the shell's operators with nothing run (what only changes how it runs is taken
off: assignments, `timeout 60`, `env A=1`, `nice`; a `cd` before it is where it runs; a `| head` after it
only shortens what is shown; heredocs are lifted out and told by what reads them), or the fields of a named
tool are taken, or an exploration's or a patch's lines. It is told as one of nineteen kinds there are
templates for (a directory listed, a file read, a text searched for, a repository's state asked, ...), or as
none, with the reason, of which there are seventeen: several commands at once, a program that changes
things, a script, another's rights, an upload, a program nobody listed, and so on. The reason decides who
may be asked instead, and a use that could change or send something is never given a template that would
say less than it does. What a use names is said only where it is of its kind (a path, a pattern, a host, a
name) and stands literally in what the use shows. Then the template's sentence for the reader's level is
filled. The library of templates is the Python Host's own file, copied byte for byte by the tool that
writes the vectors. The shape of a use nobody listed is signed with a key the Host draws for itself (HMAC
of the shape as Python writes it), as there: nothing of the use is kept, and nobody without the key can
try a guess against a signature. Held by `tool-explanation-vectors.json`: 543 uses and what the Python
Host's own `lookup` answers for each at the five levels (a template fits 253, none fits 290, 1,010
sentences), each compared as a value and as text; the first 256 were written for what uses look like, the
rest for each branch of the reading, among them what Python and Rust would read apart if nobody looked
(which signs are digits, which are blank, where a pattern's end is). The Rust agreed on all of them at
its first build, which says the reference was read before it was written.

The layers after it, the same day. How a use is read as it comes from a turn or an app (`explain/read.rs`:
the bounded fields, secrets taken out, what a heredoc only feeds a script or a file withheld) and what
parts a call has, each with the shape it is remembered under (`explain/shapes.rs`: its words with every
value that may differ between two calls replaced by a slot of its kind, or, where it cannot be shaped
safely, the hash of its whole text and why), held to the Python Host's `normalize_activity` and
`shapes.split` for the same 787 uses (830 parts) and to 366 cases of its smaller functions. Then what was
learned and the desk (`explain/learned.rs`, `explain/desk.rs`): a book that is told the time, at which an
app asks about up to eight things; each part is answered by a template or by what a model said of that
shape before, and what is left waits for the model in batches of eight for one reader; what the model
says is learned, under the shape where its sentence with places holds for other values, else under the
one text; a model that fails is left alone for a minute; who lets go of a row is not served. That desk is
held to the Python Host's service itself: 23 scenes were played to `ToolExplanations` in a process of its
own, with its queue, worker, table and ledger and a stand-in for the model, and the desk answers every
one of 107 requests alike, asks its model about the same parts, learns the same rows and counts the same
in its ledger. Writing the vectors found two requests that Host fails on, each of which takes the whole
request of eight rows with it: a shell command of no word (a comment alone: defect R39) and a shell tool
whose command stands only in what it was given, where the call is one piece after a `cd` (R40). Both are
answered here.

At the Host's edge (`clarp-host` `explain.rs`): the book is read where it is kept (the rows and the hours
of the ledger with the fleet, each by itself; the key shapes are signed with among the settings, drawn
once), the janitor that explains is looked up in the janitors' book for the agent a request is about, and
a task asks its model for what waits, in a run of that janitor. What an agent did is whatever stood in its
work, so the model is started to read and nothing else, as the Python Host starts it: Codex with a
sandbox it may only read in, no shell, no tools, none of its user's settings and rules, in a directory of
its own that holds its instructions and the shape its answer must have, with no more of the Host's
environment than it needs to find its login (`clarp-backend` `Backend::reads`, `once::read`). The scripts a
call names are read in the agent's directory, only where they lie plainly. An app asks with
`POST /tool-explanations` and reads how it went with `GET /tool-explanations/stats`. The owner's switch is
off as it comes, where the Python Host has it on: a row of a tool does not carry its explanation here yet,
and an app that is told explaining is on shows the explanation in the place of the command.

And on the row itself (`docs/live-items.md`, section 6). `explain/rows.rs` is the Python Host's watcher of
what turns show, as a book that is told the time: each new tool item (and one that is shown again with
another command) is announced as waiting at once; the desk is asked about an agent's rows in rounds of
eight, those it was asked about before keeping their place and the newest that wait taking the free ones;
a row that has waited forty-five seconds, one that fell off a backlog of twenty-four and one whose turn
ended fifteen seconds ago is settled as skipped; a new turn takes the place of the rows of the one before.
Held to 18 scenes that were played to that Host's `LiveExplainer` with a clock of their own (91 steps, 119
patches, 34 askings). At the Host's edge a task listens to what the hub shows (and reads none of it while
the owner's switch is off), asks the desk as an app would, and has what the rows say put on the item by the
agent's own thread, which tells whoever watches (`Hub::patch` of the kernel's live view, which was there and
had no caller). The Host's test shows a turn of a stand-in tool whose command has two parts: the row is
told to wait, then carries "Runs sleep. Then runs echo." while the turn still runs; the same command in the
next turn is answered from what was learned, and nobody is asked. What the Python Host does for a patch
its other process cannot take, and for what it missed while it was away, has no counterpart here: the rows
and their explainer are one process. The switch and the janitor stay off as installed: what is asked of a
model, on whose account, is the owner's to decide, and comes over with the owner's settings.

One part of the Python Host's explainer is left out on purpose: its memory of each agent's recent commands
(`tool_explanation_commands`). That Host shows a command of Codex as the name of its row, cut at 80 signs,
and remembers the whole commands to complete a cut name an app asks about. This Host shows such a command
under the name `shell` with the command beside it (2,000 signs at most on its item), and the apps ask with
what the row carries (name, summary, command, path and what it was given: read in the phone's app and in
the desktop's; the web app does not ask, it shows what the row carries). No name this Host tells is cut, so
there is nothing to complete, and no command is held in memory for it.

**What a Codex agent did, as Codex's own reader shows it** (17 Nov 2026). Found while reading the above: a
call of Codex was a row with a plain card here (the name `shell`, 100 signs of the command, nothing of what
it printed; an edit with no file named), where the Python Host shows cells: "Ran cargo test" with the first
and the last lines it printed, "Edited" with the files, "Explored" for what only looked at things. Of the
191 agents on the owner's Host that morning 58 run on Codex. The whole reader is ported, since the past
sessions of Codex need it too: `clarp-kernel` `tools/cells.rs` (the cells: `codex_tool_cells.py`) and
`clarp-backend` `history/rollout.rs` (`parse_turns` of `codex_transcript.py`: the messages in both forms
Codex writes them, told once; the items `codex exec --json` prints and those of Codex's app server; the
older records of a call and what came back, left out where a turn has the item too; the agents Codex starts
itself; reads and searches gathered; every cell within its bounds). Held to 17 made-up rollouts read by that
Host's own function: 375 lines, 49 turns, 232 cells, 75 cards, written twice (once for what rollouts look
like, once for each branch of the port). It agreed at its first build.

In the Host, the adapter that reads `codex exec --json` has the cell made of each item while all of the
item is there (the Host keeps 4,000 signs of what a command printed; the cell wants its first and last
lines and how many were between), and keeps it with the call. The row of the call carries the cell and no
card (an app shows each: both would be the call twice). The cell is known by the call's id, also where it
only looked at things, so that an app which shows the call live knows the row for the same. A call its
turn was interrupted under is no longer shown as running. One thing is left out: several calls gathered
into one cell, since a call is a row of its own here. Held by the adapter's recorded turn, the row's own
test, and a turn of a stand-in Codex through the Host (a command that fails with eight lines, a read, an
edit of two files).

**The sessions Codex kept** (17 Nov 2026), with the same reader. They are listed for whoever makes an
agent (`GET /past-sessions?backend=codex`) as the Python Host lists them: by Codex's own index of its
threads (the table `threads` of `state_5.sqlite`, opened to read and nothing else: those a user wrote in,
whose rollout is there), and by the newest two hundred rollouts beside it, so that an index that is
missing or behind hides nothing (`clarp-backend` `past.rs`, `codex_sessions`; held to 16 lists the Python
Host's own function gave for 17 made-up files and 8 made-up threads, to how it finds the rollout of a
session, and to the model it reads off one). An agent begins with one (`resume_session_id`): its
conversation shows what was said and done in it, read from the rollout, the cells with it; it runs on the
model the session last ran on; and its next turn is `codex exec resume` of that thread. A session is taken
up wherever it ran, since Codex finds a thread by its name alone; one another agent has is opened or in
use, in the Python Host's words. A copy of one is not made (`fork_session_id`): this Host has no app
server of Codex to ask, and says so in the Python Host's own words for a tool that cannot fork. Held by a
turn of a stand-in Codex through the Host: the session offered, taken up, shown, continued, and refused a
second time.

**Bookkeeping delegation lifecycle**. The fleet reserves the two active roles and the relationship in one
KeyValue write. The principal records START in its own log before creation answers. Its mailbox orders each
authorized operation with STOP. An archive or deletion records STOP for either role before the roster change
and release of both keys commit together. A Host opening its stores releases an unstarted reservation and
completes the release for a logged stop. An agent reopening its log stops an active binding whose reservation
is missing. A damaged reservation is left in the fleet store and counted in the startup journal.
The principal serves ordinary work while the fleet repairs a mismatch. The fleet waits up to
three seconds for each actor reply. History for either participant is read from the delegation rows.

**Bookkeeping listener** (20 Nov 2026). The Host checks active grants every two seconds.
Each delegate has a background job. A burst of principal activity gets eight seconds of quiet,
with the first wake due after at most a minute. An outstanding wake waits for application; an
unfinished or queued turn holds a retry even after fifteen minutes. A repeat after a failed send
backs off. The principal keeps dispatch and application separately in its log. On restart the
listener resumes from those cursors and checks the delegate’s queue before sending.
The principal keeps the listener heartbeat in memory so a quiet grant does not grow its journal.

**Delegate observation and recording** (20 Nov 2026). A verified delegate with the relationship
credential can page its principal’s messages and goal events and see its ten most recently changed
open goals. The reader excludes the delegate’s own messages and this delegation’s events before
counting either page. A claim, observation, discrepancy or unknown keeps a snapshot of each cited
source. A delegate can propose a subgoal or update its bookkeeping with an expected revision.
The principal commits the events, subgoal changes, replay keys, applied cursors and application
time in one append. An empty acknowledgement also advances the cursors and time. A retry after a
restart finds the committed keys. The plan shows the latest fifty subject-and-kind pairs drawn from
its newest five hundred accounting entries. An older imported goal gets that projection on upgrade.

**A goal’s ledger** (19 Nov 2026). The owning agent appends a goal change, its attributable
events, their per-plan ordinals and their ID locators together. The next number belongs to the
agent. A new agent arriving from another Host keeps its supplied IDs, and its next number is above
all of them. When a plan enters an existing ledger, its disjoint IDs stay and an entry marker names
their source and range. Colliding IDs are refused before the append. Earlier goal history available
at upgrade is marked as legacy. The tool and GET /goal-ledger seek within one plan by its ordinal
and return its events in commit order. A page reads only the actor identities it returns. A moved
agent brings the events of each goal with their source IDs. The keeper uses these same numbers
in its source references. A goal already updated at upgrade gets the ID of its own legacy baseline
as its keeper cursor in the same append. A moved goal retains the cursor up to its source tail.
The keeper bounds its initial page by the owner's committed high-water inside the same actor ask.
An owner can add, update and retire explicit subgoals. Each edit keeps the changed subgoal and its
ledger event in the same append. The subgoal has its own revision and evidence accumulates across edits.
The bookkeeping delegate records its own account beside the owner’s changes.

**The Goal keeper** (17 Nov 2026). The janitor that ran most on the owner's Host after the explainer (1,647
runs in the counted week), and the one a long piece of work leans on: agents rarely keep their own goal's
books while they work, so after something happened a small model restates the goal beside its owner: which
steps are pending, in progress, done or blocked, and a short checkpoint. The owner is never prompted, told
or interrupted; a step is called done only where the model quotes what shows it; and the keeper cannot
complete, pause or cancel a goal. Three layers, as the Python Host has them in `goal_keeper.py`,
`task_plans.updater_apply` and `janitor_autonomy.goal_keeper_once`:

- *The rules* (`clarp-kernel` `goals/keeper.rs`), with no clock and no store: what a keeper watches, where a
  goal's next update reads from, what happened since and which of it is a trigger, whether a goal has
  stalled (a rule, with no model: nothing for three days, or the owner's last turn stopped on a usage limit
  or a provider's error and nothing followed for an hour), the text of what was said, the sheet of facts,
  the prompt, what is taken from a model's answer, which goals are due (triggers folded for five minutes,
  thirty seconds after a pause or a resume; a failure tried once more and then left to the hourly sweep; a
  goal older than the first look back read from its start once) and how a run is settled. Held to the Python
  module's own functions by `tools/goal-keeper-vectors.py`.
- *The write* (`GoalBook::updater`): the statuses of steps and the keeper's own checkpoint beside the
  owner's, never the goal's state, its criteria or their evidence. The plan's revision stays what it was, so
  the owner's next command is never refused because of the keeper; the keeper's write is refused instead
  where the owner changed the plan since it was read. Held by seventeen writes and refusals in the goal
  tool's vectors.
- *The loop* (`clarp-host` `keeper.rs`). Every fifteen seconds, for each keeper that is on: the open goals
  of the agents in its scope are looked at, and those that are due, six at most, are updated in one run of
  the janitor, side by side. A goal's world is read off what the Host already has. What the owner and its
  helpers said since is asked of their conversations as an app asks for what changed: the revision each had
  at the time the update reads from is found once (the actor's `revision_at`, a search of the log, not a
  reading), and from then on only what is newer is asked for, so a look at a goal where nothing happened
  asks nobody anything. The model is the janitor's tool, started to read and nothing else
  (`once::read`), and held to the answer's shape where the tool can hold a model to one. The write goes
  through the thread of the agent that owns the goal, as its own tool's does: one writer for a plan.

A plan says how the keeper stands to it (`plan.updater`: its switch, its last run, this goal's last failed
update and when it is tried again, the newest receipt), on the routes and in what `clarp-goal` prints. A
receipt is kept of everything the keeper did with a goal in a run, also where it wrote nothing, the newest
two hundred of a goal: on the goal, on the keeper's runs (`receipts`), and from `clarp-goal receipts`. The
features `goal_updater`, `goal_keeper_receipts` and `goal_keeper_stalled` are listed. It is installed
paused, as on the Python Host, on the tool its job recommends, Antigravity (a Host installed before that
tool was built here keeps its keeper on Codex until it is set otherwise); every update is one call of
that tool, so whoever turns it on spends that account. Held
through the Host by a goal, three turns and a stand-in model: paused it asks nobody; on, it writes what was
shown and holds what was only said; it reads only what is new the second time; a model that fails leaves
the goal untouched and says when it is tried again; and what it kept is there after the Host was killed.
Not held by a test of the Host: a stalled goal and a goal read from its start, which need days to pass
(the rules for both are held by the vectors).

**The audio bookkeeper** (17 Nov 2026). The third of the Host's own janitors by how much it runs (444 runs
in the counted week), and the one that asks no model: the first time a clip is seen in a stage (what its
producer says of it: `producer:streaming`, `producer:complete`, `producer:failed`; what was done with it for
its listener: `client:synthesized`, `client:broadcast`, `client:queued`, `client:play-start`,
`client:play-ok`, `client:play-fail`, `client:held`), that is written down as a completed run of the janitor
that keeps the books for the clip's agent. A clip played a second time is no new fact, and what nobody keeps
the books for waits until somebody does. The rule is in the kernel (`janitors/audio.rs`: `Seen::observe`,
`Book::keep_audio`), held to the Python Host's own functions and to its database's triggers by 64 recorded
steps in which 15 passes write 66 facts down (`tools/audio-bookkeeper-vectors.py`; the reference's numbers
are written without the gaps its database leaves, and the generator says so). At the Host's edge
(`audio.rs`) a line an agent says aloud is looked at when it changed, on the janitors' tick, and what an app
says of a clip (`POST /clips/ack`) is seen as it says it; what was seen is kept with the fleet for the week
a clip is kept. Installed paused, where the Python Host has it on from the first day. Held through the Host
by two clips of a stand-in engine: nothing is written while it is paused, what waited is written when it is
turned on (the six stages of a clip that was played, each once), a second playing adds nothing, a play that
failed is told as that, and after the Host was killed the books are as they were.

**The Quota keeper** (19 Nov 2026). The janitor job `quota-monitor`: it reads what is left of the tools'
accounts and tells the owner where a window runs low. The rule is in the kernel (`janitors/quota.rs`): which
windows of a reading count (fresh, a number used, a moment seen), the name a window is remembered by (its
id, or for Claude its kind and the moment it begins anew to the nearest minute), when the owner is told (what
is left falls to the threshold or under it from above, or to nothing), the receipt and its words, the run's
line, and the lane's cadence. Held to the Python Host's own functions and to nine whole stories of its lane
on a database of its own (`tools/quota-keeper-vectors.py`: 16 windows named, 13 sequences of readings around
the threshold from both sides, the old name carried over, 75 steps of the lane in which 17 pushes go out).
At the Host's edge (`quota_keeper.rs`) the keeper is a kind of due work of the janitors' one runner: the
moment of its next look is known when a look ends, and the runner is armed for it; a change of any janitor
disarms it. It reads what `GET /backend-usage` answers, which asks a provider only on a Host that was told to
(`--ask-usage`). A look writes once: its run in the janitors' book, the windows that moved, its receipts and
its lane, each a record of its own; then a receipt is pushed as the alert of a finished turn and what became
of it is written on it. The book itself is kept as records of its tables (`janitor-config`, `janitor-run`,
`janitor-progress`, ...), so that a run every five minutes writes its own rows, about two kilobytes, where
the book was written whole, about thirty, at every change; a book kept whole before is read and kept apart
at once. Going back is not free: a build before this one reads only the one record and finds a book with no
tables, so once a Host has run this build, an older build loses its janitors' book (their configurations,
triggers and runs; the agents themselves stay). On a Host beside another (`--accounts follow`) a receipt is kept and marked as not delivered, and
nothing is pushed; with `none` and `select` the owner is told. A recovery is asked of no runtime yet: a
window with nothing left says what the Python Host says where none runs. Held through the Host by a stand-in
Codex that says its limits and a stand-in relay: turned on, the keeper looks at once and the owner is told
once; what it keeps is there after the Host stopped; a book kept whole is kept apart; beside another Host the
next crossing is kept and not pushed; a receipt settled thirty days ago is let go and one that waits stays.
Wrong rules: `tools/plants/quota-keeper.py`.

*Its recovery* (19 Nov 2026), on a Host that keeps the logins itself (`--accounts select`) and has the owner's
selector for the tool, as the Python Host's `request_recovery`, `recover_approved` and `runtime_recover`: at a window
with nothing left, a keeper that asks puts a card in its own session (the janitors' shelves are read beside the
agents' for it); one that recovers by itself has the tool's pool take up the first turn it still owns, as one that
failed on its account (`accounts::Pool::first_owned`, the pool's only new reading; its rules are as they were). A
card the owner accepted runs a recovery at the keeper's next look, once. Held to four more stories of the reference
with its coordinator stood in (14 stories, 135 steps), and through the Host by a stand-in Codex and a stand-in
selector: at once, the selector asked once and the turn taken up; by a card, recovered once it was accepted.
Wrong rules: `tools/plants/quota-recovery.py`.

**Grok and Muse** (18 Nov 2026). One CLI process serves each agent turn. Grok gets
`--session-id` on its first turn and `--resume` thereafter. Muse takes `--session-id`
for both. Under decision D26 the shim syncs the assigned session id to the turn spool
before either child can read its prompt. The actor binds that id when it sees the child start, including after a Host
restart. The shim hands a composed prompt above 64 KiB to the CLI in the turn's input
file. Stop uses the same shim control socket as the other backends.

Grok's streaming JSON and Muse's run events supply live assistant text, tool cards,
errors and token counts. Grok's tool updates settle a pending call. Muse reports the
call and its result together. The native session importer also reads the arguments
Muse committed to disk. Past sessions can be listed by directory and resumed by id.
The picker takes Grok's models from its CLI and Muse's visible models and effort tiers
from its local catalogue. The orchestrator can ask either CLI one question. Muse
suppresses its session log for that question.

**Switching accounts** (18 Nov 2026). A Host started with `--accounts select` and
`--seats-tool PROGRAM` can switch the machine's Claude and Codex defaults. The
desk reads the active alias under the pool's exclusive login lock. A turn holds
the same file locked for shared use as long as its tool runs, including while
its Host is down. A switch that needs longer than thirty seconds returns 202.
The desk reports the outcome when the tool finishes. Claude confirms the switch
in JSON. Codex confirms it by its exit status.

An enabled Hotseat Janitor uses its configured floors, required and running
models, adaptive intervals and last switch to choose a seat. Notify mode records
the advice. Automatic mode checks the owner and the current login again before
changing it. The status records each provider's most recent action. Exhaustion
leaves waiting work in the pool. Banked resets belong to a separate decision
of the owner. The other account modes leave the login where it is.

**Launch directories** (19 Nov 2026). The new-agent dialog combines recent launches,
zoxide results and explicit directory completions. The Host finds `zoxide` on PATH
and starts it without a shell. Each invocation has half a second. If it is absent,
recent and explicit paths can still answer. Both the directory lookup and the launch frequencies shown as favorites require
full access, as does browsing directories through `/dirs`.

The fleet loads one kept record per distinct launch directory at startup. A store read failure stops startup. An unreadable record stays on disk and is skipped; the journal says how many were skipped. A later launch in the same directory replaces it. The
storage port accepts text keys with no separate key-length limit in either engine.
The exact stripped path keys its count, first use and last use. Registration writes
the agent and the updated count together. The ledger grows with distinct directories.
Each read ranks the in-memory records without reading the store. A lookup may also
ask zoxide twice, enumerate one parent for explicit completion, and check candidate
directories until it has sixteen distinct matches. Launches before the ledger was
introduced have no historical counts.

**Checking working labels** (19 Nov 2026). The Janitor runner checks the labels site
when its lane comes due. The checker starts paused and the site starts off. A pass
uses the current roster, job and helper views in memory. It selects at most twelve
eligible subjects, oldest unchanged first, before reading at most three conversation
rows per selected subject. One Jev choice request shares their packets. With no
eligible subjects the pass updates only the last check and its adaptive interval.
A repeated set of findings produces one document across the checks; a successful
all-clear permits a later recurrence to be reported. The document belongs to the
checker’s chat and is excluded from Updates. The checker does not edit a subject.
Last check, cadence and fingerprint each occupy a separate fleet record. Diagnostic
entries occupy one hundred slots, each keyed separately; a full ring overwrites its
oldest entry and advances its cursor in one storage operation.

**Model fallbacks**. A Janitor inherits the Host's ordered model policy when its session
is named in `POST /janitor-policy`. The update takes the revision shown by
`GET /janitor-policy`. Each Janitor exposes its effective chain at
`GET /janitors/<session>/effective-model-chain`. Deterministic jobs have no model.
A managed scheduled turn can use the inherited primary on its own backend.

An isolated request by the router, the tool explainer or the Goal keeper starts on its
primary. On provider failure the Host durably claims each alternate before calling it.
A cancellation, permission refusal or changed policy ends the chain. Claims identify
the Janitor, request and alternate's position. A claim survives a restart, even when
its tool never answered. The next distinct request starts with the primary again.
Claude and Codex read a one-shot prompt on stdin. Grok and Muse receive a private
prompt file that is removed when the call ends. For argument-only tools the Host checks
the byte length, including the terminating NUL, before starting the process. An oversized
argument lets the next model try. At startup the Host removes its own private prompt
directories older than an hour.

Routing timeouts name the backend and duration. Attempt receipts retain the original
failure type. A tool explanation uses the failure category and reports `timeout` to
the app. Decision history and recent decisions in settings also hide command lines in
historical timeout errors. The Goal keeper records the model that answered. Conversations
keep their own provider session and account recovery rules.

**Antigravity** (17 Nov 2026). The third tool of the owner's agents (35 of his 191 ran on it when this was
counted), and the one the Goal keeper's job recommends. `clarp-backend` `agy.rs`, by the Python Host's
`backend/agy.py`:

- *A turn.* One process a turn: `agy --dangerously-skip-permissions --output-format stream-json
  --print-timeout 24h [--model M | --effort E] [--conversation ID] --print=<message>`. The message is
  bound to its flag with `=`, which takes the next word whatever it is. Antigravity names a new
  conversation itself, in its first line; the Host learns it there and names it from then on. The stream
  is read step by step: each step in which the agent says something is a message of its own that grows
  while it is written; a tool is told under the name the conversation knows tools by, with what it was
  given under that tool's own keys; the result says how the turn ended, what the answer is where no step
  said it, and what the turn took (what the model thought counts as what it wrote). A turn its provider
  refuses fails in the tool's own word (`status=ERROR: ...`). A message longer than 60 KiB is refused
  before it is kept, in the Python Host's words: a command line takes no more.
- *One question* (`once`, `reads`). Antigravity keeps every conversation under its user's home and lets
  go of all but the newest few hundred, so what a Host asks it all day would push its agents'
  conversations out. A backend says what it needs of its user's home for one question (`own_home`: the
  login, linked; the settings, copied), and the Host makes that home for the question, beside an empty
  directory to work in, and takes both away afterwards. To read and nothing else Antigravity runs in
  its sandbox with its commands off, for two minutes at most, and is held to the answer's shape by a
  schema (`--json-schema`).
- *Its models* (`catalogue.rs`): `agy models` is read as the Python Host reads it, and what that Host
  declares of the tool is in the catalogue's table.
- *In the Host*: listed for the picker, the usage screen and the router; the Goal keeper of a new Host is
  installed on it, as on the Python Host (one installed before keeps Codex until it is set otherwise);
  the installer pins its program beside the other two (`--agy`).
- *A conversation it let go of.* Antigravity keeps the newest few hundred conversations, each in a file
  of its name, and begins an empty one without a word when it is given the name of one it has no more.
  A backend says whether its tool still has a conversation (`Backend::keeps`; for Antigravity that file
  under its user's home, and where there is no store at all nothing is known and it counts as kept).
  Before a turn the actor asks; where the conversation is gone the turn names none, and its message is
  preceded by the Host's own record of what was said, between two marks (`told::restored`: the most
  recent 60,000 bytes, of one thing 4,000 signs with both its ends kept, as the Python Host's
  `_restore_prompt`). When the tool then names its new conversation, the agent's goals follow it
  (`GoalBook::carried`, the Python Host's `carry_native_session`, held by the goal tool's vectors: a
  goal that waits, one that is cancelled, a plan without a goal, a conversation no goal is bound to,
  carried twice), each one revision on with a `rebind` in its history, and a wake that is out for the
  plan as it stood stays the plan's. The conversation the user reads is the one it was: the record is
  told to the tool and written nowhere.

Held by the Python Host's three recordings of agy 1.1.21 and 1.2.16, by one of 1.5.10 made for this
(below), by nine recorded answers of `agy models` and six stories of the tool on a machine from that
Host's own functions, and through the Host with stand-ins: an agent's turn with a command and a streamed
answer, the next turn in the same conversation, a turn that fails, a message that is too long, and the
keeper asking it in a home of its own. Tried with the real tool, agy 1.5.10, on 17 Nov at 14:01, outside
any Host and on its cheapest model, in a home made for it: one question to read (it answered one JSON
object with `status` and `response`, held to the schema it was given; 9,661 tokens went in for a
question of nine words, which is what such a question costs) and one turn with a command (the stream has
the shape of the older recordings; what a command gave back now comes with when it ran and how it ended
written around it, which is read off). The conversation it let go of is held through the Host with a
stand-in that keeps its conversations as the tool does: a turn in a conversation it has, the file taken
away, the next turn without a name and with the record before its message, the goal bound to the new
conversation, the turn after that by the new name, and the same after the Host was killed. What a
conversation Antigravity kept holds is read as the Python Host reads it (`history::agy`, the Python
Host's `agy_transcript.parse_turns`: the steps by their numbers, the tools of a step answered by the
steps after it, a command in the background ended by a later message of the tool, what the model
thought, looking around folded into one cell; held by 13 made-up transcripts of 169 lines in the shapes
agy 1.1 wrote and agy 1.5 writes, in every value and in the order of every key), with two things mended
that the vectors define (the names agy gives its file tools today, and the word it writes before what a
command printed: defects R44 and R45 of the reference, counted in the owner's own store). Those
conversations are listed as the Python Host lists them (`past::agy_sessions`, its `list_sessions`: agy's
own catalogue of them read where there is one, without the helpers within other conversations, the
Host's own scratch directories, and what agy has let go of or wrote nothing of; and otherwise the last
conversation of each directory, shown by what its user asked first; held by six made-up stores, 102
lists and 61 sessions listed), offered for a new agent (`GET /past-sessions?backend=agy`), and taken up
wherever they ran (`POST /agents` with `resume_session_id`), with the Python Host's refusals in its
words. Held through the Host with a made-up store and a stand-in: offered, taken up with the command as
a card and a cell and what the model thought, continued by its name, in use, opened, not forked, not
there, and one that began as a continuation read back without the Host's record. And with the real agy
1.5.10 through the Host installed on elitebook from main `01252f59` (17 Nov at 15:43, one turn on its
cheapest model): the picker, the 61 conversations it kept offered, an agent's turn with a command, its
conversation in agy's own store, and that conversation taken up by another agent from agy's transcript
(`docs/battle-test.md`). Not there yet: making a conversation of Antigravity shorter from an app.

**OpenCode and DeepSeek** (18 Nov 2026). The two backends use the same OpenCode executable and native
session store. A served turn runs under a driver in the shim binary. It contacts a per-turn server on
loopback, verifies the continued session or creates a fresh one, writes and flushes its ID, subscribes
to events and submits the prompt. A startup failure before any submission permits the command-line
fallback. Stop signals the driver once and allows three seconds for its HTTP abort (D26). The picker
reads the command's model listing once for both cards and filters the DeepSeek card by family.
An ordinary or failed turn asks its server to terminate and gives it five seconds for its own
children before escalation. A shim-directed abort ends the driver by SIGTERM. The shim records
the observed exit when both output streams have closed; its group remains reserved until any
outstanding kill is sent. A late escalation cannot append to the finished spool.
The actor counts each step's usage and the turn's own charge. Native history reads the messages and
parts from the shared SQLite store.

**One stop for every tool.** The shim reserves its child's process-group ID until the turn's end and
any stop deadline. A stop first asks the direct child only when its backend has a private abort signal.
It sends TERM to the group and KILL after the caller's grace. A leader that has already exited can
still have a child holding stdout or stderr: until both streams close and the shim commits Exited,
a stop can still reach that child. Repeated stops share the first deadline. The terminal record uses
the observed leader status; cleanup can finish after publication without another record. An unstopped
background child can continue to write to its turn.

The served driver terminates itself with nix SIGTERM after abort. Its HTTP client has no TLS,
compression, proxy or redirect handling enabled. Native command arguments, model pairs and confined
session names are shared with the kernel without linking the Host's speech engines into the shim.

**A conversation made shorter** (17 Nov 2026). `POST /compact`, for Claude Code. The Python Host starts
the tool's own screen in a terminal and types into it; this Host has no terminal. Measured first, with
Claude Code 2.1.321 on Sonnet (three calls, the recording kept as a fixture): given `/compact` as its
whole message in print mode, on a session continued by its name, the tool says that it is compacting,
sums the conversation up, says how much it held before and holds after, and ends well under the same
name without a word of its own. So it is a turn like any other (`clarp-host` `compact.rs`): a message of
the Host's own (`Origin::Trigger` `compaction`, the tool's word from `Backend::shortens_with`), started
bare by the actor (nothing of the Host's before it or beside it: a word more and the tool would answer
it), left out of the chat an app reads, telling nobody. While it runs the agent's row says `compacting`
(the adapter read that already, for the times the tool does it by itself in the middle of a turn);
afterwards the row's `context_tokens` is what the tool said the conversation holds. The answers are the
Python Host's, in its words: started, already at it, no such agent, no live session, busy, unsupported
for the tool (Codex, whose `exec` has no command for it, and Antigravity, whose print mode takes twelve
commands as messages and none for this: measured with agy 1.5.10, where `/compress` went to the model).
Held through the Host
with a stand-in that does what the recording shows, and tried on the Host installed on elitebook with the
real tool on Sonnet (17 Nov, 16:37: from 31,381 tokens to 8,878, the row compacting meanwhile, the third
turn knowing what the first two said; `docs/battle-test.md`).

**An agent whose user is with another raises its hand** (16 Nov 2026). The third thing found by reading
what is on by default on the Python Host, and the one a user with several agents would have heard first:
until this build every agent's spoken reply was announced to the apps the moment it was begun, whoever the
user was with. On the Python Host that is so only for the agent that is open, for the agent a message just
went to (for a minute, and then for no other, not even the open one), and for a reply of a few words. Any
other agent says "<name> here, ready for an update." in its own voice, and what it said is held until the
user opens that agent, writes to it, or says yes and its name. The rule is the kernel's (`herald.rs`: a
machine without clocks, voices or streams, and the classifier of what a user said), held to the Python
Host's own manager through 15 scripts of 120 steps and to its classifier over 106 utterances
(`tools/herald-vectors.py`). The one that speaks asks it at the moment a clip would be announced
(`clarp-host` `voice.rs`), makes the herald's clip beside the reply's, and lets go of what is held when
the user opens an agent, sends to one, or says so: `/transcribe` then answers `herald_consumed`, and the
app sends nothing. One thing at a time happens to the herald, so a clip is never let go before its line
knows it. A held clip's line has the clip and no event until it is announced; an app that was away is
offered only what was announced. Three things are done otherwise, each in the register of differences: a
clip nothing came of is taken back instead of announced later, two agents let speak in one breath are
heard in the order of their sessions, and the settings a change does not name are not written.

**Calls** (16 Nov 2026). The first of the larger things an app's screens were only answered for: a call is
the Host's record of a hands-free conversation with several agents, for the phone that owns it: who is in
it, who is on hold, who has the floor. The phone captures and plays; the record changes by an explicit
request (the app's buttons, an agent's `clarp-admin call`) and by routing, which moves the floor to whoever
was spoken to. The rules are the kernel's (`calls.rs`), the Python Host's contract (`docs/group-calls.md`)
without a store, a clock or a stream, held to that Host's own module through 11 scripts of 186 steps from
two phones and the administrator, over 69 names typed and spoken, and in 13 moments of what an agent of a
call is told (`tools/calls-vectors.py`). At the edge (`clarp-host` `calls.rs`) one request at a time is
asked of them; each phone's calls are kept where the fleet keeps small things, under the Python Host's own
names for what a Host keeps to itself; every change is told to the apps (`group-call`) and noted quietly in
the conversations of the agents it concerns, by a word of the Host's that belongs to no turn. A message
that carries its call goes to the agent it names when that one can take speech, else to the floor, and a
call that is over refuses it. Every message to an agent the user spoke to in a call carries the call's
header to the turn's tool: who else is there, and what was said since the agent last spoke, gathered from
the conversations of the call's agents. The feature is advertised (`group_calls`). Not built with it: how
a call is transcribed (its own feature). And what is no route of this Host, by its path or by its method, is
answered as the Python Host answers it (`not found`, as text): the scenario of calls found that it was
answered with nothing.

**Rings** (16 Nov 2026). An agent calls its owner: it asks the Host to ring the phone with why and with the
line it will open with; the phone shows an incoming call; answering starts a call with that agent on the
floor, and the line is heard first, voiced when the ring was placed, in the agent's own voice. The rules are
the kernel's (`rings.rs`), the Python Host's contract (`docs/agent-rings.md`) for the ring whose line is
said: who may ring (rings on, a contact, not muted, one at a time, a minute between an agent's rings, ten an
hour), how a ring ends once (answered, declined, cancelled by its agent, missed after 45 seconds), the trail
of every request and outcome, the owner's switches, and the token a phone leaves for calls. They are held to
that Host's own module through 6 scripts of 480 steps (147 refused, 95 rings placed) and to its push for a
ring in each state (`tools/rings-vectors.py`). At the edge a ring and the call it becomes are one desk's
(`clarp-host` `calls.rs`), because an answer changes both: a ring is admitted, voiced and placed with no
other ring admitted in between, while reading and answering wait for nobody's voice; a ring that is overdue
is missed by a timer of the Host's own, set again for a ring that rang when the Host stopped; every change
is told to the apps (`agent-ring`), pushed to the owner's phones whether or not anybody sits at a desk (the
incoming call breaks through Focus and has Answer and Decline; what became of it takes its place), and
noted quietly in the agent's conversation. An agent rings as itself with its own tool (`clarp-admin call
ring --reason .. --say .. [--wait]`, `ring-status`, `ring-cancel`, `rings on|off|status`); the next thing its
user says in the call reaches its tool with why it rang. The feature is advertised (`agent_rings`). The listen-first opening is described below.

**A ring held as a live voice call** (18 Nov 2026; the owner uses it: most of the rings his Host keeps are
of this kind, counted there and read only). A ring may ask that its
conversation be held live: the phone that answers talks to a voice model, which is told who it is and what
the call is about, and no call of the ordinary kind starts. The owner may start such a call with an agent
too (`POST /calls/live`). It is built in three layers, each held to the Python Host by that Host's own
answers. A profile may also opt individual stories into the call tools.
*What a call is told* (`clarp-kernel` `live_call.rs`, by `agent_ring_live.py` and
`agent_live_settings.py`; `tools/live-call-vectors.py`): the context a producer gives of an event
(CONTEXT-v1: every field, its length, what a moment with a zone is, as CPython reads one: 5,298 moments),
the profile an operator approved for an agent (its stories, each file pinned by its sum), the snapshot a
ring is frozen with and its digest, what the voice is told from it, the context of a call the user starts,
which agents take such calls (the folder of profiles read as far as it may be, and what is known kept
through a moment in which a profile cannot be read), and the tools a profile opts into (170 profile cases).
*The ring* (`rings/live.rs`, by `agent_rings.py` and `agent_ring_live.py`; `tools/live-ring-vectors.py`,
12 scripts of 585 steps): it is placed only where a phone that can hold such a call still receives rings,
and never as an ordinary ring in its place; the same event rings once; it is shown to the phones that may
be told of it and to nobody else; answering hands one phone a grant that is joined once, within a minute;
the call ends by a close, which one request may ask again, or is switched once to an ordinary call before
its voice opened and before the user was heard, and a close that comes while that call is being started
calls the switch off; a call the user starts is the phone's own, already answered, and no other's to see or
to end; a phone switches rings off for itself. Where the authority of a call lies is on its ring, in a
phase from the answer to the end: whatever acts for a call asks the ring first. An ordinary ring and a
live one are placed and answered through one entry each (`Rings::admit`, `Rings::answer`), which goes by
the kind of ring: no caller chooses.
*The session* (`live_session.rs`, by `agent_ring_live_socket.py`'s `LiveSocket`;
`tools/live-session-vectors.py`, which drives the real class with a stand-in phone, voice and clock: 60
stories of 387 steps, 20 ways a call ends): both sides ready, the opening handed to the voice once after a
moment of silence, what the voice says before it took the opening held and told in its order after, the
user's sound to the voice from the first of it, breaking in, and every way either side is gone, late, or
no longer the call's.
*The Host's part* (`clarp-host` `calls/live.rs`): the profiles as their folders have them (no link leads
out of a profile, a link among the folders is no profile), the requests of an app, and the socket of a call
(`GET /calls/ring/live`, on the line every socket of this Host is carried by, at the door and through the
relay). One task owns one session and stands between the phone and the voice. Every step takes the desk's
lock, writes on the ring, and only then tells either side; a request that ends a call tells its session so
before it lets go of that lock, and a stop that waits is taken before the next tick: so a call is never
retired with its session not told, and nothing is sent for a call that has lost its authority. The voice is
a provider's, reached with the owner's key (`[openai] api_key`, `OPENAI_API_KEY`), which never leaves the
Host; in a test it is a stand-in on this machine (`CLARP_LIVE_ENDPOINT`, which takes a loopback address and
no other). A call that was under way when the Host stopped is over when it starts again; one that was
granted and not joined keeps its minute. The agent's own tool rings so too
(`clarp-admin call ring --oracle --context-file FILE`). The features are advertised (`agent_ring_live`,
`agent_call_live`, `agent_ring_device_optout`), and an agent's row says how it takes the user's call
(`call_mode`).
*The tools* (18 Nov 2026). The admitted call keeps its frozen team, caller and story. Its
voice remains connected while a side router examines new speech and proposes up to eight
actions. The Host checks team membership, records delegation attempts, asks the director
or the scripted agent, and appends receipts to the admitted voice. The director owns the
callback timer. An unclear callback can repeat once under the same operation id. A queued
delegation is checked by one read of the director's intent. The contact's answer returns
in bounded parts and is acknowledged after the voice transport accepts its first part.
An opted-in transcript keeps text, receipts and pacing entries in the call's private directory.
The receiver and sender flows are compared with `live-call-tools-vectors.json`.
**A helper inside a Claude turn** (19 Nov 2026). Agent and Task calls appear as ephemeral
cells on their parent message. The result or background notification updates that cell.
Its lines show the task, type, mode, result or status, usage, and the child transcript path
when available. The Host matches filenames and sidecar metadata within the parent's
session directory. The path is a label. The child's words stay outside the parent's history.
A live turn gets its state from its stream. The measured cost of one refresh is:
- Idle agent: zero directory listings, zero metadata reads, zero parent transcript bytes.
- Live turn with nothing new: one child-directory listing, zero metadata reads, zero parent transcript bytes.
- Live turn with one new child and its sidecar: one listing and one metadata read (at most 64 KiB), zero parent transcript bytes.

The blocking-pool reader makes one final check when the turn ends. For imported cells, the reader consumes appended parent bytes
from its saved position, retaining incomplete lines. A replacement or truncation resets
that cursor. An association that appears, disappears or changes revises the row and tells the app to
read the transcript again. The same projection serves an imported conversation and its
subsequent live turns (`claude_subagent_cells`, contract 14).

*A ring that listens first* (18 Nov 2026). The answering phone claims its opening with
a token and renews its epoch using a rotating nonce. The public ring carries neither.
The greeting is admitted to the calling agent. A silence or microphone claim can instead
admit one initiative, tied to its ring, generation and trace. The Host checks the boot,
instance, call, pairing and Stop generation again before starting the shim. A crashed Host
can reconstruct an intended admission by its reserved id. The group call and its opening
are committed together before the answer or event. Older phones receive the original
spoken line. A clip for the opening carries its token, epoch and admission id, and the
voice worker skips synthesis once its authority has been withdrawn. A ring can leave the public history
while its call continues. Its answering phone can still send a greeting, and closing the
call or revoking the pairing retires its audio. A shim that recorded a failure to spawn
lets the same admission try again. Its proof applies to that attempt alone.
R77 and R78 give opening clips a durable owner. At admission, the opening stores
`clip_bindings` entries with `turn_id`, `kind`, `admission_id` and `epoch`.
The agent and turn identify the clip through synthesis, delayed announcement and replay.
An unbound turn plays ordinarily even when it reuses an opening's trace. An opening and its lookup entries are collected 24 hours after the later of retirement and
call end. An unknown end uses retirement alone. Live calls and unretired openings are kept.
The push by which such a ring reaches a
locked phone as an incoming call came with the next landing ("A ring pushed as an incoming call" below);
until a Host is given the word for it, a live ring reaches a phone whose app is open, and a call the user
starts needs no push. No test and no live check reaches the paid voice.
Tested: each kernel layer by its vectors; the Shelf on real folders; and through a real Host process with
a stand-in voice on a loopback socket (`a_ring_is_held_as_a_live_voice_call_between_a_phone_and_a_voice`):
a ring refused where no phone can hold one, placed, the same event once, shown to the phone that can hold
it and to no other, on `GET /calls/ring` and on the stream of events; the grant; every refusal of the
socket; the join, the voice reached with the key and told who it is, the opening, held sound, the user's
sound, breaking in, the hangup; the user's own call and its close beside the socket; the tool's ring from a
file; an Android phone's word; the switch to an ordinary call; a Host started again; a Host without a key;
a profile with tools.
Wrong rules were planted one at a time, each run against the tests that should catch it. Into the kernel's
three layers 278 at first: 254 were caught and 24 were not. Of those 24, sixteen showed a rule no vector held
and got their cases (among them a pinned file under a name no pin may have that is there all the same, the
user calling while a ring rings, a grant asked for again after its minute, a join that leaves a part out, a
close that comes between the two halves of a switch, a call that lost its authority before its opening, an
interruption before a call is under way, and the two time limits at a clock off the session's ticks); seven
showed code that could never matter, which is gone (a check that an address names nobody, the form of a sum
beside its comparison, two filters of the look at a ring, a guard the one entry makes impossible, a count of
instructions that is one whenever it is asked); and one changes nothing that can be seen and was taken out
with its reason (whichever profile a planted rule goes on with is held to the same agent by the next rule, in
the same words). On the mended code 277 were planted: 275 were caught, one had been given the wrong test to be
run against and is caught by the right one, and one showed a kept detail no script kept (a way to hold a ring
that a phone says twice is kept once), which has its case. Into the Host's part 40: 37 were caught; one of the
three showed a check the kernel already makes (gone), and two got what was missing (an engine that speaks in
the test, so that it is seen that nothing is voiced for a live ring until it is switched; and frames read as
they are written, compact to the phone and with blanks to the voice). What was mended was planted again on the
code as it is landed, and is caught.

**The chip on a phone's lock screen** (16 Nov 2026). A phone that shows the fleet's work as a Live Activity
leaves the Host the token its pushes go to, and the Host keeps the chip current: how many agents work, how
many need the owner, who leads, five rows at most. The rules are the kernel's (`chip.rs`), the Python Host's
(`docs/live-items.md` §8, `server/lib/live_activity.py` and the gate in `apns.py`): what the chip says of a
fleet (top-level agents only: a helper's work and its wait belong to the agent it works under), when it is
said (routine change every fifteen seconds at most, a change of shape ten seconds after the last push, more
that needs the owner at once and loud), the register of activities, and the chip's share of the push
service's budget (ten at once, three hundred a day, a push service that says "too many" left alone for
longer each time), so that the chip can never starve an alert. They are held to that Host's own module by
26 names, 37 activities, 34 fleets, 17 steps of the register, 6 paced scripts and 6 scripts of the budget
of 113 steps (`tools/chip-vectors.py`). At the edge (`clarp-host` `chip.rs`) one task follows what every
agent's turn shows (a feed of statuses from the hub, never what a turn wrote), counts the badge off what the
agents publish, and sends through the gate one push after another, by the Host's own account at the relay:
an activity's token has no grant of the phone's. The relay tells the gate when it is asked too much, by an
alert as well. The feature is listed (`live_activity_push`) only on a Host that has that account. Not built
with it: the same summary for a phone of another kind.

**A ring pushed as an incoming call** (18 Nov 2026). A ring reaches a phone that is locked or asleep as an
incoming call only by a push of a kind the relay does not carry: the Host hands it to the push service
itself, with a key its owner gave it there. The owner's Python Host has such a key, and his rings ring so.
This Host does it where it is told the file that names the owner's key (`--push-keys`; in it `[apns]`:
`key_path`, `key_id`, `team_id`, and `bundle_id` and `environment` where they are not the usual ones, as
the Python Host reads them). That word is one of its own, and off where a Host is installed: the file of
the keys of speech is read by a Host that is only being tried too, and such a Host is not to push to its
owner's phones with his key because that file names it (shown in the test of the live call, whose Host
reads such a file and hands the push service nothing). Without the word, or the key, nothing is handed to
the service, and whoever rings is told so (`delivery.voip.enabled` is false). The rules are the kernel's, held to the Python Host's own module: who is pushed a ring as a call,
what became of it as the one who rang is told, and that the alert follows only where no phone was reached
and never for a ring held as a live voice call (`rings/live.rs`, by that Host's `send_agent_ring_voip` run
over a stand-in for the push service in `tools/live-ring-vectors.py`: 80 pushes handed over in 13 scripts
of 653 steps, two of them with why the agent calls, since the owner let a locked phone show it); and where
the service is, the request for one push of every kind, what an answer means,
what a token of the Host's says and for how long one is used, and when a push is handed over a second time
(`direct_push.rs`, `tools/direct-push-vectors.py`). The Host's part (`clarp-host` `direct.rs`): the key is
read and a token signed with it (ES256), and the service is spoken to over HTTP/2, over one connection for
the pushes of one ring. That is the one new thing this Host needs from outside for it (`h2`, the HTTP/2
client of the hyper project; the signing is done by the provider of cryptography the Host's TLS already
brings). The key never leaves the Host, and nothing said of a failure holds the key, a token signed with
it, a phone's token or what a push says. A phone whose pairing was taken away is not rung, though the
token it left is still there: this Host did so from the start, and the Python Host does since its
contract 101 (R56, reported and fixed there on 18 Nov 2026), with which neither a call push nor an alert
says why the agent calls unless the owner lets a locked phone show it.
Tested: the kernel by its vectors; the signing against the key's public half; and through a real Host
process with a stand-in push service that speaks HTTP/2 on this machine (`CLARP_APNS_ENDPOINT`, a loopback
address and no other): no push without a key; with one, each phone that left a token is handed the push,
with the token of the Host's signed for the owner's team and the ring as the app reads it, and no alert
follows; an alert where the service took the push for no phone; a token the service refuses signed anew
once; a phone that switched rings off and one whose pairing was taken away left out; a live ring pushed to
the phone that can hold one, with no alert whatever became of the push. Wrong rules were planted one at a
time, 62 into the kernel's rules and the Host's part: 58 were caught and 4 were not. A token refused twice
was signed a third time unseen (the story has an answer more, which a third try would take). A push was
handed over again after a failure that came when its request was already on its way: the Python Host does
not, and nobody knows then whether it reached the service, so that code is gone. The pusher's check of the
kind of a ring's job and the desk's skip of what becomes of a live ring changed nothing when they were
planted against, being second places for the rule that a live ring has no alert: they are gone, and the
rule is planted where it is, in the pusher by the kernel's function. On the mended code the 17 rules that
touch what was mended were planted again, and all were caught.

**The Oracle: its contact and the voice it speaks with** (18 Nov 2026). The Oracle is the voice the owner
speaks with hands-free; what it is asked to look into it hands to one agent, its contact. This is the first
of its parts on this Host, and the smallest: who the contact is (`GET` and `POST /oracle/contact`) and the
owner's choice between the two voice models a call of it can run on (`GET` and `POST
/oracle/voice-provider`), both kept among the Host's settings under the names the Python Host keeps them
under (`oracle.contact_session`, `oracle.voice_provider`). The rules are the kernel's (`oracle.rs`, by that
Host's `oracle_contact.py` and `oracle_voice_provider.py` themselves; `tools/oracle-vectors.py`: 630 names
looked for in 9 sets of agents, 153 readings of what is kept, 1,260 choosings, 540 calls, 112 settings of
the voice): a name means the one agent that has it as its session or is called it, whatever the case of
the letters; a contact that names no agent any more is let go and said to be stale once, so that an app
can ask its user to choose again; a name that is nobody's is refused in that Host's words and never kept;
and a voice that cannot run here (the second, without a key for it) falls back to the first and says why.
Names are compared as Python compares them where the case is not to matter (`str.casefold()`, which is not
lower-casing: `python::casefold`, by that interpreter's own table of 1,530 signs, written down by
`tools/casefold-table.py`). The Host's part (`clarp-host` `oracle.rs`): the contact is held against the
roster as it is when it is asked (a janitor is no agent of it), the owner's file is read where it is each
time the voice is asked (`[oracle] voice_provider`, `gemini_model`, `gemini_voice`; the key and the voices
of `[gemini_tts]`), and one request at a time reads and writes what is kept, so that a contact chosen
while a stale one is let go is not let go with it. With it, a rule of the Python Host's that this Host
lacked: a Host without a token, which asks nobody who they are, answers nothing of the Oracle (401
`Oracle requires authenticated full-device access`, for `/oracle` and everything under it). The Oracle's
calls came after ("The Oracle: a call on this Host" below), and use what is chosen here. Wrong rules were planted one at a time, 82
of them (53 into the kernel's rules, 2 into the folding of case, 20 into the Host's part, 7 into the rule
for a Host without a token), after the vectors had got the cases a rule had none for: all 82 were caught.
Tested: the kernel by its vectors; and
through a real Host process: a contact chosen by name and by session and kept over a restart, the
refusals in their words, a janitor that is nobody's contact, a contact put away that is let go and said
once, and the voice with a file that names the second voice without a key, a phone's choice over the
file, a key written into the file while the Host runs, the voice the owner names, and the choice kept
over a restart; and a Host without a token refuses each of the Oracle's routes.

**The Oracle: what it hands to agents** (18 Nov 2026). What the Oracle is asked to look into it hands to an
agent as a turn of that agent's, and the Host keeps a record of each such delegation from the moment it is
taken until its result was delivered (`POST /oracle/delegations`, `GET /oracle/delegations`, `POST
/oracle/delegations/ack`, `/cancel`). The owner's main Host has several hundred of them. The record is the
kernel's book (`oracle/delegations.rs`, by the Python Host's `oracle_delegations.py` itself, run as it is on
a database of the generator's own with a stand-in for what admits a turn; `tools/oracle-delegation-vectors.py`:
11 stories of 181 steps, and after every step the whole book must be what that Host's table was): a
delegation is taken once however often it is asked, and refused where the same id comes with other work;
it waits in the agent's queue or is under way; it ends with the turn's answer, with its failure, or with a
stop of the agent, which also calls off one that failed while it was being stopped and leaves one that
ended with an answer meanwhile; it is shown to the one who asked, and to nobody else, until they say they
have it; and what an agent's live call asked is shown to no phone. The Host's part (`clarp-host`
`oracle/delegations.rs`): the book is kept with the Host row by row and written before anybody is told; a
delegation's turn is a message like any other, sent as the Oracle's under the delegation's own ids, without
speech, waiting where the agent is busy; and what became of that turn is asked of the agent it was handed
to, by the id of the message that began it, each time the book is read (`Actor::fate_of`: it waits, it
runs, it ended and how, with what it left and when). An agent's conversation is the one record of its
turns and is there after a restart, so no delegation is ended by a guess: a turn that ran when the Host
was killed is the agent's to finish when the Host comes back, and its delegation ends with its answer,
stamped with the moment the turn ended. A stop for the Oracle stops the agent first and calls off only
then, so that a stop that fails leaves every delegation as it was; what waited is taken out of the queue.
The session of the agent's tool is kept with a delegation as it is when the turn is admitted, and for an
agent that had not run yet, from the moment its tool has one.
Tested: the kernel by its vectors; and through a real Host process: every refusal in its words, work
handed on and answered, asked again and told as it is, the same id with other work, the phone's word that
it has a result, a turn that fails (in the tool's words) and one that ends with nothing said, the two
listings and their order, work that waits behind a busy agent, a stop that calls off what waited and one
that ends the delegated turn, a phone's own delegations that nobody else is shown, two results that came
unread told in the order their turns ended, and a turn cut off by a kill of the Host whose delegation
ends with its answer after the Host is back, with every other row as it was kept.

**The Oracle: a long text relayed to its voice in parts** (18 Nov 2026; the kernel's rules, which nothing
of the Host uses until the Oracle's conversation is built). A voice model takes a bounded piece of text at
a time, and an agent's answer is often longer. The Python Host cuts such a text into numbered parts without
losing a sign, hands the voice one part, waits until it was spoken, and hands it the next; each part says
which it is, how much remains and how it is to be read, and only the last says that it is the end; a relay
keeps where it is, so that a reply can be continued or read again from the text that is kept and the agent
is not asked twice. The rules are ported as they are (`oracle/relay.rs`, by that Host's `oracle_relay.py`
and `oracle_context.context_chunks` themselves; `tools/oracle-relay-vectors.py`: 174 cuttings of 19 texts,
10 relays with 160 parts, 52 answers, 10 conversations): how a text is cut (after the last blank of a piece
that leaves it at least half full, else where the bytes run out; a sign of several bytes is never cut),
how a relay is sized (the longest head a part of it can have, and its text, fit into one piece for the
voice), what the head of a part says for every way it can be handed over, what a relay says of how far it
came, what of an answer is relayed (what the agent marked to be spoken, where it marked anything), what of
a request is said with it, and the relay of an agent's recent conversation.

**The Oracle: the cues of sound a call plays** (18 Nov 2026; the kernel's rules). A call tells its user what
happens without a word: work was handed on, a result came, the call is put through to an agent, it is back,
it could not be put through. Each cue is a few hundred milliseconds of 24 kHz sound that the Host makes
itself and sends as sound of the voice, so that every phone plays it; the chime of a call put through is
pitched by the agent's name, one of five notes. The rules are ported as they are (`oracle/earcons.rs`, by
the Python Host's `oracle_earcons.py` itself; `tools/oracle-earcon-vectors.py`: 33 sounds held sample for
sample, by their first, middle and last samples, their loudest and the digest of the whole; 24 names with
their notes).

**The Oracle: the conversation of a call** (18 Nov 2026; the kernel's rules). A call of the Oracle is a
conversation between a phone, a voice model that holds no tools, and the Host between them. The kernel has
it as one machine without a clock, a socket or a store (`oracle/conversation.rs`, `routing.rs`, `phone.rs`,
`attention.rs`, `voice.rs`; by the Python Host's `oracle_live_stable.Conversation`, `oracle_strategy`,
`oracle_attention` and `oracle_handoff` themselves, run as they are with a clock, a voice, a router and
tools of the generator's; `tools/oracle-conversation-vectors.py`: 79 stories of 1609 steps, and after every
step what the voice and the phone were told, what was kept, and what waits to be routed must be what that
Host's were). What it holds: what a phone may send and what is none of it; sound both ways, and silence
only for a moment after what was heard; what was said, a fragment at a time, with the number of the talk
that every word of the user's moves on; the routing of what the user asked, once they have stopped talking
for a second (the question a router is asked, with the talk, the agents, the work of the call and how far
each answer was told; what it answers taken up thing by thing, the same thing once; an action written down
as begun before anything is done for it, under a name that is the same whenever its place is asked for;
where a request came from written for the agent that is handed it; the user's words that came meanwhile
left for the next routing); a call in which everything goes to the contact, unread; results told between
two turns of the talk, each with its cue, a long one in parts that follow what the voice spoke, pause after
eight, and are read again or gone on with from the text that is kept; what waits for the user told one
thing at a time and once in a thread; a call that goes on within half a minute of the one before as that
call; and the call put through to an agent, as far as a conversation knows of it. Each result is told once,
by the rules the Python Host took on with its PR 421 the same day (R58 to R61): an answer counts as told
when its last part was handed over, however it was asked for, also where only that part was; one that was
told or read part of the way is gone on with from where it stopped, with its cue, in this call or in one
that goes on in its place; whether it is told is decided before its cue, so no cue sounds for an answer the
contact gave under another delegation; work that is named and not over is said to be not over, and is no
text to read; and a stop of an agent silences what of its work is over and not told (not what was called
off or handed on before the call), holds what is being told of it, and keeps that with the thread
(`silenced_results`, only where there is any), so that no later call tells it by itself; it is still read
when asked for. A result that is over only after the stop read the call's work is told as any other. An
answer paused at the eighth part is paused on purpose: a call that goes on in its place does not go on
past it by itself, nor this call where it also waited to be told, and a reading on request lifts that
(the Python Host's follow-ups, its PRs 429 and 435, the same evening).
What is kept of what was told and silenced is this call's own work only, and of the contact's answers the
newest five hundred, so that a thread does not grow with every call (an answer known only beyond those is
told once more under its next alias); a thread that could not be kept ends
no call (it is said once, and a call that goes on in its place reads what was kept last), unless the
thread is another call's now. Time passes in two
halves (whether this is the moment to look for what waits for the user; then what was found), so that a
Host reads what waits only then: the vectors hold at every tick whether the Python Host looks. The
generator plays every story with its clock a ten-millionth faster and slower and refuses a story that is
decided on the edge of a threshold. What the voice is told when a call begins and what a router is asked
with (`voice.json`, `router.json`) are written from that Host's own modules by the same generator.

**The Oracle: the memory of its threads** (18 Nov 2026; the kernel's rules). A call belongs to a thread:
what one owner and the Oracle's contact said to each other, across calls. The kernel keeps of a thread
which connection has it now, what was kept of the talk, the actions that were begun, the work that was
handed on in it, and what its voice was told of (`oracle/threads.rs`, by the Python Host's
`oracle_memory.py`, `oracle_attention.claim_context` and `oracle_result_context.result_payload` themselves
on a database of the generator's; `tools/oracle-memory-vectors.py`: 18 stories of 262 steps, and after
every step the tables must be what that Host's were; at every step it is also held which connection has
its thread, and that the rows the memory says it changed make the whole memory again). The thread a call
goes on in is the one a connection has, else the one last written; a thread asked for by its name must be
its owner's and of the call's contact; what changes a thread is done by the connection that has it, and a
connection that lost its thread can still read it; an action is begun once under the name of its place,
and never as another action; an action whose receipt was lost is ended by the work it is known to have
handed on; and the excerpt a call begins with is whole records within a bound, the newest result first,
with times on the clocks of the Host's machine.

**The Oracle: the tools of the fleet** (18 Nov 2026; the kernel's rules). What a router may want done: the
agents listed, an agent looked at, what it said read for a sentence about it or for its own words, an agent
stopped with or without other work in the place of what it was doing, and work handed to an agent or to the
contact. The kernel says what each means for a Host and what it is answered (`oracle/tools.rs`, by the
Python Host's `oracle_calls_stable.AgentTools` and its readers themselves; `tools/oracle-tools-vectors.py`:
132 things wanted of which 59 are refused, 64 readings within their bounds, 88 conversations, 48 states).

**The Oracle: a call on this Host** (18 Nov 2026; `GET /oracle/v2`, `GET /oracle/status`). The Host adds
what a conversation cannot know (`clarp-host` `oracle/call.rs`, with `router.rs`, `memory.rs`, `tools.rs`):
who may open the socket and how; one call a phone at a time, a later call of the same phone taking the
older one's place, and a call asked for while the one before is ending waiting for it; the voice reached
with the owner's key over a socket of its own, and the router asked with the same key over the web; and
one task that owns the conversation of a call and stands between its phone and its voice, while the
router's thinking, a tool at work and a file being written are done apart and come back to that task as
something that happened, so that sound goes both ways all the while. The memory is kept with the Host row
by row, read when it is first asked for and written before anybody is told, and is there after a restart.
The tools are done with the fleet: an agent's state and what it said are read from the agent itself, work
is handed on through the book of delegations as any delegation is, and a stop is the book's stop of an
agent. An action is written down and its thread confirmed as this call's before its file is written or its
tool runs; one that is not taken as begun fails its routing, and the call goes on. The socket refuses in
the Python Host's words what is no call (no upgrade, a phone of limited rights, no key, a strategy, a
contact, a thread or a handoff that is asked for wrongly, a phone that has a voice, a voice that cannot be
reached). The status says what this Host holds: calls of this kind where the owner's key is there, in the
thread of their owner and contact or anew, also to the contact alone; and nothing of what is not built.
The features are listed (`oracle`, `oracle_contact`, `oracle_direct_contact`, `oracle_context_memory`,
`oracle_earcons`, and since the section on it below `oracle_handoff`). Not built yet, and said so: the
second voice, podcast detours, the words a contact's vocabulary was prepared in, the first kind of call
and WebRTC.
Tested through a real Host process, with a voice and a router that stand in on the test's own machine (no
test reaches a provider): every refusal in its words; a whole call (how the voice is told the call begins,
with the agents and what was kept; sound both ways; a wanting said twice routed once; the question the
router is asked, in the letters of the talk; work handed on as a delegation of the phone's with the file of
where it came from, which only the owner reads; what waits for the user told once; the result with its
cue; a look at an agent and its conversation read to the voice; work that takes long stopped with other
work in its place, and what the stop silenced not told, in that call or in the next; a later call of the
phone taking the older one's place and going on in the thread with its work and what its voice was told of; the end by the phone, by
the voice in both its ways and by a phone that went away; a call that begins anew; a thread by its name
and by its contact; and the thread as it was left after the Host was started again); a call in which
everything goes to the contact, with the languages its owner wrote and no cues where the owner wants none;
a Host without a key, a voice that does not take the key, a router that does not answer as one, a talk
too long to keep (the call goes on), a file
that cannot be written, work for an agent there is none of, a tool there is none of, and then the same
call handing work on as any other; and a call carried through the relay as it is at the door (opened,
sound both ways, ended by the phone, refused, and left by a phone that went away). Four defects of the reference were found on the way (R58 to R61); the Python Host mended them the same day (its PR 421), and this Host does as it does since.

**The Oracle: what agents finished** (18 Nov 2026). Between two turns of a call its voice is also told,
as something from before the call that it is not to bring up by itself, of what an agent finished for the
user and the user did not go on about. Which finished turns are shown, the newest first, at most twenty,
none older than a day, and as old once a quarter of an hour has passed, is the kernel's rule
(`oracle/attention.rs`, `finished`: the Python Host's `oracle_attention.pending_completion_notifications`,
run as it is on a database of the generator's own, `tools/oracle-completions-vectors.py`: 13 cases, 178
turns, 31 askings). What that rule is handed is this Host's own. The Python Host keeps a row for every
end of a turn it told its user of, and compares clocks to see whether the user wrote since. This Host
keeps no such list: an agent's log has it all. Each agent's thread says, in what anyone may read of the
agent without asking it, what the agent last finished for the user or the Oracle: the turn that answers
the newest message of theirs in its conversation, where that turn ran to its end with something for the
user in it and nothing of theirs waits in its mailbox (`clarp-actor`, `last_finished_for`). It is read
anew when a turn of theirs begins or ends, when the mailbox changes, when the agent is renamed, and when
the Host starts; it is gone when the agent is started again. A call collects these when it looks for what
waits, and the rule does the rest. What the Oracle itself handed on is shown as well, as on the Python
Host: its voice is told the result, and later has it as background. The end of a turn is told to the
apps under the name the Python Host gives it (`notify::id`: of the agent and the moment), which is the
name a voice is told it under, the same whenever it is told again. The walk through an agent's log has a
test of the actor's own (what another agent wrote or left waiting changes nothing; what the user left
waiting does, until he takes it back; a turn that failed or said nothing finished nothing; the same after
a restart; nothing of a conversation that was started again). Tested through a real Host process: a call
told of what agents finished, the newest first and one thing at a time, in the Python Host's words, under
the name the apps were told and by the name an agent was given since; not of what the user wrote to since
(a turn that runs, a message that waits, a turn that failed), of what one agent answered another, or of a
conversation that was started again, each of which ended later and would have been told first; and a
later call of the thread told only of what the call before had not told: what an agent had finished
before the user wrote to it again, once he took those words back.

**The Oracle: a call put through to an agent, and back** (18 Nov 2026; `POST /oracle/connect`, `GET
/oracle/handoff`, `POST /oracle/handoffs/ack`, the feature `oracle_handoff`; the contract with the phone is
`docs/oracle-handoff.md`). The user asks the Oracle for an agent and the phone leaves the call for a talk
with that agent, hands free; later it comes back. The phone owns the sound and the Host owns the record: a
handoff is offered, prepared, activated and made, each step acknowledged by the phone under the number the
handoff has, and each with a time after which the Host acts by itself. The record and every rule of it are
the kernel's (`oracle/handoffs.rs`): who is offered what, the steps and which of them is one from where a
handoff stands, what is said twice and taken once, the deadlines, what is over and what of it is kept,
what the call of the Oracle is while its phone is away, and what a call that is open, the apps and the
Host's focus are told, in its order. It is the Python Host's module (`oracle_handoffs.py`), run as it is
with a clock, timers, names and calls of the generator's own (`tools/oracle-handoff-vectors.py`: 15
stories, 568 steps, 71 of them refused, 923 things told, 15 deadlines), and at every step the test shows
besides that a deadline which is not the handoff's changes nothing. Three conditions of that module that
nothing can reach are not in the kernel, each found by a wrong rule the vectors could not catch. The Host
(`clarp-host` `oracle/handoffs.rs`) keeps a book for each phone with the fleet, has one request at the desk
at a time, tells what the desk says to tell before the next has it, and runs the deadlines from one task,
from its start. A call registers with the desk when it is carried, before its phone or its voice is told
anything else, and a phone that asked for handoffs is told first which call this is (`oracle_v2.session`);
what the desk says to a call comes to the call's own task, which tells its conversation (the chime of the
agent, the handoff as the phone sees it on the call's socket, hold, feed again, a failure, the user back).
Whoever asks for a handoff is kept until the phone has settled it, or for every step's time and a little
more. An agent asks with its own tool (`clarp-admin oracle connect`). Tested through a real Host process
with the stand-ins of a call: a phone that did not ask is offered none; one that did is told which call
this is, put through step by step (the offer on the call's socket and to the apps, the chime, a stale
number and a step that is none refused, nothing else offered meanwhile, the same said twice), the focus
following; the way back by a call that names it, with its cue and the word to the voice; an offer nobody
answers failing in ten seconds, asked for with the agent's tool; and the tool's own words where it is
asked wrongly.

**The Oracle: the words an owner prepared for its contact** (18 Nov 2026). An owner can prepare, for the
agent that is the Oracle's contact, the names and words that occur in what is said to it: a small file that
names the contact, the text the words were taken from (by its path and its digest) and the words. Where such
a file is named (`CLARP_ORACLE_VOICE_CONTEXT_FILE` in the Host's environment, else `[openai]
oracle_voice_context_file` in the owner's own file; the environment's word is the first, and an empty one
is none), a call of the Oracle reads it when its contact is known and before anything else is asked of the
call. The voice of a call with that contact is told the words, after who the agents are and who its contact
is, as reference for hearing and saying them and no change to what it may do. A file that is not as it must
be is no call: it is refused in words (400) before the voice is reached, and so is a call that has another
contact, or none, while the file is named. Which file is one is the kernel's rule (`oracle/voice.rs`,
`prepared`: the Python Host's `oracle_voice_context.load`, run as it is on files of the generator's own,
`tools/oracle-vocabulary-vectors.py`: 63 cases): at most 16,384 bytes of JSON that is an object of the first
version for this contact; the text at a whole path, a file of at most two mebibytes whose digest is the one
written; one to sixty-four words, each a text of one to 160 signs on one line that stands in the text
whatever the case of its letters (folded as Python folds them), 3,000 signs together at most. The words
are told as they stand in the file, in its order. The kernel is handed the bytes of the small file and
something that reads a text at a path; the Host reads both on a thread of their own, and never more of
either than it may have and a byte. Where the Python Host refuses in the words of its language (a file
that is not there or is no JSON, a path that is no text, a text that is not UTF-8), this Host says in
words of its own what could not be read; and a small file that is JSON to Python's reader alone is none
here (`docs/differences.md`). The Host's test holds a call with the words told, each refusal at the door
with the voice not reached, the two bounds from both sides, a path from where the Host stands, what is no
file, the owner's file in place of the environment and the environment before it.

**The Oracle: its second voice** (18 Nov 2026). The phone speaks the same protocol on either voice.
Gemini Live takes the languages and history at setup, sends transcripts and PCM audio, and asks the Host
for work through a non-blocking function. A routing collects the Host's appends and answers that function
when it finishes. A phone interrupt suppresses the remainder of the current reply. A provider interrupt
flushes buffered playback and holds the active relay. Token usage and the final estimate belong to the
voice alone. A replacement connection takes the latest handle after a departure notice or a lost socket.
The OpenAI key is required at the door for routing. An initial Gemini connection failure falls back to
GPT-Live and holds further Gemini attempts for ten minutes. Status continues to describe the owner's
choice and the keys. The adapter, cumulative usage and conversation effects are recorded from the Python
Host by the Gemini (38 stories, 384 steps), Live usage (9 stories, 126 steps) and conversation
(80 stories, 1623 steps) vector generators. The Gemini fixture uses the reference repaired in PR 430.
**How a call participant is transcribed** (19 Nov 2026). Feature 74 lets a client mark a clip with
its call and captured session. Before a cached reply or any speech engine sees the request, the Host
checks the call's owner, Host, live state and reachable participant. The participant's language and
engine list each override the call setting. A device model precedes the global engine. Settings are
read together, and each engine attempt records its outcome and the language handed over. A live call
shows the participant map at read time. The completed vocabulary run for a call clip records the validated
session and the same `stt` report returned to the client. A cached retry reuses that run. The fleet commits each settings request in one store
transaction before publishing its directory. Call-job fingerprints bind the validated principal,
call ID and participant as specified for R87.

**Routing long recordings** (19 Nov 2026). A batch upload at or above the configured duration
can use a different model. A participant or call policy that chooses its engines keeps them.
The WAV probe reads the PCM frame count and sample rate. For other containers an idle loaded
speech worker checks container time, then stream time, then decoded sample count. An hour
without duration metadata may be decoded twice, once for measurement and once for transcription.
The probe child gets five seconds and the Host bounds the worker reply at six. A missing, busy,
failed or late probe leaves the ordinary engine selection in effect. Both paths return the usual
batch response. The word of this feature changes no upload limit or client timeout.

**Whose turn calls** (19 Nov 2026; `clarp_kernel::turn_identity`, `clarp-host/src/turn_identity.rs`). Every
turn's tool is given a token in `CLARP_PROVIDER_TURN`, and a Clarp tool inside the turn, or a worker it left
behind, presents it as `X-Clarp-Turn` (the tools of this Host pass it with their call). The token says whose turn
it is, a version, the agent's id in hex and the turn's number, and carries an HMAC-SHA256 over exactly those
fields with a key of the Host's own (the RustCrypto `hmac` crate, checked in constant time). Nothing is written
for a turn: its launch writes what it wrote before, and the token is made from the turn's number as it starts.
The key is 32 random bytes from the system, made once and kept as one record with the fleet (kind
`turn-identity`), read when the Host starts; it is never written to a log line and no route answers it. A token
names its agent while that turn runs and for seven days after it ended, the Python Host's rule
(`TURN_RETENTION_MS`); the agent's own log says which, asked by the turn's number (the turn that runs, and the
row that ended it: two keyed reads, no scan). One verification reads the key from memory and asks the agent's
actor one question. A token of another key, of a turn the log does not know, of an agent that is gone, or whose
MAC does not verify names nobody, and the route answers 403 `verified turn identity required`; a key or a log that
cannot be read is a 503 `turn identity unavailable` with `retryable: true`, as the Python Host answers a lookup
that failed. A key record that is there but cannot be read is never made anew over: the tokens given with it
would all name nobody. A key made at the start that the store does not keep is used by nobody, and
said once in the Host's log as every failed write is (`FleetHandle::unkept`). Neither the edge nor the route writes a request's headers to a log line. The outcomes are
held to the Python Host's `caller_agent_id` over turns that run, that ended around seven days ago, a token never
given and a store that cannot be read (`tests/turn_identity_vectors.rs`); how a token is made is this Host's own
(see the differences page). The one route that reads it here is the answer to a request (`POST
/decisions/<id>/resolve`): an agent's turn never answers as the user. The goals' door (`plans.rs`) still refuses a
turn's header, for a later piece. The wrong rules are `tools/plants/turn-identity.py`.

**A request answered in the owner's place** (19 Nov 2026; Host feature `decision_on_behalf`, 79). The owner names
the sessions that may answer his requests for him in his own file, `[agents] decision_on_behalf_sessions` (the
file `--speech-keys` names), read as the Python Host's `config.load` reads it: a list of texts, blanks trimmed,
empty ones and repeats left out. `POST /decisions/<id>/resolve` with `on_behalf_session` hands the kernel's rule
that list and the session of the turn that asks, known by its token (above): a session the owner did not name, a
turn of another session, a request without a turn, and the asker answering its own request are refused 403 in the
Python Host's words, and the answer is recorded as `agent:<session>`. The janitors and the agents put away count
among the agents whose session may answer, as there. The agents' tool `clarp-agent-artifacts answer-on-behalf`
answers so with the token of the turn it is called in. Held by the Python Host's `artifacts.resolve` over every
refusal and two answers (the last scenario of `artifact-vectors.json`) and its `config.load` over nine owner's
files (`decision-on-behalf-vectors.json`); the wrong rules are `tools/plants/decision-on-behalf.py`.

**Transcription guidance** (19 Nov 2026). The guidance settings route reads and updates the names
switch and the technical glossary. Its previews count Unicode characters and estimate tokens within
the reference limits. The route and delegated speech share the active roster. Own agents come
first, then helpers, including completed helpers that are still active. Within each group the
latest turn start comes first, with ties resolved by creation time and agent ID. Deduplication
keeps the first casefolded spelling. The settings preview skips a name that exceeds either budget
and continues. Speech compilation receives every name with rarity 0.9 and applies the provider's
budget. The existing agent event fold maintains the maximum turn start during operation and log
replay. For a hundred members a request reads a hundred published views in memory and sorts the
eligible names. It sends no actor requests and does no store reads for the roster.

**Quiet agents** (19 Nov 2026; Host feature `quiet_agents`, 56). An agent whose activity never claims its owner's
attention by itself is quiet: today exactly the janitors, by the kernel's one definition (`roster::quiet`, held to
the Python Host's `quiet_agents.is_quiet` over agent rows of every shape). It is asked where a record of either kind
can appear: the snapshot's row (`quiet`), what a turn tells (`wire::user_notification` gives it to the kernel's
rule, which suppresses it `janitor-maintenance` whatever began the turn), and the Live Activity, whose stream of
turns tells of a janitor's turns as well: its roster now has the janitors, each marked quiet, and their work is not
"agents working" (the Host test found a janitor's run on the lock screen before). A janitor's own runs were suppressed
before by their origin, and a turn a person began cannot end as a janitor's here: an agent at work, or with work
waiting, is not made a janitor (409 `agent_busy`), and a janitor's chat takes no message (403); the rule stands all the
same, held by the notification's own test. Where only the roster's members are walked, the attention
list, the Updates, the badge, a request's push, the separation of members and janitors is the rule, and a branch for quiet there would never run: a janitor which asks or publishes is refused as an agent the
Host has not, and what it made as a member leaves the lists with it. Held by
`a_janitor_reaches_none_of_its_owners_attention_and_its_turns_tell_nobody` (a phone pushed to and showing the agents at
work; Keeper asks and publishes as a member, is refused as a janitor while its turn runs, is made one once idle, is
refused a message, and its own run is held while Mike works: no notification, no push, nothing in `/attention`, the
inbox or the chip, and its new request and document are refused) and the wrong rules `tools/plants/quiet-agents.py`.

**What may stop the Host from starting** (20 Nov 2026; the lead's rulings of that day). Only its configuration, its
listener, a store that does not answer and a core thread that does not start stop a start. What it keeps is read through
one loader (`kept.rs`): each record of a kind alone, and one that cannot be read is LEFT OUT of what the Host does and
seen by no desk, counted by its kind and said once in the journal by its kind and key, never by what it holds (a row
stays where it is, never written over or deleted by the Host and hidden from every read until the owner repairs or
deletes it; a book's record is MOVED aside whole, in one write, never lost: the copy under `<key>.unreadable`, beside a
copy that stands the next free name, and the key let go, so its desk starts without it; a copy is counted at every start
while it stands, through a register written with each move, and a delete of its key takes it along, said as one delete;
a desk that would write over what it cannot see, a counter or a ledger's tail, is told a record was there and refuses) (a secret key, a push token or an opening's token, not even in part: such a record is said by its place in its
kind's count). The counts are said in one line when the Host starts and shown at the
Host's own door, `GET /host/start` (the administrator's only: `{agents, opening, broken, left_out, not_run,
judged_when_read}`, how many agents are still being opened and how many could not be beside what could not be read, the
agents kept and not run for want of their tool, and the kinds counted only when read); `/server-info` and `/status` keep the
reference's shape in every state, since the count is not zero for a moment at every start. `install-host` asks it until
nothing is opening, prints what is broken and left out and ends with code 3 when either is not zero; the Host stays up. The one write
such a key gets is the owner's own (a token registered again, a peer named again): that is its repair, said, and the
record is no longer counted (the fleet's store is watched for it). An agent the Host keeps and does not run (left out,
where its bytes are JSON that name it, or read and not set up for a tool this Host has not) keeps its session and the
name it goes by: no new agent gets them. A deleted agent whose goals cannot be retired is retired at a later start and
counted until then. Every kind is declared once beside its desk (`kept_registry.rs`: its table, policy, whether its key
may be printed, its desk's own type) with WHEN IT IS JUDGED: at the start, before the door opens, where some desk lists it
at the start anyway, so that a start costs what the live fleet weighs (every kind judged by its desk's own type, plain
JSON or bytes only where the desk reads it so); or when read, where it grows with a history (a
vocabulary run, a form's event, a fallback attempt, the Oracle's threads), and then it is read nowhere raw, only through
`kept_book` (by key) and `kept_listed` (whole), and a record that is not its desk's is judged at that read. One judge
(`kept::judge`) does both: a row left out, a book's record set aside under `<key>.unreadable`, counted, said once. A desk
that would write over what it cannot see (a counter, a ledger's tail) is told a record was there and refuses rather than
begin anew. The start's line claims only what was read at the start. Held by the loader's tests and the fleet's at the
port (`fleet/tolerance_tests.rs`: stores written by their own crate before a start, a store that does not answer, a kind
judged when read), the registry's own tests (no kind kept that it lacks, none declared twice, none judged when read read
raw), the Host's (`start_tolerance.rs`) and the wrong rules `tools/plants/start-tolerance.py`.
Later, not part of this piece (the lead's note of 20 Nov): a copy that fits again under a later build (a build rolled
back and forward) could be taken back when its key is free.

**Who stops or repairs an agent, and why** (19 Nov 2026; Host feature `turn_slot_repair`, 61). `POST /stop` takes
`actor` and `reason`, and so does `POST /repair-slot {session, reason, actor?}`; both are read as the Python Host reads
them (`actor` cut to 80 signs, `user` where none is said; `reason` cut to 200; what is no text is written as Python
writes it). The Host knows who acts where an agent's own turn asks (its token, `X-Clarp-Turn`: then it is
`agent:<its session>` whatever was claimed) or a paired phone says it is the user; anything else is the claim, told as
unverified; a token whose agent cannot be said is answered 503, never taken for the claim. One value says who did it
and why (`clarp_kernel::control::Control`), and one function puts it on a state's detail (`marked`) and from there on
its activity (`provenance`), for a Stop and for a repair alike, as the Python Host's `_control_provenance` is one for
both. Whether a Stop is the user's (anything but an agent's own, the one that may do what only the user's Stop does)
is one function of that value, `Control::is_the_users`, for every reader of the log. Only the user's Stop drops
what the agent had yet to say aloud, as on the Python Host: an agent stopping another orchestrates, and what was about
to be said is still the user's to hear (the lead's decision of 19 Nov; held by
`only_the_users_stop_silences_what_an_agent_had_yet_to_say`, in rows: the administrator's Stop, a paired phone's, an
unverified claim to be an agent, and an agent's own turn's). A Stop's answer says it, its
goals are paused in its words, and the interrupted state and its activity carry it
while it is the latest, through a restart: the log's `Stopped`, `StopRequested` and `Paused` keep `verified` and
`reason` beside `by` (`Paused` is the one line an idle agent's Stop leaves, where a reader asks whose Stop it was), written only where they say something, so a user's Stop with no reason is written as before and a line
of an older log is read as it was (the journal's own tests hold both directions with literal lines, and that an older
build reads a newer line: no type of the log denies a field it does not know). A repair interrupts nothing and holds
or lets go nothing. What it gives back on the Python Host, a turn's slot that no process owns, cannot be here: a turn
is the agent's log, followed through its shim's socket, and it ends when nothing answers there (held by
`nothing_holds_an_agent_busy_with_no_process_behind_it`, in the four ways the Python Host can leak one). So it answers
`released_trace: ""` and `queue_recovered: 0`, and keeps its note as the Python Host keeps it whatever it released:
the state repeated, with who repaired it and why, until the state changes. The note is an event of a kind of its own
in the log (`NOTE`), which a build that does not know it passes by, so that it still opens the agent. The agents'
tool has `clarp-admin stop` and `clarp-admin repair` (`--session --reason [--from]`), asked with the token of the
turn they run in and printed as the Python tool prints them. Held to the Python Host's own handlers
(`tools/turn-slot-repair-vectors.py`: who acts over claims, phones and tokens, the Stop and the repair of an agent with
nothing to release, the activity's provenance) and by `a_stop_and_a_repair_say_who_acted_and_why_and_an_agent_is_known_by_its_own_turn`;
the wrong rules are `tools/plants/turn-slot-repair.py`.

**A janitor's failure reaches its owner** (19 Nov 2026; no feature of its own: the reference lists none). The one
thing of a quiet agent that does. When a janitor's runs of its current configuration failed twice in a row since its
last verified success (a scheduled task's run that ended well; a run of a job that answers requests whose answer was
made for that job by a model called for it, with a summary), an item is raised for its owner: a document of the
janitor, `failed`, id `janitor-alert-` and forty signs of the sha256 of its episode (`janitor-failure:{agent}:
{generation}:{success | initial}`), whose payload says `attention_kind: janitor_failure`, how many failed, the last
run and its error, cleaned of secrets and of what a model thought. A further failure updates it; a verified success,
the janitor set anew, turned off, put away or no janitor any more closes it; the owner archives or discards it by its
id (`POST /artifacts/{id}/archive|discard`, the artifacts' own rules, which are now functions of one artifact), and
it is never made again for the same episode. `GET /attention` lists the open ones among its `janitor_items`, the
badge counts them (so the chip's count moves with them), `GET /artifacts/{id}` opens one; never the Updates, never a
push, a sound or an unread mark. The rule is the kernel's (`janitors/attention.rs`), held to the Python Host's own
`janitor_attention` by `tools/janitor-attention-vectors.py` (four janitors, twenty-five steps, run in an isolated
store); the items are a table of the janitors' book (`janitor-alert`, by the item's id), reconciled in the one write
that keeps every change of the book, so an item changes in the same write as the run or the configuration that changed
it; kept, then shown: only once that write answered well are the counts and lists given the change, the apps told
(`agent-roster`, `janitor-attention`) and the badge moved, and a write that fails tells nobody anything
(`kept_then_shown`, held by its own test and two wrong rules). Held by
`a_janitor_that_fails_again_and_again_reaches_its_owner_until_it_recovers` (the tool explainer's model fails on
request: once nothing, twice an item, the chip counts it while Mike works, a third failure counts three, a good answer
closes it; failing again, archived, failing once more, discarded; a restart keeps them) and the wrong rules
`tools/plants/janitor-attention.py`.

**Installed transcription models.** The Linux catalog lists the five faster-whisper models and the two macOS rows. The Host checks its own registered paths for the configuration, tokenizer and model files. A regular clip or a call can select an installed variant by name. The default stays warm. One additional worker loads on demand and stays resident until a different variant replaces it, the model is removed or the Host stops. Default and variant share an inference turn. A request with a fallback gives another variant ten seconds to finish selection and inference. The final choice waits for that turn.

The Host marks a managed model before deleting its folder. While marked it is hidden. A restart finishes pending deletions and clears the mark on a newly configured default. A stored inventory that cannot be parsed is preserved byte for byte under `records.unreadable` before the Host logs a single diagnostic and opens with an empty inventory. Store read or backup failures stop startup.

The additional process holds its model and inference buffers. A published CPU small-int8 benchmark used about 1.5 GB. Resident memory varies with model and workload. An idle timeout could reclaim that memory on machines under swap pressure at the cost of another cold load. This Host retains the worker as the reference does.

Removing an external model forgets its record. Removal computes the managed directory from the model id. A wrong stored path is logged by id and reason only and cannot select or prevent deletion. An absent computed directory has nothing to delete. A symlink there refuses removal before files or records change. The configured default is protected. R112 also applies to cancellation cleanup when the installer is built. Registration by an installer will need an explicit model id and path for each external model. The Host never consults a different Host’s registry.

**The computer’s background jobs.** A Host operation can register a queued or running job through
the fleet and use its generation to start, report progress, finish or cancel it. The computer owns
these jobs even when the fleet has no agents. Each registration names the kind. A second caller,
such as portrait generation, uses the same registration and transition API with its own kind and
stable job ID. The fleet arbitrates agent and computer claims within one actor turn.
A computer producer receives a typed refusal for an invalid command or an agent-owned ID.
A storage or actor failure returns `Unavailable`. Cancelling an unknown ID returns 404,
including IDs in the provider-task namespace. The storage
port writes a single record with the current job and up to 200 timeline entries before the fleet
publishes a change. The regular list, detail, event and cancellation routes show the computer as
owner; cancelling a computer job sends no cleanup prompt to an agent. A producer waits on
`FleetHandle::wait_computer_job_end(id, generation)`. The fleet subscribes it in the same actor
turn that checks the generation. A durable cancellation, finish or replacement wakes every producer
waiting on the old generation; a failed store write leaves the wait pending. At open the fleet
counts unreadable job records, logs one line, and preserves their bytes and their ids. A successful
computer registration under the same id replaces the unreadable record and logs the repair. The reference cases for
generation receipts are in `computer-jobs.json`, and the wrong rules in
`tools/plants/computer-jobs.py`.

**Installing transcription models on Linux (feature `transcription`, contract 1).** An explicit full-access POST to
`/transcription-models/install` registers a computer-owned job and answers 202 with its generation
and handle. Repeated requests join that job. Each supported catalog row fixes a repository and
revision. The speech environment streams the snapshot into checked private staging; the child
receives an open directory. The catalog pins the revision and records the estimated download size, and the child verifies file lengths and digests against the hub’s manifest for that revision, read when installation begins. Partial files survive an
interruption and may be resumed only by another Install. Admission reserves the unfilled part of
each active download's limit and 256 MiB of disk headroom. The per-model limit is 125% of the
catalog estimate.

The Host owns the downloader process group. Cancellation closes its generation and stops that
group. An interrupted installation becomes failed on reopening the Host; startup performs only
local recovery. Publication records the staging directory identity before a no-replace rename.
The generation's success, its inventory row and removal of its publication intent are one store
transaction. If the Host stopped after rename but before commit, recovery restores the same inode
to staging. Stale intent cannot move a replacement's files. Staging is cleaned on cancellation,
removal and publication, and an orphan older than seven days is swept. The install changes the
available models. The warm default remains the model named at Host installation. A newly installed
variant is selectable by name or as the configured engine for clips.

**The voice timeline.** A client batch adds at most 500 rows to the owner's permanent
timeline. Each row keeps its raw timestamp alongside the time corrected by the batch offset.
An offset more than six hours from the receipt clock uses receipt time instead.
The fleet commits each batch and its event sequence together through the storage port.
Server producers enqueue their events. A subsequent read through the fleet observes earlier
messages in order. A failed fire-and-forget write is reported through the coalescing store logger.
The event query returns the newest matching window in chronological order. Utterance summaries
also join trace-only events before filtering by session. Audio measurements run outside the
transcription request using the speech environment, with a bounded decoding time. Podcast history
has no producer here. The wire vectors use synthetic event names, measurements and text.

**The wrong rules are in the repository** (18 Nov 2026). A test that cannot fail holds nothing: for every
part of the Oracle a list of wrong rules lies in `tools/plants/` (a small change to the source that makes
the Host do otherwise than it is to do, with the tests that should notice), and `tools/plants/runner.py`
plants them one at a time and writes down whether the tests caught each. What is missed gets a test, or
the code that nothing can observe goes, or the rule is taken off the list with its reason written there.
The lists run on GitHub's machines, started by hand (`Planted rules (Rust)`, `.github/workflows/
server-rs-plants.yml`: several machines share a list and the last job fails where a rule was not caught),
or through the build machine. On the commit that landed: the delegations 165 of 165 caught, the relay 60
of 60, the cues 32 of 32, the tools 80 of 80, the memory 158 of 158, the conversation 475 of 475,
the call on the Host 93 of 93. What their first runs missed was mended before: the memory's stories went
from 10 to 18, the conversation's from 43 to 70, the tools' and the call's cases grew, and code that
nothing could observe is gone (the set of what was handed to the contact, the early end of a tick after a
part, a second look at whose work a thread's work is, guards that nothing reaches). With what agents
finished (the next landing) came a list of its own, 78 of 78 caught on the commit that landed, and the
lists whose code that landing touched were run again there, whole: the call 93 of 93, the conversation 475
of 475, the delegations 165 of 165. Its first run missed two: a turn that was stopped had no case and got
one, and a moment at which the worker read anew for an agent that was started again was code nothing
could observe, since such an agent is opened anew, and went. With handoffs came a list of 173 (142 for the
kernel's record, 31 for the Host's part), every one caught on the commit that landed, with the call's 93
and the 78 of what agents finished run again there. Its first runs missed ten: three conditions of the
Python Host's module that nothing can reach are not in the kernel, four cases got stories (a phone that
rolls back while it prepares, one that says it is ready too early, two talks with agents at once, an older
talk given up at its older number), and three got a place in the Host's test (a held call routes nothing,
a plain failure tells the voice no word, the first frames of a call that goes to the contact alone). With
the words an owner prepared came a list of 53 (40 for the kernel's rule, 13 for the Host's reading), every
one caught. Writing it showed three things the vectors did not hold yet, and they do now: a version that
is a no, a word counted in signs and not in bytes, and this Host's own words where the Python Host speaks
its language's.

**Pushes with the Host's own key, and Android** (18 Nov 2026). An alert can go through the
phone's grant, the Host's relay account or the key named by its explicit `--push-keys` file.
Direct mode bypasses the relay. Auto mode falls back when the relay is unavailable, and
a refused phone grant is cleared even when another path delivers the push. The routing
vectors run the reference router across four consecutive deliveries for each configuration
and service response. A desktop reply suppresses the phone alert on both Hosts. Neither
Host sends a silent sync push (R64). An iOS activity receives updates, including the idle state, over
the selected account or key. An uncertain relay send cannot spend the same reservation twice.
Android phones receive data messages over FCM using the explicitly named service account.
A live ring is queued for the Android phones that declared it. An end is delivered to the
eligible tokens its ringing was tried on. A registered Android summary receives updates
and an end when work becomes idle, with a durable daily count and refill budget per device.

**Schedules** (16 Nov 2026). A prompt an agent is sent at times a cron expression names, in UTC: the owner
or an agent makes one for an agent (`POST /agent-schedules`, `clarp-admin schedule add`), turns it off and
on, changes and removes it, and a runner looks every fifteen seconds for what is due, moves each schedule
on to its next time first, so that none fires twice, and then sends its prompt as something the Host sends
by itself: nobody is waited for, nothing is said aloud, and the user's focus stays where it is. The rules
are the kernel's (`schedules.rs`), the Python Host's scheduler (`server/lib/scheduler.py`) without a store
or a clock, held to it by 87 expressions (49 taken, 38 refused in its words) each run from 8 moments, and
by 4 scripts of 81 steps of making, changing, running and removing (`tools/schedule-vectors.py`). The
schedules are kept whole with the fleet. A schedule is sent to the agent it was made for, by its id. The
Python Host has no feature that stands for schedules, and neither has this one: the apps call the routes.
Found on the way and reported: the Python scheduler counts through a range far larger than its field
(reference defect R24; fixed there the same day, PR 381).

**Asking what an account has left** (18 Nov 2026). With `--ask-usage`, an app opening the usage
screen can ask the providers on the Host's current logins, whether the Host follows a selector or
runs its own. The default is off. Claude's OAuth usage endpoint reports percentages. Codex's
ChatGPT usage endpoint is tried first, then its read-only app-server. Antigravity's own
`--output-format json -p /usage` command reports buckets per model group. No model turn is
started. A reading is due after five minutes or when the app requests a refresh. A failed
request preserves the previous percentage and its age. As in the Python reference, a failed
Codex refresh after a block that had no percentage clears the block, even when its reset is
still in the future. An omitted window disappears from the current report. If it returns, its
recent limit episode still identifies the recovery an app saw. A Codex structured window is fresh for
fifteen minutes, and a Claude or Antigravity reading for an hour. A reset makes the corresponding
Antigravity bucket stale even if the other buckets are fresh. A successful reading can verify
the same login for a minute. The Host tests direct requests with a numeric loopback socket
address in `CLARP_USAGE_LOOPBACK` and give both tools private executable stand-ins.
The first run against the installed Host belongs to the release lead.

**Render specs** (18 Nov 2026; Host contract 102, feature `render_spec`). An agent declares a small form, and
the app draws it with its own views from a closed catalog (`contract/catalogs/clarp.form.v1.json`: eight
components, every property closed) and lets its user answer it once. The kind came to the Python Host the
same day (its PR 418, issue 415) under a contract its app's agent, its release owner and the lead agreed, and
this Host carries it as that Host does. The rules are the kernel's (`artifacts/render_spec.rs` and
`render_spec/`): whether a payload is a spec, refused at the first rule it fails in the reference's order of
stages, as a code from the closed list, a pointer and the catalog's sentence for the code, never with a value
and never naming a key the caller wrote unless it is an identifier; whether answers are taken (accepted, the
same again, or refused); what the agent is told; and the canonical bytes (RFC 8785, whole numbers only)
that a spec is counted, hashed and capped by. They are held to `server/lib/render_spec.py` run as it is by
851 recorded cases (`tools/render-spec-vectors.py`): 95 bodies, 260 payloads (every stage, every cap at and
one over), 71 submissions, the reference's own 47 scenarios played through as its store plays them, 278
clients, 10 messages, 33 sets of the texts besides a payload (the reference's PR 438), 10 bodies that do or do not hold a text no store can hold (its PR 441) and 47 catalog files, each that loads asked about nine payloads (the reference as of
its PR 431, which asks that a bound property's schema be the catalog's shape of a binding). The body of a request is read as the Python Host reads it,
because that reader is not JSON as it is defined and the refusals depend on it: it finds UTF-16 and UTF-32
by their first bytes, lets half of a pair of signs through (refused later as `invalid_json`), takes `NaN`
and numbers of any length (refused as `number_form`), reads `-0` as the whole number 0, and keeps that a
key came twice (refused as `invalid_object`). Both catalog files are in the program; a catalog that does
not pass its checks refuses render specs alone (503, `catalog_unavailable`). The Host (`render_spec.rs`
beside `library.rs`) publishes, revises (a whole new payload that names the version it follows) and only
checks (`?validate_only=1`); a spec is never in a row of a list, a search, the pins, Updates or an event.
Who sees it is said by each request: a client whose `X-Clarp-Client` says `<platform>/<build>
contract=<n>` at or above the owner's threshold (`[artifacts] render_spec_min_client` in his file, read at
every request; unset, as the reference ships, nobody) is shown what was left out (`content_omitted`, with
`element_count`) and reads the spec, and once it was answered the answers, from the artifact itself; any
other client sees a plain row and cannot answer (403, `client_contract`). The first answers that are taken
close the spec (`already_answered`); they are told to its agent once, through the same delivery as a
form's answers, and the same answers under the same id get the same receipt. `GET
/artifact-catalogs/clarp.form/1` serves the catalog's bytes, and the agent's tool sends a whole request as
its bytes (`clarp-agent-artifacts publish-render-spec REQUEST_FILE [--validate-only]`), so that the Host is
the only validator. Held end to end at every door the reference's own integration test goes through
(`tests/host.rs`, a render spec is published, shown to who may see it and answered once). A request to `/artifacts` or `/decisions` that is not a read and whose body holds half of a UTF-16 pair, in a text or a key, is refused before any route looks at it with the Python Host's one sentence (its PR 441, R79), a render spec's own requests excepted: their contract names the field. Not there: moving
a render spec with its agent to another Host, which is the mover's contract.

**The connection relay** (16 Nov 2026; the owner's requirement, through the Python Host's release owner: "when
we switch to rust it will use relay"). A Host without a public address is reached through a relay: the Host
keeps one outbound WebSocket to it, and what a phone asks at `<relay>/h/<host id>` comes down that socket as
frames, each for one stream. The relay knows the Host by a name and a key; it does not know the phones: the
Host checks them. The rules without the socket are the kernel's. `connector.rs`: what a frame is, what the
connector does with each frame it is given, with which headers a request is handed to the Host, how an
answer's head is written, when the relay is dialled again, which settings name a relay. `trust.rs`: which
request is local, whose failure a failed authentication is, and when failures are answered with a wait. Both
are the Python Host's own modules (`relay_connector.py`, `relay_settings.py`, `request_security.py`), held to
them by what those modules answered (`tools/connector-vectors.py`): 12 frames, 41 sets of headers, 15 waits,
89 settings, 22 files, 109 frames through 29 scripts, 22 peers, 88 requests, 22 sources and 82 counted
failures.

The Host's part is the socket and what serves a stream (`connector.rs` in `clarp-host`). **A relay's request
is asked of the Host's own router from inside the Host.** The same routes, checks and answers as at the door,
and no socket of its own: nothing about such a request says "this machine", so the administrator's token and
the token of an agent's tools, which are taken from this machine only, are never taken from the relay. A
phone presents the token of its pairing. An answer goes up as it comes, so a stream of events flows; a
request the relay gives up is given up here. The door holds the same line for whatever a proxy on this
machine hands on (`x-forwarded-*`, `tailscale-*`, `cf-*`, `forwarded`, `x-real-ip`, a page from elsewhere):
the socket is this machine's and the request is not. A token in an address is taken from this machine only.
Twenty failures a minute to say who one is, by a token or by a pairing code, are answered as failures; the
next is told to wait (429 with `Retry-After`), each asker for itself, and who asks rightly never waits. A
device whose pairing is revoked loses the streams of events it listens to at that moment.

The Host is told of its relay by a file it is given by name (`--relay-config`): a config of the Python Host's
kind, `[network] relay_enabled` in it and `relay.json` (mode 600) beside it. Both Hosts read the two files
the same way, so the Host that takes the other's place keeps its name at the relay and the phones keep their
pairing. Told of no file it dials nobody, and the file its keys of speech are read from is not that file
(hard rule 9; `tools/install-host` refuses the Python Host's file while that Host runs under the name in
it). The connector dials by itself, again 0.3 s after a session that was up, 1 to 30 s apart while the relay
cannot be reached, and 5 s after another connector took its name; each session and each loss is one line in
the journal with the Python connector's words and fields, and never the address that was dialled. The apps
read `GET /network/relay`; `clarp-host network relay status` shows the same; `clarp-host pair --relay` and
`POST /pairing/issue` with `relay` give the link whose address is the relay's for this Host. Tested against
a stand-in for the relay on the loopback (`CLARP_RELAY_DIAL`, taken for an address of this machine only): a
real Host process dials it, a phone is paired and served through it, the administrator's token is refused
there, a stream of events flows and ends at a revocation, and the Host dials again after a loss.
A WebSocket a phone asks for comes down the same socket (`WS_OPEN`) and is served by the Host's own router
too: see "Sockets" below. A run through the relay
itself, under a name of the battle test's own, is in `docs/battle-test.md` (16 Nov). Found on the way and
reported: the Python connector dropped the apps' own headers (reference defect R25; fixed there within the
hour, PR 383).

**The other engines of speech** (18 Nov 2026). Deepgram returns MP3 over HTTP, with Flux and Aura
using different API versions. ElevenLabs sends its audio in WebSocket frames for streaming speech.
Google Chirp 3 HD returns a complete MP3 in JSON. It uses the configured ADC account and project,
refuses inputs longer than 5,000 UTF-8 bytes, and preserves supported pauses as escaped SSML.
A live line can switch engines only before the listener receives audio. For an audition the Host
buffers the answer. Only the requested voice enters the cache, and a fallback is returned for that
play. The Cartesia picker reads the voice catalogue and offers previews. A preview by text creates
an audio event for the requested session.

The app can change the speaking provider and its fallback. The Rust Host keeps this selection
with the fleet and uses it on subsequent clips. The owner’s file supplies defaults and credentials.
Cartesia Ink-Whisper receives the uploaded clip as a multipart file. Ink-2 receives decoded PCM
over a socket, with vocabulary in its URL. Partial transcripts are replaced as the provider speaks,
and only the final parts are joined for the answer.


**The vocabulary** (18 Nov 2026). A transcription request compiles the selected profile against the
engine’s budget. Guaranteed terms enter first, then remaining terms compete by priority, rarity,
recency and observed mishearings. Local Whisper prompts put the strongest terms last. The preview
shows the payload, included and dropped terms, and per-pack counts without creating a run.
Workspace names are harvested only for an opted-in profile. The Host caches that harvest for five
minutes under the directory and Git revision.

The fleet keeps packs, terms, profiles, memberships and assignments in one book.
Each administrative change publishes one replacement document. A clip reserves its number in a
separate counter. Trace keys point to the newest run. Session and global pages each hold up to
200 run numbers, and a session also keeps its three most recent spoken run numbers. These lookups
survive restart. The fleet commits the counter, audit and lookups together. An interrupted transaction
spends no run number. Finishing a clip commits its transcript and spoken index together. As in the Python Host, runs have no count or age expiry. The runs endpoint
limits only the page it returns. The vocabulary, transcript and elapsed time share a run id. A completed
stream turn records the keyterms actually supplied at connection time. Retried clip jobs return
the saved result. Optional retained audio is readable by trace, with its original MIME type.
The Host keeps at most 500 clips for up to fourteen days, sweeping when it saves a clip.
An enabled junk-transcript judge can preserve speech that the phrase filter would discard. It
falls back to the phrase filter when the provider is unavailable. A billing refusal holds further
asks for an hour; three ordinary failures hold them for two minutes.

**The heartbeat** (16 Nov 2026). An agent that asked for it is given a turn when it has been idle for the
owner's interval, so that it looks at what it has standing: the prompt, the plan it has (its title and at
most twelve things that are open, each followed by its parts), and before them a word about a turn that was
interrupted. The rules are the kernel's (`heartbeat.rs`), the Python Host's `server/lib/heartbeat.py` without
a store or a clock: the owner's four settings and the words a change is refused in; when an agent is due (not
while it works, waits for its user or was stopped by its user; not within its interval after somebody spoke
with it; not sooner than thirty seconds after the last, and not a sixth time within a minute); how the
interval grows after each heartbeat that found nothing to do, by the owner's strategy up to the owner's cap,
and after how many of them the agent goes dormant until somebody speaks to it; what an answer is (the word
`HEARTBEAT_OK` at an edge of an answer, with at most a short remark beside it, says that nothing is to be
done: the conversation keeps one line that says so, and it is not shown while it is written; in a turn that
is no heartbeat's only an answer that is nothing but the word); and what an agent's profile shows. Held to
it by what that module answered (`tools/heartbeat-vectors.py`): 30 changes of the settings, 50 answers each
read in a heartbeat's turn and in another, 13 prompts, and 1288 decisions of whether an agent is due.

The Host's part is the book and the runner (`heartbeat.rs` in `clarp-host`). The book is kept whole with the
fleet: the settings, what is remembered of each agent by its id, and the heartbeat's turn that was last
counted for each, so that a turn counts once however often it is looked at, and all of it outlives the Host.
The runner looks once a minute who is due, the first time a minute after the Host came up (a Host that has
just come up wakes nobody), and sends the prompt as something the Host sends by itself: nobody is waited for,
nothing is said aloud, the user's focus stays where it is. It leaves alone an agent whose durable goal wakes
it (a goal enrolled for recovery that is active, paused or blocked), as the Python Host does, and one whose
queue its user holds. The apps read and change the settings (`GET` and `POST /heartbeat/settings`), turn an
agent's heartbeat on and off (`POST /agent-heartbeat`) and read its cadence and its last twenty answers
(`GET /agent-heartbeat/status`), with the Python Host's statuses, fields and words; the scenario
`the-heartbeat` compares them. A Host started with `CLARP_HEARTBEATS_DISABLED` wakes nobody and takes no
heartbeat from outside, as the Python Host; the Hosts of the conformance run are started so. Not built with
it: the hours of the day in which nobody is woken and the settings an environment gives
(`CLAUDE_PWA_HEARTBEAT_*`: the rule for the hours is in the kernel, the Host reads none of the variables,
and the owner's Host on elitebook sets none of them, looked on 16 Nov); a keeper that serves an agent
instead (janitors); and the two other signs that wake an agent that has backed off, its team moving and a
promotion in a leader's memory (teams and leaders). At its first run against the reference (43961721912) the
scenario's 41 steps were answered the same by both Hosts: every refusal of a setting in its words, the
switch, the cadence before and after somebody spoke. The one difference of that run was the scenario's own:
it read a snapshot that names a row of the conversation without having read the conversation.

One rule is this Host's own, since its first heartbeat with a real model (17 Nov, 00:09, a Codex agent):
**a heartbeat's turn counts by its last answer.** The agent remarked twice on its way and then answered that
nothing was to be done, and by the Python Host's rule, which this Host had, the remarks made the turn count
as activity: the agent would have been asked at the owner's interval for ever. Counted on the owner's Host,
that is what happens to most heartbeat turns of the tools that narrate (reference defect R28, reported with
the counts). Here what an agent remarks on its way to its answer is not what it found.

**What the load still asks for, and the ids an app gives its messages** (16 Nov 2026). Counted again on the
owner's Host on elitebook, its request log of three days: 175,200 requests over 395 routes, and 99.8 per cent
of them go to a route this Host has. What is asked and not there, by use: the desk of the account seats
(`GET /hotseat/desk`, 77, and `/hotseat/status`, 12), the explaining of a tool's use (`POST /tool-explanations`,
48), one janitor and its runs (`GET /janitors/<id>`, 48 and 40), a waiting message taken back (`DELETE
/turn-queue/<id>`, 14) or sent now (`POST /turn-queue/<id>/send`, 2), a crash an app reports (`POST /crash`,
11), a fork of an agent (3). The sockets of voice (`/stt/stream`, `/oracle/realtime`), of the terminal and of
a ring held as a call were asked not once in those three days, on elitebook or on laptopstudio, counted the
same way there; they stay a gate before the switch, and they are not what the next days of use need first.
Answered since, the same evening: the queue's routes (below); a crash an app reports (kept as a file each,
the newest two hundred, `apps.rs`); and, as screens of what this Host has not, the account seats as the
Python Host says them before anything was read, one janitor as one it has not, and a request to explain
tools as a Host answers where explaining is off. What a fresh Python Host answers there was asked of it
first, by a run of the one scenario (`-f scenarios=`, five minutes instead of twenty-two): it corrected one
answer that reading its source had got wrong (a janitor that is not there is `Agent not found`). Counted
the other way, by what was done and not by what was read (requests that change something and were taken):
three things were done there that this Host did not do. Choosing the picture an agent wears from what it
published (`POST /agent-portraits`, six times) was built the same night: an agent keeps up to three
portraits, a picture it published is kept as one, and one of them is made the picture it wears, by the
rules of the Python Host's `agent_portraits.py` (`kept_portraits.rs` in the kernel; the portraits are kept
with the agent's record, and every portrait's bytes under the name an agent's picture is kept by). The two
that are left: a janitor's own settings (four requests) and a fork of an agent into helpers that start from
its conversation (`POST /agents/<session>/fork`, three times). The first count of that night named only the
janitor's settings: it listed the routes by how often they were used, and a path with an id in it that the
count did not recognise as one filled the list before the forks came. The count is a tool now
(`server-rs/tools/count-load`), which names an id of any kind as one thing: on 17 Nov, three days of
elitebook's log were 167,364 requests over 139 routes, all but 41 of them to a route this Host has, and of
what was done there seven requests have no route here, the four and the three above.

**The fork of an agent** (17 Nov 2026). An agent forks itself into helpers that start from its conversation
and each get a task: `POST /agents/<session>/fork`, and for the agents themselves `clarp-admin agent fork`
(the skill `clarp-sub-agents`). It was the one thing agents did on the owner's Host in those three days that
this Host had no route for, besides a janitor's own settings. The rules are the Python Host's
(`agent_fork.py`) and are held by vectors (`tools/fork-vectors.py`: 90 requests, 61 of them refused in nine
words for the reason; 24 first messages; 15 places of a worktree): what a request asks for, read strictly,
so that one that asks wrongly makes nothing; a child's first message (what it is, the part of its maker's
conversation it starts from, its task, how to report back with `clarp-admin prompt`); and where a child's
git worktree is put when the request does not say (`fork.rs` in the kernel). The Host's part is `forks.rs`:
it finds the agent that forks, makes a child's worktree before the child (a new branch from `HEAD` or from
the base the request names, or the worktree that is there on that branch already), makes the helper and
sends it its task as a message from its maker. Children are independent: one that cannot be made is told in
its own row of the answer, and its siblings are made. Git is run for at most two minutes a command and is
ended when it takes longer (`tokio`'s `process`, a feature of a dependency the workspace had; no new
package). A child here always starts from the conversation **as words** (`fork: seed`): the newest part of
this Host's own record of it, at most 60,000 signs. The Python Host has the tool copy the conversation
itself where the tool can and falls back to words; a tool's own copy is not made here yet, which is one
registered difference in the scenario `agent-forks` and four rows in `docs/differences.md`. The scenario
asks 24 requests that are refused (a fork of an agent that has said nothing among them, on either tool: 409
on both Hosts), two children that cannot be made, and one that is; its first asking (run 43986165062, the
one scenario) found two events to differ besides: **a message the Host sends by itself is told to the apps
under no id** on the Python Host (`client_msg_id: ""`), and this Host told its own id for it. An app looks under that id for the bubble it painted, and nobody painted what the Host sent.
So it is here now, for the task of a fork, for a heartbeat and for a prompt at a set time
(`Message::told_id`): the message is filed and waits in the queue under the id the Host gave it, as before,
and the events name none. No scenario had compared such an event before: the heartbeat is off in the suite,
and no schedule comes due in it. The second asking (43987643044) found one thing more, by the first agent
of the corpus that is made and never spoken to: this Host said that a conversation had changed
(`transcript-updated`) when an agent was made, and the Python Host says it when the first thing is written
to one. It is not said at the making any more.

**The janitors: the book and the routes** (17 Nov 2026). A janitor is an agent with a job of maintenance: a
template (what it does), a scope (the agents it watches), attachments (the triggers that start it) and
options. On the owner's Host three of them run a hundred times a day each (the one that keeps goals current,
the one that explains a tool's use, the one that switches account seats); they are the largest thing this
Host did not have. The first part is what is kept and how it is set, with nothing running:

- The rules are the Python Host's (`lib/janitors.py`, `janitor_store.py`, `janitor_builtins.py`) and are held
  by vectors that its own modules write on a database made for it (`tools/janitor-vectors.py`): the catalogue
  of ten templates and eleven triggers, which the same tool writes for the kernel to carry
  (`janitor-catalogue.json`); 369 cases of what is made of options, attachments, a scope and how a job is run
  (`janitors.rs`); and three stories of 77 steps in which janitors are made, changed, turned on and off, put
  back as their job has them, removed and let go (`janitors/book.rs`), with the seven a Host installs for itself.
  Every change that could let an old run act on a new configuration moves a janitor's generation and
  revision; a save pauses it; whoever changes one says which revision they saw.
- A schedule is read in a time zone its owner names (`janitors/schedule.rs`): the local days are gone through,
  a local time that does not come is not run, and one that comes twice is run the first time, as
  `lib/janitor_schedule.py` does it. Held by 3,024 moments around the days clocks change in eight zones (a
  half-hour change, a change at midnight, a rule read from the end of a zone's file in 2045), 96 times between
  two runs and 91 previews. The zones are the machine's table, read by the crate `jiff`, pinned to `0.2.38`:
  its `1.0.0` came out two days before this was written, and a release six weeks in use was taken instead. Two
  crates are built for it. A name is a zone's own, letter for letter, as the Python Host finds a zone by its
  file.
- **A janitor is a member of the fleet and no agent of the roster.** `Directory.members` stays what it was,
  the agents a person can speak to, and the forty-five places that go through it meet no janitor; the
  janitors are beside them (`Directory.janitors`), and who means one asks for it. Inside the fleet's thread a
  request's key finds an agent of the roster only: no control of a chat renames, puts away, selects or
  removes a janitor. The snapshot shows the janitors' rows after the agents', as the Python Host shows them
  (`is_janitor`, `role: janitor`, never unread, looked at and not spoken to), each with the picture of its job.
- The Host's part is `janitors.rs`: the book kept whole with the fleet, the seven of the Host's own installed
  once at start under the ids and session names they have on every Host, and the routes (`GET /janitors`,
  `/janitors/<s>`, `/janitors/<s>/runs`, `/janitor-triggers`, `/janitor-triggers/preview`; `POST /janitors`,
  `/janitors/<s>/configure`, `/enabled`, `/reset-defaults`, `/adopt-options`, `/release`; `DELETE /janitors/<s>`), which
  answer in the Python Host's statuses, bodies and words, and tell the apps to read again (`agent-roster`,
  `janitor-changed`).
- In that first part nothing ran, and the Host said so (`runtime_available: false`, turning one on refused,
  503 `janitor_runtime_unavailable`). The second part, below, runs a scheduled task. The seven of the Host's
  own are still installed paused, and a job that does not run here yet is still refused in words.

**The janitors: the runs of a scheduled task** (17 Nov 2026). A janitor whose job is a scheduled task
(`scheduled-prompt` on `schedule@1`) runs when its schedule says: one run for each time that comes due, each
a turn in the janitor's own conversation.

- The rules are the Python Host's runner (`lib/janitor_runner.py`, `_scheduled_tick`) and its runs
  (`lib/janitors.py`), and are held to that runner itself: `tools/janitor-run-vectors.py` drives
  `JanitorRunner` look by look on a database made for it, with a clock, an ending of the turn and an answer
  of the queue that each step sets, and writes what the Python Host kept after every step. Six stories, 155
  steps, 102 looks: a task of every half hour that is idle, at work, failing and given up; two schedules of
  one janitor that is paused, set anew and removed while a run is out; and four tasks whose times are not
  evenly apart. The kernel's `janitors/runs.rs` replays them (`tests/janitor_run_vectors.rs`): what the
  runner keeps of every trigger, every run and the janitor as it is read are equal after every step, and
  so are the 42 runs handed over.
- **A look has two halves**, because the Host's queue is asked outside the book: `Book::look` says what is to
  be handed over (a run is made for a time that is due; a run that is out is offered until the queue takes
  it; a run whose turn has ended is over, `completed` or `error` with why, without what is plainly a
  secret), and `Book::handed` is told what the queue answered. A run that the queue did not take is offered
  again after half a minute, the same run and never another; one whose turn never ends is given up after two
  hours.
- **How long a task waits** is the rule of `lib/janitor_adaptive.py` (`janitors/adaptive.rs`, 71 runs of
  vectors): a run says of itself that it worked, was idle or failed (`clarp-admin janitor outcome`, `POST
  /janitor-runs/<id>/activity`); idle doubles the wait up to the task's longest, worked puts it back, failed
  leaves it, and a run that says nothing counts as worked.
- **A task that keeps its pace runs at the next time its schedule names**, and only a wait that idle runs
  have stretched is counted from the time that ran. This was this Host's own rule against defect R31 of the
  reference (which took as the wait the gap between the two runs after the first, so that a task of working
  days first run on a Thursday ran on Mondays and Thursdays only, and every daily one lost the day the
  clocks are put forward); the Python Host took it over with its PR 425 on 18 Nov 2026, and the vectors made
  anew hold both Hosts to every time of the schedule in those stories.
- **A janitor put back as it came** (`POST /janitors/<s>/reset-defaults`) gets its job's own settings of the
  triggers it was installed with (the Goal keeper: a sweep at `17 * * * *`, a goal's triggers folded over
  300 seconds), each other trigger's own defaults, and the way of running its job is installed with where its
  tool can run it so (shut off from everything, for the Goal keeper on Antigravity), else the plain one: the
  Python Host's rule since its PR 425 (R30). Whether each trigger is on stays as it is.
- **A change cuts a run off.** A pause, a save and a removal move the janitor's generation: the run that was
  out is `cancelled`, may say nothing any more, and the Host takes its turn back (a message that waits is
  withdrawn, a turn that runs is stopped, and the janitor's mailbox is not left held). A run that was cut off
  between a look and its hand-over is taken back by the runner itself.
- The Host's part (`janitors.rs`): the runner, every two seconds (`--janitors-checked-every-ms`), reads from
  the janitor's own actor how the turn with the run's id stands (started, ended and how) and hands a run
  over as a message the Host sends by itself (`origin: janitor`, the run's id as its trace, waiting behind a
  turn that runs, said to nobody). The routes for a janitor's runs (`GET /janitors/<s>/runs`, `GET
  /janitor-runs/<id>/context`, `POST /janitor-runs/<id>/activity`), and a janitor's conversation read like an
  agent's (`/log`, `/live`, the queue's contents), which is all a person does with one.
- **The agents' tool** `clarp-admin janitor ...` (and `clarp-admin trigger list`) asks the Host's own routes,
  inside the Host, what the Python tool asks them (`lib/janitor_cli.py`), and prints what it prints: `list`,
  `templates`, `inspect`, `runs`, `create`, `configure`, `attach`, `enable`, `pause`, `remove`, `release`,
  `reset-defaults`, `run-context`, `outcome`, with `--dry-run` and the revision read where none is named.
  What the routes do not have here yet (a label's review, the state for a move) the tool is told by them.
- **Looked at and not spoken to.** A control of a chat that names a janitor, by its session or its id
  (`/send`, `/stop`, `/select`, `/agent-rename`, `/agent-archive`, `/agent-llm` and the rest of the Python
  Host's list, and the removal of a janitor as an agent), is refused in that Host's word and words (403,
  `janitor_inspection_only`). It is one guard in front of the routes (`edge.rs`, `looked_at_only`), which reads
  whom a request names and lets everything else through as it came, where the Python Host has the same check
  in twenty-five handlers. A janitor's own turn uses the tools an agent has for what it left running, and a
  message it writes to an agent is from it.
- **A janitor that is made as a new agent** (`POST /janitors` with a `name`, a tool, where it works and the
  id of the request; `clarp-admin janitor create --name ...`) is one request however often it is sent
  (`janitors/creation.rs`, by the Python Host's `begin_creation` and `complete_creation`, held by 48 requests
  and a story of 13 steps from `tools/janitor-creation-vectors.py`): what it says is checked whole before
  anything is made, a session is kept for it (the letters of its name and twelve signs the Host draws), its
  agent is made as a janitor on the model its tool has for janitors, it is given its job, and what was
  answered is kept as the answer. Sent again it makes nothing; under the same id with other settings it is
  refused.
- Not yet: the jobs that are no scheduled task, no routing, no explaining and no keeping of goals or of
  audio's books (the label keeper, the janitors of accounts), and a janitor's own goals and published
  files.

**The router of spoken messages** (17 Nov 2026). The owner's own words for what this is for: hands-free,
without names. A message that is dictated hands-free and is not said to be for the agent it names can be
shown to a model that says whom it is for. On the Python Host that is the job of one of the Host's own
janitors (`message-delegator`), and so it is here: the router is on when that janitor is, set as that janitor
is set, and every message is routed in a run of that janitor, under an id made of the message's own, so that
a message sent twice is decided once. The Host installs the janitor paused: the router is off until its owner
turns it on.

- **The rules are the kernel's** (`clarp-kernel` `router.rs`, `router/packet.rs`, `router/flow.rs`), read
  from the Python Host's router (`lib/orchestrator.py` and the three files split from it, 1,900 lines) and
  held to it by vectors made by driving that Host's own code. The pure rules: when the router takes a message
  at all, what a model is told, the object found in what it answered, the decision read from it, when it is
  asked once more with every agent in view (`tools/router-vectors.py`). What a model is shown, the packet,
  with its mark: the message, the agent it was said to and the one that is open with what they were last told
  and said, every other agent by its name, the messages that are held, and the agents whose names sound like
  a word of the message (Python's `difflib`, and a score rounded as Python rounds). And a message's way, in
  four steps with the asking between them: `begin` (whether the router takes the message, the run it is
  routed in, a message sent again), `answered` (what is made of an answer, and the second asking), `apply`
  (what is done: a message given to an agent, a word aloud, a message held while its user is asked, an agent
  opened, stopped or started) and `finish` (what is kept: the decision, the receipt of the run, what the apps
  are told). `tools/router-flow-vectors.py` drives the Python Host's `OrchestratorService` through 107
  messages in nine stories with a model that answers what each story says and a Host around it that writes
  down what it is asked to do; `tests/router_flow_vectors.rs` holds this router to it message by message:
  the 170 packets as text, what was given, said, held and played again, the decisions as rows, the runs with
  their receipts. A story may make agents as it begins (one known by a name that is not its session, two of
  one name of which one is put away, one whose name folds to more letters, one with no name; twenty-one more than a packet shows in detail).
- **Holds, commands and names, as the Python Host has them since its PR 436** (18 Nov 2026; until then this
  router did three things otherwise on purpose, the Python Host's R32 to R34, which that PR fixed):
  - A command is never held. It is carried out only where it is as sure as a message has to be; a fork is
    said in the app; a new agent acts on none there is; any other acts on the agent it names and on no
    other, and only on one the model was shown (in a call, one of the call). Refused, it becomes a question
    in its sentence ("I do not have an agent by that name.", "Which agent did you mean?", "That agent does
    not accept voice commands.", "I cannot do that for that agent from here.", "I am not sure what you wanted
    me to do.", "Please fork agents from the app.", "I already have an agent by that name."), said as the
    agent the message was said to unless an app asked, and nothing is held: a later "yes" must not give a
    command's words to an agent as a message. The decision keeps the key of its sentence (`no_such_agent`,
    `which_agent`, ...: the Python Host's `orchestrator_phrases`, its R76 of the same day, PR 439). A new
    agent never takes a name an agent has already as it is spoken, one put away included (its R75). A start
    the Host itself refuses is said ("I could not do that.") and kept as a question with the Host's word for
    why, never as a silent error: the Host hands the kernel the refusal (`Done::Refused`) and says what the
    outcome still has to say.
  - A held message belongs to the session it was made in. The packet shows only the holds of the agent the
    message was said to; a confirmation claims only such a hold, and one that confirms a hold that is not
    there (its time is up, it was given, it was made while speaking to another agent) gives nothing and says
    "That message is no longer waiting. Please say it again." (kept under its key), also where an app asked. Asked about again in
    its own session while its time is not up, a held message keeps its words, its id, its trace, its time and
    the way it came in; whom it may be for, why it is asked and until when are the new question's, whom
    even where it names nobody. Anything else is held anew under an id of its own.
  - A name given in `target_session`, or of a command for an agent in the payload's `name` (a text), is the
    target; the agent the message was said to is the target only where no name is given. Names that
    disagree are asked about, and a name nobody has makes no agent. A command carried out is told and kept
    for the session it acted on.
  - Names are resolved as voice names: the agent of that session or id where there is one, else every agent
    whose session or name it is with case folded as Python folds it and accents taken off (the compatibility
    decomposition without the signs that combine), agents that were put away and janitors counted, so that
    one out of reach is told apart from one there is not. A question how an agent is doing follows the
    same: of a name nobody has, two have, or one out of reach (put away, of no call, a janitor), the router
    says so in its own voice and tells nobody's status.
  The Host only hands the kernel the session a message was said to and every agent it has; whom a hold
  belongs to is the kernel's rule. The wrong rules `tools/plants/router-436.py` hold all of it.
- **The Host's side** (`clarp-host` `router.rs`). The router's book (its decisions, the newest two hundred;
  what it holds; what it gave each agent) is kept with the fleet, and changed together with the janitors'
  book in which its runs are. A dictated message is shown to it before it goes on (`edge.rs`, `sent_from`):
  first whether there is a router at all, which is a look at the janitors' book and costs nothing; then the
  agents are read. What the router asks of the Host is done by what the Host already has: a message is given
  by a send that says whom it is for, as the message came in (a held message as it came in then, under its
  own id); an agent is opened as an app opens it and stopped as an app stops it; a word aloud is a clip in
  the agent's voice or the router's, played whichever agent is open, where an engine of this Host can say it.
- **Whom the model is, and how it is asked** (19 Nov 2026). Any tool of the Host that can be asked one
  question, as the Python Host's router asks any of its own (`backends.routing_adapters`): Claude Code,
  Codex, Antigravity, Grok, OpenCode, DeepSeek and Muse, offered to an app in that order and in its words,
  and known by the names the Python Host knows them by (`antigravity`, `claude-code`, `open-code`, ...). Each
  is asked once outside every conversation (`once.rs`, `Backend::once`), started as the Python Host starts it
  for one question (`routing_cmd`), and its answer found where the Python Host finds it (`routing_text`);
  Antigravity in a home of its own, with its login linked and its settings copied. Held by vectors of the
  Python Host's own functions (`tools/router-tools-vectors.py`: 9 providers, 33 names, 240 commands of 7
  tools, 13 answers) and by the Host's test of a router that asks Antigravity. Five things are done
  otherwise on purpose, each named in `clarp-backend` `tests/router_tools_vectors.rs` and registered:
  Claude Code is asked without its user's MCP servers; Codex is given its effort as a TOML string;
  Antigravity is asked for one JSON object that says how it ended, and given an effort only where no model
  is named; OpenCode and DeepSeek are asked with OpenCode's own question tool denied. Where the question goes
  is the next point's. It can also ask the OpenAI API and a typed decision model, which this
  Host has not built in and refuses in words when it is set to one.
- **How the router asks its model.** The router asks one question outside every conversation.
  Claude Code and Codex read it on stdin. Grok and Muse read a private file. For tools
  that require an argument, the Host checks the byte limit before launch. See **Model fallbacks**.
- **How much a model is shown when everyone is in view.** Every agent by its name; what they were last
  told and said, and what they are doing, of the agent the message was said to, of the one that is open,
  and of the twenty others that were last active (of those equally recent, the first listed): the kernel's
  packet chooses them (`in_view`, `BROAD_DETAIL_MOST`), and the Host reads the last words of those and of no
  other, as the packet chooses them. The Python Host
  does the same since 18 Nov 2026 (its `BROAD_DETAIL_AGENTS`); before, it showed every agent's last words
  (measured on the owner's Host on 17 Nov, read-only: 204 agents, half a megabyte of question). Of an agent's conversation the model is shown what
  people and other agents wrote to it and what it answered, and not what the Host sends by itself on its
  schedule.
- **The routes** are the Python Host's, in its shapes and words: `POST /orchestrator/route-delegation` (a
  send that asks for the router), `GET` and `POST /orchestrator/settings` (the janitor is what is read and
  set; the providers are the tools of this Host), `GET /orchestrator/decisions`, `POST
  /orchestrator/addressing` (how a message that names nobody finds its agent on the apps' side: the Host
  keeps what was chosen). Turning the janitor on from its own screen is allowed for this job now.
- Held on a Host by `a_message_dictated_hands_free_is_shown_to_the_router_when_it_is_on` (`tests/host.rs`),
  with a stand-in for Codex that answers by rule and writes down what it was started with and shown: a
  message said to one agent and given to another, the question on standard input and nowhere on the command
  line, the same message again (answered, nobody given it twice, no model asked), the same id for other
  words (409), a message held and then given in its own words when its user says yes, a question about an
  agent, a command that opens an agent, a model that fails (the message goes where it was said to), what is
  kept through a restart, and the router turned off again. And by
  `the_router_shows_twenty_agents_in_detail_and_says_a_start_the_host_refuses`: with twenty-four agents, the
  second asking shows in detail the agent spoken to, the one that is open and the twenty most recently active
  (one of them by a turn it had, the others by when they were made), the rest by name, and the last words are
  read of those only; a start the Host itself refuses (an agent of no name) is said in the router's sentence,
  in the voice of the agent spoken to and before the apps are told, kept as a question with the Host's word
  for why (`name required`), and makes no agent. The order of agents equally recent is the kernel's one rule,
  held by its vectors: a Host's clock gives two agents the same moment by chance only.
- In the conformance corpus (19 Nov 2026) the scenario `router` walks the rules of the Python Host's PR 436
  on both Hosts: a command not sure enough is refused and never held, so the yes that follows is given in
  its own words; a name in a command's payload that is another agent's, or nobody's, is asked about and
  makes or starts no agent (`fake_claude.py`: `[qa-unsure-stop:...]`, `[qa-restart-as:...:...]`). A planted
  change to the Python Host that holds a refused command (`router-holds-a-command`) is caught by it.
- **What nothing was delivered for, and a message sent again** (19 Nov 2026, the Python Host's PR 443, its
  fixes of R83 and R84). A command's row, its event and its decision carry the session its name resolved to,
  or nothing; a refused one too where its name resolved to an agent out of reach. A question's carry the
  session its target resolves to, or nothing. The answer to a question, and to a command that did nothing
  (refused, turned into a question, or refused by the Host after it was taken), names the session spoken to,
  so that an app that acts on that session never acts on another agent; a command that was carried out names
  what it acted on, a new agent's session for one it made. An app is told nothing aloud, whichever sentence
  it would be. The run's receipt keeps what the first answer said (`answer_session`, `answer_dispatch`,
  `answer_status`, which the demand history takes since: each only where all fit its bound, and the session
  only where it fits too), and the same message sent again is answered exactly that: its status where it is
  one a reply can carry (a whole number from 200 to 599 but 204 and 304), its session where it is a text that
  is not empty, or the session asked for where none was kept; a receipt that keeps none of it, or none a
  client can read, is answered by its recipient as before, and so is a status of failure (500 where the one
  kept is none a reply can carry). A run whose receipt is refused is closed with its least receipt (what the
  first answer said beside its action, recipient and status of failure; without the session where that does
  not fit; else its action alone), never left claimed. In the kernel's book a receipt is refused only where it
  does not fit the bound (an agent's id longer than five hundred signs); the Python Host's other way, a
  database that cannot be written, cannot happen in a book kept in memory. Held by the router-flow vectors,
  whose stories gained messages sent again: after a question, a refused command and an agent made, said to a
  session longer and as long as a receipt keeps, with the receipt made another before it is sent again in
  each form the Python Host's own tests read (a status of 299, 204, 304, 100, 600, yes, a word, a fraction,
  none; no session, an empty one, none at all; a tool that is a number; an older receipt refused 413, 100,
  503, yes, or with no status), an agent whose id the bound refuses, and a command not sure enough whose two
  names are two agents (its row names neither) or one (its row names her); and by the demand history's own
  vectors of what it takes (`fits_metadata`). On a Host, by the router's test: its question answers the
  session spoken to, and the same message sent again after a restart is answered what it was first.
- Not yet: phrases kept ready-spoken; a model's fallbacks; the two providers that are no tool. In a call,
  only those of the call who can take speech are shown, as there.

**An agent started again in place** (17 Nov 2026). What an app's Restart does (`POST /agents` with
`replace_sid`), and what the router does when it is told to restart an agent: the same agent, under the same
session name and id, begins a new conversation with its tool, and whoever reads its conversation finds it
empty until its next turn. The Python Host was asked first what it does (run 44018613566, a scenario of 34
steps made for the question, reproducible); this is built to its answers.

- **In the kernel** it is one more thing that happens to an agent (`Command::StartAgain`, `AgentEvent::StartedAgain`):
  the tool's session the agent had is no longer continued, and where the event stands in the agent's log is
  where its conversation begins from then on (the recorder's `floor`). Nothing is taken out of the log: what
  was written before is of a conversation that ended, and the reader of conversations does not go below the
  floor (`clarp-actor` `conversation.rs`, `Span`). It happens only while no turn runs. What waits its turn
  and was said already is written anew above the floor, in its own words and under its own id, so that it is
  part of the new conversation and is answered there; what was left for later enters it when its turn starts,
  as always.
- **What an app sees**, as on the Python Host: the answer of a new agent (`ok`, `session`, `name`, its row);
  `agent-roster` with `relaunched`; a conversation that is `missing`, with no name and revision 0, until the
  next turn, and then one of a new name that holds only what was said since; a reader that asks with a
  revision of the conversation that ended is told to replace what it has (`replace_required`); in the
  snapshot no conversation, no session of the tool, no gauge of how full it is and no model that answered,
  while what the agent last said and how its last turn ended stay as they were. What the request leaves out
  stays as the agent has it (its tool, where it works, its voice, its model and effort); a name another agent
  has, an effort its tool has not, an agent the Host has not and a janitor are refused in the Python Host's
  words.
- **A turn that runs is stopped first.** The Python Host lets it run on: its answer then stands in the
  conversation that ended, which nobody reads, and the user is notified of it all the same (the same run:
  `relaunched` between `thinking` and `done`, then a notification). Here the turn is stopped, the mailbox is
  held for that moment and let go again, and what waited goes on in the new conversation.
- **With another tool** the agent's thread is opened again on it (`Amend::Tool`, as for a janitor that has
  no conversation with its tool): after the event there is none. The model it had was the old tool's and is
  not kept; an effort the new tool has not is not kept either.
- Held by the kernel's own tests (the agent's state; the recorder's floor with a message moved, through a
  restart), by `an_agent_is_started_again_in_place_with_a_conversation_that_begins_anew` on a Host, and by
  the scenario `started-again`.

**Native session IDs** (19 Nov 2026, R70). A tool's session name begins with an ASCII letter or
digit and holds at most 128 ASCII letters, digits, dots, underscores and hyphens. A transcript
file must resolve inside its store. Muse also requires 8 to 64 hexadecimal digits or hyphens.
OpenCode and DeepSeek share a database and use the name as a key. Codex matches the complete
timestamped rollout filename and the full session ID. Agent creation checks both the resume and
fork fields before it can open or replace an agent. A malformed field receives HTTP 400 with
`invalid session id`. Replaying an old, invalid binding clears the session used for the next turn.
The reference vectors cover names, resolved files and rollout filenames. The Host test restores
an older binding through the log port and observes the next invocation of a stand-in Codex.

**A new agent begins with a session its tool kept** (17 Nov 2026). An app's sheet for a new agent offers the
sessions Claude Code kept in a directory (`GET /past-sessions`) and makes an agent that takes one up where it
left off (`POST /agents` with `resume_session_id`), or that begins with a copy of one and leaves the session
itself as it is (`fork_session_id`). It is how a conversation held in a terminal becomes an agent.

- **What is offered** is read from the tool's own files (`<home>/.claude/projects/<project>/`): what a
  project's index says of a session, else what the session's own transcript says (where it ran, what was
  asked first, the name its user or the tool gave it); the newest first, 20 of a directory and 100 of every
  directory (`clarp-backend` `past.rs`). By the Python Host's rule (`session_catalog.py`), held by its own
  answers for a made-up directory of projects (`tools/past-sessions-vectors.py`: 26 files, 14 lists), but for
  two things done otherwise on purpose and named in the test: the project of a directory is looked for under
  the name Claude Code gives it (a dash for every sign that is no letter and no digit; R36), and a session an
  index marks as a side chain is not offered (R37).
- **What was said in it** is read from the session's transcript by the Python Host's reading
  (`claude_transcript.py`, `parse_turns`; here `history.rs`, held by `tools/claude-history-vectors.py`: 38
  lines, 14 turns, 13 tools): what a user wrote and what the agent answered, each tool as the card a
  conversation shows with what came back, and nothing the tool wrote by itself. The agent's log takes them
  in one write with what says that the agent took the session up (the kernel's `TakeUp`; an agent moved here
  from another Host is `Adopt`ed), and the agent continues the session: its next turn starts its tool with
  `--resume`. A row is named as the Python Host names the rows it reads from a transcript (`msg-` and twenty
  signs of a digest of the agent, the session and the row's place), and is `commentary`, `final` or neither
  by that Host's own rule for such rows (`message_writes._row_phases`, held by seven made-up conversations:
  what the agent said is commentary where tool work follows it before its user speaks again, and the tools
  a turn itself used do not count as following). The agent is new until its first turn (`spawned`), as on
  the Python Host; one that came from another Host, where it had worked, is at rest.
- **A session is an agent's while that agent's tool goes on in it** (the Python Host's rule,
  `get_by_backend_session`): taken up a second time it is refused (`session_in_use`, with the agent that has
  it), and a request that says to open what is there (`open_existing`) is answered with that agent. The
  session of an agent that was removed, or started again in place, can be taken up.
- **The agent's conversation is its own, under a name of its own.** The Python Host names an agent's
  conversation after its tool's session, so an agent that takes up the session another agent had has that
  agent's conversation, counted on from where it stood: that Host counts every row it has in one sequence.
  This Host counts each agent's rows by themselves, so a reader that kept its place in the first agent's
  conversation would read the second's from a place that means something else there. Under a new name the
  conversation is read from its beginning, which is what an app does when it finds a name it has not. A
  fork's conversation goes by the name of its copy, as on the Python Host: the copy is new, and nobody has
  a place in it.
- **A fork** is a copy of the transcript under a name of its own, every line saying the new session, written
  among the sessions of the directory the new agent works in, where its tool will look for it
  (`history::claude_forked`; the Python Host's `fork.py`). The new agent is the one's whose session it was,
  unless the request names another, and the apps are told of it as of a fork (`agent-roster`, `forked`).
- **Its model** is the one the session last ran on, unless the request names one: the model the newest
  answer of the transcript names (`history::model`, by the Python Host's `_read_model`, held by five
  transcripts made for it).
- **Refused in words, and no agent made**: a session of Codex (`resume_unsupported`: this Host reads Claude
  Code's sessions and no other tool's yet, and offers none of Codex's); a session that is not among those of
  the directory the agent is to work in (404, `Session is no longer available in this directory`); a fork of
  a session that is nowhere (`fork source not found`). The Python Host asks whether a session is there only
  of a request that says to open what is there, and otherwise makes the agent, whose tool then looks for the
  session in vain (`fork.py`'s own words: Claude Code finds a conversation only under the directory it is
  started in). An agent whose session could not be taken up does not stay (`resume_failed`).
- Held by the vectors, by `a_new_agent_begins_with_a_session_its_tool_kept` on a Host whose user has a
  home of their own (the session offered, the refusals, the agent that takes it up with its rows, its tool's
  card and its model, its next turn in that session, the second taking refused and opened, a fork here and a
  fork for another directory), and by the scenario `past-sessions` (29 steps): the suite's stand-in for
  Claude Code keeps its sessions where the real one does, so both Hosts are asked about a session an agent's
  own turn left behind.
- **The Python Host was asked before this was finished** (run 44024248012). Every status was the same on
  both Hosts at that first asking: in use, opened, the two refusals, the fork, the session of a removed
  agent taken up, and in use again. What differed decided four things above: that the agent is new until
  its first turn (this Host had it at rest, as one that arrived from another Host), how a row read from a
  transcript is named and what it is in its conversation, and that two agents never share a conversation's
  name here (the Python Host showed the second agent the first one's conversation, counted on). It also
  showed two things a comparison must not take for answers: the moment a session's file was last written,
  and the session's id inside the words of a refusal. At the second asking (run 44026732020) the Python
  Host answered as it had, and the two Hosts differ in five places, all of them the one thing meant: the
  name of the conversation of the agent that took the session up, registered.

The scenario `janitors` asks 74 requests of both Hosts. Its first asking (run 43998740314, of 64) found three
differences in all of them: a janitor's row says `role: janitor` there, and the Python Host installs the one
that finds whom a spoken message is for with its five options chosen (what its owner had set for the router
before there were janitors). Both are as there now. Its third asking (43999909674), with the steps that let a
janitor go and remove the agent it then is, found what no scenario had asked before: **removing an agent**
(`DELETE /agents/<session>`) was answered with the session's name beside `ok`, where the Python Host says
`ok` alone, and the apps were told that the agent was gone and not, as there, that it had stopped because it
was removed (`agent-state` and `agent-activity`, `stopped`). Both are as there now, and an agent that is not
there is refused in the Python Host's word and words (`agent_not_found`). Two findings in the Python Host on
the way are R29 and R30 in `docs/reference-defects.md`.

Reading for the queue's routes found what no run had: **the phone's ids for its messages begin with `u-`**,
the mark a message's id has in a conversation (`"u-" + id` in the phone app's source, and so in the log:
`DELETE /turn-queue/u-6485F414-...`); the desk's come without. The Python Host files a message under its
app's id as it is when the id has the mark, and with the mark before it otherwise
(`message_import_common._client_message_id`), and names it back to its app as the app named it. This Host put
the mark before every id and took one mark off on the way back: a phone's message was filed as `u-u-...`
and named back without any, so on a phone every message would have stood twice in its chat (an app finds
the row of its bubble by the id) and no waiting message could have been taken back. Every message of the
conformance corpus is named without the mark, as the desk names them, so no run saw it. Now a message keeps
whether its app's id came with the mark (`Message::filed`, `Message::client_id` in the kernel's
`conversation.rs`), what waits and what runs carry the id its app gave it, and a message that names no id is
named by its trace, as on the Python Host. Held by the kernel's own test, by a Host test with ids as the
phone and the desk mint them, through a kill of the Host, and by the scenario `the-apps-own-ids`.

**The queue, by its user's hand** (16 Nov 2026). A waiting message is taken back (`DELETE /turn-queue/<id>`)
or sent now (`POST /turn-queue/<id>/send`), by the id its app gave it. Taken back, it never runs and was
never in the conversation. Sent now, it runs although the queue is held, ahead of what else waits, which
stays held; while a turn runs nothing is sent past it (409, the Python Host's words), and it is answered as
a send that did not have to wait. The rules are the kernel's (`agent.rs`): the command `SendNow` with its
event, and one rule this Host did not have: **a hold holds what waits, and when the last of it has left the
queue, taken back or gone to run, the hold is over** without a resume, as the Python Host ends a pause
(`turn_queue.remove`, `mark_started`). So a stop that held nothing ends with the first thing that goes
through the queue. The apps are told how the queue stands when a message is taken back; a hold that ended
because its last message went to run is told with the news of that turn. A message sent by hand counts twice
in the count of the queue's changes a client orders its news by, once when it is taken out of its place and
once when its turn starts, as the Python Host counts it: the first run against the reference (43965744063)
found every status, body and event of the scenario the same on both Hosts, and that count the one thing
that differed. Held by the kernel's tests (the
order of any 120 commands, sending by hand among them, loses nothing and runs nothing twice), a Host test
through a kill, and the scenario `the-queue-by-hand`. And the words of a waiting message are changed
(`PUT /turn-queue/<id>` with `text`): the message is written anew where it waits, outside the conversation,
its turn points at the words it has now (`Reword`, the journal's `Recorder::reword`), and it keeps its id and
its place; the apps are told to read the queue again. Only what was left for later has words that can
change. A message that was said while a turn ran waits its turn in the conversation on this Host, and what
was said stands (409, in this Host's words). Held by the kernel's test, a Host test through a kill, and the
scenario `the-queue-reworded`.

**An id two agents both use** (19 Nov 2026). An app's message id is each agent's own here: every agent's rows
are written only into its own log, the ids of a tool's answer are made from the message's id in that log, and
the rows of a taken-up transcript or of an agent taken over from another Host are the agent's own. One path
found its message by the bare id across agents: the routes of a waiting message took the first agent that held
the id. They act now only where exactly one agent holds it waiting; where more do, they touch none and answer
409 `queued message id names more than one agent` (the owner's ruling: with only the id the Host cannot know
which is meant, so it does not guess). The Python Host cannot have two: its queue's key is global and it drops
the second (R63). Held by the Host's test `an_id_two_agents_both_use_is_each_ones_own` and the wrong rules of
`tools/plants/ids-across-agents.py`.

**The battle test has begun** (15 Nov 2026, 17:15 CET). The owner declined an install earlier that day (a Host an
agent cannot live on is not put on his machine) and then gave his word for one to battle-test it: "You can
install the host to battletest." The Rust Host runs on elitebook beside the Python Host, as
`docs/battle-test.md` says: its own directories, its own unit, the loopback only, no push relay while no phone
is paired. One agent lives there, on a small model, and `tools/soak` writes to it every quarter of an hour and
audits every message. The first build installed was `d9868bea`. The week of section 2's third proof counts from
here. Two of that proof's three parts have their first evidence (`tools/soak restart`, the table in
`docs/battle-test.md`). On 15 Nov at 17:33 the Host's service was restarted while ten real turns ran, each in a
command of 45 seconds: it answered again after 102 ms with all ten, and all ten ended completed, each message
once, each command run once. And the Host was killed (`SIGKILL`, started again by systemd) with turns in their
commands, with messages just accepted, and with turns at the steps of their ends, two of which finished their
command while the Host was down: eighteen turns, all held. At 20:02 the installer updated the Host to the build
of `61194d0c` with three turns in their commands, the eighth exercise: held. One soak round of the first
thirteen did not complete: at 19:31 the Claude account was out, and the turn ended after three seconds with the
Host's sentence for it under the message; nothing was lost. That build did not wait for an account; the one
installed since does. At 20:40 the Host was updated to `87818123` with three turns running (the ninth
exercise: held), and two minutes later an agent there published an image in a real turn with its own tool,
through the door for tools: its reply is the Markdown that shows the image, and the id in it fetches the
image. At 21:03 the Host was updated to `fedf357e` with three turns running (the tenth exercise: held), and at
21:23 to `d0788034` (the eleventh: held); a minute later an agent there handed over an HTML report in a real
turn with its own tool, and the page is served behind its policy. Open: the week.

Not there yet: the first kept move (stage 2's gate), and the rest of stage 3, which is what makes an agent
livable on this Host. The iOS app names about 150 endpoints; the Rust Host answers the core contract and what
stage 3 has added so far, and leaves every other feature out of `capabilities.features`.

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

**Controller narration** (19 Nov 2026). A limited or full device can request a tutorial line of at most
240 printable characters after normalization. Cartesia speaks with the tutorial persona's configured
voice and model. The Host keeps primary MP3s by voice, model and text in its preview store. Both kinds
of cached clip must be regular files owned by the Host. A configured built-in fallback can speak on a
primary failure; its bytes last for that response. Both kinds of response accept byte ranges.

**Controller custom actions** (20 Nov 2026). The administrator defines an HTTP POST or agent prompt;
a full device may map a button and gesture, and a limited device may read and invoke by ID.
The registry change and its audit commit together before the ephemeral revision notification.
The Host retains 500 audit rows and ten minutes of results, including debounced presses.
Duplicate requests share one execution. Prompts use the same admission as the local admin tool.
HTTP checks the canonical destination at definition and execution, and never follows redirects.

**Due work (20 Nov 2026).** An agent with a full mailbox checks its deadlines after 32 messages.
The scheduled janitors, audio bookkeeper, quota keeper and label checker each get their pass even when a sibling
cannot read the store. The fleet logs the failed obligation. The schedule and heartbeat desks publish a changed
book after the store accepts it, so the next request can retry a refused write. A ring timer that fails at
startup or expiry logs the failure and tries again a second later. An iOS chip whose state cannot be read
arms another look in a minute. Each timer stays with the desk that knows when its work is due.

**Remote action (20 Nov 2026).** The Shortcut's GET and POST send a live event to listening apps.
A limited paired device may use either door. A controller edge requires POST and the receipt names the
same event ID as the broadcast. Reconnecting clients cannot replay an earlier button press.

**The router's ready-spoken phrases (20 Nov 2026).** A normal router announcement can keep its completed
primary audio after the apps are offered their clip. The phrase book belongs to the fleet and the MP3 bytes
to the blob store. A hit still names the speaker of the current request. Factoring out the session means
two agents using the same engine voice can share one fill. No operator script or scheduled operation
asks for audio. The first use after eviction follows the ordinary speech path.

## 7. Stages and gates

| Stage | Content | Gate |
|---|---|---|
| 0 Foundation | this brief, the build queue and the CI gate, the ports with two implementations each, then the compiled skeleton of every crate | green run on main |
| 1 Contract | `clarp-protocol` for the core contract; the conformance suite, proven against the Python Host and proven able to fail; the event model and spool format written down; the behaviour list from the 66 dated notes and the review registers | the suite passes on today's Host and catches every plant |
| 2 Core | kernel, shim, stores, edge for the core contract (agents, send, queue, stop, log, events, snapshot), Claude and Codex adapters, the importer for one agent | section 2, all three kinds of evidence; then the first agent moves |
| 3 Daily use | speech in and out, push, artifacts, goals and their wakes, helpers, background jobs | Peter uses it for a week with agents he chooses |
| 4 Everything | autonomy on the trigger engine, live calls, remaining backends, accounts, install and update | the last agent leaves the Python server |

### Before the Rust Host takes a phone

A phone is switched from the Python Host of a machine to the Rust Host by the Rust Host dialling the
connection relay under the name the phone is paired with. Each of these is true first; none is a note.

1. **Voice goes through the relay.** The owner talks to his agents by voice from the phone: the stream of
   speech and the Oracle's live sockets are WebSockets, and the terminal is one. Each of these routes is
   built against one port that a socket at the door and the relay's tunnel both carry, and is shown
   through a relay before the switch (the release owner of the Python Host, 16 Nov 2026: "a gate, not a
   note"). The port and its two carriers are built, and on them the Host's own terminal, an agent's, and
   the stream of speech, the socket of a live voice call and that of a call of the Oracle (18 Nov 2026,
   "Sockets" in section 6). Still closed as a socket that failed: the Oracle's call of the first kind,
   which is not built.
2. **The Python Host of that machine is stopped and cannot come back by itself.** The relay keeps one
   session for a name and closes the older one when another dials: a Python Host that came back would take
   the phones again, and neither Host would report an error, only lose its connection again and again. The
   order is: stop its unit, disable it, see that it is inactive and disabled, then install the Rust Host
   with that Host's file (`tools/install-host --relay-config ~/.config/clarp/config.toml`). The installer
   refuses the name while that unit is anything but stopped, or anything but disabled or masked.
3. **An agent's commands reach the Rust Host's tools in every shell.** Two halves, and both are needed.
   This side: a turn's path begins with this Host's tools, and every bash reads `BASH_ENV` after its
   profile, which puts them first again; that holds for bash, plain, as a login shell, and one started
   inside another. The other side (the Python Host's wrappers, issue 388): a wrapper that finds itself in a
   turn of another Host, which it knows by `CLARP_HOST_ADDR`, runs `$CLARP_SHARE_DIR/tools/<its own name>`
   instead of itself, or, where there is no such tool, refuses with its name and "this turn belongs to
   another Clarp Host" and exit 69; it never goes on to the Python Host. For that, every turn of this Host
   carries both variables (measured on 16 Nov 2026 in a Codex turn: plain, `bash -lc` and `sh -lc`), and
   its `tools/` holds a tool under every name the Python Host has (the installer writes one for each).
   A wrapper marks its hand-over (`CLARP_WRAPPER_HANDOVER=1`) and refuses a call that already has the
   mark, so that two wrappers never hand a call to each other without end (the release owner's addition
   of 17 Nov 2026, after the first review of the wrappers). This Host's tools are no wrappers and take
   the mark off before they run: what a tool starts, a helper or a worker that outlives the turn, may
   call a tool again, and that is a call of its own.
   What an agent's turn does under `sh -lc` or zsh: **before** the Python Host's wrappers hand over, the
   profile's order stands, the Python Host's tool is found, sets that Host's environment and acts on
   that Host, under the agent's own session name (`docs/battle-test.md`, "Where the fence between the two
   Hosts did not hold"); **after**, the same wrapper is found and hands the call to this Host's tool, or
   refuses. So no agent of the owner's moves before the wrappers are installed on its machine.
   **Installed on both machines on 17 Nov 2026 at 01:54** (the release owner's PR 390, release
   `76ee2f6b`), and shown from this side on elitebook the same day in a real turn of Codex
   (`docs/battle-test.md`, "The fence holds"): under `sh -lc` an agent's tool is the Python Host's
   wrapper by its name, and its call reaches this Host; without this Host's share directory in the
   turn it is refused. With that, a test agent here may call a Clarp tool as a skill does. Not part of
   that fence: the nine `clarp-*` names on the owner's path that are his own scripts and no wrappers of
   a Host (`clarp-prompt`, `clarp-contacts`, `clarp-master` and others); one of them, `clarp-prompt`,
   calls `clarp-admin`, which is fenced. They are looked at again before an agent of his moves.
4. **The phones' pairings are there.** The paired devices of the Python Host, each with the hash of its
   token, its scope and its name, are in the Rust Host before it dials: a phone presents the token it has.
5. **Pushes have an account.** The Rust Host has a credential of its own at the push relay, given by the
   owner; the Python Host's is never copied. And for a ring to ring a locked phone as an incoming call, the
   Host is installed with the word that names the file of the owner's key at the push service
   (`tools/install-host --push-keys FILE`): it is his word to give, the key stays where he keeps it, and
   without it this Host pushes no call.
6. **What the owner set for all his agents is set here.** The settings a Host keeps for itself do not
   come with an agent: the heartbeat's interval, strategy, cap and dormancy, the herald's, and what else an
   app's settings screens write. They are read from the Python Host's own routes and set on this Host
   before the switch, and read back from both and compared.
7. **The owner has tested it himself**, after everything is implemented, voice included (his word of
   16 Nov 2026), on a name of the battle test's own (`elitebook-rust`), which takes no phone from anybody.

### Known weaknesses of the Rust Host, to mend

- **The states a turn passed through are left out when the agent's next turn began in the same
  breath.** This is what is left of a weakness found on 17 Nov 2026 (conformance run 44111344344):
  an app was told the state an agent was in when what there was to read of its tool had been
  read, so a state passed through within one read (using a tool, answering) was not told and one
  a read happened to end in was. With a real tool, which takes seconds between one thing and the
  next, every state was told; with a tool that prints a whole turn in one breath the answer
  depended on whether the breath was read in one piece. Mended the same day: the reading of a
  turn's spool stops after the record in which the turn moved on to something else, what was
  read up to there is written down by itself, and how the agent stood then is handed on
  (`clarp-spool` `Read1.ends`; `clarp-actor` `pump`, `Outputs.passed`); the Host tells each such
  state, in order, before the state at the end (`clarp-host` `agent.rs`, `show`). What is told
  follows from what the tool printed. Held through the Host with a stand-in that prints a whole
  turn with a tool in one write, and in the conformance suite, which asks this Host through the
  same stand-in as the reference again: three runs with the stand-in's pause and one without gave
  the same events. Left: where a turn's last states, its end and the beginning of the agent's
  next turn (a message that had waited) are read in one breath, the end is told and the next
  turn's states are, and what the turn passed through just before its end is not. To mend by
  telling an agent's states from the log itself (`Command::TurnMoved` is in it) and not from what
  one pass of the actor hands on.

## 8. Roles

| Role | Session | Owns |
|---|---|---|
| Lead | `maximus-6c37` | this brief, the ports, the workspace, decisions; the split of what is left, the briefs, review, integration, the gates, landings and the installed Host |
| Builders | helper agents of the lead, one for each piece | their piece, on a branch and in a worktree of their own, to the lead's brief |

Stage 1 was built by the lead alone (Peter's decision of 15 Nov 2026), and so was most of what followed.
**Since 18 Nov 2026 the lead builds nothing himself** (Peter, 16:47 that day, through his front desk: "spawn
Astra 6.2 agents on high to work in parallel to speed up. He is now just coordinator"). What is left is split
into pieces. Each is built by a helper on Codex (`gpt-6.2-astra`, effort high), started through
`clarp-sub-agents` as a Clarp helper agent of the lead's with a worktree of its own
(`~/GIT/clarp-worktrees/<piece>`, branch `rebuild/<piece>`). The lead writes the brief, reviews what comes
back, puts the pieces together, runs the gates, lands and installs. A helper pushes its own branch and nothing
else, installs nothing, messages nobody but the lead, and asks him when its brief does not say. The helpers'
guide (what the method is, where to compile, the limits) is `/var/tmp/maximus/helpers/GUIDE.md` on elitebook;
where it and this document differ, the guide is the newer.

Out of scope by the owner's word of the same day: dreaming ("discard dreaming. It's an old feature"). In
scope by his word of 17 Nov 2026: every backend the Python Host offers, not only Claude Code, Codex and
Antigravity ("we need all the backends"), and asking a provider how much of an account is left, with his
logins ("Of course Max can ask how much it is left").

Reviewers use a different model family from the authors and never edit sources; the owner decides when they
come ("We will wait with reviewers", 16 Nov 2026).

## 9. How to work

- One worktree per agent under `~/GIT/clarp-worktrees/`, one branch per slice. Commit after every meaningful step.
- A helper's compiler is `server-rs/tools/build check`, `clippy` and `fmt`, and its tests run in GitHub Actions on
  its branch: the build machine has one slot and a small disk, and a test build of every helper's checkout does
  not fit on it (18 Nov 2026: 99 percent full; one checkout's test build had grown to 82 GB). The lead's
  compiler is `server-rs/tools/build check` (seconds once warm), then `test`, `clippy`, `fmt`; `status` shows
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
- A candidate lands when three things are green on its commit: `Server (Rust)`, the conformance workflow, and
  the repository's own gates with a release built from a clean checkout of that commit. When main moves under
  a green candidate (it does several times a day: the Python Host's line lands there too), the candidate is put
  on top of it and `Server (Rust)` and the repository's gates are run on the new commit, which takes minutes.
  The conformance workflow, which takes twenty, is run again when main moved by something a scenario can reach:
  anything under `server/` that a route of the corpus runs through, and anything under `tests/replay/`. It is
  not run again for a move no scenario can reach (tests, documents, and the modules the corpus has no door to:
  the janitors' own, the hotseat, dreaming, the scheduler), because the reference it compares with would answer
  the same. Which it was is written down with the landing. A wrong reading costs nothing that runs: the next
  candidate's run compares with the newest reference, and says so. Where main moved by nothing either run
  reads at all, no reading is needed: `tools/gated-trees <the commit the verdict was given for> <the new
  commit>` compares the trees and files the two runs build and run (`server`, `server-rs`, `tests/replay`,
  `bin`, the lock of the Python packages, the two workflows) and ends 0 when every one is the same. Then the
  verdicts stand, and what runs again on the new commit is what reads the rest of the repository: its own
  gates, and the release that is installed (17 Nov 2026: main moved twice under one green candidate within
  twenty minutes, both times by a hook of Claude Code and its tests).

**The reference** (20 Nov 2026). The Python Host is merged into main several times a day, and it does not
wait for this Host. A gate taken against main's tip was voided by every such merge in its middle. So this Host
is held to one named commit of main, the reference: the Python Host as it is at that commit.
`server-rs/REFERENCE` names it, the full commit on its first line. The fixtures are generated from it, and the
conformance run compares with it. Where the paragraph above says main, read the reference.

A candidate is gated on its gate tree: the reference's tree with `server-rs/` replaced by the candidate's.
`Server (Rust)`, the planted lists, the conformance run and the regeneration of every fixture are taken there.
The build machine's Rust tests and the repository's own gates are taken on the commit that lands. What lands is
main of that minute with the piece's commits on top. That is sound while the candidate's `server-rs/` is byte
for byte the gate tree's. It holds because the piece touches nothing outside `server-rs/` and only the lead's
trains write under it.

The debt is what main has outside `server-rs/` beyond the reference: the merges of the Python Host not yet
ported. A catch-up piece moves the reference forward, and only a catch-up piece does; the lead lands it. It
first registers what the new reference changes: a new name in `NOT_YET` or `NOT_BUILT` of
`conformance/conform.py`, a changed value by its id in `conformance/awaited.txt`, a new key a generator leaves
out. Then it ports, and the port takes those registrations out again.

`server-rs/tools/reference` is the tool for it, git alone and no network. `show` names the reference and the
debt: the first-parent commits of the fetched `origin/main` beyond it that touch anything outside
`server-rs/`, and how long ago it was fetched. `commit [REV]` writes the gate commit of REV (HEAD by default)
and prints its id, without touching a checkout: the reference's tree with REV's `server-rs/`, the reference as
its one parent. It refuses, in words, a `REFERENCE` that names no commit, a REV whose history lacks the
reference, and a REV that changes anything outside `server-rs/` against its merge base with main. A helper
runs a generator with `reference run <generator> [args]`, never by hand: the tool stands up the gate tree of
the working tree in scratch (uncommitted changes of `server-rs/` included), runs the generator there with the
Python Host's interpreter, copies back exactly the files under `server-rs/` it changed, names them, and
removes its scratch. `reference run --all` does so for every generator. A generator that fails has nothing
copied back.

`reference check` runs every generator the same way and writes nothing back: it names each fixture that would
change and leaves the working tree as it was. It ends 0 only when none fails and no fixture would change. It is
what a piece runs before it reports, and what a catch-up piece reads to see what the new reference moved.

Every generator the tool runs gets a scratch place of its own, whatever the generator sets itself:
`CLAUDE_PWA_DB` names a fresh database inside it, and `HOME`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME`,
`XDG_CACHE_HOME` and `XDG_STATE_HOME` point inside it too; nothing else of the runner's environment passes. A
generator of ours read the default database of whoever ran it once the Python code under it began to ask its
store (20 Nov 2026). The interpreter is read before the home is changed. `reference selftest` holds each of
these by example in a repository it builds in scratch, the refusals included.

**Model portraits and the avatar display preference.** The Host keeps the owner's
choice from `POST /avatar-settings` and returns it to the settings screen and in
the agents' snapshot. For bundled model art the Host tries the agent's model,
the tool's configured default in the owner's settings, then the family the tool
names. Janitors wear the bundled picture of their role or the default janitor
picture. These reads and display changes make no image request.

**Portrait edits requested by the owner.** A full-access POST to
`/agent-portrait-generation` starts a Computer job. The capability GET tells
the app which account pays, why a request is unavailable, and which generation
is active. Opening the gallery, changing a display setting, and restarting the
Host make no paid request. Up to twenty launches of this kind may begin per
Computer in a rolling day. All attempts count. An active duplicate costs
nothing.

The worker sends the current primary portrait to two successive OpenAI image
edits with the fixed close-up and everyday-scene prompts. Both use
`gpt-image-2` at medium quality, 1024 by 1024. A dispatched request can cost
money even if the other fails or the owner cancels. Each request has a
180-second deadline. An input or generated image is bounded at 20 MiB, a
provider response at 32 MiB. `CLARP_PICTURE_ENDPOINT` is the shared loopback
test base for `generations` and `edits`.

At acceptance the fleet checks the running generation and the primary again.
One store transaction records the two new alternates, the successful job and
the local publication receipt with both images. The agent's journal receives
the two media records together. Once local publication finishes the Host
removes the receipt and its staging bytes. On restart a committed receipt can
finish locally and an interrupted generation ends failed. The bounded edit
fixtures come from the Python Host's own functions in an isolated store.

**The tools' skills kept installed (`managed_skills`, 20 Nov 2026).** A release ships a tree of skills with a
manifest (the repository's `skills/`, or the tree `--skills-from` names); the install tool copies it to `<share>/releases/<version>/skills`, keeps the
version installed before and removes older ones, and names it to the Host (`--skills`), with the releases of the
Python Host on the machine (`--skills-other-releases`) and that Host's record of its chosen skills
(`--skills-chosen-before`). It never touches a tool's skill folder, and the programs a skill requires are looked up
on the unit's path, which is the Python Host's. The tools' folders (Claude Code's, Codex's, and Antigravity's where
it is installed) are the machine's, and one Host keeps a machine's tools (the lead's ruling): this Host keeps them
only when it is started with `--accounts select` and a tree. The install tool refuses `--accounts select` while the Python Host of the machine runs
(its unit anything but stopped), with or without a selector of accounts: that Host keeps the tools while it runs,
and links the skills at every update of its own. Then `GET /managed-skills` reports each skill as the
Python Host does (`server/lib/managed_skills.py`: how each link stands, the skill's health, its requirements) and
`POST /managed-skills` switches one on or off as it does: links made or taken away in every folder, a skill of the
user's own preserved and the change refused, the same refusals in the same words, nothing written on a refusal and
the links put back when the list cannot be kept. The chosen list is the Host's own, in its store; it is taken once,
from the Python Host's record where there is one, else from the setup's defaults. At every start the Host enrols the
core skills a newer release brought and links each chosen skill to its own release where nothing or another
release's link is; a link into the Python Host's releases is Clarp's too (outdated), so a machine that Host kept is
taken over without every skill reading as the user's. A Host started with `--accounts follow`, or with no mode,
answers the list empty, refuses every change with `skills_kept_elsewhere` and one sentence, and writes nothing into
a tool's folder, at a request or at its start; `managed_skills` is listed only by a Host that keeps the tools. The
rules are in `clarp_kernel::managed_skills`, held to the reference's own functions run in stand-in homes and release
trees (`tools/managed-skills-vectors.py`); the Host's part is `clarp-host` `managed_skills.rs`; the wrong rules are
`tools/plants/managed-skills.py`. Left out: personal skills and the admin tool's `skills` words.

What a Host in `follow` would need to pass a change on to the Host that keeps the tools, rather than refuse it (not
built): which peer keeps them (a word of its configuration, which the install tool can write from the Python Host it
finds; `peers.rs` already holds a peer's address and token); a token of full scope there, since the route takes no
limited device; the change relayed as it was asked and the keeper's status and body relayed as they came, and the
reading relayed too, since what is linked is the keeper's release and catalogue, not this Host's; and a keeper that
does not answer told as 502 with the reason, with a change that may have landed never sent again, only read back.

**Signing a tool in and out from an app (`backend_auth`, 20 Nov 2026).** The Host that keeps
its machine's tools (`--accounts select`) runs the tool's own sign-in (`claude auth login
--claudeai`, `codex login --device-auth`, `grok login --device-auth`) when an app asks
(`POST /backend-auth/login`), shows the app what it prints in `GET /backend-auth`'s `tasks`,
hands a code pasted back to the tool's input (`/login-code`), stops a sign-in after ten
minutes, and runs the tool's own sign-out (`/logout`). What an app is shown (a terminal's
signs and anything that looks like a key taken out, the address, the device code, whether a
code is asked for, why it failed) is `clarp_kernel::sign_in`, held to the Python Host's own
functions by `tools/backend-auth-vectors.py`. One task owns the sign-ins
(`logins::sign_in::Desk`); a sign-in lives as long as the Host, as there. Two Hosts may share
a machine, and its tools keep one login each, so only one Host changes them: a Host beside it
(`follow`) or with no mode refuses each change, 409 in words, whatever the request says, and
starts nothing. A sign-in or sign-out takes no lock and waits for no turn, as there: it is the
owner's own act. A turn that runs while its tool is signed out under it fails on the login,
and this Host's account rule takes it up (`recovery::after_failed_attempt`): with a pool of the
tool's accounts the pool decides whether it waits for another; without one the turn ends and
says the sign-in is gone, and the agent answers again once the tool is signed in (held by
`a_turn_whose_tool_is_signed_out_under_it_ends_on_the_login_and_holds_nothing`). A sign-in is
stopped after ten minutes; the Host's tests shorten that with `CLARP_SIGN_IN_MOST_MS`, which is
no owner's setting. The wrong rules are `tools/plants/backend-auth.py`.

Not built: a Host in `follow` passing a change on to the Host that keeps the tools, instead of
refusing it. It would need (1) where that Host is: a loopback address in this Host's
configuration, and proof that it runs on the same machine and home; (2) a credential to it
with full device scope, kept in a file the owner provisions and never in the repository; (3)
the three changes forwarded as they came, and that Host's status and body answered as they
are; (4) `GET /backend-auth`'s `tasks` taken from that Host, since the app follows a sign-in by
reading this one, while the rows are still read here off the same files; (5) when that Host
cannot be reached, 502 in words and never a change sent twice (a sign-in may have started:
its `tasks` are read back instead); (6) a code sent to the Host that holds the sign-in it is
for. The client contract would not change, and the follower of logins already wakes the turns
that wait when the login file changes.

**A harness's providers of models, signed in from the app (20 Nov 2026).** OpenCode, and the
DeepSeek card that runs through it, serve models of many providers, each with a sign-in of its
own (`harness_model_providers`, listed at 75). `GET /harness-providers` lists OpenCode's catalogue
(its cache of models.dev) with the type of each credential in OpenCode's store, never a key;
`GET /harness-providers/models` lists a provider's models; `POST /harness-providers/login` writes
an API key to OpenCode's store or starts the provider's own device or code sign-in in a private
`opencode serve` (loopback, a password of its own), which writes the store itself;
`/login-code` hands it the code; `/logout` takes a credential away. The rules are the Python
Host's, run from its own module and route handlers (`tools/harness-provider-vectors.py`) and held
in `clarp_kernel::harness_providers`; the Host reads and writes the files and runs OpenCode
(`clarp-host` `harness_providers`). A key is written in one move, 0600 in a folder made 0700, and
is in no answer, no log line and no error. One desk owns the sign-ins that run, and is told by
threads of its own what OpenCode answered: a sign-in's server is ended before its end is told, and
the app's picker of models is made to ask again before a completed sign-in is told, so whoever
sees it complete finds the provider's models. Which sign-ins a phone can complete is asked of
OpenCode aside, once a day, and kept in the fleet's settings, and only by a Host that keeps the
tools: any other never starts OpenCode (the lead's review: a Host beside the Python Host would
run the owner's tool in his home for a sign-in it refuses). The tools' logins and stores are one
per machine, and one Host keeps a machine's tools (the lead's ruling): with `--accounts select` the
Host changes them as the reference does; beside a Host that keeps them (`--accounts follow`) and
with no mode it refuses every change in words, 409, before anything is read, and writes nothing;
the readings answer in every mode.

Not built: passing a change on to the Host that keeps the tools instead of refusing it. It needs
the address of that Host and a credential of full-device scope on it (a peer today holds an
address and a token for `/send` only; such a credential is the owner's to grant); the four
changes passed on as they came, the key in the body over loopback only and never logged, and the
other Host's status and body answered as they are; the sign-ins that run, which live in the
memory of the Host that runs them, read from it too (the `tasks` of `GET /harness-providers`, or
the whole answer), or an app never sees its code; this Host's picker of models made to ask again
after a change passed on (or OpenCode's store watched by its digest, as the tools' logins are);
and the other Host down means refused, and a change whose acceptance is not known is never sent
again.
