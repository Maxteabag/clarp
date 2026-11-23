# Codex chat turns on a long-lived lane

Issue 742. Owner's decision of 23 Nov 2026: a message sent to a running Codex turn reaches it
(`turn/steer`), Stop uses `turn/interrupt`, a chat turn no longer starts a fresh `codex exec`,
and the apps still show no text as it is written.

## What exists, and what this changes

The goal lane (`codex-goal-lane`, `codex_lane_*.rs`) already does most of the work for one
kind of thread. One shim process supervises one `codex app-server --stdio` for one native
thread. It holds the thread's exclusive lock (`run/codex-threads/<sha>.lock`) for as long as
the app-server lives. It takes the actor's reserved attempt (`Claim`) and creates that
attempt's spool before it hands Codex anything. It writes the ordinary Codex spool format
(`TurnSpool`), which the Codex interpreter already reads. It answers `Follow` and `Stop` on
the attempt's own socket, so a restarted Host rejoins the turn as it rejoins an exec turn.
It already accepts `Request::Prompt` (`turn/start`) for ordinary turns while a goal holds
the thread.

So this is not a new runtime. The changes are:

1. **The lane is per agent, not per goal.** Any Codex agent's chat turn can run on it, and
   a thread with no goal no longer goes back to exec while the setting is on.
2. **A lane can start the thread.** A new conversation calls `thread/start`. The thread id
   reaches the actor through the spool's `Session` record, as exec's `thread.started` does
   today.
3. **Steering.** The lane accepts a steer for the claimed attempt and sends `turn/steer` with
   `expectedTurnId` and `clientUserMessageId`. The read receipt is written to the spool.
4. **Idle life.** A lane with no claim and no active goal retires by itself after a set time.
   The Host also caps how many lanes stay alive.
5. **Settings between turns.** Model, effort and identity are compared on every turn. A change
   Codex cannot apply per turn restarts the lane while it is idle.

The Python Host's lesson stays in force (`server/lib/codex_app_server.py`, header). There,
one app-server per agent left writers behind that blocked `thread/resume` with -32600. Here
the thread lock is held until the app-server child has been reaped, and the lane's own
`.lock` holds until shutdown (`run()` drops `lane` before both locks). A new lane for the
thread cannot start while an old writer lives.

## 1. Data and invariants

**One lane per agent.** Its socket is `run/<agent>-cxg.sock` and its owner lock is
`<agent>-cxg.lock` (`codex_goal_lane::Lane`). A lane serves exactly one thread for its whole
life. A different thread for the agent (a new conversation, a reset, a moved session) means
the old lane retires while idle and a new lane starts.

**One writer per thread.** An exec turn takes the thread lock shared (`guard_exec`), and a lane
takes it exclusive. Both are non-blocking, so a collision fails the attempt before Codex
receives anything. That failure is a `NoShim`-style refusal: the attempt is retried after the
lane is released, never run on two writers.

**A turn is never run twice.** This rests on the same three facts as an exec turn (D16):

- The actor commits the attempt (turn, attempt, spool path, socket path) before the Host
  writes anything to the lane.
- The lane creates the attempt's spool exclusively (`SpoolWriter::create`, `create_new`) before
  it writes `turn/start`. A spool that exists means the attempt may have reached Codex, so the
  Host follows it and never relaunches it (`launch_on_native_lane`: `spool.try_exists()`).
- A second `Prompt` for the same claim is answered from the slot (`slot.claim() == claim`) and
  is not sent again. A different claim while one is held is refused.

**A steered message is read at most once.** The actor commits `Join { into, uuid }` before the
Host sends the steer (as for Claude). `uuid` becomes `clientUserMessageId`. Only a read receipt
in the spool moves it to `JoinRead` and out of the mailbox. A join that was never read is
cleared when the turn ends (`joins.clear()` in the kernel), and the message runs as the next
turn. Section 4 covers the one ambiguous case: a steer that was accepted but the turn was
interrupted.

**What is durable, and where:**

- The agent's log holds the turn and its attempt, the `Join`, and the lane record. The record
  is today's `native-codex-goal` item (`NativeGoal { thread, goal, lane: Ownership }`), widened
  as described below.
