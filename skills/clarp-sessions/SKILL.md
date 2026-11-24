---
name: clarp-sessions
description: Resolve Clarp agent personas, session slugs, backends, and working directories, show or switch an agent's model, and move an agent to another backend with its conversation. Use before targeting another agent, diagnosing session state, or when asked to change your own or another agent's model or backend.
---

# Clarp Sessions

Read the canonical local database through the platform-aware admin command:

```bash
clarp-admin sessions
```

Use the `session` slug for Clarp API requests. Do not guess a session from a
persona when more than one row could match.

## Show or switch a model

An agent can change its own model and effort; with no `--session` the command
targets `$CLAUDE_PWA_SESSION`:

```bash
clarp-admin model                                 # current override + available IDs
clarp-admin model claude-opus-5-5 --effort high   # switch yourself
clarp-admin model sonnet --session lena-1a3f      # switch another agent
clarp-admin model default                         # clear back to the config default
```

Use an exact ID from `available`; do not substitute a model the catalog does
not list. The change applies from the agent's next turn, so the turn that ran
the command keeps its current model. Janitors refuse model changes.

## A Codex agent on a model provider of its own

Only on a Rust Host that lists the feature `codex_own_provider` (contract 170) in
`GET /server-info`; the Python Host and older Rust Hosts ignore the fields below and
run the agent on the machine's own Codex, so check the feature first and do not use
this where it is missing.

Use it when an agent must run wholly on someone's own OpenAI-compatible provider (a
deployment in a region of their choice, say) and never on the default one. Give the
agent a Codex home of its own: a directory holding a `config.toml` that sets
`model_provider = "<name>"` and defines `[model_providers.<name>]` (`base_url`,
`wire_api = "responses"`, and its auth, for example a command that prints a fresh
token). Codex reads a provider only from its home's `config.toml`: one in a
project's `.codex/config.toml` is ignored. Never put the machine's own
`~/.codex/config.toml` to this use, and never copy a customer's endpoint, key or
token into a repository, a test or a message.

```bash
clarp-admin agent create Ada --backend codex --cwd ~/work \
  --codex-home ~/codex-homes/ada --codex-provider their-provider \
  --model their-deployment-name
clarp-admin agent provider --session ada-1a2b --codex-home ~/codex-homes/ada \
  --codex-provider their-provider          # bind an existing Codex agent
clarp-admin agent provider --session ada-1a2b --clear   # back to the machine's Codex
clarp-admin agent provider --session ada-1a2b --cloud-speech on|off|default
```

(The same over HTTP: `codex_home`, `codex_provider`, `cloud_speech` on
`POST /agents`, and `POST /agent-provider`.) Both halves are given together; the
Host refuses half of one, an agent that is not on Codex, or a home that is no
directory (400 `invalid_codex_provider`).

What the Host then does:

- Every turn (exec, the chat lane, a goal) runs with `CODEX_HOME` set to that home
  and `model_provider` set to the provider on the command line or the lane's thread.
- Before a turn starts it reads the home's `config.toml`; if it does not set
  `model_provider` to the agent's provider and define it, the turn does not run.
  If Codex reports any other provider once it runs, the turn is stopped at once.
  Either way the turn fails with the kind `provider_mismatch` and the Host's words
  of what it found, and is not tried again: fix the home's `config.toml` (or the
  agent's binding) and send again.
