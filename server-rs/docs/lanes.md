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
muse_chat_lane = true         # Muse chat turns run on lanes (default false)
opencode_chat_lane = true     # OpenCode and DeepSeek chat turns run on lanes (default false)
codex_lane_idle_minutes = 15  # a lane with no turn and no work that may continue leaves after this
lane_limits = "auto"          # the limits follow the free memory (the default), or "fixed"
lane_reserve_percent = 20     # auto: of the machine's memory, kept free of new lanes (the default)
lane_ceiling = 24             # read and told, never written, and bounds nothing: auto has no count
codex_lane_most = 8           # fixed: live lanes this Host keeps, of every tool (the default)
lane_memory_mb = 2200         # fixed: what they may hold together, in MiB of PSS (the default)
```

The owner changes them from his apps too (issue 779, feature `lane_settings`): `POST
/lane-settings` writes the same keys into the same file, keeping the rest of it, and `GET
/lane-settings` says them with what the lanes hold now (`docs/protocol.md`). An edit by hand works
as before.

- **One switch per tool.** `codex_chat_lane`, `claude_chat_lane`, `muse_chat_lane` and
  `opencode_chat_lane` (one switch for the `opencode` and `deepseek` backends, which run one
  program on one session store).
- **The idle time and the count are shared by every tool's lanes.** They keep the names they
  had when Codex had the only lane, so an installed config means exactly what it meant.
- **The limits follow the machine's memory unless fixed** (the owner's decision of 25 Nov 2026,
  issue 792; `lane_limits`, `"auto"` unless set):
  - **In auto**, a lane starts while the memory the machine has free (`MemAvailable` in
    `/proc/meminfo`, read at each start: one file read, no polling), less the new lane's typical
    size, stays at or above `lane_reserve_percent` of its memory (20 unless set), however many
    lanes run: auto has no count (the owner's word of 25 Nov 2026: many live agents are fine;
    what loads the machine is a tool started anew for each turn). `lane_ceiling` is still read
    and told, and bounds nothing. Each idle lane that leaves gives back what it weighed. A machine with memory to spare keeps more lanes, and a busy one fewer, with
    no number to tune.
  - **Between starts**, the Host has the kernel tell it of memory pressure (a trigger on
    `/proc/pressure/memory`: tasks waiting for memory 10% of two seconds) and then sends idle
    lanes away, in the order below, until what is free is above the reserve. Its own thread
    waits on the trigger; nothing is polled. A kernel without the file, or one that refuses
    the trigger, is said once in the log, and lanes still keep the reserve as they start.
  - **A burst of starts** cannot overshoot the reserve: a lane that has not weighed itself yet,
    or started less than a minute ago (its socket's time), counts against what is free at its
    tool's typical size beyond what it weighed, since Codex loads the conversation on its first
    turn.
  - **A machine whose memory cannot be read** keeps the fixed limits.
  - `codex_lane_most` and `lane_memory_mb` are not used in auto, and are kept for
    `lane_limits = "fixed"`. Setting either from an app (`POST /lane-settings`, an old app too)
    switches the Host to fixed with that value, unless the request names `limits`: the owner's
    count or budget is his choice of fixed limits.
  - **A test's Host** runs fixed (`--lane-limits-default fixed`, which the host harness passes
    unless a test asks for auto) and sheds nothing on pressure (`--no-lane-pressure`), so its
    lanes never follow the test machine's memory.
- **No turn waits for the machine's pressure** (the owner's decision of 25 Nov 2026). Contracts
  186 to 193 held the turns nobody waited on while io, cpu or memory pressure was high (feature
  `turn_pressure_admission`, `src/pressure.rs`), and from 188 showed them waiting for the
  machine. From 194 every turn starts when it is its turn, and a lane may start for any turn
  within the limits above. What keeps the memory safe is the reserve: a lane starts only while
  `MemAvailable` stays above it, and idle lanes leave when it falls below, at a lane's start and
  on the kernel's memory-pressure trigger (`lane::watch_pressure`); no turn waits for it.
  `/lane-settings` tells the live lanes (`live`) apart from the turns that started a tool anew in
  the last hour (`cold_starts_last_hour`, `src/cold_starts.rs`; feature `lanes_live`).
- **Fixed: two limits, both on** (the lead's decision of 25 Nov 2026). A lane starts only when
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
- **With every place busy, an optional lane does not start** (issue 786). Where no idle lane
  is left to leave and the limits still do not hold, a chat turn's lane is not started: a
  Claude agent's next turns run per turn, and a Codex chat turn goes back to the agent's thread
  and runs in a process of its own. Nothing is asked to leave for a lane that will not start:
  which idle lanes would leave is reckoned first, and none leaves unless that is enough. A
  native goal's lane is needed: it sends away what idle lanes it may, starts past the limits
  and counts, so the next optional lane is not started either, and that agent's turns run per
  turn, until a place is free.
- **The owner's agents come first** (his rule of 25 Nov 2026). A lane is the owner's when its
  agent had a turn the owner sent (the Oracle's included) that started or ended in the last 30
  minutes, or a chat of it is open on one of his devices: one of his apps, by a paired device's
  token or the Host's own, watches it live (`/events?live=`); an agent's tool token never counts.
  Any local tool that presents the Host's token counts as well, though none watches live today.
  A chat counts as long as an app names it: the web app names the chats on screen and the few
  opened last; the desktop still names every chat opened since it started, until it caps them
  too (issue 793), which counts more agents as his, never fewer. Every other lane is the agents'
  own: helpers, janitors, one agent asking another. Both are kept as file times in the run folder
  (`<agent>-owner-turn.mark`, set when such a turn starts and when it ends;
  `<agent>-owner-view.mark`, set every 15 s while the chat is watched, counted for 60 s), so
  room is decided without asking any agent's thread. When room is needed, idle lanes of the agents' own leave first, longest idle
  first, then idle lanes of the owner's, longest idle first; a busy lane never leaves. A lane for
  an agent of the owner's may send any idle lane away. A lane of the agents' own may send away
  only idle lanes of the agents' own; where that is not enough it does not start, and its agent
  runs per turn. A native goal's lane is never refused, and counts. No turn waits for a lane.
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
  - **No turn waits on a lane's exit without a bound, and no lane that works is ended**
    (issue #772). Every retirement waits at most 15 s (`LEAVE_WAIT`), and one a turn stands
    behind at most `TURN_LEAVE_WAIT` after the lane began leaving (issue 861, below), for the
    lane's owner lock.
    - **Asked first.** The lane is asked what it does (`Inspect`, whose `working` says whether
      it runs a claim, or for Codex a turn its tool began or a goal that may continue). A lane
      that works, or does not say (an older shim), or is slow to answer, is asked to leave and
      waited for, and never ended: a busy Codex lane can take 25 s to load a long conversation.
      A lane answers a retirement only once it has gone, so a lane that said it was idle and
      has neither answered nor let go by the bound is on its way out. So is one whose socket
      nobody listens on any more: a lane removes its socket as soon as it stops serving, before
      it ends its tool.
    - **Ended with its tool.** Such a lane is ended with its tool, logged once, and the turn
      goes on without it: a new lane, or its own process. A tool that hangs may not read the
      end of its input, and with only the lane gone it would write the conversation beside the
      next lane or turn, and hold memory outside the budget. Three things end it:
      - **its own systemd scope.** The launcher starts every lane in a scope of its own
        (`clarp-claude-lane-*.scope`, `clarp-codex-goal-*.scope`), a cgroup, so `systemctl
        --user kill --signal=SIGKILL` on it ends the lane and every process it started,
        whatever their groups, with no window in which a name means another process. The unit
        is read from the lane's process (`/proc/<pid>/cgroup`), so it is known after a Host
        restart too; only a lane scope that is not the Host's own unit is ended;
      - **where it has no scope** (a lane started outside systemd, as in tests): its tool's
        process group, which the lane writes into its owner lock beside its own id
        (`<pid> <group>`), signalled only while that group's leader is still the lane's child,
        then the lane's process;
      - **a death signal on the tool.** The lane starts its tool through `clarp-shim
        tool-child <lane pid> -- <program>`, which sets `PR_SET_PDEATHSIG` (SIGKILL), checks
        the lane is still its parent, and becomes the tool in place (`execvp`). The tool so ends
        with its lane however the lane ends. It is set there, not in a `pre_exec` of the lane,
        because that is `unsafe` and the workspace forbids it; it fires when the starting
        thread ends, and both lanes start their tool on their main thread.
    - **Whose process.** The lane's id is the holder of its owner lock as the kernel names it
      (`/proc/locks`), never the lock file's words alone: a new lane takes the lock before it
      writes its id, so the file may still name the lane before it. A handle on that process
      (`pidfd_open`, through `rustix`, as the workspace has no `unsafe`) is opened, the holder is
      looked at again, and the lane is signalled through the handle (`pidfd_send_signal`), so a
      process whose id came free and was given again is never signalled. The lane's scope is
      read by its id, so the handle is looked at after that read too: a process that ended
      meanwhile is left, as its id may name a process of another lane's scope. Only a kernel
      without process handles falls back to the id alone, right after the second look.
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
      (the lead's decision of 25 Nov 2026). The wait is bounded as every retirement is
      (above), so the terminal never holds the request and the agent's lease on a lane's exit.
      While a turn of the agent may run its tool (it starts or runs, and does not wait for a
      terminal already open), or a lane refuses to leave because it runs work of its own, the
      terminal is refused: 409 with words that say to stop the turn or wait for its end, in
      plain text like the route's other refusals, and the code `agent_turn_running` in the
      header `X-Clarp-Code` (issue #770). This holds for every tool's terminal, since it is
      asked of the agent's turns and of every tool's lane. A turn between two attempts has no
      tool running: the terminal opens, and the next attempt waits for it.
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
- **Claude keeps the rule by starting the turn's own process as the lane** (issue 833). A
  stream-json Claude Code takes its conversation when it starts (`--resume <session>`, or
  `--session-id` for a conversation its first turn opens) and has no request that takes one up
  later. A turn with no lane there for its conversation starts its Claude Code as the lane, with
  its own arguments, and runs on it: the start is the turn's start, which it would pay as a
  process of its own anyway, and the process is kept for the next turn. Before issue 833 the lane
  was started only after a per-turn process had exited and while the agent ran no turn, so a
  busy agent, whose next turn was already live by then, never got one: on the owner's EliteBook
  one hour saw 199 of 344 Claude turns start cold, and the busiest agent's 46 all did, each
  reading about 1.2 GB of its conversation. A turn for which no lane may start (no place within
  the limits, or a turn nobody waits on while the machine is under pressure) runs per turn as
  the rule says, and a lane may start after it as before. No turn ever waits for a Claude lane.
- **Muse's queued turn has the lane take the session** (issue 833). A conversation's first turn
  still runs on exec, which names the session. A turn launched while no lane holds a session
  that is there (the one queued behind an exec turn, whose lane could not take the session while
  that next turn was already live) has the lane take it first, starting the lane where none
  runs, and runs on it; one the lane cannot take runs on exec as before.
- **A turn refused for a changed login starts the lane under the new one** (issue 849). A seat
  switch makes every lane refuse its agent's next claim (`lane-login-changed`) and leave, with
  nothing of the turn reaching the tool. That turn used to run per turn, so one switch cost a
  cold start per live lane at once (one Host saw 9 Claude cold starts in the 11 minutes after a
  switch, against none in the hour before), each reading about 1.2 GB. Now the refused turn waits
  for the lane to let go (within `TURN_LEAVE_WAIT`, below) and then does what a turn with no lane
  there does: a Claude turn starts its own Claude Code as the lane, still within the lane's start hold, and a
  Muse turn has a new lane take the session. The new lane reads the login there now as it starts.
  A start that still finds the old lane's owner there a moment after it was seen gone is tried
  once more once the owner has let go, within `TURN_LEAVE_WAIT` (issue 857: one seat switch saw
  it, as other agents' lanes made room). The Host's own looks at an owner lock take it shared, so two of
  them never make a lane that has left seem there to each other.
  It is tried once for the attempt, never again; where the start gets no place, fails, or no lane
  may start for the turn (one nobody waits on under pressure), the turn runs per turn as before.
  Codex already did so: its chat turns and goals offer a refused attempt to a new lane once
  (`once_more`), started within the same limits. OpenCode does so for a refused claim and for a
  lane that differs from the turn's by the login alone (issue 857, in "OpenCode's lane").
- **A start waits for a lane on its way out, a turn's at most 3 s** (issue 860). A lane that
  leaves (retired, idle, or refused for its login) stops answering first and lets go of its owner
  lock only once its tool has ended, up to seconds later (Claude Code's `EXIT_LIMIT` is 2 s
  before its group is ended). Every lane start's `place`, of every tool, waits for such a lane,
  nobody on its socket and its owner lock held, rather than looking once and giving up: one seat
  switch's turn came in that moment, through the start of a turn with no lane there, and ran
  cold. A turn waits for a lane on its way out at most `TURN_LEAVE_WAIT` (3 s: the 2 s a tool
  is given, and what a cold start costs anyway), never the 15 s of `LEAVE_WAIT`, in all of it:
  `place`'s wait, the one retry of its start (`start_once_more`, bounded from before its first
  try) and a refused turn's wait before it starts the new lane. A lane that is starting looks the
  same, since every lane takes its owner lock before it binds its socket (OpenCode's holds it
  about 1.5 s while it starts its server first): `place`'s wait ends as soon as the socket
  answers, and the lane is then used, or retired if it was started otherwise, as any lane that
  answers. No lane is ended from that wait. One that has not let go or answered by the bound is
  left; the turn runs in a process of its own at once, and the lane starts for the next turn.
  `LEAVE_WAIT` stays for retirements no turn stands behind (a move, a login change, a lane
  started after a turn of its own).
- **Every wait a turn stands behind, and the one-writer rule** (issue 861). No turn runs beside
  a lane that still holds its conversation; within that, no turn waits more than about 3 s for
  a lane on its way out, and a turn ends a lane only when it said it was idle.
  - **A turn's retirement** (the lane a turn of its own process makes way for, before it starts:
    Codex's, the kept Claude Code's, Muse's that holds the session, OpenCode's that holds it; a
    terminal's; and `place`'s retirement of an idle lane started otherwise that holds the turn's
    conversation) waits while the lane is still in its loop, at most `LEAVE_WAIT` from the ask,
    and from when its socket goes, which every lane removes first, `TURN_LEAVE_WAIT` (its own
    2 s `EXIT_LIMIT` and a second). Only then is what is left of an idle lane ended, so no end
    lands inside its own clean exit or flush. A lane that runs work refuses, is never ended,
    and the turn queues.
  - **`place`'s retirement of an idle lane of another thread** carries on beside the turn, within
    `LEAVE_WAIT`; the turn waits for it at most `TURN_LEAVE_WAIT`, then runs on its own.
  - **Codex's `once_more`** for a chat turn refused for its login waits at most
    `TURN_LEAVE_WAIT` and ends nothing; a lane that has not let go by then is left to leave on its
    own, and the turn runs in a process of its own. A goal's attempt, which no process of its own
    can run, waits as a retirement does.
  - **A lane that hangs while leaving**, which a turn gave up on without ending it, is ended in
    the background once `LEAVE_WAIT` has passed since it was seen leaving, only if its socket
    still does not answer and its lock is still held; a lane that answers by then (one that was
    starting) is never ended. No turn waits on it, and the next turn finds room for a fresh lane.
- **Where a turn's start includes the lane's.** A turn that starts the Claude lane, or has the
  Muse lane take its session, waits for that before its first output: the lane's start, or the
  session's load (`session/resume`, given up at the lane's `LOAD_LIMIT` of 25 s, within the
  Host's 30 s). It is the one place a turn waits on a lane, and it stands in for the per-turn
  process's own start and load of the conversation, which that turn would pay instead.

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
- **OpenCode** starts no run of its own for words stored while its run goes on (a prompt then
  only waits for that run), so its lane takes back unread words at the turn's end, as Muse's
  does. Words that reach it after the run ended do start one; the lane keeps the turn open for
  it (in "OpenCode's lane").
- **Muse's driver does not let it happen** (issue 762, the build). At the end of the turn, and
  at a Stop, every steered message Muse accepted and did not read is taken back from it
  (`turn/unqueue`; one Muse already began is interrupted with `retract` as it begins). The
  message is then unread, and the actor runs it as a turn of its own on the same lane, which is
  what it already does with words a turn never read. The result for the user is the same as a
  follow-up turn, a fraction of a second later, and the actor needs no separate-read line for
  Muse.

## Fallback: a process per turn

Every tool without a lane, and every tool whose switch is off, runs a process per turn exactly
as D2 says. Today that is Antigravity and Grok, and Codex, Claude, Muse, OpenCode and DeepSeek
with their switches off. A Codex agent then holds the shared thread lock for each `codex exec`
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
`claude_lane_runtime.rs` in the shim), and OpenCode's serve (`lane/opencode.rs` and
`agent/opencode_lane.rs` in the Host, `opencode_lane_runtime.rs` in the shim; see "OpenCode's
lane" below).

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
- **Muse** (`muse serve` over MSP, issue 762; `lane/muse.rs` and `agent/muse_lane.rs` in the
  Host, `muse_lane_runtime.rs`, `muse_lane_turns.rs`, `muse_lane.rs` and `muse_turn.rs` in the
  shim):
  - its switch, `muse_chat_lane`;
  - "warm, no session" and the hand-over: an exec turn's thread starts the lane beside exec on a
    thread of its own (exec never waits for it), and once exec's process is gone asks the agent's
    thread whether the lane takes the session. The agent's thread is the one place that decides:
    it lets the lane `take` only while no turn runs, no terminal is open and the agent is not
    moving, and holds every launch of the agent until the take answers. Muse's own
    `.session.lock` is the one-writer lock, so no thread lock is added;
  - a model change applied in place (`session/setModel`) rather than a restart;
  - steered words Muse did not read taken back at the turn's end and at a Stop (above), so no
    follow-up turn and no separate-read line;
  - attempts it cannot take (an opening, a turn with no lane holding its session ready) left to
    exec, which never waits for it;
  - before any exec of the session, a lane that holds it leaves, waited for at most 15 s
    (`retire_within`); one that does not let go fails the attempt in words, and a lane that runs a
    turn is never ended for it; the terminal and a move do the same;
  - Muse's reminder agents are off on the lane as on exec: exec passes `--disable-reminders`, and
    serve is started with `-c run.reminder_roster=[]`, its form of it, named in the lane's options
    (so a lane started with them on has another digest and is replaced at its next turn). The owner's answer on
    decision-d4c9cad82138ed99698f3691 was "Muse reminders off now, a kept-running lane later", and
    the lane follows it until its switch-on card asks him again, with the hold they cost. With
    them on, serve holds `turn/completed` 1.7-3.9 s after the answer, so the lane writes
    `turn.answered` at `turn/foregroundCompleted` and the Muse reader gives `Signal::Answered`: the
    turn ends for the user at its answer and its claim lingers until `turn/completed`. With them
    off, Muse sends no `foregroundCompleted` and the turn ends at `turn/completed`, at once. A turn that read a steered
    message ends at `turn/completed`, since the actor settles that message at the turn's end;
  - its turn spool written in exec's lines, so the Host's one Muse reader takes both, through
    the layer's neutral claim spool (`clarp_shim::lane_spool::ClaimSpool`: made exclusively and
    synced before anything reaches the tool, then each line the driver calls, the lane lines,
    and one end);
  - forks on the lane (`session/fork`) wait until Muse forks exist in Clarp.
- **OpenCode** (`opencode serve`, issue 763): built, below.

Every driver runs its claims in the lane process, as Codex does. No kept process is lent to a
separate per-turn driver.

## OpenCode's lane

Issue 763. One `opencode serve` per agent, for agents of `opencode` and `deepseek`, while
`opencode_chat_lane` is on. Per agent, never shared by agents: a serve's tools inherit its
environment (the agent's session name, its turn file, `OPENCODE_PERMISSION`), and one shared
serve would be one point of failure for every such agent.

- **The process.** `clarp-shim opencode-lane SOCKET PROGRAM OPTIONS`, started through the
  launcher's lane command, in a scope of its own. It makes a password, starts the serve on
  127.0.0.1 with it in the serve's environment only, and keeps the HTTP client itself: the
  password never leaves the lane process. The serve runs in a process group of its own, under
  `setpriv --pdeathsig TERM` where `setpriv` is there, so a lane that is killed takes it along
  (the workspace forbids the unsafe `pre_exec` that would say it otherwise).
- **The program** is the Host's resolved one (`CLARP_OPENCODE_PROGRAM`, the owner's 1.18.33 pin),
  made absolute by the Host: the lane never looks it up on PATH. At start the lane asks the
  serve's `/global/health` and leaves when its version is not of the 1.x line (2.x cannot read
  Clarp's sessions).
- **Ready with no session.** The lane warms the agent's directory (`GET /session`), opens the
  event stream, weighs itself, and says `ready`.
- **The hand-over.** A turn on its own serve runs under `flock --shared` on the session's lock
  (`<run>/opencode-sessions/`). The lane takes that lock exclusive with its first claim, without
  waiting: while it is held the claim is refused before its spool exists, and the same attempt
  runs on its own serve at once. A lane that holds the session lets it go (retired) before a turn
  that must run on its own serve.
- **The lock is taken whether or not the switch is on.** Every OpenCode or DeepSeek turn that
  names a session runs under that `flock --shared --nonblock`, as Codex's exec turns do, so a
  lane left from when the switch was on can never write beside it. The Host needs `flock`
  (util-linux) for these turns.
- **A turn on the lane.** The Host gives it only when the lane answers within 0.5 s, is free, was
  started with the same digest, and holds this session or none. The lane makes the spool, writes
  the session line and the same event lines `opencode-turn` prints, answers permissions and
  questions as it does, and ends the claim at `session.idle`.
- **Stop** is `POST /session/:id/abort`; the idle that follows ends the turn as stopped and the
  serve stays. A serve that does not go idle within the Stop's grace is killed with its group and
  the lane leaves.
- **Restart.** The digest is computed at each turn's start: the program's path and (for a wrapper
  up to 1 MiB) its words, the agent's folder, the environment the serve inherits, and the
  contents (never sizes or times) of `opencode.json` and `opencode.jsonc` from the agent's
  folder up to its worktree's root, `.opencode/` there, the global configuration and `auth.json`.
  The digest has two parts, everything but `auth.json` and then all of it, so the Host can tell a
  lane that differs by the login alone; the lane compares the whole. A different digest runs the
  turn on its own serve and starts a new lane beside it, unless the login alone differs (below).
- **Another login starts the new lane as the turn** (issue 857). A lane that differs only by
  `auth.json`, from a seat switch or a refreshed token (OpenCode's file names no account, so any
  change of it is a changed login), and is free and fits the turn's session or holds none, is
  replaced by the turn itself: the idle lane leaves within `LEAVE_WAIT`, the turn starts the new
  lane in place of its own serve's start, which costs the same (about 1.3 to 1.6 s), and runs on
  it. A claim the lane refused for its login (`lane-login-changed`, a change between the turn's
  digest and the lane's claim) takes the same path once the lane has let go. Once per attempt;
  with no place for the lane (the lanes' memory reserve), or a start that fails, the turn runs on
  its own serve and the lane starts beside it, as before. Any other
  difference (the configuration, the program, the environment) keeps the own serve and the lane
  beside it, so a turn started otherwise never replaces the lane itself. A turn with no lane
  there at all (the first, or after the lane's idle time) runs on its own serve as before.
- **Death.** A serve that exits under a turn fails that turn ("OpenCode server stopped before the
  turn finished"), and the lane leaves; the next turn runs on its own serve while a new lane
  starts.
- **Steered words** (issue 784) go to the busy session with `prompt_async`, under a message id
  the lane makes in OpenCode's ascending form. The lane writes `steer.open` once the turn's
  prompt is posted, and `steer.read` when an answer whose `parentID` is that message starts;
  OpenCode reads it at its next step and answers it in the same busy period. At the idle that ends
  the turn, words still unread are deleted from the session, and the actor runs them as a turn of
  their own, unless the session runs again or already answered them (words that reached
  OpenCode after its run ended start a run of their own, so that idle is stale): then the turn
  stays open until the next idle. A delete refused as busy keeps it open too; any other failed
  delete counts the words as read. A Stop deletes them after its abort, but for words the
  session's stored messages show an answer to, with those very words as its parent (issue 933):
  those are read. An answer to later words proves nothing there, since a post answers before
  OpenCode stores it and the earlier words may never have been stored. Words sent after
  the lane ended the turn are refused and wait for a turn of their own.
- **A reading after a Stop** (issue 933, every lane). The Host takes nothing a stopped tool prints,
  but for one line: a `steer.read` that says `"confirmed": true`, which a lane writes only from the
  tool's own record (Codex's `userMessage` item or its list of the turn, an OpenCode answer whose
  parent is those words, Muse's steered item). A reading inferred from other words (by id order,
  or an answer to later words) is written without it; the tool's own record found later writes
  the reading again, once, confirmed (at a Stop the OpenCode lane looks for it in the stored
  messages for the words it only assumed read, too). It reads those words into the stopped turn while they still wait
  there, so they do not run again. A reading the lane only assumed (OpenCode words it could not
  delete) has no `confirmed`, nor has any line of a lane started before this change: after a Stop
  the Host gives those words back, as before.

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
Host's address and token); the MCP choice; and what the catalog defines those servers as now
(`mcp::definitions`), so a server edited under the same name starts a new lane. Any other turn
runs per turn, as with the switch off, and holds its conversation's lock shared
(`lane::claude::guard_turn`).

**When the lane starts.** As the turn that finds none (issue 833): a stream-json Claude Code takes
its conversation when it starts and has no way to take one up later, so the turn's own process
is started as the lane, with that turn's arguments (`--session-id <session>` for a conversation's
first turn, `--resume <session>` after), within the limits and the reserve, and the turn is its
first claim. A turn for which none may start runs per turn; once its shim has exited, and the
agent runs no turn, has no terminal open and is not moving, a lane starts with that turn's
arguments, resuming what it made (`lane::claude::resuming`). Its start (about 0.6 s) falls in the
time the user reads the answer.

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

**When the lane leaves.** Retired, idle, or Claude Code gone; when the login changed, whatever
wrote the change: the Host's own login watcher (`.credentials.json`) ends the lane once idle,
and the lane itself refuses its next claim (`lane-login-changed`) when the account
`.claude.json` names (or, for a key login, its key) or the login in `.credentials.json` (its
refresh token) is another than at its start. A refreshed access token is no change, though the
Host's watcher, which reads the whole file, may end an idle lane for one; the next turn then
starts the lane, as any turn with no lane there does. A turn the lane refused for its login starts
the lane anew under the new login and runs on it (issue 849). It also leaves after a turn that ended in an
error (a usage limit among them: the pool takes it as today); after a compaction (a Host
`/compact` runs per turn after the lane left, and any `compact_boundary` the lane sees ends it
after its attempt); when Claude Code answers with no turn of the Host's running; and before a
terminal on the agent's tool or a move. For a terminal the layer's `make_way_if_idle` retires
it too; the driver's own `end_claude_lane` also takes the agent's lane-start lock, so a lane that
is starting at that moment is ended as well. A fork does not end it: a Claude fork runs
`--fork-session`, which reads the conversation and writes a session of its own, so it is no
second writer.

**What a kept process does not see.** Claude Code reads some of its setup only when it starts:
an edited `CLAUDE.md`, or skills installed or changed meanwhile, may reach the agent's turns only
once its lane has left (idle, or any of the above), where a process of its own would read them
at once.

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
