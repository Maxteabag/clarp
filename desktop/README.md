# Clarp Native Desktop

Native Qt 6.11 desktop client for Clarp. It uses Qt Quick/QML for presentation
and C++20 for protocol, state, networking, credentials, and media. It does not
link Qt WebEngine or embed the PWA.

The native client includes multi-pane conversations, revision-safe transcript
sync, resumable SSE, agent lifecycle and schedule controls, voice selection,
microphone transcription, authenticated audio playback, system notifications,
tray controls, MPRIS media-key integration, multiple independent instances, and
native Secret Service credential storage.

Each launch opens a new window with the sidebar collapsed, including when an
older saved preference had it expanded. Ctrl+B toggles the sidebar; Ctrl+K opens
the command/agent picker. Chat uses square edges, monospace text, text-only
controls and author names, and an empty composer without placeholder text or an
inner border. The full composer height accepts typing focus and uses a blinking
block caret. Ctrl+Shift+K shows/hides the keybinding bar and remembers that choice.
The header shows the Host's status label, configured model and effort, and the
working directory. Shared local paths are classified from Git metadata as a
repository or linked worktree; remote paths remain directory labels. The More
menu and workspace context use compact icons.

Each window is independent. Instances share saved preferences, pane layout,
and conversation drafts; the latest write to the same saved item wins. Closing
or quitting one instance does not close the others.

Automatic voice replies have one playback owner per desktop user and Host
credential. Play/pause/stop and mute controls in every window target that owner;
changing focus does not move playback. A replacement owner recovers unstarted
queued clips from a private local journal. Replies already started are not
replayed automatically after a crash, and the latest 4,096 terminal clip IDs
remain deduplicated across restarts. This coordinates this desktop's windows;
it does not arbitrate playback with a phone or another computer. The desktop
session bus must be available; playback stays stopped if coordination cannot
start. Screenshot captures never join live audio coordination.

Recording belongs to the window and conversation where it starts. Switching
focus does not retarget its transcription. Only one Clarp window can hold the
microphone, regardless of Host; other windows show an error when asked to record.
Queued reply playback waits until recording ends. Changing the recording
window's Host or credentials cancels its recording and pending transcriptions.
The microphone shortcut remains local to the focused window.

## Install a bundled release

