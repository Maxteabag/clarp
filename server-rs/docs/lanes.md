# Lanes: a tool kept between turns, where the owner switched it on

Issue 761, written 24 Nov 2026 with step 1 of the lane layer. The lead's decision is the issue's
comment of that day. This note says what changed in the design of 14 Nov, why, and what holds
either way.

## What the design of 14 Nov said, and what it says now

Decision D2 (REBUILD.md): one Host process, `clarp-host`, plus one `clarp-shim` per running turn,
which owns the CLI child and records its output to the turn's spool. A restart of the Host never
interrupts a turn, and one process writes each spool.

The design now has two ways for a chat turn to run:

- **A process per turn** (D2 as it was). The shim starts the tool for the turn, hands it the
  message, and the tool exits when the turn ends. This is the default for every tool, and the
  only way for a tool that has no lane.
- **A kept lane**, where the owner switched it on for that tool in the Host's `config.toml`. One
  process under the shim keeps the agent's tool alive between turns. Each turn is a *claim* on
  the lane: the Host admits the attempt as before, the lane hands the message to its tool, and
  copies what the tool prints for that turn into the turn's own spool. The turn ends when the
  tool says it has, while the tool stays.

A native Codex goal always runs on a lane, because Codex can start a goal's turn by itself
(REBUILD.md, "Native Codex goals"). That is the same lane, and it is not a switch.

## Why

A process per turn pays for the tool's start on every turn: loading the CLI, resuming the
conversation from disk, connecting MCP servers. Measured on elitebook on 24 Nov 2026 (issue 761,
Claude Code on Sonnet at low effort, "Reply with only the word X"):

- with the process kept, the first text came 0.64-0.75 s after the message;
- with a new `--resume` process per turn, it came 1.91-2.38 s after.

Codex's app-server lane (issue 742) showed the same kind of gain. A kept process also has a
cheaper Stop, the tool's own interrupt, after which the process lives on.

The cost is memory (a kept Claude process is a few hundred MB), and a long-lived process whose
start-time inputs can go stale. The layer holds both: a cap and a memory budget, and a restart
whenever an input changes.

## The switches

Top-level keys of the Host's `config.toml`, read anew at each turn. They are off unless set:

```toml
codex_chat_lane = true        # Codex chat turns run on lanes (default false)
codex_lane_idle_minutes = 15  # a lane with no turn and no work that may continue leaves after this
codex_lane_most = 8           # lanes this Host keeps, of every tool; the longest idle leaves first
lane_memory_mb = 4096         # what they may hold together (unset: no budget)
```

- **One switch per tool.** `codex_chat_lane` today; `claude_chat_lane`, and Muse's and
  OpenCode's, come with their drivers.
- **The idle time and the cap are shared by every tool.** They keep the names they had when
  Codex had the only lane, so an installed config means exactly what it meant.
- **The memory budget is one budget in bytes for every lane of the Host.** The weight of a lane
  differs by tool: measured on 24 Nov 2026, an idle Codex lane about 270 MB, a Muse serve
  274-301 MB, an OpenCode serve 571-590 MB. A count alone would let eight OpenCode lanes take
  about 5 GB, so the budget counts bytes. Each lane's memory comes from, in this order:
  - what its driver reads itself and says in its status (`memory_bytes`);
  - else the proportional set size of its process tree, read from `/proc` (the shim, its tool,
    and what the tool started);
  - else its tool's typical size.
  The lane about to start counts at its tool's typical size.
- **Room is made before a lane starts.** The idle lanes leave, longest idle first, until the
  count is under the cap and the memory is within the budget. A lane that runs a turn, or whose
  work may continue (an active goal), refuses to leave and counts.
- **The budget's default is the lead's to set.** Until he does, it is unset, and only the count
  holds, as before. Whether the count still applies once lanes of other tools exist is the same
  decision.
- **Who sets them.** `install-host` carries these keys through an install. Switching a lane on
  for the owner's agents is his decision.

## What holds either way

These hold for a turn whether its tool runs per turn or on a lane. A driver that cannot hold one
of them is not done.

