# Moving an agent to the Rust Host

An agent moves from a Python Host to the Rust Host with one command. It takes the agent as the
other Host's apps see it, through that Host's ordinary API, and changes nothing there.

## What moves

| Moves | How |
|---|---|
| The agent: its name, session name, backend, working directory, model and effort | read from `GET /agents/snapshot`. An agent's row shows the model that last answered when the agent is set to none, and does not say which of the two it shows: here the agent is set to the model its row shows, so it goes on with the model it ran |
| Who it is: its voices, its symbol, the picture it has of its own, and how it speaks | its voices and its symbol from its row; the picture from `GET /avatars/<agent>`; how it speaks from the persona of its name in `personas` of the snapshot, because an agent's row does not say. An agent whose own way of speaking is no longer its persona's (the persona was changed or removed after the agent was made, or the agent was given another name) arrives speaking as the persona of its name speaks now, or with its name alone |
| The portraits it keeps, the one it wears among them | read from `GET /agent-portraits?session=` and each portrait's picture from the address the list gives; kept here under the ids they have, each with the file of the gallery it was chosen from named as it was. A portrait whose picture cannot be read stays behind; the agent arrives |
| Whether it has the heartbeat on | from its row (`heartbeat_enabled`). What its heartbeat remembered on the other Host does not come (how often it found nothing to do, whether it had gone dormant): here it begins at the owner's interval, and while the agent is held after its arrival it is given none |
| Its conversation, every row as the apps show it, in the same order, under the same ids | read from `GET /log`, page by page |
| The tool's own session (Claude's session, Codex's thread) | the next turn continues it: the agent remembers |
| The conversation's name (`conversation_id`) | kept, so it is the same conversation to an app |
| Its library: what it made for its user (documents, tables, reports with every version they had, recordings and files) and what it asked, each under its id, with its times, whether it is pinned or put away, and a request as open or as answered as it was | read from `GET /artifacts?session=` and, for a report, `GET /artifacts/<id>/revisions`. An answer that still waited to be told to the agent is told on the Rust Host, after the resume |
| Its plans and goals: each plan under its id and at its revision, with its steps, what was done, the goal's criteria and evidence, its history, how its work goes on, and the newest version of each of the goal's documents | read from `GET /task-plans?session=` and `GET /task-plan/document`. The library and the plans arrive with the conversation or not at all: an agent does not arrive without what it made and what it is committed to |
| The files it published: its gallery (the newest 500 images), and the files its recordings, films and documents in the library are made of | read from `GET /media?session=` and `GET /media/<id>`, and published here under the ids they have, as the agent's own: the gallery row for row, a library file under the name and the kind its artifact says, so that the agent goes on with the artifact here |
| Every other file its library, its plans or its conversation names: a picture another agent published that it shows, a still of a film | read from `GET /media/<id>`, on the Host the agent is on or on a second Host that is named (`--files-from`), and kept here under the id it has, so that an image in an old reply still shows. It comes along and is not made the agent's own: it is not in its gallery, and no artifact of its is made of it. When the agent that published it moves here too, it is that agent's own, in its gallery as it was, and still one file |

## What does not move yet

- How much it says while it works and how human it sounds: neither is in what a Host shows of an agent.
- What it threw away: an artifact that was discarded stays where it was.
- What users answered to a form before the move: it is in the conversation, where the agent
  was told it. The form arrives as it is and takes answers anew, unless its schema has a
  word this Host does not read (`docs/differences.md`): then it is shown, and answers to it
  are refused with that word. Answers that were sent and not yet told to the agent when it
  moved do not come: move an agent when nothing waits for it.
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

**Files that stayed on another Host.** A conversation that once came from another Host names
files that stayed there: on elitebook, 72 files that conversations name were never on that
Host and are all on laptopstudio (counted on both machines by the release owner on 15 Nov
2026, `Maxteabag/clarp` issue 362). The move can ask a second Host for what the first does
not have:

```bash
CLARP_SOURCE_TOKEN=<the first Host's token> CLARP_FILES_TOKEN=<the second Host's token> \
CLARP_HOST_TOKEN=<the Rust Host's token> \
  clarp-host import --from 127.0.0.1:7682 --session <session name> --host 127.0.0.1:7690 \
    --files-from <the second Host's address>:7682
```

It says how many files came from there, and how many are on neither. With `--files` the same
words bring them to an agent that has moved already. The second Host is only read, like the
first.

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
| 15 Nov 2026, 21:37 and 21:55 | The same agent again, with its library (22 things: its 18 plans, 3 films, 1 document). The first of the two runs found two faults, the second is the move as it is now | **Found at 21:37:** the 3 films and the document did not come. Their artifacts arrived, pointing at files that were not there: the move took the gallery and what the conversation names, and nothing the agent said names them. And it said "155 more could not be read there or were not taken here", which reads as a move that is not finished. Counted on the Python Host: of the 156 files that conversation names, 155 have no row in its media table at all (the table begins on 28 Oct, the rows that name them are from October), and the one it has is another agent's film. **At 21:55, fixed:** 9 files arrived, the 4 images of its gallery, the 4 files its library is made of (206 MiB, byte for byte, and each artifact was taken again as it is by the Rust Host, which it does only for a file that is the agent's own under the same name and kind), and the other agent's film. The 155 are said apart: "155 files that it names are not on the Host at 127.0.0.1:7682 either: there was nothing to bring." Of the 163 ids its library, plans and conversation name, 8 are fetched on both Hosts and 155 on neither. The gallery is 4 rows there and 4 here, equal field for field. The library is 22 rows there and 22 here: the 4 that are not plans equal field for field, the 18 plans differing only in the goal updater's four fields and the time of reading. Held, nothing waiting, nothing ran; the move and every comparison took 36 seconds; the scratch Host and its directory were removed |
| 16 Nov 2026 | `an_agent_that_moves_stays_who_it_was` (`crates/clarp-host/tests/host.rs`): two Rust Hosts, real binaries, a stand-in for Claude Code that writes down what it is started with | An agent made as a persona of the owner's, with a picture and a symbol, has on the second Host the same picture under the same address, the same symbol and voices, and its tool is told the same name and way of speaking, though the second Host has no persona of that name |

| 15 Nov 2026 | The service arrangement, on elitebook: the Rust Host as a systemd user unit (`--launcher systemd`), six turns running, `systemctl --user restart` of the unit | Each turn's shim was in a scope of its own; all six scopes outlived the restart and all six turns finished, each answered once. (The first try found a defect: `systemd-run` rewrote `${...}` in a tool's arguments. Fixed, and `a_turn_in_a_systemd_scope_gets_its_tool_unchanged_and_outlives_the_host` holds it) |

Not yet done: a move that is kept. It needs the owner to choose the agent and archive it on the
Python Host.
