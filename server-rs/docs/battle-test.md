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

Since 16 Nov 2026, 18:23, the agents of this test are Codex agents, by the owner's word that day: "if he's
going to test live agents on the host use codex. Use Claude only if testing Claude. But use sonnet." The
soak writes to `Soak Codex` (session `soak-codex`, Codex on its own model, the same directory, muted) from
round 102 on, as the job `rust-host-soak-3`; the job on the Claude agent was stopped that minute. The restart
exercises make Codex agents of their own (`restart-codex-1` and so on). The Claude agents that were made
before (`soak`, `restart-1` to `restart-10`, `check`) are kept for what tests Claude Code itself, and were set
to Sonnet that minute. Everything in this record up to then, the first 101 rounds and the first 26 exercises
included, was done with Claude Code on Haiku, and says so where it says "the small model".

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

The soak agent (a Codex agent since 16 Nov, 18:23; Claude Code on the small model before) works in a
directory of its own, and is asked for two words a quarter of an hour apart: the load on the owner's
accounts is small on purpose. Heavier work comes with agents the owner
chooses to move.

## A restart with turns running

`tools/soak restart` is the second rule on the installed Host: a running turn survives any restart of the
server. It gives each of its agents (Codex agents `restart-codex-1` to `-N`, made once and muted; with
`--backend claude` the Claude agents `restart-1` to `restart-N`, on Sonnet, for what tests Claude Code) one
command that takes a while and leaves a mark in a file, and a word to answer with afterwards. When every one
of them is in its command, the Host's service is restarted. Afterwards every message must be in its
conversation once, with one turn end, completed and answered with its word, and every command must have run
exactly once: a turn that was run a second time would leave a second mark.

