# Moving an agent to the Rust Host

An agent moves from a Python Host to the Rust Host with one command. It takes the agent as the
other Host's apps see it, through that Host's ordinary API, and changes nothing there.

## What moves

| Moves | How |
|---|---|
| The agent: its name, session name, backend, working directory, model and effort | read from `GET /agents/snapshot` |
| Its conversation, every row as the apps show it, in the same order, under the same ids | read from `GET /log`, page by page |
| The tool's own session (Claude's session, Codex's thread) | the next turn continues it: the agent remembers |
| The conversation's name (`conversation_id`) | kept, so it is the same conversation to an app |

## What does not move yet

- Its portrait and voice (stage 3).
- Messages that wait in its queue, its goals, schedules and background jobs. Move an agent
  when nothing waits for it.
- The details behind the tool cards of the old history. The cards arrive in their short
  form, as a list of a chat shows them. What the agent does after the move is kept in full.

## Before

1. **The same machine, the same user.** A tool keeps its session on the disk of the machine it
   ran on (`~/.claude/projects`, `~/.codex/sessions`). The Rust Host must run there, as the same
   user, or the conversation arrives and the tool starts a new session under it.
2. **The Rust Host runs**, with its administrator's token in `CLARP_HOST_TOKEN`:
   `clarp-host --data <dir> --listen 127.0.0.1:7690`.
3. **The agent is idle** on the Python Host. The command refuses an agent that is in the middle
   of a turn.

## The move

```bash
CLARP_SOURCE_TOKEN=<the Python Host's token> CLARP_HOST_TOKEN=<the Rust Host's token> \
  clarp-host import --from 127.0.0.1:7682 --session <session name> --host 127.0.0.1:7690
```

It answers with the number of rows that arrived. The conversation arrives in one write or not
at all, and an agent whose conversation did not arrive does not stay on the Rust Host: the
command can simply be run again.

An agent moves to a Rust Host once. One that went back and comes again is refused, because its
history from the first time is still on the Rust Host's disk; taking that up again is not
built yet.

## After

1. **Archive the agent on the Python Host.** Until then it is on both, and two Hosts that
   resume one tool session write into each other's turns. This is the one step that is not a
   command of the Rust Host: it is done on the Python Host, by whoever owns it.
2. **Point the app at the Rust Host**: `clarp-host pair --url <the address the phone reaches it at>`
   prints the link to open on the phone.
3. Send it a message. Its first turn here resumes the tool's session.

## Back again

Nothing was taken away from the Python Host, so the way back is to delete the agent on the
Rust Host (`DELETE /agents/<session>`) and unarchive it on the Python Host. What it said on
the Rust Host in between stays in the tool's own session, and the Python Host reads it from
there.

## Evidence

| When | What | Result |
|---|---|---|
| 15 Nov 2026 | `an_agent_moves_from_one_host_to_another_with_its_conversation` (`crates/clarp-host/tests/host.rs`): two Rust Hosts, real binaries | The conversation arrives whole, the next turn continues the tool's session, a second move of the same agent is refused |
| 15 Nov 2026 | Rehearsal against the live Python Host on elitebook (reads only), into a scratch Rust Host: one Claude agent (23 rows) and one Codex agent (179 rows) | Both arrived in under two seconds. Every row equal to the Python Host's in every field but its revision; same order; same `conversation_id`. No turn was run on the scratch Host and it was removed afterwards |

| 15 Nov 2026 | The service arrangement, on elitebook: the Rust Host as a systemd user unit (`--launcher systemd`), six turns running, `systemctl --user restart` of the unit | Each turn's shim was in a scope of its own; all six scopes outlived the restart and all six turns finished, each answered once. (The first try found a defect: `systemd-run` rewrote `${...}` in a tool's arguments. Fixed, and `a_turn_in_a_systemd_scope_gets_its_tool_unchanged_and_outlives_the_host` holds it) |

Not yet done: a move that is kept. It needs the owner to choose the agent and archive it on the
Python Host.
