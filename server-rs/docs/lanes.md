# Lanes: a tool kept between turns, where the owner switched it on

Issue 761, written on 24 Nov 2026 with step 1 of the lane layer. The lead's decision is the
issue's comment of that day. This note says what changed in the design of 14 Nov, why, and what
holds either way.

## What the design of 14 Nov said, and what it says now

Decision D2 (REBUILD.md) is one Host process, `clarp-host`, plus one `clarp-shim` per running
turn. The shim owns the CLI child and records its output to the turn's spool. A restart of the
Host never interrupts a turn, and one process writes each spool.

A chat turn now runs in one of two ways:

- **A process per turn** (D2 as it was). The shim starts the tool for the turn and hands it the
  message, and the tool exits when the turn ends. This is the default for every tool, and the
  only way for a tool that has no lane.
- **A kept lane**, where the owner switched it on for that tool in the Host's `config.toml`. One
  process under the shim keeps the agent's tool alive between turns. Each turn is a *claim* on
  the lane: the Host admits the attempt as before, the lane hands the message to its tool, and
  it copies what the tool prints for that turn into the turn's own spool. The turn ends when the
  tool says it has, and the tool stays.

A native Codex goal always runs on a lane, because Codex can start a goal's turn by itself
(REBUILD.md, "Native Codex goals"). That is the same lane, and it is not a switch.

## Why

A process per turn pays for the tool's start on every turn: loading the CLI, resuming the
conversation from disk, and connecting MCP servers. Measured on elitebook on 24 Nov 2026 (issue
761; Claude Code on Sonnet at low effort, "Reply with only the word X"):

- with the process kept, the first text came 0.64-0.75 s after the message;
- with a new `--resume` process per turn, it came 1.91-2.38 s after.

Codex's app-server lane (issue 742) gained in the same way, and Muse's kept serve did too (issue
762). A kept process also stops more cheaply: the tool's own interrupt ends the turn, and the
process lives on.

The cost is memory, and a long-lived process whose start-time inputs can go stale. The layer
holds both: a memory budget, and a restart whenever an input changes.

## The switches and the budget

These are top-level keys of the Host's `config.toml`, read anew at each use. Every lane is off
unless switched on:

```toml
codex_chat_lane = true        # Codex chat turns run on lanes (default false)
claude_chat_lane = true       # Claude chat turns after the first run on a kept Claude Code (default false)
codex_lane_idle_minutes = 15  # a lane with no turn and no work that may continue leaves after this
codex_lane_most = 8           # live lanes this Host keeps, of every tool (the default)
lane_memory_mb = 2200         # what they may hold together, in MiB of PSS (the default)
```

- **One switch per tool.** `codex_chat_lane` and `claude_chat_lane`. The switches for Muse and
  OpenCode come with their drivers.
- **The idle time and the count are shared by every tool's lanes.** They keep the names they
  had when Codex had the only lane, so an installed config means exactly what it meant.
