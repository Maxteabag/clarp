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

Held means, for every turn of the exercise: its message is in the conversation once, one turn ended for it,
completed, answered with its word, and its command ran exactly once. Fifty-six turns in fourteen exercises,
none interrupted, lost or run twice. The ten of 17:33 took 52 to 54 seconds from the message: the length of
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

## What a round does not prove

The soak holds the first rule (an accepted message is never lost and never runs twice); the restart
exercise holds the second (a running turn survives a restart). Neither exercises goals, jobs,
helpers, requests or push on the installed Host: those are held by the tests and the conformance suite
until an agent that uses them lives there.