```sh
~/.local/share/clarp-host/libexec/soak restart --agents 10                    # the service is restarted
~/.local/share/clarp-host/libexec/soak restart --agents 3 \
    --by "server-rs/tools/install-host --start"                               # an update, with turns running
~/.local/share/clarp-host/libexec/soak restart --agents 3 --backend claude    # the same with Claude Code, on Sonnet
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
| 18:01 (16 Nov) | the Host updated by the installer, `0917e7a4` to `afbf6959`: the build with the herald | 3 | every turn in its command (`sleep 40`) | held: the new build answered in the same millisecond, with all three |
| 19:38 (16 Nov) | the Host updated by the installer, `afbf6959` to `a9c9f41e`: the build with calls. The first exercise whose agents are Codex agents | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 20:45 (16 Nov) | the Host updated by the installer, `a9c9f41e` to `99fe37a7`: the build with rings, the chip on a phone's lock screen, and schedules | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 21:52 (16 Nov) | the Host updated by the installer, `99fe37a7` to `6390a692`: the build with the connection relay, the door that trusts only what nobody handed on, and this Host's tools first in every bash | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 22:30 (16 Nov) | the Host updated by the installer, `6390a692` to `a60c7839`: the build that keeps an answer to a heartbeat as one line, and takes a name off a spoken message as the Python Host does since its second fix of the evening. The first update with the connection relay on: the new Host was connected to it again 194 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 00:07 (17 Nov) | the Host updated by the installer, `a60c7839` to `22ca39e7`: the build with the heartbeat, a message filed under the id its app gave it, the queue by its user's hand, and the portraits an agent keeps. The relay was on: the new Host was connected to it again 95 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 00:40 (17 Nov) | the Host updated by the installer, `22ca39e7` to `aada15c0`: the build that counts a heartbeat's turn by its last answer. The relay was on: the new Host was connected to it again 44 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered at once, with all three |
| 01:35 (17 Nov) | the Host updated by the installer, `aada15c0` to `d63e4c09`: the build whose tools take off the mark of another Host's hand-over before they run (the release owner's addition to issue 388). The relay was on: the new Host was connected to it again 42 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 02:13 (17 Nov) | the Host updated by the installer, `d63e4c09` to `35dd10c4`: the build in which an agent forks itself into helpers, and a message the Host sends by itself is told to the apps under no id. The relay was on: the new Host was connected to it again 119 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 03:48 (17 Nov) | the Host updated by the installer, `35dd10c4` to `2b48004c`: the build with the janitors as they are kept and set (nothing of one runs in it yet). At its first start it installed the seven janitors a Host has of its own. The relay was on: the new Host was connected to it again 40 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 04:26 (17 Nov) | the Host updated by the installer, `2b48004c` to `34641890`: the build in which a janitor's scheduled task runs. The relay was on: the new Host was connected to it again 146 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered at once, with all three |
| 04:56 (17 Nov) | the Host updated by the installer, `34641890` to `c7332730`: the build in which a janitor is looked at and not spoken to, a janitor is made as a new agent, and the rules of a run that answers one request are in. The relay was on: the new Host was connected to it again 66 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered at once, with all three |
| 06:11 (17 Nov) | the Host updated by the installer, `c7332730` to `1473b6c7`: the build with the router of spoken messages. The relay was on: the new Host was connected to it again 74 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered at once, with all three |
| 06:27 (17 Nov) | the Host updated by the installer, `1473b6c7` to `bebd58a7`: the build in which an agent is started again in place. The relay was on: the new Host was connected to it again 177 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 07:48 (17 Nov) | the Host updated by the installer, `bebd58a7` to `e46cbf51`: the build in which a new agent begins with a session its tool kept, an agent asks for an event in its user's calendar, and an app's picker is told the models. The relay was on: the new Host was connected to it again 93 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 09:39 (17 Nov) | the Host updated by the installer, `e46cbf51` to `5bf5ab93`: the build in which the Host's own turns are counted for an app's usage screen, the gauge shows what the last answer was made from, a turn is told this Host under the names a skill's own script reads, and the explainer of the use of tools is in as far as its desk (nothing of it runs in this build). The relay was on: the new Host was connected to it again 134 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered at once, with all three |
| 09:50 (17 Nov) | the Host updated by the installer, `5bf5ab93` to `382d1c37`: the build in which an app that asks for the use of tools to be explained is answered (off as it comes, and its janitor paused). The relay was on: the new Host was connected to it again 45 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |
| 10:08 (17 Nov) | the Host updated by the installer, `382d1c37` to `d725481b`: the build in which a row of a tool carries its explanation while its turn is shown (off as it comes). The relay was on: the new Host was connected to it again 66 ms after it came up | 3 | every turn in its command (`sleep 40`) | held: the new build answered after 1 ms, with all three |

Held means, for every turn of the exercise: its message is in the conversation once, one turn ended for it,
completed, answered with its word, and its command ran exactly once. A hundred and forty-three turns in
forty-three exercises, none interrupted, lost or run twice: ninety-two of Claude Code in twenty-six, and
fifty-one of Codex in the seventeen since 19:38 on 16 Nov, from when the agents of this test are Codex agents. The ten of 17:33 took 52 to 54 seconds from the message: the length of
the command and no more.

The updates from 20:02 on were installed from a release built out of a clean checkout of the commit that had
just landed, not out of the working tree: what runs is what is on main.

What the update of 01:35 on 17 Nov installed was then looked at where it is installed: each of the fourteen
tool scripts under `~/.local/share/clarp-host/tools` has the line that takes off `CLARP_WRAPPER_HANDOVER`; a
call that carries the mark, made as a turn of this Host makes it, was answered (exit 0); and the script's own
lines, run with the mark set, leave none in the environment before the Host's binary is run. How that commit
landed is the first use of a rule written down before it was needed (REBUILD.md, "A candidate lands"): main
moved twice under it within twenty minutes (pull requests 394 and 392, a hook of Claude Code and its tests),
`Server (Rust)` (43987962764) and the repository's gates with the release ran on the commit itself, and the
conformance verdict is that of run 43981568229, given for a commit in which nothing that run reads differs
(`git diff` over the crates, the corpus, `server/`, `tests/replay/` and what installs them: empty). A second
run on the same inputs (43985072956) ended with two differences in `who-is-told`, and they are the
reference's: both of its recordings had the first reply of the conversation without the turn's summary and
with a notification that names a row the conversation does not have (defect R19, there for the first time in
a turn that is not streamed), where the recordings of the evening before had neither. The Rust Host's answer
was the same in both runs. The two places are registered beside R19's others.

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

## An agent raises its hand

On 16 Nov 2026 at 18:01, on the build of `afbf6959`, with real Claude Code turns on the small model and the
owner's own engines (Gemini, then Cartesia), by a check that makes an agent of its own and removes it. The
herald was as it comes: on, and a reply of 120 signs or fewer said at once.

The user was with one agent (it was the open one) and another had a report of 180 signs to say aloud, by a
message nobody waits for the answer to. Until this build its clip would have been announced to the apps the
moment it was begun. Now one clip was announced, and it was not the report: the herald's own, in that
agent's voice, marked to be played whichever agent is open, with no line of the agent's behind it. The
report was held, and an app that had been away was not offered it. When the user opened that agent, the
report's clip was announced, with the same mark. Then the user turned away again and the agent said "Done.":
that was announced at once, with no herald. No engine failed.

The same check asked what a tool is told when the app says its user has not heard the last reply
(`unheard_audio_sessions`). A real model, asked whether its message came with such a note, answered "Yes.
The note begins: "The user began a new spoken"."; one whose user had heard everything answered that there
was none; and the conversation kept the user's words alone. Two things this check taught. Asked in a more
roundabout way, the same model with the same note answered that there was none: what a model makes of a
note is the model's, and the proof that the note is handed over is the test with a tool that writes down
what it is handed. And the build held the note back from an agent its user had muted, which is a mistake of
this Host's (on the Python Host only a janitor's run is exempt): put right in the build after this one.

## A call

On 16 Nov 2026 at 19:38, on the build of `a9c9f41e`, with real Codex turns, by a check that makes two
agents of its own and removes them. The Host said that it has calls. A call was started for a phone with
both agents by their names, one as it is written and one in small letters: the first had the floor, the
second was asked in, the apps were told, and the first agent found a quiet word about it in its
conversation. A message of the call for the second agent went to it and was answered ("pong"); it was in
the call from then on and had the floor. It was put on hold and the floor went back. A message of the call
for the agent on hold went to the one on the floor, and the answer of that agent's real turn showed what its
tool had been told of the call: "Corvin is on hold; he last said "pong."" The agents' own tool, called as an
agent calls it, took the second off hold, and printed the Host's refusal of a name nobody has with exit 1.
The call ended, and a message of the call that was over was refused as one (409, `call ended`, with the
call's id). The apps were told of every change and of nothing else: start, route, hold, resume, end; a
message that falls to the agent that already has the floor changes nothing and tells nothing.

## A ring

On 16 Nov 2026 at 20:46, on the build of `99fe37a7`, with real Codex turns and one short clip of the owner's
own engine, by a check that makes two agents of its own and removes them. The Host said that agents can
ring; rings were on, as a Host comes; nobody rang. No phone is registered with this Host, and the one who
rang was told so: nothing sent, nothing failed. An agent rang by the owner's tools: the ring stood for 45
seconds, its opening line had been voiced (a clip of 56,468 bytes, fetched), and a second agent that tried
meanwhile was refused (409, `the user is already being called`). Answered as a live voice call it was
refused (409, `live_mode_conflict`); answered plainly it started a call with the agent on the floor and
handed back the line to play first, with its clip. The agent found a quiet word about both in its
conversation. What the user said next went to it, and the answer of its real turn showed what its tool had
been told: "I called to check the ring; my opening line was, "This is a check of the ring. Nothing is the
matter."" With rings turned off nobody rang (403, `agent calls are turned off`); on again, the trail had
every step. The apps were told of each change of the ring: ringing, answered.

Not shown by that run: a ring by an agent's own tool from its own turn. The command ran, and not against
this Host: see the next section. Repeated at 21:53 on the build of `6390a692`, with the repair installed:
all twenty-eight checks, and among them an agent rang with its own tool from its own Codex turn, the user
declined, the tool that had waited printed `declined`, the agent said so and found "Call: The user declined
your call about: A check by the agent itself" in its conversation; the apps were told ringing, answered,
ringing, declined. The Python Host's trail of rings had nothing new.

## Where the fence between the two Hosts did not hold

An agent on this Host is to reach this Host's tools by their names, and never the Python Host's, which act
on that Host. Until 16 Nov the fence was the order of the path alone: this Host's tools come first on the
path a turn's commands run with. At 20:46:17 a Codex agent of the check above ran `clarp-admin call ring`
through a login shell (`bash -lc`, which a Codex model starts when it likes). A login shell runs the user's
profile first; the profile on this machine puts `~/.local/bin` in front; the Python Host's `clarp-admin`
is there, and it sets its own Host's environment before it runs, whatever the turn's said. The request went
to the Python Host on elitebook. Measured there, with that Host's own tool and nothing changed: one entry in
its trail of rings, refused, `agent required`. No ring was placed and no phone rang. A probe a few minutes
later read that Host's ring status the same wrong way and wrote nothing. The release owner of the Python
Host was told the same hour.

Reproduced without any Host: with the turn's path, `bash -c 'type -a clarp-admin'` names this Host's tool
first and `bash -lc` names the Python Host's. The repair on this side is in `tools/install-host`: a turn is
given `BASH_ENV`, a file every bash reads after its profile, which puts this Host's tools first again. That
closes `bash -lc`, also one started inside another shell. It does not close a login shell of another kind
(`sh -lc`, zsh): there the profile's order stands, and only the tool that is found first can hand over. That
hand-over is asked of the Python Host's wrappers, and settled with their owner the same evening (issue 388):
a wrapper that finds itself in a turn that carries `CLARP_HOST_ADDR` runs `$CLARP_SHARE_DIR/tools/<its own
name>`, or refuses with exit 69; it never goes on to the Python Host. Until both are in place no agent of the
owner's moves.

Installed with `6390a692` at 21:52 and measured in a Codex turn, by a probe that calls no tool: a plain
shell, `bash -lc`, and a `bash -lc` inside another all find this Host's tools first; `sh -lc` still finds the
Python Host's, as said. The turn carries `BASH_ENV`, `CLARP_HOST_ADDR` and this Host's `CLARP_SHARE_DIR` in
all of them, which is what the hand-over reads.

## Through the connection relay

On 16 Nov 2026 at 21:54 the installed Host (`6390a692`) was told to use the relay (`tools/install-host
--relay on --start`) under a name of the battle test's own, `elitebook-rust`, which whoever keeps the relay
had made for it; no phone is paired under that name. It dialled by itself: connected after 143 ms, one line
in the journal (`relayConnectionOpened`), and `clarp-host network relay status` showed the relay's address,
the name, the address a phone would be paired with, and `connected`.

At 21:57, by a check that acts as a phone and makes one device and one agent of its own and removes both,
everything through the relay's public address: the device handed in its pairing code and was given a token
of its own (201); it asked the Host and was answered; ten small requests, each on a connection of its own,
took 0.05 to 0.07 seconds; a token nobody has was refused in a header and in an address (401), and so was
the device's own token in an address, and no token at all; the stream of events opened (200,
`text/event-stream`); the device sent a message to a Codex agent and read its answer ("relayed") through
the relay, and twelve events of that turn came down the stream, the first 0.06 seconds after the send. Then
the device was revoked: its stream ended in the same instant, and its token opened nothing any more (401).
The administrator's token never left the machine: that it opens nothing through the relay is shown against
a stand-in, by the Host's own test, with the same code.

Two things the check itself ran into, neither the Host's. The relay's edge refuses the name Python's own
client gives itself (403, `error code: 1010`), before a request reaches the relay: the check names itself
otherwise. And the build gate of `6390a692` failed at its first attempt in one test, the herald's, which
asserted that the voice engine had been asked at the moment a clip was announced; a clip is announced
before its first byte is there. Alone the test passed five times of five; the second attempt of the same
run on the same commit was green, and the test waits now.

Not shown yet: a WebSocket through the relay (this Host has no WebSocket route), a loss of the connection
on the real relay and the dial after it (shown against the stand-in), and the owner's own phone.

**Voice, as the owner's phone uses it** (16 Nov, 23:19, on the build of `a60c7839`). Counted on his Hosts over
three days, his voice goes as clips: what he says is sent to be heard (`POST /transcribe`, 56 times on
elitebook), and what an agent says aloud is fetched as a clip; the sockets of live voice were asked not once,
on either Host. So that is what was shown through the relay's public address, by a device of the check's own
that was paired through the relay: it asked a Codex agent for a spoken reply; the clip was announced to it
four seconds after its message; the whole clip came through the relay in 0.52 seconds (39,750 bytes of
`audio/mpeg`); a piece of it, asked for as a player asks (`Range: bytes=0-1023`), came as that piece (206),
and what came through the relay was the clip as the Host has it, byte for byte. Then the same clip was sent
back through the relay to be heard, as a phone sends what its user said: heard in 1.67 seconds by this
Host's own Whisper, all five words of the line that had been said. A clip from nobody was not heard (401).
The line was said once by the owner's engine; nothing else left the machine but through the relay. Not
shown by this: a WebSocket through the relay, which this Host has not.

## A schedule

On 16 Nov 2026 at 20:49, on the build of `99fe37a7`, with one real Codex turn, by a check that makes an
agent of its own and removes it. An expression that is none was refused in words (`Cron expression must
have 5 fields, got 1: nonsense`). A schedule for every minute was made, 46 seconds before its minute, and
was listed for its agent. When the minute came the agent was sent the prompt as something the Host sends by
itself (`automation`), 12.8 seconds after the minute: the runner looks every fifteen. The agent answered
with its word. The user was not moved to that agent. The schedule had moved on to its next minute before it
sent. Turned off it had no next time; it had been sent once; removed, it was gone, and the Host had the
agents and the schedules it had before.

## The queue by its user's hand, and ids as a phone mints them

On 17 Nov 2026 at 00:13, on the build of `22ca39e7`, with real Codex turns, by a check that makes an agent
of its own and removes it. A message was sent under an id as the phone mints one (the mark `u-` and a UUID
in capitals), and its turn ran a command of 45 seconds; three more were left for later behind it, one of
them under such an id and two under ids as the desk mints them. The queue named each as its app had named
it, and the conversation had the first message under the phone's id as it is: the fault of the build
before, which filed a phone's message under its id with a second mark before it, is gone on the installed
Host. Then, by hand: the words of one waiting message were changed, and words that are none were refused
(400); one was taken back by the phone's id, and could not be taken back twice (404); nothing was sent past
the turn that ran (409). The turn was stopped, which held the two that waited. The later of them was sent
by hand past the hold and answered with its word, and the other stayed held; then that one was sent by
hand and answered with the word it had been reworded to, and with nothing left waiting the hold was over
without a resume. The conversation had the three messages that ran, each under the id its app looks for,
and nothing of the one that was taken back.

## The portraits an agent keeps

On 17 Nov 2026 at 00:09, on the build of `22ca39e7`, without a turn of a model, by a check that makes an
agent of its own and removes it. Two pictures were put into its gallery. One was kept as a portrait (kept
again, it was kept once) and made the picture the agent wears: the snapshot then showed the agent with
that picture, by what the address carries of the picture's digest; fetched there, the picture was the one
that was published, byte for byte; and the portrait was handed out as the kind of picture it is
(`image/png`). The second was kept beside it, with the one it wears first. A portrait the agent has not
was refused in words (404).

## A heartbeat, and what its first real turn found

On 17 Nov 2026 at 00:09, on the build of `22ca39e7`, with one real Codex turn, by a check that makes an
agent of its own in an empty directory, sets the owner's interval to a minute for as long as it runs, and
puts the settings back. The settings were read and changed, and a change that cannot be was refused in
words. The agent had not asked: no heartbeat was due. It asked, and one was due at once; the runner, which
looks once a minute, gave it the heartbeat 58 seconds later, by itself. A chat shows the prompt as the
Host's own business. The turn ended; the next was due an interval after this one was started; there was
one heartbeat, not one at every look; turned off, none was due.

What the turn counted as was wrong, and that is the finding. The agent said three things: two remarks on
its way, and then that nothing was to be done, which the conversation kept as the line for it. By the rule
this build had from the Python Host, a turn in which anything is said beside the token is activity: the
count of turns that found nothing stayed at none, and the interval did not grow. An agent whose tool
narrates would so be woken at the owner's interval for ever. The same rule on the owner's Host, counted
there over seven days without reading what was said: 87 of 122 heartbeat turns of Codex agents and 230 of
280 of Muse agents counted as activity, 4 of 31 of Claude agents. Reported to the release owner as
reference defect R28. This Host counts a heartbeat's turn by its last answer from the build after this
one, and the same check is run on it again.

Run again on 17 Nov at 00:41, on the build of `aada15c0`, with another real Codex turn: the runner gave
the agent its heartbeat 8 seconds after it asked; the agent again said three things, two remarks on its way
(122 and 191 signs, each a message of its own) and then that nothing was to be done. This time the turn
counted as what its last answer says: nothing found, once (asked again, still once), and the next heartbeat
was due two intervals after this one was started instead of one. Everything else as before, and the
settings were put back.

## A fork

An agent forks itself into helpers that start from its conversation (`POST /agents/<session>/fork`, landed with
`35dd10c4`). Tried on the installed Host on 17 Nov, 02:13 to 02:30, five times, each time with three Codex
agents of the check's own and a scratch git repository (`verify-fork.py`): an agent is told a word; before
it has said anything a fork of it is refused (409, `no_conversation`); a child without a task is refused in
words; then it forks into two, one where it works and one in a git worktree of its own.

| What | In all five runs |
|---|---|
| The answer | both children made, each from the conversation as words (`fork: seed`), each with its task delivered |
| Where they work | the first where its maker works; the second in `repo-worktrees/check-fork-tree`, which was made for it and is on the branch `check/fork-tree` |
| Who they are | helpers of the agent that forked: the maker's id is theirs to answer to |
| The first message | from the maker, 1,049 to 1,060 signs: what the child is, its maker's conversation, its task, and the command to report with |
| What a child knows | the first child answered with the word its maker had been told, which it could only have from the conversation it was given; the second answered with the branch of its own worktree |
| Afterwards | the two children, the maker and the repository removed; every agent that was there before still there |

What varied is the model's. Each child is told to send its report with `clarp-admin prompt`, in the Python
Host's own words. Of the ten children, six ran the command: each was marked as reported, and its maker had
the report from the child that wrote it. Four did not: the child whose task needs no tool answered with
the word and stopped (in the two runs that looked: a row of ten signs and no call of any tool). The Host
has nothing to mark there, and marked nothing. That is how the report of a helper works on both Hosts: it is
the helper that sends it. A maker that waits for a report from a child whose task was a question may wait
for nothing; the check says so in a line of its own instead of counting it as the Host's failure.

## The janitors

A janitor is an agent with a job of maintenance. What is kept of one and how it is set landed with
`2b48004c` (17 Nov, 03:47) and was tried where it is installed a minute after exercise 35 (`verify-janitors.py`,
no turn of any model): 27 points, all as meant.

| What | On the installed Host |
|---|---|
| The Host's own | the seven are there under the ids and session names they have on every Host, each paused, each with its triggers and the picture of its job; the snapshot shows them after the agents, as janitors, looked at and not spoken to; a message sent to one reaches nobody (404) |
| What a Host offers | ten jobs and eleven triggers, as the Python Host's |
| An agent made a janitor | an agent of the check's own given a scheduled task at a quarter past three by Oslo's clocks: paused, revision 1, a day between two runs |
| Set | an option and the one agent it watches (revision 2); a change by somebody who saw another revision refused (409); a schedule in a zone there is not refused in the Python Host's words |
| Turned on | refused in words (503): nothing started a janitor in that build |
| A schedule previewed | half past eight on working days by Oslo's clocks: Tuesday, Wednesday, Thursday |
| Removed, and let go | removed: listed no more, its agent put away, and not removable as an agent; let go: an agent like any other again |
| Afterwards | the check's two agents removed; the 23 agents that were there still there |

How that candidate landed is worth a line. The first one (`e9d6f20e`) passed everything on the build machine
and failed in `Server (Rust)` (44000758560) on one test: whether a name is a time zone. The runner's system
(Ubuntu) keeps the machine's own zone as a file named `localtime` in the directory of zones; the owner's
machines (Arch) do not, and the library that reads zones takes any file of that directory as a zone. So the
same Host would have taken `localtime` as a zone on one machine and refused it on another. The rule is now the
Host's own and the same everywhere: the files of that directory that are no zone's name are no zone
(`localtime`, `posixrules`, `Factory`, everything under `posix/` and `right/`). The candidate with it
(`2b48004c`) was green in `Server (Rust)` (44002161507), in the repository's gates with the release, and in
the conformance run on the same commit (44002165436: 82 differences, all registered; every planted change
caught), as the run of the first candidate had been (44000762295).

Two things of the janitors landed with `c7332730` (17 Nov, 04:55, installed by exercise 37 at 04:56) and were
tried where it is installed two minutes after, by the same check grown by nine points (37, all as meant; the
check of a scheduled task with a real Codex agent was run after it on the same build, and came out as in the
section below).

| What | On the installed Host |
|---|---|
| Looked at and not spoken to | a message for a janitor, a stop for it and its removal as an agent are each refused in the Python Host's word and words (403, `janitor_inspection_only`). The build before answered a message for one as for nobody (404) |
| Made as a new agent | a request with a name, a tool, a directory and a scheduled task, under an id of its own: a janitor under a session name the Host drew from the name (`nightcheck-` and six signs), paused, with no voice of its own; one agent more on the Host |
| The same request again | answers the same janitor, and the Host has no second agent |
| The same id for another janitor | refused (409) |
| Afterwards | let go and removed with the check's other agents; every agent that was there still there |

## A scheduled task runs

A janitor's scheduled task runs since `34641890` (landed 17 Nov, 04:25, installed by exercise 36 at 04:26).
Tried where it is installed, minutes after, with a real Codex agent of the check's own (`verify-janitor-run.py`):
the agent is given a task of every minute ("run `date -u +%H:%M` once, then close the run as you are told,
with the status worked and the time as its summary"), is turned on, and two minutes come.

| What | On the installed Host |
|---|---|
| Turned on | 200, at rest; within a look the runner said when it runs next: when the next minute begins (50 seconds on) |
| The first minute | a run was made for it, under an id of its own, waiting; a second later its turn ran in the janitor's own conversation and the janitor was told as running |
| Its end | the run was over when its turn was: completed, 10 seconds after it was made; the janitor at rest again with the time of its run |
| What the turn did | it closed its run itself with the agents' tool (`clarp-admin janitor outcome`): `worked`, with a summary of five signs, the time the command printed. That it does so is the model's; the Host counts a run that says nothing as work |
| The conversation | read like an agent's (`GET /log`): five rows, what it was told and what it did |
| After it | the task keeps its pace: its next run was the next minute |
| The second minute | a second run was made and its turn ran; the janitor was paused while it did |
| Cut off | the run was cancelled at once; its turn was ended; a word from the run after that was refused (409, `run_terminal`); nothing waited, the janitor's mailbox was not held, and no third run was made |
| Afterwards | the janitor let go, its agent removed; every agent that was there before still there |

The older check of what is kept and set (`verify-janitors.py`) was run beside it on the same build, with one
line changed for it: a scheduled task is turned on (200) where the build before refused it, and a job that
does not run here yet is still refused in words (503). 28 points, all as meant. Its first run of the night
failed in a way worth a line: it names its triggers, and a trigger's id is never another agent's, also after
that agent is gone, on both Hosts. The check now names its triggers anew for every run.

## The router of spoken messages

The router landed with `1473b6c7` (17 Nov, 06:10, installed by exercise 38 at 06:11) and was tried where it is
installed a minute after, with a real Codex as its model (`verify-router.py`): two agents of the check's own,
each told in one turn what it keeps (the notes of a sailing trip: harbours, tides and berths; the budget of a
kitchen renovation), and then a message that names nobody, said to the wrong one of the two.

| What | On the installed Host |
|---|---|
| As it comes | off; set to Codex with the model its job recommends; the tools it can be set to are Claude Code and Codex, both installed |
| Turned on | by the app's own route (`POST /orchestrator/settings`): on, and still asked only by an app that could place a message by no name |
| An app asks whom a message is for | "what do the berths in the harbour cost in July", said to the agent of the kitchen: answered after 8.6 seconds, `route`, to the agent of the harbours |
| What was decided | kept: `agent_message`, confidence 0.98, asked of Codex with that model, 8,573 ms, no error. The question was given to Codex on standard input |
| The agents | the one of the harbours was given the message and answered it; the one it was said to was given nothing |
| The same message again | answered by what it came to (`route`): no model asked, no second decision, nobody given it twice |
| Afterwards | the router set back as it was (off); the check's two agents removed; the 23 agents that were there still there |

That a model finds the right agent is the model's; what the Host showed here is the whole way of a real
question: its janitor's run, the tool started for one question and answered in time, the decision carried out
and kept. The check of the janitors was run after it on the same build (37 points, as before), and the one of a
scheduled task with a real Codex janitor.

## An agent started again in place

Restart landed with `bebd58a7` (17 Nov, 06:27, installed by exercise 39 the same minute) and was tried where
it is installed right after, with a real Codex agent of the check's own (`verify-started-again.py`, 22
points, three turns of Codex).

| What | On the installed Host |
|---|---|
| Before | the agent is made, written to, and answers; its conversation has a name and its tool a session |
| Started again (`POST /agents` with `replace_sid`) | 200: the same agent under the same session name |
| For those who read | its conversation is empty; a reader that kept the old one is told to drop it (`replace_required`) |
| Its row | no conversation, no session of its tool, no gauge and no model that answered; what it last said is still what a list of chats shows of it |
| A message after it | answered, in a new session of Codex: the session the tool names is another, so it was not asked to go on where it was |
| Its conversation then | holds only what was said since, goes by another name, and its revisions go on after the old ones |
| Started again while a turn runs, under another name | 200 after 0.1 seconds; the turn was stopped first (`interrupted`), the conversation is empty again and the stopped turn is no part of it; the row has the new name |
| Afterwards | the check's agent removed; the 23 agents that were there still there |

## A new agent begins with a session its tool kept

Past sessions landed with `e46cbf51` (17 Nov, 07:47, installed by exercise 40 a minute later) and were tried
where they are installed with real Claude Code (`verify-past-sessions.py`, 31 points). The agents of the
check ran on Sonnet, since Claude Code's own sessions are what is tried; three turns, in a directory of the
check's own, which it removed with the sessions Claude Code had kept for it.

| What | On the installed Host |
|---|---|
| A session to offer | an agent of the check's answers one message; its session is then offered for its directory: one, under its name, with what was asked first, and among those of every directory (100 are listed); of Codex none |
| A session an agent has | taken up: 409, in use, with the agent that has it; opened: 200, that agent; a session nobody kept: 404, and no agent made |
| A fork | 200: a new agent (`spawned`), the first agent's, on the model the session ran on as its transcript names it; its conversation shows what was said; its tool goes on in a copy under a name of its own, and the conversation goes by that name |
| The fork is asked what was said before it was made | it knows: Claude Code took the copy up as a session; and it stays in the copy afterwards |
| The agent removed, its session taken up | 200: a new agent, on the session's model; its conversation shows what was said, under a name of its own, and its tool goes on in the session |
| That agent is asked what was said in the session | it knows; and Claude Code went on in that same session: the session it names afterwards is the one taken up |
| Taken up a second time | 409, in use by the agent that took it up |
| Afterwards | the check's agents, directory and sessions removed; the 23 agents that were there still there |

The first run of the check failed in two of its points, which were its own: it expected the new agents'
model to be the short name the first agent was made under (`sonnet`), where the session's transcript names
the model in full, and that is what an agent that takes the session up runs on, as on the Python Host. The
check was corrected and run again whole.

## The models an app's picker offers, and a calendar request

The same build, the same minute (`verify-model-options.py`, 14 points), with the tools of this machine and
no stand-in.

| What | On the installed Host |
|---|---|
| The catalogue | told in 0.06 seconds: Claude Code and Codex, and no other tool |
| Claude Code | on the machine, version as it says it (2.1.321); one model seen in what its account may run, thirteen known beside; no model twice |
| Codex | on the machine, version as it says it (0.167.1); eight models as it lists them, each with the efforts it takes, and one known beside |
| What this Host cannot do with a tool yet | not said to be possible; taking up and forking Claude Code's sessions is |
| Asked again | told as it was found, in a millisecond: the tools are not asked again |
| A calendar request | given an id (no device is paired with this Host, so no app was told and nobody was asked); one that leaves out when it begins: 400, `start required` |

## What the Host's own turns used, and the gauge

Landed with `5bf5ab93` (17 Nov, 09:39, installed by exercise 41 a minute later) and tried on the installed
Host with the real tools (`verify-usage.py`, 17 points): an agent of Codex and an agent of Claude Code
(Sonnet: Claude Code's own numbers are what is tried), each of the check's own, each answering one message.

| What | On the installed Host |
|---|---|
| A turn of Codex | counted for the Host on the usage screen (`GET /backend-usage`): one turn more than the three of the exercise before it, 45,438 tokens taken in, 5 given out; told as what was spent, fresh, and as no share of an account |
| Its gauge | none: Codex says nothing of how full its conversation is, and keeps it within bounds by itself. The build before showed what the turn took in |
| A turn of Claude Code | counted: 31,684 tokens taken in (what was sent anew, read from the cache and written to it: the build before counted the first alone, a handful), 3 given out |
| Its gauge | 31,684: what the answer was made from, which for a turn of one step is what the turn took in |
| An agent's own command for its user's place | answered as the Python tool answers when nobody shares one in time: it ends with 1 and says so |
| Afterwards | the check's two agents removed; the 23 agents that were there still are |

## The use of tools explained

Landed with `382d1c37` (17 Nov, 09:50, installed by exercise 42 a minute later) and tried on the installed
Host with the real Codex (`verify-explain.py`, 18 points): an agent of the check's own that runs no turn, and
an app's requests about made-up uses of tools. The owner's switch and the janitor that explains are off as
installed; the check turns both on, and puts both back.

| What | On the installed Host |
|---|---|
| With the switch off | every thing asked about is `disabled`, and nobody is asked |
| With the switch on and the janitor paused | `disabled` still, and the answer names the model that would be asked (`gpt-6-sol`) |
| A directory listed (`ls -la src`) | answered at once by a template: "Looks at the files and folders inside src." |
| A program nobody has, with a file (`zorbulate-… reports/q3.csv`) | waits; Codex is asked once, started to read and nothing else (a sandbox it may only read in, no shell and no tools, a directory of its own, the shape its answer must have), and answers in 5.1 seconds: "Run an opaque command on the CSV file; its purpose is unknown." |
| The same program with another file | answered at once from what was learned, and nobody is asked |
| The ledger (`GET /tool-explanations/stats`) | the template, the one answer from what was learned and the one question to the model are counted; one row learned, with places |
| The janitor's runs | one, completed |
| Afterwards | the janitor paused, the switch off, the check's agent removed; the 23 agents that were there still are |

**On the row itself.** Landed with `d725481b` (17 Nov, 10:07, installed by exercise 43 a minute later) and
tried on the installed Host with the real tools (`verify-explain-rows.py`, 15 points): the switch and the
janitor turned on, an agent of the check's own on Codex asked to run one harmless command of a made-up shape
(`printf 'zorb-…\n' | wc -c`), and what is shown of its turn (`GET /live`) read three times a second.

| What | On the installed Host |
|---|---|
| The row of the command, when the turn shows it | told to wait for its explanation (`pending`) |
| 7.6 seconds later | carries its sentence (`ready`): "Print the supplied text. Then count the bytes in standard input." No template says either part, so the janitor's model was asked: Codex, started to read and nothing else |
| The turn | completed; its row was settled 12.4 seconds after the message |
| Afterwards | the janitor paused, the switch off, the check's agent removed; the 23 agents that were there still are |

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

Rounds 98 to 100 (to 17:55 on 16 Nov) completed, on the build of `0917e7a4`. In all, 100 rounds over 24.7
hours, a day and a night, on twenty builds: 99 completed and answered with their word, the one of 19:31 as
told above; none lost, none doubled. The build of `afbf6959` runs from 18:01.

Round 101 (18:09) completed, the last on the Claude agent. From round 102 (18:23) the soak writes to a Codex
agent of its own, as told above. Rounds 102 to 107 (to 19:38 on 16 Nov) completed, on the build of
`afbf6959`. In all, 107 rounds over 26.4 hours on twenty builds: 106 completed and answered with their word,
the one of 19:31 on the 15th as told above; none lost, none doubled. The build of `a9c9f41e` runs from 19:38.

Rounds 108 to 167 (19:53 on 16 Nov to 10:45 on 17 Nov) completed, each in six to nine seconds, across the
seventeen builds from `a9c9f41e` to `d725481b`. In all, 167 rounds over 41.5 hours on thirty-seven builds:
166 completed and answered with their word, the one of 19:31 on the 15th as told above; none lost, none
doubled.

**Codex ran out of credits** (17 Nov, about 10:56). Round 168 (11:00) and the three turns of the
forty-fourth restart exercise (10:57, to install `de2da7c1`) were accepted and started, and Codex ended each
at once. Asked itself at 11:01, with one question it may only read for, Codex says: "Your workspace is out
of credits. Ask your workspace owner to refill in order to continue." The Host is run with `--accounts
follow`: each of the four turns is shown as waiting for an account (`limited`, "Waiting for a codex account
with available usage"), its message is in the conversation once, and nothing ran. The exercise saw that no
turn had reached its command and ended without touching the installed Host, which stays on `d725481b` while
main is ahead of it. The owner was told by a card at 11:03, and the release owner of the Python Host, whose
Codex agents share the account, by a message. What becomes of the four turns when Codex answers again is
told here when it has happened.

## What a round does not prove

The soak holds the first rule (an accepted message is never lost and never runs twice); the restart
exercise holds the second (a running turn survives a restart). Neither exercises goals, jobs,
helpers, requests or push on the installed Host: those are held by the tests and the conformance suite
until an agent that uses them lives there.
