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

## Move an agent to another backend

On a Rust Host that lists the feature `agent_backend_transit` (contract 158; the
Python Host has no such command), an agent can be moved to another tool and keep
its conversation:

```bash
clarp-admin agent transit --session lena-1a3f --backend codex --model gpt-5.5
clarp-admin agent transit --session lena-1a3f --backend claude --model opus --effort high
```

`--backend` is one of `claude`, `codex`, `agy`, `grok`, `opencode` whose row in
`GET /agent-model-options` says `available: true`; `--model` is an exact ID that
row lists (`""` for the tool's default). The command waits until the move is
done or failed and prints its last state. The Host refuses while a turn of the
agent runs or messages wait in its queue, so never move yourself from inside
your own turn: ask another agent or the user's app. What is carried is the text
of the user's and the agent's turns (the newest 400,000 characters); tool calls,
their results and thinking are not. The agent's name, voice, goals and chat
history stay. To move an agent to another Host instead, use
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