- Any model name is taken (the provider's deployment names); with none, Codex
  uses the model its `config.toml` names. It waits for none of the machine's Codex
  accounts. Its sessions, for `resume_session_id`, are read from its own home.
- It cannot be moved to another tool (`agent transit` answers 409 `own_provider`),
  and helpers it forks (`clarp-admin agent fork`) run on the same home and provider.
- `cloud_speech` is off for such an agent unless set: nothing it says is spoken by a
  cloud speech provider (its replies stay text, no ring opening or announcement in
  its voice, live calls and live rings for it answer 409 `cloud_speech_off`, and the
  Oracle is told none of its words). This Host has no speech engine on the machine
  itself, so off means not spoken. What is said to it is heard only by the Host's own
  speech engine (refused in plain words where there is none). Any agent can have it set
  off the same way.
- The Host's own model calls leave it out: no janitor reads it (whatever its scope says),
  its tools are explained without a model, and the goal keeper (also of a bound helper),
  the label checker, the spoken-message router and the other agents of a call leave its
  conversation out. Other agents can still read it (see the limits below).
- A turn whose rollout in the agent's home names no provider within 3 seconds is stopped,
  and one whose rollout is still not found (or names none) when its process is gone fails
  closed (`provider_mismatch`, "could not be verified"),
  and its answer is not shown. This check after start only detects: by then the request has
  reached whatever provider Codex used. What prevents it is the home and the
  `model_provider` set on every launch, the chat lane, a fork and the agent's terminal.
- Push notifications, questions and the lock-screen chip of a bound agent carry only a
  neutral line ("<name> answered", "<name> asks", "<name> is working"), never its words,
  tools or commands. A binding that cannot be
  used (half of one, a malformed one, one on another tool) starts nothing at all.

Limits to know:

- Other agents can still read a bound agent's conversation through the Host: the log API
  (`GET /log`, the snapshot), a status fork or a front desk's relay, a message it sends to
  another agent or one another agent's tool reads, and a team leader's ticks. Those agents
  run on their own providers. This is not gated yet: keep customer material away from
  agents that are not bound to the same provider, and out of teams and calls with them.
- Clearing the binding (`--clear`) moves the agent to the machine's own Codex: its next
  turn begins a new thread there, carrying the Host's record of the conversation to the
  default provider.
- The Host installs its `clarp-*` skills into the machine's Codex home only. A bound agent's
  own home has none unless you copy or link them there.
- The Host's environment reaches a bound turn. Do not define the customer's provider with
  `env_key = "OPENAI_API_KEY"` or another variable the machine already sets for its own
  provider; use the provider's own auth command or a variable of its own.

## Move an agent to another backend

On a Rust Host that lists the feature `agent_backend_transit` (contract 158; the
Python Host has no such command), an agent can be moved to another tool and keep
its conversation:

```bash
clarp-admin agent transit --session lena-1a3f --backend codex --model gpt-5.5
clarp-admin agent transit --session lena-1a3f --backend claude --model opus --effort high
```

`--backend` is one of `claude`, `codex`, `agy`, `agy-acp`, `grok`, `opencode` whose row in
`GET /agent-model-options` says `available: true`; `--model` is an exact ID that
row lists (`""` for the tool's default). The command waits until the move is
done or failed and prints its last state. The Host refuses while a turn of the
agent runs or messages wait in its queue, so never move yourself from inside
your own turn: ask another agent or the user's app (the Backend row in an
agent's profile, in the iOS, Android and Rust desktop apps). What is carried is the text
of the user's and the agent's turns with the
tools the agent used and what they gave back (feature
`agent_backend_transit_tools`, contract 161; before it, text only): in the new tool's own form for
Claude and Codex, as quoted `[Tool call: <name>]` blocks for Grok, Antigravity
and OpenCode. A call's input or result past 8 KB is cut with a line saying how
much was. Attached files come as their absolute paths, never their content:
open them with your file tool when you need them. Thinking is not carried. A
conversation longer than about 60% of the new model's context keeps its newest
part as it was and the rest as one summary at its head. You may be asked for that
summary in one last automated turn before you move (a `[Clarp] You are about to
move to ...` message): answer with the summary alone, use no tool. If you cannot,
a cheap model writes it. The
agent's name, voice, goals and chat history stay.

`agy-acp` ("Antigravity (ACP)" in the pickers, on a Rust Host with the feature
`backend_agy_acp`, contract 165) is Antigravity driven over the Agent Client
Protocol (`agy acp`): the same program, sign-in, models and conversations as `agy`
(unless the Host sets `CLARP_AGY_ACP_PROFILE`, which gives it a sign-in and store of
its own), but its message has no 60 KB limit, a picture a user attached reaches the
model as itself, a PDF is given to Antigravity as a link to the file, and Stop
cancels the turn cleanly. Sounds stay a path in the message for now (agy 1.5 takes no
audio over ACP). On a Host with the feature `agy_acp_steering` (contract 168) a
message the user sends while its turn runs (or a front desk relays for them) reaches
that turn, as one to a busy Claude agent does: Antigravity takes it into the turn, or
answers it right after the turn's answer, and a message it does not take runs as the
next turn, as does one sent before the turn has opened its conversation. Another
agent's own message, and one that names an attached picture, sound or PDF or begins
with `/`, wait for the next turn, as on an older Host every message does. A move between `agy` and `agy-acp` carries nothing: they
share one store, so the agent goes on in the same agy conversation (the row says "The
same conversation goes on."). If agy has let that conversation go, the first turn after
the move fails as "Antigravity no longer has this conversation" (or, on `agy`, finds it
gone), and the next turn begins a new one with the Host's record of the old. A Host without the feature leaves an `agy-acp` agent out until
it is updated. To move an agent to another Host instead, use
`clarp-switch-agent-host`.

## An agent stays busy with no process

Repair the slot; do not Stop the agent. Stop pauses its queue and goals until
someone resumes them, while a repair interrupts nothing and changes no pause:

```bash
clarp-admin repair --session pebble-2596 --from "$CLAUDE_PWA_SESSION" \
  --reason "busy since 07:25 with no process"
```

`released_trace` is empty when a process, launch, account recovery or Stop
still owns the slot; then the agent is not stuck. Stop only when the work
itself must end: `clarp-admin stop --session X --from "$CLAUDE_PWA_SESSION"
--reason "..."` records you as the actor.

To resume an existing terminal conversation as a Clarp agent, use the
`clarp-adopt` skill.
