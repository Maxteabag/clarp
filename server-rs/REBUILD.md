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
fractions are read exactly now (`float_roundtrip`). Not there: what a form's page logs of its use
(`html_form_events`), and the reply spoken.

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
recordings, Deepgram and Cartesia as engines that listen, the record of each thing said (`/voice-events`),
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
while it works (`/agent-voice-verbosity`). One thing is not copied: the Python Host never tells Claude Code
the last of these (reference defect R21); here every tool is told. And when an app says that its user began to
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
paused, as on the Python Host, and on Codex until this Host has Antigravity, which is the tool its job
recommends; every update is one call of that tool, so whoever turns it on spends that account. Held
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
user says in the call reaches its tool with why it rang. The feature is advertised (`agent_rings`). Not
built with it, and refused in words that say how to ring instead: a ring held as a live voice call, a ring
that listens first, a phone's own switch for rings (`docs/differences.md`).

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
Not built with it: no route of this Host is a WebSocket yet, so a phone that asks for one through the relay
is told what the Python connector tells it of a Host that could not open one; each such route is built
against one port that a socket at the door and the relay's tunnel both carry. A run through the relay
itself, under a name of the battle test's own, is in `docs/battle-test.md` (16 Nov). Found on the way and
reported: the Python connector dropped the apps' own headers (reference defect R25; fixed there within the
hour, PR 383).

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
  evenly apart. The kernel's `janitors/runs.rs` replays them (`tests/janitor_run_vectors.rs`): where the
  Python Host passes over nothing, what the runner keeps of every trigger, every run and the janitor as it
  is read are equal after every step.
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
- **Where this Host does otherwise** (defect R31 of the reference): the Python Host takes as the wait of a
  task that keeps its pace the gap between the two runs after its first, and asks for the first time at
  least that gap after the time that ran. A task of working days first run on a Thursday measures three
  days and runs on Mondays and Thursdays only; a daily one first run two days before the clocks are put
  back runs every other day; every daily one loses the day the clocks are put forward. Here a task that
  keeps its pace runs at the next time its schedule names, and only a wait that idle runs have stretched is
  counted from the time that ran. The test names the three stories in which the reference loses runs and
  holds this Host to every time of the schedule in them.
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
  are told). `tools/router-flow-vectors.py` drives the Python Host's `OrchestratorService` through 53
  messages in six stories with a model that answers what each story says and a Host around it that writes
  down what it is asked to do; `tests/router_flow_vectors.rs` holds this router to it message by message:
  the 73 packets as text, what was given, said and held, the decisions as rows, the runs with their receipts.
- **Three things it does otherwise, on purpose**, each named in that test with what the Python Host did, and
  each a finding in that Host (R32 to R34 in `docs/reference-defects.md`): a held message keeps its own words
  when its user is asked a second time; no agent is made of a name alone; and nobody's status is told as
  another's.
- **The Host's side** (`clarp-host` `router.rs`). The router's book (its decisions, the newest two hundred;
  what it holds; what it gave each agent) is kept with the fleet, and changed together with the janitors'
  book in which its runs are. A dictated message is shown to it before it goes on (`edge.rs`, `sent_from`):
  first whether there is a router at all, which is a look at the janitors' book and costs nothing; then the
  agents are read. What the router asks of the Host is done by what the Host already has: a message is given
  by a send that says whom it is for, as the message came in (a held message as it came in then, under its
  own id); an agent is opened as an app opens it and stopped as an app stops it; a word aloud is a clip in
  the agent's voice or the router's, played whichever agent is open, where an engine of this Host can say it.
- **Whom the model is, and how it is asked.** A tool of the Host, Codex or Claude Code, asked one question
  outside every conversation (`once.rs`), with the question on standard input. The Python Host gives it as an
  argument of the command line, where every process of the machine can read it and where Linux takes no
  more than 128 KiB (R35); it can also ask the OpenAI API and a typed decision model, which this Host has
  not built in and refuses in words when it is set to one.
- **How much a model is shown when everyone is in view.** The Python Host shows the last ten messages of
  every agent that is not put away. Measured on the owner's Host on 17 Nov (read-only, by a query of the
  check's own): 204 such agents, half a megabyte of question. Here every agent is in view by its name and
  what it is doing, and the last words are shown of the twenty that were last active, and always of the agent
  the message was said to and of the one that is open. Of an agent's conversation the model is shown what
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
  kept through a restart, and the router turned off again.
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
   speech and the Oracle's live sockets are WebSockets, and the terminal is one. The Rust Host has no
   WebSocket route yet and answers such a request through the relay as a Host that could not open it. Each
   of these routes is built against one port that a socket at the door and the relay's tunnel both carry,
   and is shown through a relay before the switch (the release owner of the Python Host, 16 Nov 2026: "a
   gate, not a note").
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
   refuses. So no agent of the owner's moves before the wrappers are installed on its machine, and until
   then no test agent here calls a Clarp tool from anything but bash.
4. **The phones' pairings are there.** The paired devices of the Python Host, each with the hash of its
   token, its scope and its name, are in the Rust Host before it dials: a phone presents the token it has.
5. **Pushes have an account.** The Rust Host has a credential of its own at the push relay, given by the
   owner; the Python Host's is never copied.
6. **What the owner set for all his agents is set here.** The settings a Host keeps for itself do not
   come with an agent: the heartbeat's interval, strategy, cap and dormancy, the herald's, and what else an
   app's settings screens write. They are read from the Python Host's own routes and set on this Host
   before the switch, and read back from both and compared.
7. **The owner has tested it himself**, after everything is implemented, voice included (his word of
   16 Nov 2026), on a name of the battle test's own (`elitebook-rust`), which takes no phone from anybody.

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
