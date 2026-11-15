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

Both Hosts run their turns with the same logins of Claude Code and Codex. While the Rust Host has no
account recovery of its own, a turn there that meets a usage limit ends as failed, and says so.

The Host is on the loopback. A phone reaches it only once something is put in front of it (a Tailscale
serve entry) and the phone is paired (`clarp-host pair`): neither is done by the installer.

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

## What a round does not prove

The soak holds the first rule (an accepted message is never lost and never runs twice) and, across the
updates of the week, the second (a running turn survives a restart). It does not exercise goals, jobs,
helpers, requests or push on the installed Host: those are held by the tests and the conformance suite
until an agent that uses them lives there.
