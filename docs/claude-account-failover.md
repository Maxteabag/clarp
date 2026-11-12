# Claude account recovery

When a Claude turn reaches a classified usage limit, the runtime can pause its
Claude turns, activate another saved account, and continue unfinished requests
on their existing native conversation IDs. Configure this separately on each
Host. It is disabled until an account selector is configured.

```toml
[agents]
claude_account_switch_command = ["python3", "/absolute/path/to/account-selector.py"]
```

The selector is a trusted local executable, run without a shell. It receives
`{"models": ["sonnet", "opus"]}` on stdin (an empty model string means the CLI
default). It must activate an account and verify that every requested model can
make a request before returning `{"available": true}` on stdout with exit code
zero. Any other result leaves the turns waiting. Its stdout and stderr are not
logged. It has a five-minute deadline; on POSIX the deadline kills its entire
process group. Keep account credentials in the provider's own credential store.

A selector may activate an account that serves only some of the requested
models and say which: `{"available": true, "models": ["sonnet"]}`. The runtime
then resumes only the turns whose model is in that list. A model that is not in
it is not served by the chosen account now: its quota for that model is spent
(Claude's weekly limits are per model), its plan lacks it, or its probe failed.
That says nothing about the other saved accounts, so such a turn is neither
resumed nor ended. It stays parked in its own park (see below). A `models`
value that is not a list of strings is no verdict; one that names none of the
requested models keeps every turn parked for the next check. Without `models`
the answer covers every requested model, as before.

The local `claude-usage` skill supplies `scripts/auto_switch.py` for installations
using `claude-switch-account` saved profiles. That helper checks saved account
usage, attempts native OAuth refresh when needed, verifies each requested model,
and restores the original profile when no candidate works. Its `--dry-run`
validates stdin without switching accounts or making provider requests.

## Runtime behavior

- A persisted runtime row identifies a conversation; it does not establish
  unfinished work. Runtime restart generates **no continuity prompts**. It
  restores conversation bindings and marks interrupted work visibly. The agent
  performing a planned restart owns the inventory, original work and direct
  targeted continuation; see [runtime restart handoff](runtime-restarts.md).
  Periodic heartbeats and their Janitor must not independently resume a
  restart-interrupted turn. User stops, valid queued work and explicitly
  enrolled goals retain their own authority and recovery paths.
- A queued goal wake is fenced by its exact request, plan revision, owner and
  native conversation. Permanently superseded packets are retired once with
  payload-free cancellation receipts; temporary capacity/approval/pause waits
  stay queued. The goal scheduler checks durable pending rows before deciding
  an owner is idle, including before restart rehydration.
- Claude's rejected/blocked structured limit event and classified terminal usage
  errors trigger recovery. Allowed/warning events and temporary 429s do not.
- A plan failure ("this account does not offer this model") is a fact about
  one turn's model, not about the account's quota. It starts a recovery only
  when no other turn of the pool is running. Otherwise that one turn ends at
  once with the provider's sentence and nothing else is paused or stopped: the
  selector switches the machine's account while it probes, which must never
  happen under turns that run fine. When a recovery does start for it, the
  turn stays paused until the selector has answered three times without its
  model (`PLAN_CHECKS`, about two minutes: "no account", or a `models` list
  that lacks it) and then ends with the provider's sentence from its own
  failure; a plan does not reset the way a usage window does. No other turn
  ends early.
- Turns a `models` answer leaves out stay parked while the turns it covers
  work. Their state reads "Waiting for a claude account that serves <model>;
  the active one does not right now". For them nothing is done to the working
  turns: none is stopped, and the selector, which switches the account, is not
  run. A new turn for such a model waits with them instead of running into the
  same limit; a new turn for a served model runs. Once a minute the runtime
  looks again: a parked turn whose own park is 30 minutes old is released to
  run and fail visibly with the provider's message, as the cap always did; and
  when no turn of the pool is running, a recovery begins for the parked ones
  and the selector is asked. A working turn that meets its own limit starts a
  recovery as usual, and that one asks for the parked turns' models too.
- A relapse is an account failure of a turn of the pool less than ten minutes
  (`QUIET_SECONDS`) after the pool's last recovery resumed its turns: the
  selector said an account was available and it was not, or not for long.
  - A plan failure of a turn that recovery resumed ends the turn at once with
    the provider's sentence. Asking again stopped and resumed every turn of
    the pool, round after round (2026-11-11: 146 rounds in three and a half
    hours, started by two helpers whose model id no account offered).
  - A usage limit or a failed sign-in starts the next recovery, because
    waiting can help: a selector can pick an account with a sliver left (the
    same day, 14:28 to 15:01: resumed twice onto such accounts, parked again
    each time, working once the windows reset). But the pool counts the
    recoveries a relapse followed. The selector's next run waits 60, 120, 240
    and then 480 seconds after the relapse. At the fifth relapse in a row
    (`MAX_RELAPSES`) a turn that a recovery resumed in the last ten minutes
    ends visibly with the provider's message, whatever the gap between its
    resume and its relapse was; the turns that run on are not stopped for it.
    A turn that was never resumed still gets its recovery, eight minutes
    later, and ends at its own relapse. The count starts again once the pool
    has run ten minutes after a recovery without an account failure.
  - What this does not bound: a turn that relapses more than ten minutes
    after each resume is recovered every time, as before; and five accounts
    in a row that each have only a sliver left end the relapsing turns
    instead of leaving them to wait for the reset.
- The wait is bounded per park, as it always was: 30 minutes
  (`MAX_PARK_SECONDS`) from the park's start, and a relapse begins a new
  park. Only a turn that stayed parked while the pool worked (above) keeps
  its clock through the recoveries that follow.
- One recovery message for each run of relapses. It is built from the
  original request, never one wrapped inside another. A turn resumed again
  after a relapse already has it in its conversation, so that resume carries
  one line (`RELAPSE_RESUME_TEXT`: carry on, check interrupted operations,
  nothing new is asked) in the same envelope, without the message and without
  the request. A resume needs a prompt, so it cannot carry nothing. If the
  resumed process was stopped before it started its session, the message
  never arrived and is sent again. A limit met after ten quiet minutes is a
  new recovery and gets the full message.
- Turns Clarp does not dispatch are invisible to the coordinator. A Codex
  goal continuation on the shared app-server is not registered with it, so
  "no other turn of the pool is running" can be true while one runs, and a
  recovery (a plan recovery included) can switch accounts under it. This was
  so before these rules and is not changed by them.
- One recovery coordinates all Claude turns owned by the runtime. New turns wait
  behind the recovery barrier; queued follow-ups retain their order. Other
  providers, completed agents, and idle agents receive no continuation.
- The runtime fences the old callbacks, stops the exact owned processes, and
  waits for their transcript drainers before checking accounts. A process that
  will not stop is killed as a group. Remaining owned group members are killed
  and checked with `ps`, including tools that redirected their output. A live
  group member or transcript still draining prevents recovery from advancing.
- A verified account resumes each unfinished request with its original model,
  effort, voice setting, trace, and native conversation ID. The continuation
  instructs the agent to inspect interrupted operations and avoid repeating
  completed work. If the native transcript was never created, Clarp delivers
  the original request as a fresh session using its already assigned ID.
- Recovery does not re-admit the user message. Stop, deletion, or a replacement
  that releases the original turn's ownership prevents automatic continuation.
- New requests parked before their first spawn retain their normal native user
  boundary, even when they resume an agent's older conversation.
- Parked live turns retain their place and report `limited` with the account
  wait reason, rather than `thinking`. HTTP-to-runtime status delivery carries
  the durable state-log sequence so a delayed busy update cannot reopen a
  newer stopped turn.
- If no account is verified, the runtime keeps the work paused and checks again
  after 60 seconds. Rapid repeated exhaustion also enforces this cooldown. A
  newly arriving model must be verified before the group resumes.
- This covers runtime-dispatched turns. Interactive terminal attachments and
  isolated dreaming subprocesses retain their separate lifecycles.

`claudeAccountRecovery`, `claudeAccountWaiting`, `claudeAccountRecoveryFail`,
`claudeAccountRecovered` (with how many stayed parked for which models) and
`claudeAccountPlanEnded` (turns parked for their plan, ended after three
answers without their model) log events explain recovery progress without
credentials. The same coordinator and the same rules serve the Codex pool
(`codex_account_switch_command`); its events carry the same names.
The runtime status includes `claude_account_recovery` with `recovering` and waiting
agent IDs. Waiting agents keep a THINKING state with `account_recovery=waiting`.
The separate runtime service preserves this coordination across HTTP restarts.
Existing runtime startup recovery remains responsible for a runtime/Host crash.

## Verification and rollout

Run focused tests with:

```bash
uv run --group dev pytest -n 2 tests/unit/test_account_failover.py \
  tests/unit/test_turn_dispatch.py tests/unit/test_clarp_runner.py \
  tests/unit/test_config.py
```

Use fake account selectors and fake providers for exhaustion tests. Do not burn
through a real account's quota or interrupt real work to test the feature.
Deploy through the normal versioned installer. The runtime adopts a new release
at an idle boundary, so check the runtime's release ID as well as HTTP health
before claiming recovery is active. Configure the selector's absolute path on
the Host and verify its executable dependencies are available to the runtime.