- **Two limits, both always on** (the lead's decision of 25 Nov 2026). A lane starts only when
  both allow it:
  - **a count**, `codex_lane_most`, 8 unless set: live lanes of every tool, the one about to
    start included, with its meaning unchanged;
  - **a memory budget in bytes**, `lane_memory_mb`, 2,200 MB unless set: what all lanes hold
    together, by proportional set size.
- **Why both.** Lanes weigh differently by tool. Measured on 24 Nov 2026: an idle Codex lane
  83-142 MiB of PSS, a Muse serve 274-301 MB, an OpenCode serve 571-590 MB.
  - A count alone would let eight OpenCode lanes take about 5 GB.
  - A budget alone would let Codex keep 15 to 20 idle lanes, more than the eight the owner agreed
    to.
  - 2,200 MB is the figure he approved for the Codex lane (eight lanes at about 270 MB), so
    switching on lanes of other tools never quietly raises it.
- **What each lane counts at.** A lane counts at what it weighed itself (`memory_bytes` in its
  status). That weight is the proportional set size of its own process, every process below it,
  and its tool's process group, read from `/proc`.
  - A lane weighs itself when it is ready and again after each claim ends, never on a turn's
    path.
  - The Host reads no `/proc` when it makes room: it adds up what the lanes said. One
    `make_room` over any number of lanes reads `/proc` not at all.
  - A lane that has not said a weight, or said 0 (its `/proc` could not be read), counts at its
    tool's typical size (Codex 270 MiB), never at 0. So does the lane about to start.
- **Room is made before a lane starts.** The idle lanes leave, longest idle first, until both
  limits hold with the new one included. A lane that runs a turn, or whose work may continue
  (an active goal), refuses to leave and counts.
- **What holds with Codex alone.** The count allows eight lanes. The budget allows eight lanes
  that have not weighed themselves yet (270 MiB each). Lanes that have weighed themselves count
  at what they measured: an idle Codex app-server measured 130, 139 and 142 MiB of PSS on
  elitebook (25 Nov 2026, Codex 0.167.1, one process, no thread loaded), so about fifteen of
  those would fit the budget. The count is the tighter limit, and Codex keeps eight lanes as
  today.
- **Who sets them.** `install-host` carries these keys through an install. Switching a lane on
  for the owner's agents is his decision.

## What holds either way

These hold for a turn whether its tool runs per turn or on a lane. A driver that cannot hold one
of them is not done.

- **One writer per session.**
  - **The layer retires the lane and waits for its release** before any other process touches the
    session a lane holds. That covers an exec turn on it, a fork or status fork that runs the
    tool on it, a transit that opens the old session, and the terminal. It waits rather than
    locks because a tool may refuse a second process at once instead of waiting: Muse fails
    `muse exec` on a session that a `muse serve` holds. A lane that still runs a turn refuses to
    leave, and the other process must not go ahead.
  - **The conversation's lock** (`clarp_shim::lane::thread_lock`) is the rule's other half.
    - A per-turn process holds it shared, without waiting, for its whole life (`flock --shared
      --nonblock`, `lane::codex::guard_exec`). Where a lane holds it, the per-turn process fails
      at once rather than write beside it.
    - A lane holds it exclusive. It takes the lock before it touches the conversation in any way
      (a read, a resume, a prompt), and keeps it until its tool has been reaped.
    - For a tool that does not lock its sessions itself, this lock is the only guard. OpenCode
      does not refuse a second open: two serves share `opencode.db`, and a session's running
      state (busy, its prompt loop, its abort) lives in each process. Two serves prompting one
      session would run two loops writing one conversation.
  - **Forks copy rather than exec.** A fork that copies the conversation opens nothing and costs
    the lane nothing. This Host's helper forks copy it as words. A tool that can fork inside its
    own process should do it on the lane (Muse's `session/fork`), so a status question never
    ends a lane.
  - **Where it holds today, for Codex:**
    - Every exec turn retires the agent's lane and waits for its release first, then holds the
      lock shared.
    - Transit writes the new tool's session from the Host's record and never opens the old one.
    - Helper forks are words. Conversation forks are Claude's only, and no Claude lane exists yet.
    - Status forks are a Clarp skill, not a route of this Host.
    - The agent terminal (`GET /terminal/<session>`): when the agent is idle, its lane is
      retired and waited for until its tool has exited, and the terminal then opens as before
      (the lead's decision of 25 Nov 2026). The wait is bounded at 15 s: a lane whose tool hangs
      on its way out is said once in the Host's log, and the terminal opens beside it rather
      than holding the request and the agent's lease. While a turn runs, the terminal opens as
      before, with no refusal; that two-writer case is older than lanes and is issue #770.
- **A turn starts at most once** (D16). A lane makes the claim's spool before it hands the tool
  anything. An attempt whose spool is not there reached nothing of the tool. Only such an
  attempt is offered to a new lane, and only once (`lane::once_more`).
- **Re-attach after a Host restart.** A lane is a process of its own (`setsid`), under the
  launcher's lifetime scope, like a turn's shim. A Host that starts again asks the lane's
  control socket what it holds (`Inspect`) and follows a running claim's spool. It does not
  start a second lane. The lane holds its owner lock until its tool is reaped, so the Host knows
  the lane has left by the lock, not by a reply.
- **The turn-identity file.** A tool that a turn calls knows which turn it is in through
  `CLARP_PROVIDER_TURN`. A kept process cannot have its environment changed between turns. So a
  lane is started without that variable and with `CLARP_PROVIDER_TURN_FILE`, and it rewrites the
  file at each claim: whole, by rename, readable by the owner only. `clarp-host tool` reads the
  file where the variable is not set.
- **A usage limit goes to the pool.**
  - A turn on a lane ends like any other. Its spool is read by the same classifier, so a usage
    limit goes to the account pool as before.
  - The pool's switch of seat changes the tool's login. A lane holds the pool's login lock
    shared only while it holds a claim, so an idle lane never blocks a switch.
  - The lane then refuses the next claim with `lane-login-changed` and leaves, and the attempt
    goes to a new lane under the new login.
- **Start-time inputs restart the lane.** What a tool is given at start goes into the lane's
  options digest: model, effort, system prompt and identity, MCP set, settings and provider. A
  turn that needs other inputs retires the idle lane and starts a new one. A lane busy with its
  own thread is used as it is, and one busy with another thread refuses. Files a driver puts in
  its digest are compared by the hash of their content at each turn's start, never by their time
  of change.
- **What a turn left running ends with it.** The lane ends what the tool keeps for the turn when
  the turn ends, and its process group when the lane leaves.
- **Stop.** The tool's own interrupt comes first, then its process group once the grace has
  passed (D26). A shim never waits for the Host.

## No turn waits for a cold lane

This is the lead's rule for every driver. When no warm lane is ready, the turn runs per turn:
the tool's exec path, as with the switch off. The lane's process starts beside it, without the
session.

- **Warm, no session.** The lane's process is spawned and initialized (Muse in 0.35-0.58 s). It
  holds no conversation, touches none, and takes no claim. It need not leave for the per-turn
  process beside it.
- **The hand-over.** The per-turn process holds the conversation's lock shared for its whole
  life. The lane waits for the lock exclusive, in its own process and never in a turn. Once the
  per-turn process has exited and released the lock, the lane takes up the session (Muse's
  `session/resume`, OpenCode's `GET /session/:id`). The next turn's claim then runs on it. A new
  conversation's id is known only once the per-turn turn has named it, so it is handed to the
  lane then.
- **The worst first turn** is today's per-turn turn, never a cold start (Muse measured 9-11 s for
  one). The cold start shows only as memory taken early. A lane that fails to start costs
  nothing: the next turn runs per turn again.
- **Codex today does not keep this rule: a cold Codex lane makes its turn wait.** The first turn
  on a new Codex lane starts the app-server and waits for it to be ready. That turn's prompt then
  resumes the thread (`thread/resume`, up to 25 s for a long rollout) before `turn/start`. Codex
  stays so in step 1, until the lead says otherwise. Moving it means a lane that waits for the
  thread's lock, where today it fails at once while an exec turn holds it.
- **Claude keeps the rule, but starts its lane after the per-turn process, not beside it.** A
  stream-json Claude Code takes its conversation when it starts (`--resume <session>`) and has no
  request that takes one up later, so a process spawned beside the per-turn one would have no
  session to hand over to. Its lane is started once the per-turn shim has exited (and the agent
  runs no turn, has no terminal open and is not moving), with that turn's own arguments. The
  start (about 0.6 s, measured 24 Nov 2026) falls in the time the user reads the answer, so the
  next turn finds the lane warm. A turn that comes sooner runs per turn again, as the rule says.
  No turn ever waits for a Claude lane.

## A follow-up turn in the same process

A tool may run words steered during its final answer as a turn of their own, in the same
process, right after that answer. Muse does so 0.08-0.4 s after the answer, with the steer's
`commandId` as the follow-up's turn id. Claude Code does so past its `result`.

- **The claim follows the follow-up turn.** The lane writes the first turn's end, says when the
  follow-up starts, writes the follow-up's lines, and ends the claim at the follow-up's end.
- **The actor takes the follow-up on its separate-turn path**, as it does for Claude's words
  read after a `result`. The follow-up gets its own turn and its own reply. It is neither counted
  as the first turn nor dropped.
- **Codex** reads every steered word inside its running turn and never does this.

## Fallback: a process per turn

Every tool without a lane, and every tool whose switch is off, runs a process per turn exactly
as D2 says. Today that is Muse, OpenCode, Antigravity and Grok, Codex with `codex_chat_lane` off
and Claude with `claude_chat_lane` off. A Codex agent then holds the shared thread lock for each `codex exec`
turn, so a goal's lane and an exec turn never write one thread together.

Some turns always run per turn, even with the switch on:

- an agent's opening turn (`order.opening`), which keeps its own gated launch;
- a turn whose start-time inputs a lane cannot take for one turn. For Claude these are a
  read-only turn's edit guard and a takeover's `--resume-session-at`.
- a turn for which no warm lane is ready yet, under the rule above.

## The layer, and what each driver adds

The layer is `clarp-host`'s `src/lane.rs` (settings, endpoint, registry and budget, the
reuse-or-restart rule, `once_more`, the turn file) and the shim's `lane.rs` (the wire),
`lane_own.rs`, `lane_login.rs`, `lane_socket.rs` and `lane_memory.rs`. Its drivers are Codex's
app-server (`lane/codex.rs` in the Host, `codex_lane_runtime.rs` in the shim) and Claude Code's
stream-json (`lane/claude.rs` and `agent/claude_lane.rs` in the Host, `claude_lane.rs` and
`claude_lane_runtime.rs` in the shim).

No trait or hook is built for drivers that do not exist yet (REBUILD.md section 5). Each
driver's pull request adds what it needs, next to what Codex has, and moves into the layer what
two drivers then share. Named here so that nothing is forgotten:

- **Claude** (stream-json on stdin, issue 761 step 2, after #750 and #747; the full list is the
  last section):
  - its switch;
  - the turn-identity file read by its background hook (`has_turn` reads the variable today);
  - a lane variant of the hook's words, which say "one Claude process per turn";
  - a per-turn provider boundary that does not mark a lane's background tasks as exited;
  - no `guard_turn` login lock held for the process's life;
  - the MCP config file kept for the lane's life;
  - its follow-up turn after a `result`.
- **Muse** (`muse serve` over MSP, issue 762):
  - its switch;
  - "warm, no session" and the hand-over after exec;
  - a model change applied in place (`session/setModel`) rather than a restart;
  - its follow-up turn and its separate-read line;
  - attempts it cannot take (an opening, a first turn under a new id, a fork, a takeover) left
    to exec;
  - forks on the lane (`session/fork`);
  - the retire-and-wait rule for every other touch of a session.
- **OpenCode** (`opencode serve`, issue 763):
  - its switch;
  - a spawn with a death signal and a process group of its own, since its serve does not end
    with its input;
  - a neutral claim spool writer for its copied event lines;
  - its account read from `auth.json`;
  - content hashes of its config files in the digest;
  - its end of claim at `session.idle`;
  - the lane taking the lock exclusive only after the per-turn serve is gone.

Every driver runs its claims in the lane process, as Codex does. No kept process is lent to a
separate per-turn driver.

## What step 2 (Claude) must handle: what assumes a fresh Claude process per turn

Checked on 24 Nov 2026 against the Rust Host (the Python Host noted where it differs).

**Hooks.**
- **SessionStart.** Neither Host installs or passes a SessionStart hook, so nothing of Clarp's
  depends on it firing every turn. A kept Claude process fires it once.
- **The one hook the Rust Host passes** is `PreToolUse` on `Bash`, which runs `clarp-shim
  claude-background-hook <agent>` (`launcher.rs` `claude_settings`, joined into `--settings` for
  turns and forks; the handler is the shim's `background_hook` and `clarp-provider-hook`). It
  fires per tool call, so it survives a kept process. Two things in it assume one process per
  turn:
  - Its `has_turn` reads `CLARP_PROVIDER_TURN`, which a lane removes. It must also read the turn
    file (`CLARP_PROVIDER_TURN_FILE`), as `clarp-host tool` does.
  - Its words (`TURN_CONTEXT`) tell the model "Clarp runs one Claude process per turn ... that
    process exits and this task stops with it". In a kept process that is false, so it needs a
    lane variant.
- **The Python Host's plugin hooks** (UserPromptSubmit, PreToolUse, PostToolUse, Stop,
  PreCompact, Notification, SubagentStop) fire per message or per tool call. They exit at once
  under the Rust Host (`CLARP_RUST_MANAGED_TURN`).

**What is per process today and would break or freeze in a kept process.**
- **The turn token** (`CLARP_PROVIDER_TURN`, set per launch). A kept process would present
  turn 1's token on every later turn. That breaks the trace of artifacts, leader decisions and
  message provenance. The fix is the layer's turn file. A detached worker of an earlier turn
  then reads a later turn's token, as with Codex.
- **The provider boundary** (`helper_source::boundary`, `provider_boundary`). It is recorded
  per launch, and the earlier attempt's background tasks are stopped as
  `provider_turn_exited_without_result`. In a lane those tasks still run. The driver must record
  the boundary for ownership only, and the exit only when the lane's process exits.
- **A read-only turn's edit guard** (`--settings` with the fork's edit guard and `CLARP_FORK=1`,
  as for the transit summary). A kept process cannot take a guard for one turn, so such a turn
  runs per turn.
- **`--model`, `--effort`, `--append-system-prompt`.** Fixed at spawn, so they go into the
  lane's digest and a change restarts the lane.
- **`--resume`, `--session-id`, `--resume-session-at`, `--resume-drops-turn`** (takeover and
  rewind). These need their own process, and the lane makes way first.
- **Per-turn reminders** (persona, spoken, call). They travel in the stdin user line and
  survive. On a takeover they go into `--append-system-prompt`, which is covered above.
- **`pool.guard_turn`** (`flock --shared` on the login lock for the process's life). An idle kept
  process would hold the lock and block the seat switch. The driver passes the login lock and
  file to the lane instead, which holds the lock only while it holds a claim, as Codex's lane
  does.
- **The per-launch MCP config file** (`mcp::configure`), removed when the launch thread ends.
  The MCP set goes into the digest, and the file must live as long as the lane.
- **`gate_steering`, `CLARP_SESSION`, the Host's address and token.** These are per Host or per
  agent and survive. A Host that comes back on another address or token must retire the lane,
  so the address goes into the digest.
- **The shim's per-turn parts** (spool, socket, `--stdin-until result`, `launch_permitted`).
  The lane's claim, turn socket and spool copy replace them, as for Codex.

## The Claude driver, as built (issue 761 step 2)

**When a turn goes to the lane.** Only when a lane is already there for its conversation, free,
and started with exactly what this turn would start Claude Code with. The digest covers the
program; its arguments but the conversation's own (so model, effort, `--append-system-prompt`,
the hook's `--settings`); the working folder; the environment but the turn's identity (so the
Host's address and token); and the MCP choice. Any other turn runs per turn, as with the switch
off, and holds its conversation's lock shared (`lane::claude::guard_turn`).

**When the lane starts.** After the per-turn process is gone, not beside it: a stream-json
Claude Code takes its conversation when it starts (`--resume`), and has no way to take one up
later. So once that turn's shim has exited, and the agent runs no turn, has no terminal open and
is not moving, the lane starts with that turn's own arguments: it takes the lock exclusive and
runs `claude -p --input-format stream-json --resume <session>`. Its start (about 0.6 s) falls in
the time the user reads the answer, so the next turn finds it warm. A conversation's first turn
named it with `--session-id`; the lane resumes it.

**A turn on the lane** (the shim's `claude_lane_runtime.rs`):
- its attempt gets a spool of its own, opened as a turn's shim opens one (`session`, `spawned`,
  `input-open` with the lane as the input's owner), then every line Claude Code prints, but its
  replies to control requests;
- the actor's stream-json user line is written to Claude Code's input as it is, so the prompt
  keeps the uuid the turn's end at its `result` looks for (issue 750);
- more of the user's words come as `Request::Stdin` on the attempt's socket while the input is
  open, until the turn's first `result`, as with `--stdin-until result`. The Codex lane's socket
  refuses them, as before;
- the attempt ends at the `result` that settles every message written into it
  (`user_message_uuids`), and `exited` code 0 follows at once: the linger of issue 750 lets the
  next launch go on it. A message read after a `result` is a turn of its own in the same process
  (the follow-up turn above);
- Stop writes the interrupt `control_request`. Its `result` ends the turn (`exited` with SIGINT)
  and the process is kept. One that does not answer within the Stop's grace is ended with its
  group, and the lane leaves.

**When the lane leaves.** Retired, idle, or Claude Code gone; when the account `.claude.json`
names changed (`lane-login-changed`, the pool's seat switch); after a turn that ended in an
error (a usage limit among them: the pool takes it as today); after a compaction (a Host
`/compact` runs per turn after the lane left, and any `compact_boundary` the lane sees ends it
after its attempt); when Claude Code answers with no turn of the Host's running; and before a
terminal on the agent's tool or a move. For a terminal the layer's `make_way_if_idle` retires
it too; the driver's own `end_claude_lane` also takes the agent's lane-start lock, so a lane that
is starting at that moment is ended as well. A fork does not end it: a Claude fork runs
`--fork-session`, which reads the conversation and writes a session of its own, so it is no
second writer.

**What the list above asked, and where it is held.**
- The hook reads `CLARP_PROVIDER_TURN_FILE` where the variable is not set, and says
  `LANE_TURN_CONTEXT`: a kept process outlives the turn, and ends when the lane leaves.
- The turn's identity is the lane's turn file, rewritten at each claim; the lane's Claude Code
  has no `CLARP_PROVIDER_TURN`.
- The provider boundary is recorded for a lane's first attempt only; its later turns run in the
  same process, and its tasks are closed when a per-turn process next starts.
- A read-only turn (edit guard, `CLARP_FORK`), a takeover or rewind, an opening, a compaction and
  a Claude Code too old to replay what it read run per turn.
- No `guard_turn`: the lane holds the pool's login lock shared only while it owns a claim.
- The MCP config the lane is given is the lane's own file, removed when it leaves.
- Its weight is reported after each claim (`memory_bytes`); until then it counts at 300 MiB (a
  kept Claude Code on Sonnet with no MCP server weighed 159 MiB after a turn).
