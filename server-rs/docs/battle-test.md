# The battle test: the Rust Host on a real machine

The owner's word, 15 Nov 2026: "You can install the host to battletest." This is how that is done, what
runs on the installed Host, and where the evidence is. It is the third proof of `REBUILD.md` section 2:
a real Host, real agents, a week, no lost or doubled message.

## What is installed, and what is not touched

`tools/install-host` puts the Rust Host beside the Python Host on the same machine, as the same user:

| What | Where |
|---|---|
| The Host and its shim | `~/.local/share/clarp-host/bin` |
| Its data: the fleet and one directory for each agent | `~/.local/share/clarp-host/data` |
| What an agent's tools cache and log, apart from the Python Host's | `~/.local/share/clarp-host/cache`, `logs` |
| The agents' own tools, first on their path | `~/.local/share/clarp-host/tools` |
| Its token and what its tools read to find it | `~/.config/clarp-host/env`, `config.toml` (both 0600) |
| Its service | `~/.config/systemd/user/clarp-host.service` |
| Where it listens | `127.0.0.1:7690`, the loopback only |

Nothing of the Python Host is written: not its units, its database, its configuration or its tools. The
installer reads from the Python Host's unit what gives a turn the same world it has there: the path its
tools are found on, and with it the pinned `claude` and `codex` programs; how many files a process may hold
open; how glibc keeps its memory (`MALLOC_ARENA_MAX`). And like that unit, this one says `UMask=0077`: what
the Host writes and what a turn makes is its user's alone.

On that path, every tool of the Python Host has this Host's tool of the same name in front of it. Other
programs whose names begin with `clarp` may be on it too: on elitebook, ten helpers of the owner's own
(`clarp-prompt`, `clarp-contacts`, `clarpd`, `clarp-master` and others, linked into `~/.local/bin` from his
dotfiles). They are not a Host's tools and are left as they are; `clarp-prompt` calls `clarp-admin` by name,
which is this Host's.

A turn is given the address of its Host and a token for its agent's tools, which opens the tools and nothing
else. The administrator's token (`~/.config/clarp-host/env`) is not in a turn's environment.

Both Hosts run their turns with the same logins of Claude Code and Codex. The Python Host puts another
account in place when one is out; the Rust Host is installed to follow it (`--accounts follow`, chosen by
the installer when the Python Host has a selector): a turn there that meets a usage limit waits, is taken
up when the login has changed, and ends after half an hour if none comes. It never changes a login itself.

The Host is on the loopback. A phone reaches it only once something is put in front of it (a Tailscale
serve entry) and the phone is paired (`clarp-host pair`): neither is done by the installer. While no phone is
paired the Host is installed with `--push-relay off`, so that it speaks to nobody outside the machine. The
installer keeps what an installation was told (`~/.config/clarp-host/install.conf`), so an update leaves the
relay off; `--push-relay default` turns it on when a phone is paired.

## The installation on elitebook

Installed on 15 Nov 2026 at 17:14 CET from the release build of `d9868bea`, with
`tools/install-host --push-relay off --start`. One agent lives there: `Soak` (session `soak`), Claude Code on
the model `haiku`, in `~/clarp-host-soak`, muted. The soak has run since 17:15 as a background job of the
lead's own agent on the Python Host (`rust-host-soak-1`), so that the owner sees it and can stop it. That job
does not come back by itself after the machine is started again: the lead looks after it, and a run that is
started again goes on counting where the evidence ends.

## Installing, updating, removing

```sh
server-rs/tools/build release            # on the build machine; brings dist/ home, with its checksums and commit
server-rs/tools/install-host --start     # writes the files, starts the service, waits until it answers
```

An update is the same two commands. The service is restarted; a turn that runs goes on under its shim, and
the new Host reads on where the old one stopped. `systemctl --user status clarp-host.service` and
`journalctl --user -u clarp-host.service` say how it is.

To take it away: `systemctl --user disable --now clarp-host.service`, then remove the unit file and the two
directories. Agents that were moved there are still on the Python Host, put away, until someone removes them.

## What runs on it

`tools/soak run` writes to one agent of the Host around the clock and audits every message:

