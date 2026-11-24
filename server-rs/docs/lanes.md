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
codex_lane_idle_minutes = 15  # a lane with no turn and no work that may continue leaves after this
lane_memory_mb = 2200         # what all lanes of the Host may hold together, in MiB (the default)
```

- **One switch per tool.** `codex_chat_lane` is the only one today. The switches for Claude,
  Muse and OpenCode come with their drivers.
- **The idle time is shared by every tool's lanes.** It keeps the name it had when Codex had the
  only lane, so an installed config means exactly what it meant.
- **The cap is a memory budget in bytes, one for every lane of the Host** (the lead's decision of
  24 Nov 2026). It is not a count of lanes, because lanes weigh differently by tool. Measured on
  24 Nov 2026: an idle Codex lane about 270 MB, a Muse serve 274-301 MB, an OpenCode serve
  571-590 MB. Eight OpenCode lanes would take about 5 GB.
- **What each lane counts at.** A lane counts at what it weighed itself (`memory_bytes` in its
  status). That weight is the proportional set size of its own process, every process below it,
  and its tool's process group, read from `/proc`. A lane weighs itself when it is ready and
  again after each claim ends, never on a turn's path. A lane that has not said a weight, or
  said 0 (its `/proc` could not be read), counts at its tool's typical size, as does the lane
  about to start. So the budget bounds the lanes even without `/proc`, at about eight Codex
  lanes by default, and no separate count is needed.
- **Room is made before a lane starts.** The idle lanes leave, longest idle first, until what all
  lanes hold, the new one included, is within the budget. A lane that runs a turn, or whose work
  may continue (an active goal), refuses to leave and counts.
- **The default is 2,200 MB.** This is the figure the owner approved for the Codex lane: eight
  lanes at about 270 MB. Switching on lanes of other tools never quietly raises what he agreed
  to. With Codex as the only driver, the default keeps about eight Codex lanes, as the count of
  eight did. Lanes that weigh more than 270 MB leave sooner, because the bytes decide.
- **Who sets them.** `install-host` carries these keys through an install. Switching a lane on
  for the owner's agents is his decision.

### Compatibility: an installed `codex_lane_most`

`codex_lane_most`, the count of lanes that was the cap, is no longer read. A Host whose
`config.toml` still has it logs once, at its start:

> `codex_lane_most = N in config.toml is no longer read: lanes are kept within lane_memory_mb
> (2200 MB unless set), the memory all lanes may hold together`

`install-host` still carries the key through an install, so nothing in the owner's file changes
behind his back. The owner may delete it. To keep fewer or more lanes than the default allows,
set `lane_memory_mb`.

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
    - The agent terminal (`GET /terminal/<session>`) does not retire the lane yet. A person who
      opens a Codex agent's terminal while its lane is idle runs `codex resume` beside it, as
      before the layer. Closing that refuses the terminal while a turn runs, which changes
      Codex's behaviour, so it waits for the lead's word.
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
as D2 says. Today that is Claude, Muse, OpenCode, Antigravity and Grok, and Codex with
`codex_chat_lane` off. A Codex agent then holds the shared thread lock for each `codex exec`
turn, so a goal's lane and an exec turn never write one thread together.

Some turns always run per turn, even with the switch on:

- an agent's opening turn (`order.opening`), which keeps its own gated launch;
- a turn whose start-time inputs a lane cannot take for one turn. For Claude these are a
  read-only turn's edit guard and a takeover's `--resume-session-at`.
- a turn for which no warm lane is ready yet, under the rule above.

## The layer, and what each driver adds

The layer is `clarp-host`'s `src/lane.rs` (settings, endpoint, registry and budget, the
reuse-or-restart rule, `once_more`, the turn file) and the shim's `lane.rs` (the wire),
`lane_own.rs`, `lane_login.rs`, `lane_socket.rs` and `lane_memory.rs`. Codex's app-server is its
only driver: `lane/codex.rs` in the Host, `codex_lane_runtime.rs` in the shim.

No trait or hook is built for drivers that do not exist yet (REBUILD.md section 5). Each
driver's pull request adds what it needs, next to what Codex has, and moves into the layer what
two drivers then share. Named here so that nothing is forgotten:

- **Claude** (stream-json on stdin, issue 761 step 2, after #750 and #747):
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

What in Clarp assumes a process per turn, and must be held by the Claude driver before
`claude_chat_lane` exists, is listed in issue 761.
