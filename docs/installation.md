# Installation and distribution

## Bootstrap

Do not clone the repository. Install with:

```bash
curl -fsSL https://maxteabag.github.io/clarp-site/install.sh | bash
```

That downloads a source snapshot, runs the setup wizard, and copies a versioned
release under `~/.local/share/clarp/releases/`. `clarp-admin update` pulls later
releases from GitHub. Developers who want a git checkout can still clone
`https://github.com/Maxteabag/clarp` and run `./setup.sh`.

## Requirements

Supported native hosts:

- Linux x86_64/arm64 with a working systemd user session
- macOS 14 (Sonoma) or newer on Intel or Apple silicon
- `git`, `curl`, and uv on `PATH`
- 4 GB RAM minimum; 8 GB recommended for local transcription
- Internet access while downloading locked runtimes, models, and signing into
  the selected AI providers

Clarp installs without root access. Linux uses XDG configuration/data/cache
directories and `systemd --user`. macOS uses `~/Library/Application Support/Clarp`,
`~/Library/Caches/Clarp`, `~/Library/Logs/Clarp`, and a per-user LaunchAgent in
`~/Library/LaunchAgents`.

`ffmpeg` is optional unless audio delivery is explicitly configured as HLS.
Managed transcription is platform-native: Linux uses Faster-Whisper; macOS
uses `whisper.cpp` with Metal acceleration on Apple silicon and Accelerate on
Intel. The macOS option additionally requires CMake and the Xcode Command Line
Tools (`xcode-select --install`). Clarp builds the pinned runtime itself; it
does not install a Homebrew package.

## Wizard

The setup wizard configures:

1. Claude, Codex, or both.
2. A Clarp-managed pinned toolchain, existing commands, or deferred setup.
3. Clarp Voice, Cartesia, ElevenLabs, Deepgram, iPhone speech, or no voice output.
4. An explicit voice fallback; `none` avoids surprise use of another paid API.
5. Local-only, Tailscale Serve, Bonjour/LAN, or an existing HTTPS address.
6. Bind address, port, and generated authentication for exposed modes.
7. No transcription model, the platform-recommended local model (approximately 488 MB), or
   another supported model id. This is Faster-Whisper on Linux and
   `whisper.cpp` on macOS.
8. Core Clarp skills and optional native iOS or messaging integrations.
9. The platform user service, hooks, and health checks.

With no arguments in a terminal, `setup.sh` asks whether to open the Textual
TUI or the interactive CLI wizard. The routes are also explicit and equivalent:

```bash
./setup.sh --tui
./setup.sh --cli
./setup.sh --non-interactive --help
```

Both wizards and the non-interactive flags invoke the same `clarp-admin setup`
engine; the TUI is not a second installer. `./setup.sh --help` and
`clarp-admin setup --help` contain copy-pasteable automation examples for AI
agents and unattended provisioning.

Setup also seeds `~/.config/clarp/user-values.md` from the generic example.
This optional file describes the installing user's durable values and risk
preferences for team leaders; it is not an authority source and existing
content is never overwritten.

On first server startup, SQLite is created automatically at
`~/.local/share/clarp/state.sqlite` and migrated to the current schema.
When that database has no live agents, setup transactionally seeds the twenty
built-in personas and focuses Mike. Existing databases are never reseeded.

For Antigravity terminal sessions, installation adds a `clarp-status` entry to
`~/.gemini/config/hooks.json`. It reports working and stopped states only for
conversations already registered in Clarp. Start a new CLI process after
installation to load the hooks. Existing unrelated hooks are preserved; a
customized `clarp-status` entry or a symlinked configuration is left untouched.
Clarp-launched turns keep using their existing stream-json status reporting.

The equivalent non-interactive command is:

```bash
./setup.sh --non-interactive --backend both \
  --toolchain managed --transcription recommended \
  --tts cartesia --tts-fallback none --network tailscale \
  --optional-skill clarp-calendar
```

## Voice-output providers

```bash
clarp-admin tts status
clarp-admin tts configure cartesia
clarp-admin tts use cartesia --fallback none
```

Cartesia is the recommended low-latency cloud path. ElevenLabs and Deepgram
remain explicit alternatives, and Clarp Voice routes the same Deepgram voices
through Audio Central so a Computer needs no provider key of its own. Each
cloud provider appears in the picker only once its credential is configured.

Local synthesis is a custom adapter now. The built-in Kokoro and Piper runtimes
were removed: two bundled model stacks with their own installers, licences, and
system prerequisites earned their keep for nobody, and an adapter covers the
same ground without Clarp maintaining the runtime.

`none` marks queued speech complete without calling any provider. Cartesia may
use the lower-latency raw-PCM path; the others use `chunked-file` delivery.

### Custom voice adapters