- it is in the conversation exactly once (not lost, not doubled);
- exactly one turn ended for it;
- a completed turn answered with the word that was asked for (`ack <n>`), so an answer cannot be mistaken
  for an answer to another message.

A turn that ends without completing is counted under the Host's own word for it (`turn_failed`,
`turn_interrupted`), with the sentence the Host put under the message for why: it is not a lost message. A
turn that has not ended when its round stops waiting, which a turn that waits for an account may not have
for half an hour, is written down as that (`waiting_for_account`, or `no_end_in_time` with what the Host
says the agent is doing), and looked at again after every later round until it has ended; how it ended is
a line of its own (`late_end`). Every twelfth round the whole conversation is read again,
page by page, for a message that came twice later. Each round is one line in
`~/.local/share/clarp-host/soak/evidence.jsonl`, with the build the Host ran and since when its service
was up: an update or a restart between or during turns is on record.

```sh
~/.local/share/clarp-host/libexec/soak report   # rounds, outcomes, lost, doubled, restarts, builds
```

The installer puts the program there (`tools/soak` in this repository), so that a run of a week does not
depend on a checkout.

The soak agent runs on a small model, in a directory of its own, and is asked for two words a quarter of an
hour apart: the load on the owner's accounts is small on purpose. Heavier work comes with agents the owner
chooses to move.

## A restart with turns running

`tools/soak restart` is the second rule on the installed Host: a running turn survives any restart of the
server. It gives each of its agents (`restart-1` to `restart-N`, made once, muted, on the small model) one
command that takes a while and leaves a mark in a file, and a word to answer with afterwards. When every one
of them is in its command, the Host's service is restarted. Afterwards every message must be in its
conversation once, with one turn end, completed and answered with its word, and every command must have run
exactly once: a turn that was run a second time would leave a second mark.

```sh
~/.local/share/clarp-host/libexec/soak restart --agents 10                    # the service is restarted
~/.local/share/clarp-host/libexec/soak restart --agents 3 \
    --by "server-rs/tools/install-host --start"                               # an update, with turns running
```

The result is in the same evidence file, one line for each agent and one for the exercise, and `soak report`
names every exercise.

Three more things can be said to it. `--by COMMAND` restarts the Host by that command instead: the
installer, for an update with turns running, or `systemctl --user kill -s KILL clarp-host.service`, for a
Host that is killed and started again by systemd two seconds later. `--moment accepted` restarts right after
the messages were accepted, when the turns are in their first steps. `--moment ending` restarts `--settle`
seconds after the first command ended; the messages go out `--stagger` apart, so one restart meets turns
that have just ended, turns that are answering, and turns still in their commands.

On 15 Nov 2026, with real Claude Code turns, on the build of `d9868bea` and from 17:55 on that of `2e935ef7`:

