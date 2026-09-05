# Clarp Native Desktop

Native Qt 6.11 desktop client for Clarp. It uses Qt Quick/QML for presentation
and C++20 for protocol, state, networking, credentials, and media. It does not
link Qt WebEngine or embed the PWA.

The native client includes multi-pane conversations, revision-safe transcript
sync, resumable SSE, agent lifecycle and schedule controls, voice selection,
microphone transcription, authenticated audio playback, system notifications,
tray controls, MPRIS media-key integration, a single-instance guard, and
native Secret Service credential storage.

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
The expanded sidebar starts at 232 logical pixels; dragging its divider saves
your preferred width across hiding and relaunching.

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

Enable **Settings → Experiments → Plain-English tool activity**, or search for
**plain-English** in `Ctrl+K`. It is **off by default**. When enabled, visible
activity is explained in blue by a local background worker running
`codex exec --model gpt-6-astra` with low reasoning effort. This uses the desktop's
Codex login and consumes additional Codex usage; no Host deployment is required.

The worker sends bounded tool-command snippets, not the chat transcript or command
results. Common inline credentials are redacted on a best-effort basis; do not
enable it for tool inputs that must not be sent to OpenAI. Explanations describe
operations, not guaranteed intent or success. The original status stays visible;
expand the row (hover for live activity) to read the original details.

Requests are batched, deduplicated, and cached in memory (512 entries). Only one
Codex process runs at a time. Original rows remain available while waiting or if
translation fails. Failures pause new requests until you toggle off/on; disabling
cancels the worker and restores the normal view immediately. Switching Hosts
clears the cache. Translations are not written into the conversation transcript.

The invocation uses a private temporary working directory, read-only sandbox,
disabled shell/browser/plugin integrations, no personal hooks or project
instructions, an ephemeral session, and structured output. See the official
[non-interactive Codex documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

`clarp-tool-narrator-tests --live-smoke` is a separate, opt-in real-model check.
The regular test suite uses a fake subprocess and never consumes model usage.

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