- Each attempt's spool holds Codex's events in exec format, plus new `steer.read` lines and
  the terminal `Exited` record.
- The filesystem holds the lane's owner lock and the thread lock. These locks are the truth
  about who may write the thread. The log record is the Host's memory of what it asked for.
- Nothing about the lane lives only in the Host's memory.

**The lane record.** `Ownership` keeps its meaning (`Acquiring`, `Owned`, `Stopping`,
`AccountHold`, `SchedulerHold`, `Exec`) and is written for goals only. A chat lane writes no
record: the setting routes the agent's launches to it, and the lane's owner and thread locks
say who may write the thread. A goal that completes while the setting is on is marked `Exec`
without retiring the lane, which the chat turns keep. A goal-less lane leaves by the idle
policy.

**What a Host restart finds, and how it rejoins.** The lane's process runs in its own systemd
scope (`goal_lane_command` → `shim_command`), so a Host restart does not touch it.

- **A turn was running.** The actor's rejoin emits `Action::Follow` on the attempt socket. The
  lane answers `Follow` for its claim, and the actor reads the spool from where it stopped. A
  steer that was in flight is settled by the spool alone: its receipt is there or it is not.
- **The lane is idle.** The next launch calls `Lane::start`, whose `Inspect` succeeds, so the
  lane is reused. If it serves another thread, it is retired and replaced.
- **The lane died while the Host was down.** If the shim lived, the spool has its terminal
  record (`Slot::lost`: `turn.failed` + `Exited(1)`). If the shim was killed too, the spool has
  no terminal record, the socket is gone and its pid is dead. The existing orphan path ends the
  turn as failed, and the next turn starts a fresh lane.
- **The lane outlived a moved or archived agent.** The worker retires it when it opens the
  agent and finds the agent moved or on another backend (section 2).

## 2. Failure cases

**The app-server dies mid-turn.** The frames thread sends `ProviderGone`, and `serve()` returns
an error. `Slot::lost` writes `turn.failed` ("Codex app-server disconnected") and `Exited(1)`,
then the shim exits. The turn ends Failed through the ordinary interpreter. A pending join is
cleared, so its message runs as the next turn on a new lane. Nothing is retried in place:
Codex may have done half the work, and the user decides.

**The app-server dies between turns.** The lane exits and its socket goes away. The next
launch's `Inspect` fails, `released()` is true (the owner lock is free), and a fresh lane
starts. That costs one cold start (section 2, idle agents). No turn is lost, because no claim
was held.

**The app-server hangs.**

- *A request hangs.* Every lane RPC has `RPC_LIMIT` (12 s). A timeout on `turn/start` fails
  the attempt. The spool exists, so the Host follows the attempt instead of relaunching it.
  Without a terminal record, Stop (below) ends it.
- *A turn goes silent.* The Host's stall flag applies as for any turn. A Stop sends
  `turn/interrupt` and waits for `turn/completed` up to the Stop's `grace_ms`. If that does not
  come, the lane kills the app-server's process group and writes the terminal record
  (`Exited` with SIGKILL). The lane then exits, and the next turn gets a fresh one. This keeps
  D26: the tool's own way first, bounded, then the group.

**A steer arrives as the turn ends.** `turn/steer` carries `expectedTurnId`, and Codex refuses
it if that turn is no longer active ("no active turn", or the id differs). There are three
outcomes, each with one meaning:

- *Refused.* The join stays Pending until the turn ends, then it is cleared. The message runs
  as the next turn, at once, ahead of later work (its place in the mailbox never changed).
- *Accepted and echoed.* Codex records steered input as a `userMessage` item whose `clientId`
  is our uuid. The lane writes `{"type":"steer.read","uuid":…}` to the spool, and the actor
  commits `JoinRead`.
- *Accepted, not echoed, and the turn completed.* Codex completes a turn only after it has
  drained pending input. **This must be verified (build step 0).** If verified, the lane
  writes the receipt at `turn/completed` for every accepted, unechoed steer of that turn. If
  not, it falls back to the next case.
