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

A Progressive Web App for talking to [Claude Code](https://www.claude.com/product/claude-code)
from a phone (or any device with a browser) over your own network.

Native setup is the default. An optional self-contained Docker node is
documented in [docs/docker.md](docs/docker.md); each container has one private
data volume and can be added to iOS as an independent Clarp server.

- Voice in: managed local Whisper — Faster-Whisper on Linux and Metal-enabled
  `whisper.cpp` on macOS.
- Voice out: explicit Clarp Voice, Cartesia, ElevenLabs, Deepgram, a custom
  adapter, or disabled. Paid-provider fallback is never implicit.
- Agent chat: each submitted turn launches the selected AI CLI backend and
  streams conversation updates back to the client.

The PWA and native iOS client are front-ends; Whisper, TTS, and AI CLI
backends run on the host you install the server on. Designed for use over
[Tailscale](https://tailscale.com/) so your phone and the host can talk to
each other without exposing anything to the public internet, but nothing
about it is Tailscale-specific.

Linux users can download the native Qt desktop client as a self-contained
AppImage from the [latest GitHub release](https://github.com/Maxteabag/clarp/releases/latest).
It does not require Qt development packages:

```bash
chmod +x Clarp-*-x86_64.AppImage
./Clarp-*-x86_64.AppImage
```

## Quick start

Requirements:

- Linux with a systemd user session, or macOS 14+
- x86_64 or arm64
- `curl` (the installer fetches [uv](https://docs.astral.sh/uv/) if needed)
- 4 GB RAM minimum; 8 GB recommended for local transcription
- Internet access during installation and provider sign-in

```bash
curl -fsSL https://maxteabag.github.io/clarp-site/install.sh | bash
```

Or Docker:

```bash
docker run -d --name clarp --restart unless-stopped \
  -p 127.0.0.1:7682:7682 -v clarp-data:/data \
  ghcr.io/maxteabag/clarp:stable
```

With a real terminal, `setup.sh` opens the Textual setup interface. Choose the
agent tools, transcription, voice provider, and phone-network mode there. After
setup, use **Pair iPhone** to scan a short-lived one-time QR code in the Clarp
app. Cloud voice credentials are requested only for the provider you select.

`setup.sh` is the recommended bootstrap. It creates a lock-hash Clarp Python
environment with uv and opens `clarp-tui`. The TUI explicitly selects
Claude/Codex, a Clarp-managed or existing agent toolchain, networking,
transcription (including download size), and optional integration skills.
`clarp-admin` remains the automation and recovery interface.

For automation:

```bash
./setup.sh --non-interactive \
  --backend both \
  --toolchain managed \
  --transcription faster-whisper:small.en \
  --tts cartesia --tts-fallback none \
  --network off \
  --bind 127.0.0.1
```

See [Installation and distribution](docs/installation.md) for the complete
wizard, release, managed-skills, model, update, rollback, and uninstall model.

`install.sh` is idempotent. It never uses system Python packages or global npm,
never writes Claude settings, and creates a default configuration only when one
does not already exist.

## Updating

Use the installed updater:

```bash
clarp-admin update
```

`clarp-admin rollback` atomically activates the previous installed release.

`setup.sh` (or `install.sh`) installs a versioned server release, locked Python
environment, managed skill links, and either a systemd user service or macOS
LaunchAgent. The
service restart picks up server-side code changes; static asset
changes (CSS / JS / HTML) trigger an automatic reload in any
already-open PWA via the `SERVER_VERSION` SSE event the server
broadcasts on boot (driven by the newest static-file mtime).

### Golden rule: never hand-edit the installed copy

The running server executes from the platform data directory (`~/.local/share/clarp`
on Linux; `~/Library/Application Support/Clarp` on macOS), which is a
**generated copy** — it is not under
version control and is overwritten wholesale on every deploy. Editing it
directly (or `python … patch the installed lib/*.py`) will silently get clobbered
and drifts from the repo.

Always edit in the worktree and deploy:

```bash
cd /path/to/clarp
make deploy        # sync locked runtime + release, then restart the user service
```

`make deploy` deploys whatever is in the worktree (committed *or* not), so it's
the one command to use after any server change. Verify it took:

```bash
clarp-admin doctor
```

### Detached deploys for voice-mode work (Linux only)

On Linux, voice-driven agent sessions can use the detached deploy target.
Foreground tool calls can be interrupted by a new voice turn, while the
detached deploy runs as its own transient systemd user unit and keeps writing
to a durable log. On macOS, use the cross-platform foreground `make deploy`
command above; the detached helper requires systemd.

```bash
cd /path/to/clarp
make deploy-detached    # starts claude-pwa-deploy.service and returns immediately
make deploy-status      # systemd status plus latest deploy log lines
make deploy-log         # latest deploy log only
```

The deploy log is written to `~/.cache/clarp/deploy.log`. The detached
target still runs the normal `make deploy`, so it copies the generated server
and static files into `~/.local/share/clarp/` and restarts
`clarp.service`; it only changes how the deploy process is launched.

`make deploy-detached` only detaches the updater. Agent turns run in the
separate `clarp-runtime` service, so restarting `clarp.service` does not stop,
resume, or inject another prompt into them. Runtime-affecting releases are
adopted automatically at the next idle boundary; existing turns finish on the
runtime version that started them.

If you only changed static assets and don't want to bounce the
service, you can sync just the static dir:

```bash
rsync -a --delete ~/GIT/clarp/static/ ~/.local/share/clarp/static/
systemctl --user restart clarp.service   # to push SERVER_VERSION
```

Tail the service log if anything looks wrong:

```bash
journalctl --user -u clarp.service -f
```

## Build & test

New here? [ARCHITECTURE.md](ARCHITECTURE.md) is the map,
[CONTRIBUTING.md](CONTRIBUTING.md) the development loop, and
[docs/protocol.md](docs/protocol.md) the contract every client follows.

The server is plain Python — there's no compile step, so "build" just means
running the tests and deploying with `install.sh`. A `Makefile` wraps the
common tasks:

```bash
make test         # pytest (server, parallel) + vitest (static JS)
make py           # pytest only
make js           # vitest only
make e2e          # Playwright against a throwaway Docker node
make docker-test  # build the image; exercise install, restart, backup
make deploy       # sync the locked environment and install a new release
make deploy-detached        # detached deploy; active agent turns continue
make deploy-status
```

Typical loop after editing server or static code:

```bash
make test                                      # confirm nothing broke
make deploy-detached                           # detached server deploy; runtime continues
make deploy-status                             # confirm the detached unit exited cleanly
```

A quick syntax sanity check without the full suite:

```bash
python -m py_compile server/server.py server/lib/*.py
```

See [**Updating**](#updating) above for the full deploy details (what
`install.sh` copies, the static-only fast path, and the live-reload SSE event).

## Finding & Resuming Sessions

To find current or past session IDs (the `backend_session_id`) for any agent, use the helper script:

```bash
./scripts/find-session.sh [agent_name]
```

For example, to list sessions for the agent `Bella`:
```bash
./scripts/find-session.sh Bella
```

### Resuming a session

Once you have the session ID, you can resume it using one of the following methods:

#### Method A: Via the Web UI (PWA)
1. Next to the agent's name in the web interface, click the relaunch button (**`↻`**).
2. Choose **Resume** from the dialog options.
3. Select the target session ID from the list and confirm.

#### Method B: Via Direct HTTP cURL Request
You can POST a resume payload directly to the server's `/send` endpoint:
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"session": "<app_session_name>", "text": "continue", "hands_free": false, "synthesize_audio": true}' \
  http://localhost:7682/send
```

## Scheduling a prompt to an agent (self-prompting)

There's no separate scheduler API — an agent can hand itself (or another agent)
a prompt at a future time by combining an OS timer with the same `/send`
endpoint the PWA uses. `force_session: true` skips the orchestrator and delivers
the text straight into that agent's session (via `tmux send-keys`), exactly as
if you'd typed it.

```bash
# Send a prompt to a specific agent (session = the agent's app session id, e.g. "antoni").
# Auth: omit the header if server.auth_token is empty in ~/.config/clarp/config.toml.
curl -X POST "http://localhost:7682/send" \
  -H "Authorization: Bearer $CLAUDE_PWA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Do the thing we deferred.", "session": "antoni", "force_session": true}'
```

To make it fire later, wrap that call in a one-shot timer. Example — a
persistent systemd timer (survives reboots; `Persistent=true` catches up if the
box was off at fire time):

```ini
# /etc/systemd/system/my-reminder.service   (Type=oneshot, runs as your user)
[Service]
Type=oneshot
User=youruser
Environment=HOME=/home/youruser
ExecStart=/usr/bin/curl -fsS -X POST http://localhost:7682/send \
  -H "Authorization: Bearer THE_TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"<prompt>","session":"<agent-id>","force_session":true}'

# /etc/systemd/system/my-reminder.timer
[Timer]
OnCalendar=2026-06-19 17:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now my-reminder.timer
systemctl list-timers my-reminder.timer   # verify next fire
```

Notes:
- `session` is the agent's app session id (the persona/agent name shown in the UI).
- For an autonomous task that doesn't actually need an agent in the loop (e.g. a
  build/deploy that's just a `git push`), prefer having the timer run the script
  directly — only route through `/send` when you want the *agent* to act/reason.
- The `/send` call is gated by `server.auth_token`; keep that token secret since
  it permits prompt-injection into any agent.

## Configuration

Everything deployment-specific lives in `~/.config/clarp/config.toml`.
See `config.example.toml` for the full schema. The short version:

```toml
[server]
bind_addr  = "127.0.0.1"      # safe default; see below
port       = 7682
auth_token = ""               # set to a random string to require auth

[elevenlabs]
api_key = "sk_..."            # or via ELEVEN_API_KEY env var
model   = "eleven_flash_v2_5"
speed   = 1.2

[roster]
Mike   = "nPczCjzI2devNBz1zQrb"
Rachel = "21m00Tcm4TlvDq8ikWAM"
# ... persona name → ElevenLabs voice id
```

## Security model

By default the server binds to `127.0.0.1:7682` and runs without auth. That is
safe for local-only use, but a phone cannot reach it until you deliberately
expose it through Tailscale Serve, another reverse proxy, or a Tailnet address.
**Be aware of what you're exposing**:

- `/send` lets a caller submit any text into the active Claude Code
  session. That session is spawned with `clarp -p --dangerously-skip-permissions`
  (clarp wraps `claude -p` so we get token-level streaming output natively),
  so a caller can effectively run arbitrary code as the user.

For phone access, use both of these:

1. Keep `bind_addr = "127.0.0.1"` and put `tailscale serve` (or any other
   reverse proxy that handles auth) in front of `:7682`, or deliberately bind
   to this machine's Tailnet address.
2. Set `auth_token = "<a long random string>"`. Every request then needs
   `Authorization: Bearer <token>` or `?token=<token>`. The PWA picks the
   token up from `?token=` on first visit and stores it in `localStorage`,
   so the URL you bookmark is the one with the token in the query string.
   Setup prints that link, and `clarp-admin url` (or `clarp-admin url --qr`)
   prints it again. If the PWA says the server rejected the token, open the
   link once more.

Generate a token with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Layout

| Path             | Installed location                            |
| ---------------- | --------------------------------------------- |
| `server/`        | `~/.local/share/clarp/`                  |
| `static/`        | `~/.local/share/clarp/static/`           |
| `systemd/`       | `~/.config/systemd/user/`                     |
| `bin/`           | `~/.local/bin/`                               |
| `hooks/*.py`     | `~/.claude/hooks/`                            |
| `commands/`      | `~/.claude/commands/`                         |
| `skills/`        | `~/.claude/skills/`                           |
| `config.example.toml` → `~/.config/clarp/config.toml` (first install) |

## Components

- **`server/server.py`** — companion HTTP server. Routes: `/` (PWA shell),
  `/events` (SSE for audio clips), `/audio/<id>`, `/send` (voice text →
  session), `/transcribe` (faster-whisper with `vad_filter=True`),
  `/sessions`, `/select`, `/agents`, `/agents/snapshot`.
- **`static/`** — PWA front-end. Service worker with auto-update, session
  picker, granular playback-speed control, always-on Whisper-VAD recording,
  iOS audio-unlock on first tap.
- **`systemd/clarp.service`** — runs `server.py`. The ExecStart is
  templated at install time with the Python interpreter you used (defaults
  to `/usr/bin/env python3`; override with `PYTHON=/path/to/python ./install.sh`).
- **`hooks/`** — Claude Code UserPromptSubmit / PreToolUse / PostToolUse /
  Stop / PreCompact / Notification / SubagentStop hooks. They record agent
  state and live tool activity so the PWA can show what an agent is doing.
  They do not produce audio.

## Audio

Audio is driven server-side, not by hooks. The transcript streamer tails
each agent's JSONL, enqueues `<speak>` regions into `tts_queue`, and the
TTS worker synthesizes them into `~/.cache/clarp/pwa/`, from where
the configured delivery (chunked-file + broker, or HLS) reaches the
client.

Muting is per turn, per client: the PWA and iOS app send
`synthesize_audio: false` with `/send` and the queue drops the utterance
before anything is synthesized, so nothing is billed.