- **One writer per session: the layer's rule, for every driver** (the lead, on #762). Before
  any other process touches a session a lane holds, the layer retires the lane and waits until
  its tool has exited (`lane::make_way`, with what touches it: `Touch`).
  - **Waiting, not locking:** a tool may refuse a second process at once rather than wait for it.
    Muse fails `muse exec` on a session a `muse serve` holds, and a second serve gets
    `sessionInUse`.
  - **A lane that still runs a turn refuses.** The touch must not go ahead.
  - **What touches a session:** an exec turn on it (the switch off, an attempt the lane does
    not take, a tool without a lane), a fork or status fork that runs the tool on it, a transit
    that opens the old session, and the terminal (`GET /terminal/<session>`).
  - **Forks copy rather than exec.** A fork that copies the conversation opens nothing and
    costs the lane nothing. This Host's helper forks copy it as words. A tool that can fork
    inside its own process does it on the lane (Muse's `session/fork`, as a request on the
    lane's control socket). A status question then never ends a lane.
  - **The lease:** the lock is the rule's other half. A lane takes its thread's lock
    (`clarp_shim::lane::thread_lock`) exclusive and keeps it until its tool has been reaped. A
    per-turn process of the same thread takes it shared and without waiting
    (`lane::guard_exec`, for every tool that has lanes), so if a lane is still there it fails
    rather than writes beside it.
  - **Where it holds today (Codex, as before the layer):**
    - Every exec turn: lanes leave first (`make_way(Touch::Exec)`), and the lease guards the
      turn.
    - Transit: it writes the new tool's session from the Host's record and never opens the old
      one, so it needs nothing.
    - Forks: helper forks are words. Conversation forks are Claude's only, and no Claude lane
      exists yet.
    - Status forks: not a route of this Host; they are a Clarp skill.
    - The terminal does not make way yet. A person who opens a Codex agent's terminal while
      its lane is idle runs `codex resume` beside it, as before the layer. Wiring
      `make_way(Touch::Terminal)` into `GET /terminal/<session>` closes that, refusing while a
      turn runs. It changes Codex's behaviour, so it waits for the lead's word.
- **A turn starts at most once** (D16). A lane makes the claim's spool before it hands the tool
  anything. An attempt whose spool is not there reached nothing of the tool, and only such an
  attempt is offered to a new lane, once (`lane::once_more`).
- **Re-attach after a Host restart.** A lane is a process of its own (`setsid`) under the
  launcher's lifetime scope, like a turn's shim. A Host that starts again asks the lane's
  control socket what it holds (`Inspect`) and follows a running claim's spool. It does not
  start a second one. The lane's owner lock is held until its tool is reaped, so "it has left"
  is known from the lock, not from a reply.
- **The turn-identity file.** A tool a turn calls knows which turn it is in through
  `CLARP_PROVIDER_TURN`. A kept process cannot have its environment changed between turns.
  So a lane is started without that variable and with `CLARP_PROVIDER_TURN_FILE`, and it
  rewrites the file (whole, by rename, owner-only) at each claim. `clarp-host tool` reads the
  file where the variable is not set.
- **A usage limit goes to the pool.**
  - A turn on a lane ends like any other. Its spool is read by the same classifier, so a usage
    limit goes to the account pool as before.
  - The pool's switch of seat changes the tool's login. A lane holds the pool's login lock
    shared only while it holds a claim, so an idle lane never blocks a switch.
  - The lane refuses the next claim with `lane-login-changed` and leaves. The attempt goes to a
    new lane under the new login.
- **Start-time inputs restart the lane.** What a tool is given at start is in the lane's
  options digest: model, effort, system prompt and identity, MCP set, settings, provider. A
  turn that needs other inputs retires the idle lane and starts a new one.
  - A lane busy with its own thread is used as it is.
  - A lane busy with another thread refuses.
- **What a turn left running ends with it.** The lane ends what the tool keeps for the turn
  when the turn ends, and its process group when it leaves.
- **Stop.** The tool's own interrupt first, then its process group once the grace has passed
  (D26). A shim never waits for the Host.

## Fallback: a process per turn

Every tool without a lane, and every tool whose switch is off, runs a process per turn exactly
as D2 says. Today that is Claude, Muse, OpenCode, Antigravity and Grok, and Codex with
`codex_chat_lane` off. A Codex agent then takes the shared thread lock for each `codex exec`
turn, so a goal's lane and an exec turn never write one thread together.

Some turns always run per turn, even with the switch on:

- an agent's opening turn (`order.opening`), which keeps its own gated launch;
- a turn whose start-time inputs a lane cannot take for one turn. On Claude these are a
  read-only turn's edit guard and a takeover's `--resume-session-at`.

## The seam

The layer is `clarp-host` `src/lane.rs`, with the shim's `lane.rs` (wire), `lane_own.rs`,
`lane_login.rs`, `lane_socket.rs` and `lane_driver.rs`. A tool's driver is the rest. Four
drivers are planned, by decision D4 (a second implementation is planned, so the seam is a
trait). Only Codex is built:

- the Codex app-server over JSON-RPC (`lane/codex.rs`, `codex_lane_runtime.rs`);
- Claude Code's stream-json on stdin;
- Muse's MSP over stdio;
- OpenCode's serve.

What every driver gets from the layer, built once:

- **Switch**: `Tool` names each tool's switch, socket suffix, shim word (the lane process's
  subcommand), lock folder and typical size.
