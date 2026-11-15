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
| Its library: what it made for its user (documents, tables, reports with every version they had, recordings and files) and what it asked, each under its id, with its times, whether it is pinned or put away, and a request as open or as answered as it was | read from `GET /artifacts?session=` and, for a report, `GET /artifacts/<id>/revisions`. An answer that still waited to be told to the agent is told on the Rust Host, after the resume |
| Its plans and goals: each plan under its id and at its revision, with its steps, what was done, the goal's criteria and evidence, its history, how its work goes on, and the newest version of each of the goal's documents | read from `GET /task-plans?session=` and `GET /task-plan/document`. The library and the plans arrive with the conversation or not at all: an agent does not arrive without what it made and what it is committed to |
| The files it published: its gallery (the newest 500 images), and the files its recordings, films and documents in the library are made of | read from `GET /media?session=` and `GET /media/<id>`, and published here under the ids they have, as the agent's own: the gallery row for row, a library file under the name and the kind its artifact says, so that the agent goes on with the artifact here |
| Every other file its library, its plans or its conversation names: a picture another agent published that it shows, a still of a film | read from `GET /media/<id>` and kept here under the id it has, so that an image in an old reply still shows. It comes along and is not made the agent's own: it is not in its gallery, and no artifact of its is made of it. When the agent that published it moves here too, it is that agent's own, in its gallery as it was, and still one file |

## What does not move yet

- Its portrait and voice (stage 3).
- What it threw away: an artifact that was discarded stays where it was.
- A form that takes answers arrives as it is and is shown; it takes no answers on the Rust
  Host yet.
- The older versions of a goal's documents: the newest of each comes, with its number.
- What a goal waits for when it waits for a background job: jobs do not move, so the wait
  is over on arrival, as it is when a job is gone.
- Messages that wait in its queue, schedules and background jobs. Move an agent when
  nothing waits for it.
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

It reads everything from the other Host before it gives this one anything, and answers with
the number of rows, of things in the library, of plans and of files that arrived. The
conversation arrives in one write or not at all, and an agent whose conversation did not arrive
does not stay on the Rust Host: the command can simply be run again. The files follow the
conversation, and it says three things of them apart:

- how many arrived;
- how many it names that are **not on the other Host either** (a reply that shows a picture
  which that Host never had or no longer has). There was nothing to bring, and nothing is
  unfinished: the picture is as missing here as it was there;
- how many **did not arrive**, with the reason of the first: the other Host did not answer for
  it, or this Host did not take it. That does not undo the move. `clarp-host import --files`
  with the same words gives the files once more to an agent that is here already, and a file
  that arrived before does not arrive twice.

A Python Host lists an agent's newest 500 images and no more. An agent with more brings the
newest 500 as its own; an older one comes along when something names it.

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
4. **Resume it here.** An agent arrives held: what its user says to it runs, and what would
   start by itself (a goal that is due, an answer that waits to be told) waits, because until
   step 1 is done the agent is on both Hosts and both would wake it. When it is archived on the
   Python Host, resume it on the Rust Host: the app's Resume, or `POST /turn-queue/resume`
   with its session. Its goals arrive as they stood, not paused: the hold is on its mailbox.

## Back again

Nothing was taken away from the Python Host, so the way back is to delete the agent on the
Rust Host (`DELETE /agents/<session>`) and unarchive it on the Python Host. What it said on
the Rust Host in between stays in the tool's own session, and the Python Host reads it from
there.

## Evidence

| When | What | Result |
|---|---|---|
| 15 Nov 2026 | `an_agent_moves_from_one_host_to_another_with_its_conversation` (`crates/clarp-host/tests/host.rs`): two Rust Hosts, real binaries | The conversation arrives whole, the next turn continues the tool's session, a second move of the same agent is refused. An image and a document the agent published arrive under their ids, byte for byte, the image in its gallery as it was; the document, which nothing the agent said names, arrives because an artifact in its library is made of it, and the agent revises that artifact on the new Host. A picture another agent published, which the conversation shows, comes along and is fetched as it was; it is not in the agent's gallery and the agent can make no artifact of it; when its publisher moves too it is in the publisher's gallery, and still one file on the disk. A file the conversation names that is gone is said to be on neither Host, not to have failed; asked for again, nothing arrives twice. Its goal arrives as it stood (the plan read on the new Host equals the plan read on the old one, field for field; so do its working notes), is shown as the artifact it had, and the agent's next step on the new Host follows the revision it had. Its library arrives row for row equal: notes still pinned, a report with both its versions and its page for a browser, a request still waiting in the inbox |
| 15 Nov 2026 | Rehearsal against the live Python Host on elitebook (reads only), into a scratch Rust Host: one Claude agent (23 rows) and one Codex agent (179 rows) | Both arrived in under two seconds. Every row equal to the Python Host's in every field but its revision; same order; same `conversation_id`. No turn was run on the scratch Host and it was removed afterwards |
| 15 Nov 2026, 21:22 | Rehearsal of the move as it is now, against the live Python Host (reads only), into a scratch Rust Host that had `false` in place of every tool: a Claude agent that was idle, chosen for having the most goals among those with images (12,771 rows, 18 plans with a goal, 4 images) | It arrived whole. The newest page of the conversation has the same ids in the same order. All 18 plans arrived, each equal to the Python Host's field for field except the four fields of the goal updater, a janitor this Host does not have (`updater.enabled`, `janitor_agent_id`, `last_run_at`, `last_receipt`). The 4 images arrived byte for byte, their rows equal field for field. It arrived held, nothing waited and nothing ran. The scratch Host and its directory were removed. It also showed that the command counted paths of the machine with `/media/` in them as files it could not read; fixed |

| 15 Nov 2026 | The service arrangement, on elitebook: the Rust Host as a systemd user unit (`--launcher systemd`), six turns running, `systemctl --user restart` of the unit | Each turn's shim was in a scope of its own; all six scopes outlived the restart and all six turns finished, each answered once. (The first try found a defect: `systemd-run` rewrote `${...}` in a tool's arguments. Fixed, and `a_turn_in_a_systemd_scope_gets_its_tool_unchanged_and_outlives_the_host` holds it) |

Not yet done: a move that is kept. It needs the owner to choose the agent and archive it on the
Python Host.
