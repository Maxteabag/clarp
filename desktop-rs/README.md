# Clarp desktop (Rust, Slint)

The native Clarp desktop client: a Rust app with a Slint UI, no Qt.

| Crate | What it is |
|---|---|
| `core` | protocol types, reducers, conversation sync, settings, launch options |
| `net` | HTTP/SSE client for the Host and keyring credentials |
| `engine` | UI-neutral app state and commands on top of `core`/`net` |
| `switcher-slint` | the quick switcher's ranking |
| `slint-app` | the window (`clarp-slint`): Slint views over `engine` plus the desktop platform (audio, tray, MPRIS, notifications) |

`SLINT_PARITY.md` maps every part of the former QML UI to its Slint
implementation and the check that verifies it.

## Build and run

```sh
cargo build --release -p clarp-slint
./target/release/clarp-slint            # connects to the Host in your settings
./target/release/clarp-slint --help
```

## Check

Never point checks at the real Host: they connect, select and send. The
checks below run against `tests/fake_host.py` (fixtures in `tests/fixtures/`)
with no display, a private bus (`tests/private-bus.conf`) and scratch config.

```sh
cargo build --workspace && cargo test --workspace
slint-app/tests/check.sh NAME [OUT]      # e.g. startup, artifacts, scroll, transcript, composer,
                                         # panes, sidebar, updates, launch, profile, extras, voice
slint-app/tests/run-e2e.sh [OUT]         # against the real server (tests/qa/host.py) in a network namespace
slint-app/tests/shot.sh OUT.png SESSION  # one screenshot
slint-app/tests/perf.sh [RUNS]           # launch-to-first-chat time and memory (release build)
net/tests/run-keyring-tests.sh           # keyring against a throwaway gnome-keyring
```

`check.sh` prints `ok`/`FAIL` lines; read those, not the exit code alone.
Screenshots land in `slint-app/docs/checks/` by default.

## Install

On Peter's machines `clarpd` (in dotfiles) builds `main`'s `desktop-rs` in a
private worktree when the installed build is older, installs the stripped
binary to `~/.local/lib/clarp-slint/clarp-slint`, and opens it.
`clarpd --no-update` opens the installed build.
