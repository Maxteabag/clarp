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

## Settings

Every preference (typography, layout, colours per reading theme,
behaviour) is one entry in `core/src/prefs.rs`: a short name, its key in the
settings file, a label, a one-line description, aliases, a section, a kind
and a default. Ctrl+K (`+`/`-` step the selected one), the Settings view
(Left/Right or `-`/`+` step, Enter edits, Backspace resets, Shift+Backspace
resets the section), `:set name=value`, export/import and the settings
file's JSON schema and reference are all made from that list, so a new
setting is added there once; `core/tests/prefs.rs` refuses one without a
description or two aliases. The window reloads the settings file
(`~/.config/MaxTeaBag/ClarpSlint/settings.json`) when it changes and reports
invalid values instead of applying them. `slint-app/src/look.rs` applies the
values; `--check customize` exercises them.

Chat zoom (`chatzoom`, 60–250% in 10% steps, kept in the settings file) draws
the chats' text, code, cards and spacing larger or smaller apart from the
window: Ctrl+= (or Ctrl++), Ctrl+- and Ctrl+0, or Ctrl+wheel over a chat; the
rows are measured again and the reader keeps their place. The whole window's
scale (the chats with it) is Ctrl+Alt+PageUp / Ctrl+Alt+PageDown / Ctrl+Alt+0.

Slint's rich text has no line height: "Paragraph spacing" (alias line
spacing) sets the space between a message's paragraphs, lists, code and
headings, while lines inside one paragraph keep the font's own spacing.
"Text weight" reaches interface text without a weight of its own; message
text keeps the face's regular weight.

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