| When | How | Turns | What it met | Result |
|---|---|---|---|---|
| 17:31 | the service restarted | 2 | every turn in its command (`sleep 30`) | held |
| 17:33 | the service restarted | 10 | every turn in its command (`sleep 45`) | held: the Host answered again after 102 ms with all ten |
| 17:35 | the Host killed (`SIGKILL`) | 5 | every turn in its command | held: back after 2.1 s with all five |
| 17:36 | the Host killed | 5 | the messages just accepted, the turns starting | held |
| 17:37 | the Host killed | 8 | one turn just ended, two whose command ended while the Host was down (one had answered too before it was back), five in their commands | held |
| 17:55 | the Host updated by the installer, `d9868bea` to `2e935ef7` | 3 | every turn in its command (`sleep 40`) | held: the new build had all three |
| 17:57 | the service restarted, on the new build | 2 | every turn in its command | held |
| 20:02 | the Host updated by the installer, `2e935ef7` to `61194d0c` | 3 | every turn in its command (`sleep 40`) | held: the new build answered 1 ms after the restart was done, with all three |
| 20:40 | the Host updated by the installer, `61194d0c` to `87818123` | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 8 ms, with all three |
| 21:03 | the Host updated by the installer, `87818123` to `fedf357e` | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 16 ms, with all three |
| 21:23 | the Host updated by the installer, `fedf357e` to `d0788034` | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 35 ms, with all three |
| 21:44 | the Host updated by the installer, `d0788034` to `6b06bb21` | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 22:15 | the Host updated by the installer, `6b06bb21` to `bcb9a916` | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 23:59 | the Host updated by the installer, `bcb9a916` to `d3b9193c` | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 2 ms, with all three |
| 00:32 (16 Nov) | the Host updated by the installer, `d3b9193c` to `4eebf671` | 3 | every turn in its command (`sleep 40`) | held: the new build had all three |
| 06:28 (16 Nov) | the Host updated by the installer, `4eebf671` to `14bb3f1c` | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 07:08 (16 Nov) | the Host updated by the installer, `14bb3f1c` to `3175ffcc`: the build that shows a message of the agent as one row with its tools' cards, taken over in the middle of three turns the build before had begun | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 66 ms, with all three. Each of the three turns shows its command twice: once as the build before wrote it (a row by itself, never closed: `recorded`), once as the new build wrote it when it took the turn over (on the row of its message, `ok`). A turn that begins on the new build shows it once |
| 08:41 (16 Nov) | the Host updated by the installer, `3175ffcc` to `652c6d6a`: the build that hears (speech in) | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 10:53 (16 Nov) | the Host updated by the installer, `652c6d6a` to `5d74ac9d`: the build that speaks (speech out) and hears each agent by its own engines | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 2 ms, with all three |
| 11:35 (16 Nov) | the Host updated by the installer, `5d74ac9d` to `9781947b`: the build with the catalogue of voices | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 3 ms, with all three |
| 13:45 (16 Nov) | the Host updated by the installer, `9781947b` to `24cddc80`: the build that shows a turn as it runs and gives an agent a face | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 14:16 (16 Nov) | the Host updated by the installer, `24cddc80` to `5976918e`: the build that offers the personas a Host has built in | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 3 ms, with all three |
| 15:58 (16 Nov) | the Host updated by the installer, `5976918e` to `47ff6794`: the build that tells an agent's tool who the agent is, whose turn it is and how to speak | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 16:10 (16 Nov) | the Host updated by the installer, `47ff6794` to `341125e3`: the build with the personas the owner makes, agents made as them, the files where an agent works and the screens of what this Host has not | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 17:19 (16 Nov) | the Host updated by the installer, `341125e3` to `0917e7a4`: the build with the app's remaining screens, an agent's face in a push, a spoken message that finds its agent by its name, and a phone's place | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |

Held means, for every turn of the exercise: its message is in the conversation once, one turn ended for it,
completed, answered with its word, and its command ran exactly once. Eighty-nine turns in twenty-five
exercises, none interrupted, lost or run twice. The ten of 17:33 took 52 to 54 seconds from the message: the length of
the command and no more.

The updates from 20:02 on were installed from a release built out of a clean checkout of the commit that had
just landed, not out of the working tree: what runs is what is on main.

## An agent publishes a file

On the build of `87818123`, 15 Nov 2026, with the installed tool (`clarp-media-publish`, the repository's own
program):

| When | Who | How it reached the Host | Result |
|---|---|---|---|
| 20:41 | a shell with nothing of a turn in its environment, as a worker an agent left running | the file the installer wrote, the administrator's door (`POST /media`) | a PNG of 3,860 bytes taken as `image/png`, 64 by 32, under a safe name; in the agent's gallery; fetched back byte for byte with the headers that keep a browser from running anything in it; a `Range` answered with the part |
| 20:42 | the agent `restart-2` in a real Claude Code turn, asked to run the tool and answer with what it printed | what its turn was given: the door for tools (`POST /tools/media`) and the token of the agent's tools | the turn ran the tool, completed, and its reply is the Markdown that shows the image; the id in the reply fetches the image; the same image published twice lies on the disk once, readable by its user alone |
| 21:04 | the artifact tool, by hand, on the build of `fedf357e` | the installed config file | a `file` artifact that names the image `restart-2` published was made for `restart-2`; the same for another agent was refused: the file is not found for that session |

## An agent hands over a report