- **Starting the lane process**: `Launcher::lane_command(tool, ...)`.
- **Claim requests**: `clarp_shim::lane::Control` (`Inspect`, `Watch`, `Prompt`, `Stop`,
  `Retire`). These are the same lines as Codex's requests of those names; Codex's goal requests
  stay its own. Steer, Stop and Follow of a running turn come on the claim's socket.
- **Claim spool**: admitted by `lane_spool::ClaimSpool` before anything reaches the tool. A
  claim that bound no turn of the tool is ended there, once.
- **Login**: one login file per driver (`Login.file`, from the backend). The shim compares the
  account the driver's reader names in it, or the whole file where it names none (OpenCode's
  `auth.json`).
- **Restart digest**: `lane::digest::OptionsDigest` covers options as text, a file by its
  content (bounded; a file over the bound keeps no lane), and a file by its path, length and
  time of change (a program, or settings a tool reads by time).
- **Process locks**: the owner lock, the thread lock and the exec guard.
- **Room and registry**: idle retirement, the turn-identity file and `once_more`.

What a driver says of itself, rather than the layer assuming it:

- **which attempts it takes** (`lane::Driver::takes`). An attempt it does not take runs as a
  process of its own, after the agent's lanes have made way. Every driver leaves an opening turn to
  its gated launch. A resume-only driver (Muse's serve can take up a session but not start one
  under the Host's id) also leaves the first turn of a conversation, a fork and a takeover.
- **what starts it again** (`lane::Driver::restart_for`, with the digest). What the tool takes
  with a turn or by a call to its running process (a model on Codex's `turn/start`, Muse's
  `session/setModel`) is applied in place. What it reads only at start is in the digest.
- **goals** (`lane::Goals`). Codex's native goals, with its `Reserve`, `Goal`, `Recover` and
  `Probe` requests, are an optional capability that only Codex has.
- **steering**: a tool that takes steered words on its lane says so with `Steers::Words`. The
  actor then reads its lane's lines (`steer.open`, `steer.read`, the turn's end) whatever the
  tool, and the Host offers steering only while that tool's switch is on.

What a driver answers, and where:

- **start**: `lane::Driver::start` in the Host. Attach where `Endpoint::place` says the
  running lane is the one wanted, else start the lane process.
- **send a turn**: `lane::Driver::submit` in the Host, then `lane_driver::Driver::send_turn`
  in the lane. The agent's attempt reaches the driver of its tool
  (`Worker::launch_on_native_lane`, by `Tool::of(backend)`).
- **steer**, **Stop**: on the claim's own socket, as for any turn, answered by the lane's
  `Driver::steer` and `Driver::stop` in the tool's words.
- **end of turn**: `lane_driver::Driver::end_turn` when the tool says it ended (Codex
  `turn/completed`, Claude its `result` line).
- **health**: the lane's snapshot, which any Host reads as `clarp_shim::lane::Status`
  (`Endpoint::inspect`).
- **memory reading**: the driver's own `memory_bytes` in its status where it reads it. Else the
  Host reads the lane process's tree (`Endpoint::memory`). Else the tool's typical size.

What in Clarp assumes a process per turn, and must be held by the Claude driver before
`claude_chat_lane` exists, is listed in issue 761.