- *Accepted, not echoed, and the turn was interrupted or failed.* Before it writes the terminal
  record, the lane asks Codex for that turn's items (`thread/turns/list`, or `thread/read` on
  this toolchain). It writes a receipt for each `userMessage` with one of the pending
  `clientId`s. With no receipt the message is not read, and it runs next. Codex holding it for
  a later turn would double it, so build step 0 also checks what Codex does with pending input
  on interrupt.

**Stop while starting.**

- *Before the lane has the claim.* `launch_permitted` is false and nothing is sent (today's
  code).
- *While `turn/start`'s reply is outstanding.* The lane processes inputs one at a time. The
  Stop from the attempt socket is queued in `deferred` until the reply binds the native turn id,
  then it interrupts exactly that turn. If `turn/start` failed, the slot is clear, and the
  Stop is acknowledged with nothing to do.
- *A Stop on a goal thread.* It pauses the goal first, as today.
- The attempt's spool records an `Exited` with SIGINT either way, so the turn ends Stopped
  once.

**A login or account switch.** Codex reads `auth.json` when it starts. `Login` already compares
the account id (not the token, so a refresh does not count) at each claim. A change sets
`stale_login`, and the lane refuses the claim *before* it reserves the spool, then exits. New
in this change, the refusal carries a stable code (`lane-login-changed`). On that code the
Host waits for `released()`, starts a fresh lane and submits the same attempt once. This is
safe because no spool exists. The lane holds the pool's login lock shared only while it holds
a claim, so an account switch between turns is never blocked by an idle lane.
`AccountHold` keeps its meaning for goals.

**A model or effort change between turns.** `turn/start` takes `model` and `effort` "for this
turn and subsequent turns". The lane therefore:

- sends both on every `turn/start` when the agent has them set;
- remembers the last pair it applied;
- restarts while idle when a turn asks for the *default* after an explicit value (Codex has
  no "reset to config default"). That turn then pays one cold start.

The identity (`developerInstructions`) and the turn's environment are fixed at
`thread/resume`/`thread/start`, so a persona change restarts the lane the same way. The lane
keeps a digest of its start options, and the Host compares it before each submission (see
decision 3 for the turn token).

**The agent moves to another backend or Host.**

