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