Clarp discovers validated version-1 adapter packages installed under its
configuration directory at `tts-adapters.d/<adapter-id>/`. Each package must
provide a manifest plus a relative executable implementing the mandatory
`voices`, `preview`, and `synthesize` JSON operations. The server publishes
those providers and their voice catalogues to unmodified iOS clients.

Use the supported manager rather than copying files into the generated release:

```bash
clarp-admin tts adapters validate /path/to/my-adapter
clarp-admin tts adapters install /path/to/my-adapter
clarp-admin tts adapters test custom.my-adapter
clarp-admin tts use custom.my-adapter --fallback none
clarp-admin tts adapters list
```

Adapters are trusted executable code on that Computer. Installation rejects
symlinks, absolute executable paths, reserved provider IDs, oversized packages,
missing operations, and unsupported output formats. Runtime requests use a
minimal environment, bounded responses and audio files, and explicit timeouts.
Provider credentials should remain in adapter-owned configuration, never in the
manifest or the phone app.

### Custom transcription adapters

Speech-to-text providers can be extended without changing the iPhone client.
Version-1 packages live under `stt-adapters.d/<adapter-id>/` and implement the
mandatory `models` and `transcribe` JSON operations. Installed adapter models
appear alongside Whisper choices in Computer Settings.

```bash
clarp-admin transcription adapters validate /path/to/my-stt-adapter
clarp-admin transcription adapters install /path/to/my-stt-adapter
clarp-admin transcription adapters test custom.my-stt
clarp-admin transcription use custom.my-stt:general
```

Installation is transactional and runs a silent-WAV transcription probe before
activation. Adapter executables receive temporary audio paths and the user's
transcription guidance, but never phone authentication credentials.

## Phone networking and pairing

Tailscale is optional:

```bash
clarp-admin network status
clarp-admin network use tailscale
clarp-admin network use lan
clarp-admin network use manual --url https://clarp.example.com
clarp-admin network use off
```

Tailscale mode explicitly configures an HTTPS Serve route to the loopback
server. LAN mode binds with authentication and advertises `_clarp._tcp` over
Bonjour. Manual mode records an existing HTTPS reverse proxy. Clarp does not
configure Tailscale Funnel or public internet exposure.

Create a short-lived QR code after choosing a reachable network mode:

```bash
clarp-admin pair create --url https://computer.tailnet.ts.net
clarp-admin pair list
clarp-admin pair revoke DEVICE_ID
```

The QR contains a single-use bootstrap code, not the administrator token. The
iPhone exchanges it for a device-specific Keychain credential. Codes expire
after ten minutes by default; revocation takes effect on the next request.

## Releases and rollback

```text
~/.local/share/clarp/
├── releases/<git-version>/
│   ├── server.py
│   ├── lib/
│   ├── static/
│   ├── hooks/
│   ├── skills/
│   └── bin/
├── current -> releases/<git-version>
├── environments/<uv-lock-hash>/
├── toolchain/                    # managed mode only
└── state.sqlite
```

Compatibility links (`server.py`, `lib`, `static`, and `scripts`) point through
`current`, so the service definition remains stable. `clarp-admin rollback`
switches the release atomically, restores its exact Python/toolchain metadata,
and restarts systemd or launchd. Configuration and SQLite data are never stored
inside a release.

## Agent toolchains

Managed mode downloads a checksummed Node runtime into Clarp's private data
directory and installs the exact Claude Code and Codex npm packages recorded in
`toolchain/package-lock.json`. The service invokes Clarp-owned wrappers; global
npm and user-managed vendor binaries are not modified. Authentication remains
in the vendors' normal user configuration directories.

Existing mode validates the selected commands on `PATH` and leaves their
versions and upgrades to the user. None mode defers backend setup. Setup never
installs global npm packages silently.

## Managed skills

Clarp owns only individually namespaced `clarp-*` links. Personal skill files
and directories are never replaced.

```bash
clarp-admin skills list
clarp-admin skills install clarp-calendar clarp-location
clarp-admin skills remove clarp-calendar
clarp-admin skills repair-links
```

Canonical skill files live in `current/skills`. The installer links each chosen
skill into both `~/.claude/skills` and `${CODEX_HOME:-~/.codex}/skills`.

The iOS app exposes the same inventory under **Settings → Servers → Computer
settings**. It reports healthy, inactive, missing, outdated, modified/conflicting,
and missing-dependency states. Optional skills can be enabled or disabled there;
core skills remain enabled and can be repaired when a managed link is missing or
outdated. Non-Clarp files are reported as conflicts and never overwritten.

Default core:

- `clarp-media`
- `clarp-sessions`
- `clarp-self-prompt`
- `clarp-background-jobs`
- `clarp-agent-communication`
- `clarp-server-admin`
- `clarp-transcription`
- `clarp-voice-adapters`

Optional packs contain only Clarp-native and messaging integrations. Developer,
web-research, travel, workplace, hardware, and personal skills are deliberately
outside the product.

