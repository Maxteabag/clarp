# Architecture

Clarp is a Host that runs AI coding CLIs (Claude Code, Codex, Antigravity,
OpenCode) as agents on your machine, and the clients that talk to it: the web
app in this repository, the Rust desktop app (`desktop-rs/`), and the native
iOS and Android apps. This document is the map. The wire contract the clients
follow is in [docs/protocol.md](docs/protocol.md), with its schemas and
fixtures in [contract/](contract/README.md).

## The Host

The Host is written in Rust, in `server-rs/`: one `clarp-host` binary and its
`clarp-shim`, split into crates under `server-rs/crates/`. Read
[server-rs/REBUILD.md](server-rs/REBUILD.md) first: it names the owner, the
rules and who owns which crate. [server-rs/docs/design.md](server-rs/docs/design.md)
is its design. It is built on the build machine through `server-rs/tools/build`
and installed with `server-rs/tools/install-host`, which copies the binaries,
the `skills/` tree, the web app's pictures and a few helper programs
(`scripts/clarp-media-publish.py`, `server-rs/tools/whisper-worker.py`) into
its own directories and writes a systemd user unit.

The earlier Host, written in Python (`server/`), was retired and removed from
the repository; git history keeps it.

`skills/` are the skills the Host links for its agents so they can publish
artifacts, message each other, start helpers and manage the Host from inside a
conversation.

## Clients

- **Web app** (`web/src`, Svelte 5, built by Vite into `static/app/`;
  deprecated, kept working): one
  conversation store per agent (`stores/conversations.svelte.js`) implements
  the sync algorithm in `docs/protocol.md`; `stores/app.svelte.js` owns the
  agent snapshot and the open chat; `stores/sse.svelte.js` turns server
  events into store updates. Pure logic shared with tests lives in
  `static/lib/` (imported as `@core/*`).
- **Desktop** (`desktop-rs/`, Rust and Slint): built and checked only in
  GitHub Actions (see AGENTS.md).
- **iOS and Android**: separate private repositories. They implement the same
  core protocol plus the native surfaces.

## Where things live at runtime

```
~/.config/clarp-host/config.toml        settings and the Host's own token
~/.local/share/clarp-host/bin/          clarp-host and clarp-shim
~/.local/share/clarp-host/data/         the Host's state
~/.local/share/clarp-host/releases/<id>/skills/   the skills of the installed version
~/.local/share/clarp-host/libexec/      helper programs the agents' tools run
~/.local/share/clarp-host/tools/        the agents' own tools (clarp-admin, clarp-sub-agent, ...)
~/.local/share/clarp-host/static/       the web app the Host serves
```