On the build of `d0788034`, at 21:24 on 15 Nov 2026: the agent `restart-3`, in a real Claude Code turn, was
asked to write a small page of HTML and hand it over with `clarp-agent-artifacts create-report`. It did, and
answered `done`. The report is in its library as it was written (kind `html_form`, read-only, version `1`);
`GET /artifacts/<id>/html` serves the page behind its policy (a sandbox that allows scripts and no connection
anywhere); a summary of the library names the page by its length and digest instead of carrying it; its
revisions list its one version; and the page lies in the store of files, once.

## An agent asks with a form

On the build of `d3b9193c`, just after midnight on 16 Nov 2026: the agent `restart-3`, in a real Claude Code
turn, was asked to write a small form and the JSON Schema of its answers and to publish them with
`clarp-agent-artifacts create-form`. It did (the tool printed the artifact, exit 0). The form is in its
library with its page and its schema. Answers that do not fit were sent the way an app sends them
(`POST /artifacts/<id>/submit`) and refused in the words the Python Host's library has for them: `'soon' is
not one of ['now', 'later']`. Answers that fit were answered with a receipt (`pending`); the agent was told
them as what its user said, with the form's title, its version, the submission and the answers, and replied
in a real turn that it took them as a preference and had started nothing, which is what the message asks of
it. The same answers sent again under their id were answered `delivered`, and the agent was not told twice.

## A clip is heard

On 16 Nov 2026 at 08:42, on the build of `652c6d6a`. The installer had found the Python Host's interpreter
(faster-whisper), the model `small.en` in the machine's own cache and the owner's file of keys, and the
Host says what it listens with: its own `faster-whisper:small.en`, which hears a clip nobody named an engine
for, and `elevenlabs:scribe_v2`, there because the owner's file has a key (`GET /transcription-capabilities`).

A two-second tone, which is no speech, was handed to `POST /transcribe`: answered 200 after 0.33 s with an
empty text. The model had been loaded since the Host started. The same clip under a name
(`X-Transcription-ID`): heard once, and the second request answered with what was kept (`cached: true`);
another clip under the same name refused (409, `transcription id reused for different audio`); one answer
kept on disk, none after `DELETE /transcription-results/<name>`, and the name free again.

The same tone, once, to ElevenLabs Scribe through this Host: ElevenLabs took the request and the key and
answered that the account has no credits left (`quota_exceeded`), which this Host passed on as the engine's
words, status 500. That is the owner's account as it stood that morning, not a defect of either Host; it was
told to the Python Host's release owner. It showed something else: on the Python Host, the two agents that
are spoken to in Norwegian have engines of their own with a second one after Scribe, and went on being heard
by that one. This Host had no engines per agent then; they were built the same day (an agent's own language
and engines, and Chirp), which is where that observation led.

## An agent is heard aloud

On 16 Nov 2026 at 10:55, on the build of `5d74ac9d`, with the owner's file as it is (Gemini speaks, on his
Google project; Cartesia after it). The Host says which engines listen (`GET /transcription-providers`):
ElevenLabs and Google can be used, because the owner's file has a key for the one and names a project for the
other; Deepgram and Cartesia cannot. No agent has engines of its own there.

An agent made for this (`check`, the small model, a Gemini voice of its own) was asked, in a real Claude Code
turn, to answer with one spoken line. The line became a clip: announced to the apps under its name
(`1794822849329__check.mp3`), fetched whole (200; 25,121 bytes; MP3, 44.1 kHz, one channel, 1.57 s), fetched
in part (`Range`, 206), acknowledged as played (`POST /clips/ack`, `updated: true`), and after that not
offered again (`GET /clips/recoverable`).

The clip was then handed back to this Host to be heard (`POST /transcribe`). Google Chirp, with the owner's
account and project, answered in 0.88 s: "speech our works". The Host's own Whisper (`small.en`) answered in
9.9, 16.1 and 10.8 s in three tries: "Speed Child Works." The line was "Speech out works."; neither engine
heard a made voice exactly.

Ten seconds for a clip of a second and a half is slow, so it was looked into the same morning:

- The tone of 08:42 was no measure. A clip with no speech in it is dropped by the model's own filter for
  silence before the model runs, which is why it took a third of a second.
- The laptop stood at a load of 20 to 25 on its eight cores (many agents work on it). The same worker,
  started by itself with the same interpreter and model, heard the same clip in 3.4 to 6.5 s at a load of 11.
- The Python Host's own log has the same engine on the same machine between 0.8 and 1.7 s on the evening of
  14 Nov and at 2.3 s and 260 s on the morning of 16 Nov. Its log does not say how long those clips were, so
  that is the range the engine has there, not a comparison clip for clip.
- One difference was found by reading and closed: the Python Host gives its model half the machine's cores
  and four at most, so that hearing does not starve the server beside it; this Host left the count to the
  library. From the next build on it gives the same. Measured at a load of 11, the two counts hear the
  clip in the same time.

So the time is the machine's, and the engine is the same one with the same settings on both Hosts. An agent
that should be heard fast on a loaded machine is given an engine outside it, which is what its own engines
are for.

## The catalogue of voices

On 16 Nov 2026 at 11:36, on the build of `9781947b`, with the owner's file as it is. The catalogue for the
agent `check` (`GET /voice-catalog?for=check`) says what the owner's file says: Gemini speaks and can be used
(111 voices, `Kore` this agent's), Cartesia after it (359 voices: Cartesia's own list, asked for with the
owner's key), and ElevenLabs, Deepgram and Google listed as engines that cannot be used here, which is true
of this Host: it has not built them. A Google voice that is none is refused (400, `unknown voice`), an agent
that is not there is not found (404), a preview of a voice that is none is not made (404). No preview was
made: one costs the owner's account a clip.

One slip, repaired at once. The first probe sent a voice without naming its engine and expected a refusal.
The route takes any text then, as the Python Host's does, and so the agent `check` had `no-such-voice` for a
voice. It was given `{"gemini": "Kore"}` again and the catalogue showed `Kore` as its own. Only that agent,
which exists for such checks, was touched. What it says of the route is in the corpus now by the scenario
`voices`: a voice is checked against a list for Google alone.

## A turn is watched as it runs

On 16 Nov 2026 at 13:46, on the build of `24cddc80`. The Host lists `live_items` among what it can do. The
agent `check` was asked, in a real Claude Code turn, to run one command that takes six seconds and to answer
with what it printed, while a connection that follows the agent listened (`/events?live=check`) and the
whole was asked for three times (`GET /live?session=check`).

Before the turn: nothing shown, the count at 0, the run of the Host named by twelve hex signs, explanations
of tools off (they are not built here). While the command ran: one item, the tool, running; the agent's
line said it was in a tool; the turn running, with one tool counted. After it: the turn completed, 12.1 s of
work, one tool; the agent idle; two items, both settled: the tool (labelled with its command, the last line
of its output, exit 0, 6.3 s) and the answer, final, naming its row of the conversation.

Nine events were sent for it, numbered 1 to 9 without a gap, none of them with a place in the numbered
stream of the Host (they are never kept and never replayed). In them, seventeen steps: three that open or
replace an item, two that add to one, two that close one, the agent's line seven times and the turn three
times.

The check that matters: the Python Host's own reader of these events (`server/lib/live_projection.py`, the
code an app's reader is tested against) was given the nine events as they arrived. What it held at the end
is what the Host answers when asked for the whole, in the items, in the agent's line and in the turn. Every
answer shown names a row that `GET /log` has, and the last row of the conversation carries the end of the
turn.

## An agent has a face

The same build, the same minute. Each of the twelve agents on this Host is told with a picture
(`avatar_url`): the one for a Claude agent nobody gave a persona, under an address that ends in the first
sixteen signs of the picture's own digest. Fetched without a token, as an app's picture view fetches it: 200,
`image/png`, 354,766 bytes, to be kept for a year and never asked for again (`immutable`), and the digest of
what came is the one in the address. The same picture without its version: `no-cache`. For one who has it
(`If-None-Match`): 304 and no bytes. A part of another picture (`Range`): 206, the first eight bytes of a
PNG. A picture that is none: 404. Anything else under `/static/`: 404. A path that climbs out (`..`): 403.
The list of agents without a token: 401, as before; the pictures are the one thing an app is given without
one, as on the Python Host.

`GET /controller-actions` answers the empty registry (no actions, no mappings, the two kinds, the two
buttons, the nine gestures) and its audit an empty list: what the Python Host answers on this machine, where
nobody has made an action.

## Who an agent can be

On 16 Nov 2026 at 14:18, on the build of `5976918e`, with the owner's file as it is. The Host offers 84
personas (`personas` in `GET /agents/snapshot`), every one built in, 74 of them with a way of speaking, and
96 names (`roster`: the personas', then those of its twelve agents that are none of them).

They were held against the Python Host's own table on the same machine, read and not written: it has 84
personas built in and nine the owner made. The 84 are the same 84 by their ids, and each has the same name,
the same voices for every engine and the same way of speaking on both Hosts. The nine the owner made are
not here: making a persona was not built on that build (it is from `341125e3` on: see below). None of the Python Host's built-in personas is one the owner
made first and its roster named later, the one case in which the two Hosts would list a name differently.

## An agent knows who it is, and speaks when it is listened to

On 16 Nov 2026 at 16:00, on the build of `47ff6794`, with real Claude Code turns on the small model. Until
this build the Rust Host told an agent's tool nothing: no name, no way of speaking, no word that its user
listens. A spoken turn was answered in writing and no clip was made unless the message itself asked for the
`<speak>` tags.

A message sent as spoken, with no hint in it ("Can you hear me?"), was answered `<speak><vox>[curious]</vox>
Yes, <break time="350ms"/> I can hear you.</speak>`: the tags, and inside them the marks the owner's level
of sounding human asks for. One clip was made of it (62,737 bytes, MP3), offered by `GET
/clips/recoverable`, fetched, acknowledged (`play-ok`) and not offered again.

An agent made that minute under the name of a persona this Host has built in, and asked who it is in
writing, answered with the persona's name and its way of speaking in its own words. It was removed
afterwards; the Host had its twelve agents as before.

The test agent `check` has a name and no way of speaking, so all it is told as standing instruction is "You
are Check." Asked for its name it gave the model's ("I'm Claude, the Haiku 5.5 model"), and named its
session rightly from the line every turn carries ("Check (Clarp session check)"). That is the model with a
thin identity and thirty turns behind it in which nobody had told it a name, not the Host leaving something
out: what the tool was given is in the test `an_agents_tool_is_told_who_the_agent_is_whose_turn_it_is_and_how_to_speak`,
and sign for sign against the Python Host's own words in `told_vectors.rs`.

## A persona the owner makes, and an agent made as it

On 16 Nov 2026 at 16:12, on the build of `341125e3`, by a check that makes a persona and an agent of its
own and removes both.

A persona was made with a picture (201; a voice picked for the one engine this machine has a key for). Its
picture has an address of its own, `/persona-avatars/<id>?v=` and the first sixteen signs of the picture's
digest; fetched with the token it is the picture that was given, without the token 401. The list of
personas names it, as not built in. The same name again in capitals: 409, "QUILLON already exists."

An agent was made as it, the way the app makes one that names no directory (`cwd: "~"`): it works at the
home directory, has the persona's name and voice, and a picture of its own under `/avatars/<agent>?v=` with
the same digest, which is the persona's picture when fetched.

Beside it: `GET /dirs?path=~/` listed twenty directories of home (the most it lists); `GET /agent-files`
listed the 81 entries where the agent works; a new conversation in place (`replace_sid`) was refused 400
`relaunch_unsupported`, in words; `GET /past-sessions` offered none. Then the agent and the persona were
removed: the Host had its 84 personas and twelve agents as before, and the removed persona's picture was
404.

## A spoken message finds its agent by its name

On 16 Nov 2026 at 17:19, on the build of `0917e7a4`, with two real Claude Code turns on the small model, by
a check that makes an agent of its own and removes it.

A message dictated hands-free, "Hey Quillon, answer with the one word pong.", was sent as an app sends it
while another agent is open: naming that other agent's session, with `hands_free` and without saying it is
for the agent it names. It went to Quillon, who was given "answer with the one word pong." and answered
"pong"; Quillon was the open agent from then on. The next one, "And now answer with the one word ping.",
named nobody and the other agent's session again: it stayed with Quillon, who answered "ping". Until this
build both would have gone to the agent the app named. A message the app could not tell the agent of
(`POST /orchestrator/route-delegation`) was answered as by a Host whose router of spoken messages is off
(`action: disabled`), and nothing was sent.

Beside it, in the same check. Each of the two tools is told as installed, with a login this Host does not
verify, and how much of its account is used as unknown. The Host says who it is and that it is not updated
from an app. The engines that speak are listed as the owner's file has them (Gemini, then Cartesia; both
with their keys). Compacting a conversation from an app is refused, 501, in words. The agent of the check
was made with a picture of its own: its portraits are that picture; the address a push would carry for it,
signed by the rule with the Host's token, opened without a token (200, the picture's 74 bytes), and neither
with another signature nor unsigned (403). The phone's place was said for the agent's session and read back
with the moment it was said; a place north of the pole was refused, 400, in words; and an agent's question
for it was taken. Then the agent was removed: the Host had its twelve agents as before.

## The soak so far

Rounds 1 to 13 (15 Nov, 17:15 to 20:01): twelve completed and answered with their word; none lost, none
doubled. Round 11, at 19:31 on the build of `2e935ef7`, did not complete: the Claude account the machine's
agents share was out at that moment (the same minute ended a turn of the lead's own agent on the Python
Host). The turn ended after three seconds, and the Host said why under the message: "Claude usage limit
reached". The message is in the conversation once, with one turn end. That build ended such a turn; the
build installed at 20:02 runs with `--accounts follow` and lets it wait for the login to change. The next
time the account runs out during a round, the soak shows whether the turn waits and is taken up.

Rounds 14 to 16 (to 20:43) completed, on the builds of `61194d0c` and `87818123`. At 20:43 the soak was
started again as the job `rust-host-soak-2`, on the tool as installed with `87818123`: it writes down why a
turn did not finish and follows a turn that waits to its end. It went on counting where the evidence ended.

Rounds 17 to 28 (to 23:46) completed, across five more builds (`fedf357e`, `d0788034`, `6b06bb21`,
`bcb9a916` and, from 23:59, `d3b9193c`). In all, 28 rounds over six and a half hours: 27 completed and
answered with their word, the one of 19:31 as told above; none lost, none doubled.

Rounds 29 to 73 (to 11:06 on 16 Nov) completed, across five more builds (`4eebf671`, `14bb3f1c`, `3175ffcc`,
`652c6d6a` and, from 10:53, `5d74ac9d`): through the night and the morning. The account the agents share
ran out six times that morning for the lead's own agent; no round failed for it. In all, 73 rounds over
17.9 hours on fourteen builds: 72 completed and answered with their word, the one of 19:31 as told above;
none lost, none doubled. Six audits of the whole conversation, the last over 72 messages: no message
twice, no turn ended twice. A completed turn took 6.0 s at the median and 138 s at the longest.

Rounds 74 to 83 (to 13:37 on 16 Nov) completed, on the build of `9781947b` from 11:35. In all, 83 rounds
over 20.4 hours on fifteen builds: 82 completed and answered with their word, the one of 19:31 as told
above; none lost, none doubled. The build of `24cddc80` runs from 13:45.

Rounds 84 to 93 (to 16:09 on 16 Nov) completed, on the builds of `24cddc80`, `5976918e` and, from 15:58,
`47ff6794`. In all, 93 rounds over 22.9 hours on eighteen builds: 92 completed and answered with their
word, the one of 19:31 as told above; none lost, none doubled. Seven audits of the whole conversation,
the last over 84 messages: no message twice, no turn ended twice. The build of `341125e3` runs from 16:10.

Rounds 94 to 97 (to 17:09 on 16 Nov) completed, on the builds of `47ff6794` and, from 16:10, `341125e3`. In
all, 97 rounds over 23.9 hours on nineteen builds: 96 completed and answered with their word, the one of
19:31 as told above; none lost, none doubled. Eight audits of the whole conversation, the last over 96
messages: no message twice, no turn ended twice. The build of `0917e7a4` runs from 17:19.

## What a round does not prove

The soak holds the first rule (an accepted message is never lost and never runs twice); the restart
exercise holds the second (a running turn survives a restart). Neither exercises goals, jobs,
helpers, requests or push on the installed Host: those are held by the tests and the conformance suite
until an agent that uses them lives there.