Download `Clarp-<version>-x86_64.AppImage` from the
[latest GitHub release](https://github.com/Maxteabag/clarp/releases/latest),
make it executable, and run it:

```bash
chmod +x Clarp-*-x86_64.AppImage
./Clarp-*-x86_64.AppImage
```

The AppImage includes Qt, QML, and multimedia libraries. If FUSE is not
available, run it with `APPIMAGE_EXTRACT_AND_RUN=1`. A `.flatpak` bundle is
also attached to tagged releases and can be installed with
`flatpak install ./Clarp-*-x86_64.flatpak`.

## Building from source

Requirements:

- CMake 3.22+
- Ninja
- Qt 6.11: Core, GUI, QML, Quick Controls, Network, Multimedia, SVG, and Test
- A C++20 compiler
- A freedesktop Secret Service provider (for example GNOME Keyring or KWallet)
  for persistent paired-device credentials

On Arch Linux:

```bash
sudo pacman -S --needed cmake ninja clang qt6-base qt6-declarative \
  qt6-multimedia qt6-multimedia-ffmpeg qt6-svg qt6-wayland gnome-keyring
```

## Build and run

```bash
cd desktop
cmake --preset dev
cmake --build --preset dev
ctest --preset dev
./build/dev/clarp-desktop
```

The client defaults to `http://127.0.0.1:7682`. A local native installation
reads the existing administrator token from `~/.config/clarp/config.toml` when
present. Remote installations should use `clarp-admin pair create` and enter
the one-time `clp_…` code; the resulting revocable `cld_…` credential is kept
through the freedesktop Secret Service DBus API.

Environment overrides are useful for development:

```bash
CLARP_BASE_URL=https://computer.example.ts.net CLARP_TOKEN=cld_… \
  ./build/dev/clarp-desktop
```

`Ctrl+B` switches between the full sidebar with agent names and no sidebar.
This preference is remembered between launches. The header hide button and
the command palette's **Hide sidebar** / **Show sidebar** use the same toggle.
The expanded sidebar starts at 354 logical pixels, including the narrow navigation
strip and searchable conversation list. Dragging its divider saves your preferred
width; Ctrl+B hides/restores both parts together. Older default-width preferences
migrate once, while custom widths remain subject to the new 298-pixel minimum.

The messenger-style redesign is integrated on top of the keyboard workspace:
search and All/Unread filtering, archive access, conversation previews and dates,
round antialiased portraits, right-aligned user bubbles, and a softer composer.
It retains the native streaming/Markdown renderer, semantic tool cells, Spark
explanations and audience dial, pane drafts, Settings, and the existing shortcuts.
Portrait rounding uses bounded native image processing, including under the
software renderer, rather than depending on a GPU-only mask.

`Ctrl+Alt+T` opens the active agent in the OS's default terminal window, using
its working directory and exact native conversation ID. Claude, Codex, AGY,
and Grok run their own interactive interfaces. This requires a local/shared
filesystem Host and the corresponding CLI installed on the desktop. Chat
continues independently; reopening the terminal picks up later chat changes.

To start an idle contact, press `Ctrl+Alt+N` for the idle-contact picker, or
press `Ctrl+K`, type their name, and select **Start
<name>**. The row shows the saved backend and folder. **Start** in the ready
contacts view does the same one-step fresh launch; use `Ctrl+N` to change launch
options. The new chat receives typing focus after creation.

Your own chat messages use a lighter background instead of a left accent line.

Settings is keyboard-first: `Ctrl+,` opens it and focuses the last-used setting.
Use `↑`/`↓`, `J`/`K`, or `Tab`/`Shift+Tab` to move through actionable rows;
`Home`/`End` jump to the first/last. `Space`/`Enter` toggles a value or opens a
link; `←`/`→` explicitly turns a toggle off/on. The focused row is highlighted
and automatically scrolled into view. `Esc` closes a settings dialog first,
then returns to the chat input. Closing the command palette restores the same
setting, and UI zoom keeps keyboard focus in Settings.

### Experimental plain-English tools

Use **Settings → Experiments → Tool detail**, a five-stop keyboard-friendly dial:

- **Developer:** original tool calls; no translation requests.
- **Technical:** precise commands, paths and terminology with an explanation of their effect.
- **Balanced:** useful technical context without shell syntax.
- **Plain English:** everyday task descriptions without implementation jargon.
- **Grandma:** short, concrete descriptions with no assumed technical background.

Focus the dial and use `←`/`→`, or click/drag it. Each translated level has unique
audience instructions. The selection is saved; existing enabled installations
migrate to Plain English, while disabled installations stay at Developer. A level
change cancels in-flight work and discards cached translations from another style.
The **plain-English** command in `Ctrl+K` toggles Developer/the last translated level.
When translation is enabled, visible
activity is explained in blue by a local background worker running
`codex exec --model gpt-5.3-codex-spark` with low reasoning effort. This uses the desktop's
Codex login and consumes additional Codex usage; no Host deployment is required.

The worker sends bounded tool-command snippets, not the chat transcript or command
results. For a shared-filesystem Host it also reads bounded excerpts of directly
referenced local scripts so explanations describe their purpose, not just their
filename or programming language. It does not execute scripts, follow imports,
or read hidden paths, symlinks, binaries, or files larger than 64 KiB. Missing
source is treated as missing evidence, not permission to guess. Common inline credentials are redacted on a best-effort basis; do not
enable it for tool inputs that must not be sent to OpenAI. Explanations describe
operations, not guaranteed intent or success. The original status stays visible;
expand the row (hover for live activity) to read the original details.

Requests are batched, deduplicated, and cached in memory (512 entries). Only one
Codex process runs at a time. Raw commands stay hidden while waiting: rows show
cycling `.`, `..`, `…` until ready, or “Explanation unavailable” on failure.
Failures pause new requests until you toggle off/on; disabling
cancels the worker and restores the normal view immediately. Switching Hosts
clears the cache. Translations are not written into the conversation transcript.

The invocation uses a private temporary working directory, read-only sandbox,
disabled shell/browser/plugin integrations, no personal hooks or project
instructions, an ephemeral session, and structured output. See the official
[non-interactive Codex documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

`clarp-tool-narrator-tests --live-smoke` is a separate, opt-in real-model check.
The regular test suite uses a fake subprocess and never consumes model usage.

The translator status shows elapsed seconds and queued activity count. Metadata-only
timings are recorded in `diagnostics/tool-narrator.jsonl` under the desktop's local
application-data directory, capped at 256 KiB. Events distinguish queue wait,
process startup, Codex thread/turn events, process completion, cancellation and
failure; no command, script, prompt, or generated explanation is logged. This
measures latency; it does not remove the current serial-batch/full-response delay.
Use `clarp-tool-narrator-tests --live-script-smoke` for an explicit real-model
script-purpose check without executing the fixture script.

## Quality gates

```bash
cmake --preset release
cmake --build --preset release

CC=clang CXX=clang++ cmake --preset sanitizers
cmake --build --preset sanitizers
ctest --preset sanitizers

cmake --preset analysis
cmake --build --preset analysis

cmake --build --preset dev --target all_qmllint
```

Set `CLARP_SCREENSHOT_PATH=/tmp/clarp.png` to run a deterministic two-second
visual smoke capture. For headless CI, also set `QT_QPA_PLATFORM=offscreen` and
`QT_QUICK_BACKEND=software`.

The default scene graph uses GPU acceleration. On the development NVIDIA/
Wayland system, a live release build measured about 129 MB PSS with the normal
GPU backend and about 87 MB PSS with `QT_QUICK_BACKEND=software`; those are
environment-specific reference numbers, not fixed requirements. The software
backend is a useful low-memory fallback when GPU throughput matters less.

## Distribution

Every `v*` tag whose version matches `pyproject.toml` runs the native quality
gates and publishes a versioned AppImage, `.zsync`, SHA-256 checksum, and
sideloadable Flatpak to the corresponding GitHub Release. The release builder
uses `packaging/appimage/build-in-flatpak.sh`, pinned packaging tools, and the
KDE 6.11 SDK.

- Flatpak is the primary sandboxed channel:

  ```bash
  flatpak-builder --user --force-clean --install build/flatpak \
    packaging/flatpak/com.maxteabag.Clarp.yml
  flatpak run com.maxteabag.Clarp
  ```

- `packaging/appimage/build-appimage.sh` creates a portable AppImage using
  `linuxdeploy` and `linuxdeploy-plugin-qt`. Build it in a stable Qt 6.11 SDK,
  not against an accidentally partial rolling-distribution upgrade. Set
  `CLARP_APPIMAGE_RUNTIME_FILE=/path/to/runtime-x86_64` for a fully offline
  build; otherwise `appimagetool` downloads its runtime.
- `packaging/aur/PKGBUILD` and `.SRCINFO` follow Clarp's repository version and
  are ready for AUR publication once the matching source tag exists. Replace
  `SKIP` with the release archive
  checksum before publishing.

AppImage releases must include a checksum and the dependency-license inventory
described in `THIRD_PARTY_NOTICES.md`.

See `REWRITE_PLAN.md` for the behavioral scope and completion gates.
