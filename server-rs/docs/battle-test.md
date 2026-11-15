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
| The agents' own tools, first on their path | `~/.local/share/clarp-host/tools` |
| Its token and what its tools read to find it | `~/.config/clarp-host/env`, `config.toml` (both 0600) |
| Its service | `~/.config/systemd/user/clarp-host.service` |
| Where it listens | `127.0.0.1:7690`, the loopback only |

Nothing of the Python Host is written: not its units, its database, its configuration or its tools. The
installer reads two things from the Python Host's unit, to give a turn the same world it has there: the path
its tools are found on, and with it the pinned `claude` and `codex` programs.

On that path, every tool of the Python Host has this Host's tool of the same name in front of it. Other
programs whose names begin with `clarp` may be on it too: on elitebook, ten helpers of the owner's own
(`clarp-prompt`, `clarp-contacts`, `clarpd`, `clarp-master` and others, linked into `~/.local/bin` from his
dotfiles). They are not a Host's tools and are left as they are; `clarp-prompt` calls `clarp-admin` by name,
which is this Host's.

A turn is given the address of its Host and a token for its agent's tools, which opens the tools and nothing
else. The administrator's token (`~/.config/clarp-host/env`) is not in a turn's environment.

Both Hosts run their turns with the same logins of Claude Code and Codex. While the Rust Host has no
account recovery of its own, a turn there that meets a usage limit ends as failed, and says so.

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
`turn_interrupted`): it is not a lost message. Every twelfth round the whole conversation is read again,
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

On 15 Nov 2026, on the build of `d9868bea`, with real Claude Code turns:

| When | How | Turns | What it met | Result |
|---|---|---|---|---|
| 17:31 | the service restarted | 2 | every turn in its command (`sleep 30`) | held |
| 17:33 | the service restarted | 10 | every turn in its command (`sleep 45`) | held: the Host answered again after 102 ms with all ten |
| 17:35 | the Host killed (`SIGKILL`) | 5 | every turn in its command | held: back after 2.1 s with all five |
| 17:36 | the Host killed | 5 | the messages just accepted, the turns starting | held |
| 17:37 | the Host killed | 8 | one turn just ended, two whose command ended while the Host was down (one had answered too before it was back), five in their commands | held |

Held means, for every turn of the exercise: its message is in the conversation once, one turn ended for it,
completed, answered with its word, and its command ran exactly once. Thirty turns, none interrupted, lost or
run twice. The ten of 17:33 took 52 to 54 seconds from the message: the length of the command and no more.

## What a round does not prove

The soak holds the first rule (an accepted message is never lost and never runs twice); the restart
exercise holds the second (a running turn survives a restart). Neither exercises goals, jobs,
helpers, requests or push on the installed Host: those are held by the tests and the conformance suite
until an agent that uses them lives there.