- *Another backend.* Every exec launch first asks a live lane of the agent to retire
  (`Lane::retire` on the turn's launch thread), so the new tool's first turn finds the lane
  gone. A lane that still runs work refuses, and that launch fails as it would today.
- *Another Host.* `actor.moved()` already stops new launches. The lane of a moved agent is not
  sent `Retire`: it leaves by its idle time. The thread lock is per Host (`run/`), and Codex's
  rollout is per machine, so the other Host runs its own lane. An active native goal still
  blocks a move, as now.

**Many idle agents.** A probe on this machine found the cost of one lane. It ran
`codex app-server`, 0.167.1, with an empty `CODEX_HOME`, `initialize` followed by
`thread/start`.

- *Memory.* About 150 MB RSS per idle app-server with a thread loaded, before any MCP server
  starts. The shim adds a few MB. Twenty idle agents would hold about 3 GB.
- *Cold start.* `initialize` took 126 ms and `thread/start` 10 ms. A `thread/resume` of a long
  rollout costs more (`excludeTurns` keeps the reply small, but Codex still reads the file).
  The harness measures it.
- *Warm turn.* A turn on a warm lane costs one socket round trip and `turn/start`.

The idle policy has two parts:

- *The lane retires itself.* After `CODEX_LANE_IDLE` with no claim and no active or uncertain
  goal, it exits (`may_continue()` false). This needs no Host, so lanes never pile up while a
  Host is down.
- *The Host caps live lanes.* Above `CODEX_LANE_MOST` live lanes, the worker sends `Retire` to
  the idle lane whose last turn is oldest. Lanes with a running turn or an active goal never
  count as retirable.

The first turn after a retirement pays the cold start. That is still far below an exec start,
and the harness shows by how much. The numbers are decision 2.

## 3. Boundaries

**The shim (lane process)** owns:

- the app-server child, the thread lock and the login check;
- the claim and its spool;
- `turn/start`, `turn/steer` and `turn/interrupt`, and the goal RPCs;
- read receipts, the bounded stop escalation and the idle self-retirement.

It never waits for the Host and never decides admission. It only refuses what breaks an
invariant (a second claim, a steer for an attempt it does not hold, a stale login).

**The backend** (`clarp-backend/src/codex.rs`) owns:

- the lane's start options (cwd, model, effort, identity, environment), today built in
  `codex_goal_lane::spawn`; they move behind a `Codex::lane_options(&TurnSpec)` so exec and
  the lane share one description of an agent's Codex;
- the input of a turn (the headed prompt the exec path builds);
- the interpreter, which already reads the lane's spool. It gains one line kind
  (`steer.read`) that it hands to the actor as read evidence;
- whether Codex can be steered (`steers() -> bool`) instead of the actor's
  `backend.id() == "claude"`.

**The actor** owns:

- admission: which messages steer, the `Join` before the write, `JoinRead` on a receipt and
  the clearing at turn end. This is `steer_active`, generalized from Claude.
- turn states and rejoin, unchanged;
- the lane record in the log.

For Codex, `Recovery` carries no `InputOwner`. The kernel's `Join.recovery` is optional
already, and Codex's recovery is the spool receipt plus the lane's end-of-turn check.

**The Host worker** (`agent/native_goal.rs`, to be renamed `agent/codex_lane.rs`) owns:

- the lane's lifecycle: start, reuse, retire, the idle cap and the setting;
- routing `Action::Launch` to the lane or to exec;
- delivering `Action::Steer` and `Action::Stop` to the attempt socket.

The edge and the contract do not change.

## 4. Steering, Stop, and what the apps see

**Which messages steer.** The rule is the same as for Claude, and `can_offer_steering` widens
from `backend == "claude"` to the backend's `steers()`:

- a direct user send (`Queue`, a user origin, not coordination);
- a front-desk relay of the user's word (`Later`, `user_word`, coordination).

They do not steer:

- an opening turn;
- a compaction turn (Codex refuses those itself: `NonSteerableTurnKind` is review or compact);
- an empty text;
- a turn whose lane has no native turn id yet (`turn/start`'s reply not bound).

**The mapping.** `steer_active` commits `Join { into: live.turn, uuid }` and emits
`Action::Steer { socket, input }` as today. For Codex, `input` is the steered text with the
team brief, exactly as the prompt is headed. The attempt socket accepts a new
`Request::Steer { uuid, text }` alongside `Follow` and `Stop`. The lane sends:

```
turn/steer { threadId, expectedTurnId: <bound native turn>, clientUserMessageId: uuid,
             input: [{type: "text", text}] }
```

The lane replies `ok` once Codex accepted it, or `failed` with Codex's reason. The Host keeps
no state for it: the receipt in the spool is the only thing that moves the join forward.

**Stop** is `Action::Stop` on the attempt socket, and becomes `turn/interrupt { threadId,
turnId }`. A goal thread pauses first. The escalation is bounded (section 2). A Stop also
holds what waits, as D14 says. A steered message whose join is still Pending stays in the
mailbox, and the hold holds it.

**What the apps see** is what they see for a steered Claude turn:

- The message is queued at once (its delivery receipt says queued).
- When the receipt lands, `JoinRead` takes it out of the queue, the queue changes, and the row
  is written into the conversation at that point. `conversation::split` closes the reply so far
  and opens a new one after the row.
- The turn's end tells the steered message's own `TurnEnd`.
- `busy` and `turn_started_at` stay the parent turn's.

No delta reaches an app. The lane writes `item/agentMessage/delta` to the spool as it does now.
The Host may read the deltas for activity, but the live view stays the stored reply, as for
exec (streaming display off, as decided).

**`supports_steer` for Codex.** It stays out of `FEATURES` until both Hosts run the lane with
the setting on, as the Claude note in `models.rs` explains. Advertising it is a contract
change for the release owner, not this change.

## 5. Rollout and rollback

The lane sits behind a Host setting: `codex_chat_lane = true` or `false`, a top-level key of
the Rust Host's `config.toml`, read at each launch. It defaults to false.

- **False.** Codex chat turns run on exec, as today. A goal still gets its lane. A lane that is
  still alive is retired before the agent's next exec turn starts (`Lane::retire` on the turn's
  launch thread).
- **True.** Every Codex agent's chat turns run on its lane. A call's opening turn keeps exec.
- **Rollback.** Set it to false. Lanes leave as their agents' next turns start, or when idle,
  and the next turn runs exec on the same thread: the lane and exec share Codex's thread and
  rollout, so the conversation continues. No data migrates either way. A running lane turn is
  never cut by the switch.

## 6. What it should move

The harness of issue 742 (branch `rebuild/turn-timings`, not yet pushed when this was written)
measures each backend on a disposable Host. This change should move these Codex numbers:

- **Launch overhead.** The time from the actor's launch to the spool's `Spawned`, and to
  `turn/started`. Today it is 1.6 to 11.5 s per exec. The target is under 100 ms on a warm lane.
  A cold lane is measured as its own row.
- **Time to first activity.** The first item in the spool. This falls by the launch overhead.
- **Steer-to-effect latency.** The time from the send's acceptance to the spool's `steer.read`,
  and to the first item after it. Today there is none: the message waits for the whole turn.
- **Stop latency.** The time from Stop to the terminal `Exited`. Today this is an exec process
  signal. The target is at most the same.
- **End-to-end turn time.** This falls by the launch overhead.
- **Tool-call round trip.** It should be unchanged. It is measured to show that the lane costs
  nothing there.
- **Memory.** RSS of idle lanes per Host, with the cap reached.

## Build plan (after approval)

0. **Verification, no account needed.** Point a lane's Codex at a local stub Responses
   endpoint (`model_providers` with a `base_url` on 127.0.0.1, with an empty `CODEX_HOME`).
   Record three things: whether steered input is echoed as a `userMessage` with `clientId`;
   whether a turn completes before it drains steered input; and what happens to pending input
   on interrupt. The steer refusal texts are recorded at the same time. The answers fix the
   receipt rules in section 2 before code depends on them.
1. **Shim.**
   - A thread-less lane with `thread/start`.
   - `Request::Steer` on the attempt socket, with `steer.read` receipts and the end-of-turn
     check.
   - The bounded stop escalation and the idle self-retirement.
   - The stable refusal codes, and the model, effort and options digest.
   - Tests on the existing fake (`tests/codex_goal_lane.rs`, `tests/qa/fake_codex.py` already
     answers `turn/steer`).
2. **Backend.** `lane_options` and `steers()`, and `steer.read` in the interpreter.
3. **Actor.** `steer_active` for Codex, with `Recovery` optional, and the routing rule.
4. **Host worker.** The setting, lane routing for chat turns, the retry on `lane-login-changed`,
   retirement on a backend or Host move, the idle cap and steer delivery.
5. **Skill.** The `skills/` text saying a message to a busy Codex agent now reaches its running
   turn on the Rust Host with the setting on. It ships in the same change (AGENTS.md rule).

Rust builds go through `server-rs/tools/build`, one batch per crate (D13). Tests run once at
the end.

## Step 0: what Codex 0.167.1 does (23 Nov 2026)

Run against a local stand-in Responses endpoint, with an empty `CODEX_HOME` and no
account:

- **The read.** `turn/steer` during a model request: Codex answers `{turnId}` and at once
  says a `userMessage` item whose `clientId` is the `clientUserMessageId` sent. It abandons the
  request in flight and asks again with the steered words in its input. The echo is the
  reading, as designed.
- **Interrupted before the read.** `turn/steer` accepted, `turn/interrupt` right after: the turn
  ends `interrupted`, no `userMessage` with that id is said, and the next turn's request does
  not carry the words. They are dropped, never run twice. `thread/turns/list` returns the turn's
  items with their `clientId`s, and the lane asks it before a turn's end when an accepted id
  has no reading. A completed turn did not leave accepted words unread in any run.
- **Refusals.** Error -32600 has two texts: `no active turn to steer` after the turn ended, and
  ``expected active turn id `A` but found `B` `` for a stale id.
- **The first turn of a thread.** `turn/started` came 0.5 to 1 s after `turn/start`'s reply.
  Later turns started at once.

## Build notes

- Two defects of the goal lane's spool were fixed with it. A `userMessage` item was stored as a
  tool call named `userMessage`. And each `item/agentMessage/delta` rewrote the whole reply so
  far into the spool, so the spool grew with the square of the reply.
- Settings are top-level keys of the Rust Host's own `config.toml`, read at each launch, as
  `conversation_forks` and `experience_watch` are read. The fleet's settings need a dot in their
  key and a route to write them, and routes are frozen.
- An opening turn (a call's) keeps its own gated exec launch.

## After review (23 Nov 2026)

- Steering is offered to a Codex agent only while `codex_chat_lane` is on, so with it off a goal
  turn is not steered, as before.
- An attempt that never reached Codex (no spool) goes to a new lane once, whatever the lane
  said.
- A reply that arrives while another request is waited on is kept for that request.
- The Stop deadline is looked at before queued input is taken.
- The lead's decisions after review:
  - **M1:** the turn file stays as built. It is rewritten at each claim and not cleared when
    a turn ends, so a background job of the turn keeps its identity.
  - **The goal-lane spool fixes stay ungated:** a `userMessage` is no longer a tool call, and
    no line is written per delta. They are bug fixes.
  - **M4, M8 and M9:** kept by the lead.
  - **M5 and M7:** fixed in 52b1d8881 and 61b929a2e.

## Measured on a real lane (turn-timings preview, 23 Nov 2026)

Codex 0.167.1 with the owner's Codex configuration, on the 30e9df83c build:
- **Idle memory.** RSS is 214 to 273 MiB per idle lane (PSS 83 to 142), and the shim is 4 MiB.
  So `codex_lane_most = 8` allows up to about 2 GiB RSS of idle lanes. The MCP server (exa) is
  remote: it has no process of its own.
- **Launch.** Send to `turn.started` takes 0.01 s on a warm lane, 0.53 s on a cold one, and
  0.95 s with exec.
- **Steering.** `steer.read` comes 0.01 s after the send.

The lead keeps 8 lanes and 15 minutes for now. The preview found that a Stop left the turn's
command running: Codex ends the turn on `turn/interrupt` and keeps the command as the
thread's background terminal. A probe of 0.167.1 showed that `thread/backgroundTerminals/clean`
ends the command within a second.

**The lead's rule (23 Nov 2026), the same as for Antigravity:** what the tool keeps for a turn
ends with the turn. So the lane sends that clean at every turn's end, whether the turn
completed, failed, was stopped, or was a turn the lane refused, as with exec. A process the
agent detached itself (`setsid`, `nohup`) is not a background terminal and is not touched.
A lane that leaves in order (retired, idle, or a changed login) first sends the same clean and
waits at most 2 s. When the lane ends Codex (as it leaves, or because Codex did not answer a Stop
in time), it reads every process below Codex with its start time. It then ends each one that is
still that process by pid, then each group one of them leads, and Codex's group last: Codex runs a
command in a group of its own, and a terminal command in a session of its own. This is the lead's
ruling on review finding F3: no cgroups. The Python Host leaves these commands
running (`docs/differences.md`).

## Decisions (the lead, 23 Nov 2026)

1. `codex_chat_lane` lands off. It is turned on for both Rust Hosts after the harness's before
   and after runs on a disposable Host.
2. `codex_lane_idle_minutes = 15`, `codex_lane_most = 8`. Both are settings. Idle memory is to be
   measured on a real lane with the owner's Codex configuration: it starts an MCP server that a
   lane keeps alive between turns.
3. The turn token is (a): `CLARP_PROVIDER_TURN_FILE`, per agent in its run folder, mode 0600,
   written whole by rename at each claim. It is set only in a lane's environment and read only
   where `CLARP_PROVIDER_TURN` is absent.