## Transcription models

Clarp does not scan provider caches or infer installation from OpenAI download
URLs. Its small built-in catalog covers managed Faster-Whisper downloads on
Linux and managed `whisper.cpp` builds/downloads on macOS. Capability discovery
validates the local Clarp registry; transcription never downloads a model
implicitly.

Custom transcription adapters are installed with the `clarp-transcription`
skill and are discovered from the Computer rather than hard-coded in the app.
After validation, each adapter appears under **Computer settings → Voice &
Transcription → Custom transcription adapters**, and its namespaced models
appear in the transcription picker.

## Custom voice adapters

The built-in voice list contains remote services only. Local engines and
private services are installed as versioned custom packages through the
`clarp-voice-adapters` skill. A validated adapter supplies its own voice
catalogue, previews, and synthesis. It then appears automatically as a provider
tab in Contact voice selection and in **Computer settings → Voice &
Transcription → Custom voice adapters**; the iOS app does not need a new release
for each provider.

## Developer telemetry

Detailed diagnostics are disabled by default. Enable them per Computer under
**Settings → Developer Diagnostics** in the iOS app and select only the needed
categories. Operational state remains in `state.sqlite`; opt-in diagnostic
events are written to the isolated `telemetry.sqlite` store. Detail expires
after 24 hours and compact hourly summaries expire after 30 days.

Advanced diagnostic families are independently opt-in: interaction
waterfalls, transcript-import phases, database repetition detection, network
task phases, server/device resource pressure, anomaly bookmarks, and confirmed
shake-to-report markers. The shake gesture never submits by itself; iOS first
asks **Did something feel slow?** and records a timestamped bookmark only after
confirmation. Message content, prompt text, credentials, and bound SQL values
remain outside performance telemetry.

Export the current detail window to Parquet for local DuckDB analysis without
querying the live business database:

```bash
uv run --with duckdb scripts/export_telemetry.py --output telemetry.parquet
```

```bash
clarp-admin transcription list
clarp-admin transcription install faster-whisper:small.en
clarp-admin transcription use faster-whisper:small.en
clarp-admin transcription remove faster-whisper:base.en
clarp-admin transcription test
```

On macOS, use `whisper.cpp:small.en` instead. `recommended` selects the right
provider automatically. Clarp pins `whisper.cpp` b4938 and verifies both its
source archive and GGML model by SHA-256 before building or registering them.
OpenAI's reference Python Whisper runtime is not part of the supported locked
environment or public model catalog.

The setup wizard can skip server models entirely for users who choose Apple
Speech in the iOS app. Model weights remain outside the release package.
Clarp records only models installed or explicitly imported through this manager;
it does not infer product state by scanning arbitrary provider cache files.
Removing a Clarp-downloaded model deletes its managed files. Removing an
explicitly imported model only unregisters it; Clarp never deletes user-owned
external model paths.

In the iOS app these controls live under **Settings → Servers → Computer
settings** for each server. The selection and installed files are scoped to that
computer; Apple Dictation can be selected independently for every server.
Transcription guidance is Computer-owned and split by purpose. Delegated voice
messages can be biased toward names with a live session on that Computer; the
toggle is enabled by default. Ordinary Computer transcription can use the
user's independently editable technical glossary. The settings screen previews
both bounded prompts and their estimated token budgets. Clarp does not ship a
personal name or project glossary, and Apple Dictation bypasses both prompts.

## Claude and Codex sign-in

Each server's Computer settings page shows the authentication state reported by
the CLI installed on that computer. Clarp invokes `claude auth status --json`
and `codex login status`; it never reads or stores their credentials. **Sign in**
starts the CLI's own OAuth or device flow and displays its instructions. Accounts
are server-specific rather than global app settings.

The two CLIs finish differently. Codex uses a device flow: open the URL, enter
the one-time code, and the CLI picks up the result on its own. Claude instead
sends you to a page that *shows* an authorization code, and `claude auth login`
waits for that code on its own standard input. Clarp keeps the login process
alive, detects its paste prompt, and gives you a field to paste the code into;
it is forwarded to the CLI, which then completes the exchange itself. Nothing
about the code is stored — the CLI still owns the resulting credentials.

## Maintenance

```bash
clarp-admin doctor
clarp-admin update
clarp-admin update --ref v0.5.0
clarp-admin rollback
clarp-admin uninstall
```

The original bootstrap checkout may be removed after setup. Clarp records its
Git remote and creates a managed update checkout automatically if the original
source directory is no longer available.

Updates stage a detached Git worktree, install the new version beside the old
one, activate it, repair managed links, and restart. A failed installation does
not overwrite configuration or conversation state.

`clarp-admin uninstall` removes managed runtime files and links while preserving
configuration and conversations. `--purge-data` is the explicit destructive
variant.
