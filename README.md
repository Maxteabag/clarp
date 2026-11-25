# Clarp

## Why this exists

Clarp exists to give people back their dignity — to free them from the desk,
from the screen, from the office, and to close the distance between new
information and the source of their own creativity.

A human body should not be a meat proxy between an idea and the world. We
should not spend our lives doing repetitive manual work with our thumbs and
our hands just to make a machine understand us. Clarp is an attempt to
decouple our limbs from the part of human existence that actually matters:
information, creativity, joy, excitement, emotion — the interface through
which we express our intentions and reach for what we want.

Nobody should need their eyes and their fingers to make a computer do
something. Intention should become action with as little friction as is
physically possible, and the goal is to close that distance as far as a
machine will allow.

That means removing the need for a desk. For a keyboard. For a mouse. It also
means removing the repetitive junk we are forced to filter out by hand — the
noise we have to look at and listen to before we reach anything real. We
should be exposed to as much novel information as possible and as little
repetition as we can manage, so that our intentions are fulfilled with the
least friction there is.

Clarp is source-available under the [PolyForm Shield License
1.0.0](LICENSE.md). It may not be used to provide a competing agent-control
product or service. Separate [commercial licensing](COMMERCIAL_LICENSE.md) is
available for uses outside that grant.

Clarp runs AI coding CLIs (Claude Code, Codex, Antigravity, OpenCode) as
agents on your own machine and lets you talk to them from a phone, a desktop
app or a browser over your own network, by voice or text.

- Voice in: a local Whisper model, or a cloud engine you configure.
- Voice out: Cartesia, ElevenLabs, Deepgram, Gemini, Google, a custom adapter,
  or off. A paid fallback is never implicit.
- Agent chat: each turn runs the selected CLI and streams the conversation to
  every client.

The clients are front-ends; Whisper, speech and the CLIs run on the machine
the Host is installed on. It is designed for use over
[Tailscale](https://tailscale.com/), so the phone and the Host can talk
without exposing anything to the public internet, but nothing about it is
Tailscale-specific.

## What is in this repository

| Path | What it is |
| --- | --- |
| `server-rs/` | The Host, in Rust. Start with [server-rs/REBUILD.md](server-rs/REBUILD.md) |
| `contract/` | Wire schemas, fixtures and catalogs the Host and the clients share |
| `skills/` | The skills the Host links for its agents |
| `web/`, `static/` | The web app (deprecated, kept working) and its built assets |
| `desktop-rs/` | The Rust desktop app |
| `src-tauri/` | The Tauri desktop shell |
| `docs/` | The protocol ([docs/protocol.md](docs/protocol.md)) and design notes |

The Host was first written in Python (`server/`). That Host is retired and
was removed from the repository; git history keeps it.

## Install

The Host is built on a build machine and installed from the binaries:

```bash
server-rs/tools/build release        # builds clarp-host and clarp-shim into server-rs/dist
server-rs/tools/install-host --start  # installs them with a systemd user unit
```

The comment at the top of `server-rs/tools/install-host` lists its options (where it listens, the
CLIs it runs, speech, push relay, skills). It installs into
`~/.local/share/clarp-host/` and `~/.config/clarp-host/`, and copies the
repository's `skills/` and the web app's pictures with it.

## Security model

The Host listens on `127.0.0.1:7690` by default and takes its own token only
from the loopback. Put whatever a phone reaches (Tailscale Serve, the managed
relay, an HTTPS reverse proxy) in front of it, and pair each device: a paired
device gets its own bearer token. Be aware of what you expose: a caller who can
send to an agent can make it run code as the user the Host runs as. See
[authentication boundaries and relay trust](docs/security/relay-authentication.md#authentication-boundaries).
