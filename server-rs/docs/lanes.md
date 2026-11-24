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
lane_memory_mb = 2200         # what all lanes of the Host may hold together, in MiB (the default)
```

- **One switch per tool.** `codex_chat_lane` today; `claude_chat_lane`, and Muse's and
  OpenCode's, come with their drivers.
- **The idle time is shared by every tool.** It keeps the name it had when Codex had the only
  lane, so an installed config means exactly what it meant.
- **The cap is a memory budget, in bytes, one for every lane of the Host** (the lead's rule,
  issues 761 and 763). It is not a count of lanes: the bytes decide. The weight of a lane
  differs by tool: measured on 24 Nov 2026, an idle Codex lane about 270 MB, a Muse serve
  274-301 MB, an OpenCode serve 571-590 MB. A count alone would let eight OpenCode lanes take
  about 5 GB, so the budget counts bytes. Each lane's memory comes from, in this order:
  - what its driver reads itself and says in its status (`memory_bytes`);
  - else the proportional set size of its process tree, read from `/proc` (the shim, its tool,
    and what the tool started);
  - else its tool's typical size.
  The lane about to start counts at its tool's typical size.
- **Room is made before a lane starts.** The idle lanes leave, longest idle first, until what
  all lanes hold, the new one included, is within the budget. A lane that runs a turn, or whose
  work may continue (an active goal), refuses to leave and counts.
- **The default is 2,200 MB**, the figure the owner approved for the Codex lane: eight lanes at
  about 270 MB. Switching on lanes of other tools never quietly raises what he agreed to. With
  Codex as the only driver, the default keeps about eight Codex lanes, as the count of eight did.
  Lanes that measure heavier than 270 MB leave sooner, because the bytes decide.
- **`codex_lane_most` is no longer read.** The budget replaced the count (the lead's decision of
  24 Nov 2026). A Host whose config still has it logs once, at its start, that it is not read
  and that `lane_memory_mb` holds lanes now. `install-host` still carries the key through an
  install, so nothing in the owner's file changes behind his back.
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
  - **The conversation's lock: the rule for tools that do not enforce one themselves.** The
    lock (`clarp_shim::lane::thread_lock`) is held in one of two ways (`clarp_shim::lane::Holder`):
    - **a per-turn process** (`Holder::Turn`) holds it shared, without waiting, for its whole
      life (`lane::guard_exec`, for every tool that has lanes). Where a lane holds it, the
      per-turn process fails at once rather than write beside it;
    - **a lane** (`Holder::Lane`) holds it exclusive. It takes the lock before it touches the
      conversation in any way (a read, a resume, a prompt), and keeps it until its tool has been
      reaped.
    A lane that warms beside a per-turn turn starts its process without the session. It waits
    for the lock in its own process, never in a turn (`lane_own::hold_thread` with
    `Take::Waiting`), and takes the session up only once the per-turn process is gone. Codex's
    lane takes the lock without waiting (`Take::Now`), and fails to start while a turn holds it.

    For OpenCode this lock is the only guard. It does not refuse a second open: two serves
    share `opencode.db`, and a session's running state (busy, its prompt loop, its abort) lives
    in each process. Two serves prompting one session would run two loops writing one
    conversation. Muse refuses a second process itself (`.session.lock`), and the lock holds
    for it as well.
  - **Where it holds today (Codex, as before the layer):**
    - Every exec turn: lanes leave first (`make_way(Touch::Exec)`), and the lock guards the
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

## No turn waits for a cold lane

The lead's rule for the layer and every driver (24 Nov 2026). When no warm lane is ready, the
turn runs per turn, the tool's exec path as with the switch off. The lane's process starts
beside it, without the session.

- **Warm, no session.** The lane's process is spawned and initialized (Muse in 0.35-0.58 s)
  but holds no conversation and touches none. Its status says `warming: true`, no claim goes
  to it (`Endpoint::warm` is false), and nothing makes way for it: a per-turn process of the
  agent's goes ahead beside it (`lane::make_way` leaves it). The conversation is named to it
  at its start, or, for a new conversation whose id only the per-turn turn learns, afterwards
  with `Control::Take` (`Endpoint::take`).
- **The hand-over.** The per-turn process holds the conversation's lock shared for its whole
  life (`lane::guard_exec`). The lane waits for that lock in its own process, never in a turn,
  then takes the session up: Muse's `session/resume`, OpenCode's `GET /session/:id`. Muse's own
  session lock goes with the per-turn process, so the lane can take the session only once that
  process is gone. The next turn runs on the lane.
- **The worst first turn** is then today's per-turn turn, never a cold start (Muse measured
  9-11 s for one). The cold start shows only as memory taken early.
- **A lane that fails to start** costs nothing: the next turn runs per turn again.
- **The seam.** A driver says which way it goes (`lane::Driver::COLD`, `Cold::Waits` or
  `Cold::Beside`). A `Beside` driver starts its lane with `Driver::warm`. The agent sends an
  attempt to the lane only when `Tool::runs_on_lane` says so: the driver takes it, and, for a
  `Beside` driver, the lane answers and is free (`Endpoint::warm`).
- **Codex today does not keep this rule: a cold Codex lane makes the turn wait.** The first turn
  on a new Codex lane starts the app-server and waits for it to be ready (`ready` after
  `initialize`). That turn's prompt then resumes the thread (`thread/resume`, up to 25 s for a
  long rollout) before `turn/start`. Codex is `Cold::Waits` in step 1, unchanged, until the lead
  says otherwise. Moving it to `Beside` needs a lane that waits for the thread's lock, where it
  now fails at once when an exec turn holds it (`lock_exclusive`, without waiting).

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
  driver that copies its tool's own lines writes through it: the bound turn's `Session` and
  `Spawned`, its `Out` lines, one `Exited`. Codex's lines go through its own `TurnSpool`. A
  claim that bound no turn of the tool is ended there, once.
- **Spawn**: `lane_spawn::kept` starts a tool that does not end when its input closes
  (OpenCode's serve) in a process group of its own, with a death signal from the lane.
- **Claims run in the lane process**, for every driver. The claim is handed to the lane
  process, not lent to a separate per-turn process, so a tool's address or password never
  leaves it.
- **Login**: one login file per driver (`Login.file`, from the backend). The shim compares the
  account the driver's reader names in it, an owned name the reader may derive from several
  fields (OpenCode's `auth.json`). Where the reader finds none, the whole file is compared.
- **Restart digest**: `lane::digest::OptionsDigest`, taken at each turn's start, covers
  options as text and files by the hash of their content. A file over the driver's bound keeps
  no lane. It never uses a time of change (the lead's rule, issue 763): a file written again
  with the same content keeps the lane, and a change made within the same second still restarts
  it.
- **No lease: claims run in the lane process, for every driver** (the lead's decision, issue
  763). A claim holds the lane for one admitted attempt, from its admission (its spool made) to
  its end (its `Exited` written), and is carried out by the lane process itself, as Codex's are.
  The attempt's text, model, effort and identity travel with it, and its Stop, Steer and Follow
  come on the claim's socket. The turn needs no process of its own, and nothing of the tool's
  (an address, a password) leaves the lane process. There is no mechanism that lends a kept
  process to a separate per-turn driver.
- **A claim's turn aborted while the tool is busy** (`lane_driver::Driver::abort`). Today's one
  cause is a Stop whose grace passed with the turn still running. Codex then ends its group
  (D26), the turn is told as killed, and the lane leaves. A tool with a bounded abort of its
  own (OpenCode's `POST /session/:id/abort`, then its idle) may keep its process. A Host that is
  gone aborts nothing: a shim never waits for the Host (D26), and a lane's turn runs to its end
  and is followed again after a restart.
- **The kept child's death with its lane** (the lead's decision, issue 763: a layer spawn
  helper for every driver whose child does not end on its input's end).
  `lane_spawn::kept` runs the tool in a process group of its own, with `SIGTERM` set to reach
  it when the lane's starting thread ends (`setpriv --pdeathsig`; this code allows no
  `unsafe`). A tool that does not end when its input closes (OpenCode's serve listens on a port)
  cannot outlive a killed lane. Codex's app-server ends with its standard input and is started
  as before.
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
- **a follow-up turn in the same process.** A tool may run words steered during its final
  answer as a turn of their own, right after that answer (Muse, 0.08-0.4 s later; Claude Code
  past its `result`). The claim then follows the follow-up turn, and the lane writes
  `steer.read` with `separate: true` when the follow-up starts. The actor takes it on its
  separate-turn path (`Observation::Separate`): its own turn and reply, neither counted as the
  first turn nor dropped. Codex reads every steered word inside its running turn and never
  writes it.
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
- **end of turn**: decided by the driver, which calls `lane_driver::Driver::end_turn` when
  its tool says the claim's turn ended. Codex says it with `turn/completed`, Muse with
  `turn/completed`, OpenCode with `session.idle` after its session was busy, and Claude with
  its `result` line.
- **health**: the lane's snapshot, which any Host reads as `clarp_shim::lane::Status`
  (`Endpoint::inspect`).
- **memory reading**: the driver's own reading, in bytes, in its status (`Status.memory_bytes`;
  bytes rather than MB, so the sum over lanes loses nothing). Else the Host reads the lane
  process's tree (`Endpoint::memory`). Else the tool's typical size. The registry weighs lanes
  by these bytes, never by their number.

What in Clarp assumes a process per turn, and must be held by the Claude driver before
`claude_chat_lane` exists, is listed in issue 761.
