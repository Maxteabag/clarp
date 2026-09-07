# Architecture

Clarp is a small server that runs AI coding CLIs (Claude Code, Codex,
Antigravity) as agents on your machine, and two clients that talk to it: the
PWA in this repository and a native iOS app. This document is the map. The
wire contract the clients follow is in [docs/protocol.md](docs/protocol.md).

## One turn, end to end

```
  phone / browser             clarp.service          clarp-runtime.service          host
  ───────────────             ─────────────          ─────────────────────          ────
  POST /transcribe ──► stt.py (Whisper) ──► text
  POST /send ────────► Unix socket ──────► turn_dispatch.py ──spawn──► claude -p … --plugin-dir plugin/
                                                                             │
                       hooks (plugin/hooks/*.py) ◄── UserPromptSubmit/Pre/PostToolUse/Stop/…
                       write state_log + messages rows into state.sqlite
                                │
  SSE /events ◄───── audio_stream.py broadcasts agent-state / agent-activity / transcript-updated
  GET /log ◄──────── conversation.py reads message_store.py (SQLite is the read model)
  audio ◄──────────── tts_worker.py ──► provider (Cartesia / ElevenLabs / Deepgram / custom) ──► clip_delivery/
```

- **A turn is one subprocess.** `/send` launches the backend CLI for that
  message through `clarp-runtime`. The replaceable HTTP server never owns that
  subprocess. Restarting `clarp.service` reconnects to the same runtime and
  cannot terminate the turn.
- **Runtime releases roll at idle boundaries.** A runtime-affecting release is
  identified separately from server/static changes. The old runtime keeps
  accepting and finishing work until it reaches an idle boundary, fences new
  admissions, records a clean handoff, and restarts on the new version. Only a
  runtime crash produces interruption markers and continuity prompts.
- **SQLite is the source of truth.** `~/.local/share/clarp/state.sqlite` holds
  agents, runtimes, turns, messages, clips, queues, and settings. Hooks
  (separate processes) and the server share it through WAL mode. The schema
  is `_SCHEMA_SQL` in `server/lib/db.py`; changes bump the version and add
  one `_migrate_to_vN`.
- **Hooks are sensors.** `plugin/hooks/` is a Claude Code plugin the server
  passes with `--plugin-dir`. Each hook records what happened (prompt
  submitted, tool started, tool finished, turn stopped, compaction, waiting)
  and exits 0. It never decides permissions.
- **Clients never parse transcripts.** Backend transcript files are imported
  into `messages` by `transcript_log.py` (Claude), `codex_transcript.py`, and
  `agy_transcript.py`; every client reads the same `/log`.

## Server layout

`server/server.py` is the HTTP layer: a `ThreadingHTTPServer` with one route
table per method, bearer or paired-device auth, SSE, and static files. It
should hold no business logic; handlers delegate to `server/lib/`.

| Area | Modules |
|---|---|
| Persistence | `db.py` (schema, connections, migrations), `settings_store.py`, `maintenance.py` (pruning), `instance_backup.py` |
| Agents and turns | `agents.py`, `agent_lifecycle.py` (create/relaunch/fork/delete), `turn_dispatch.py` (queue, preemption, retries), `turn_queue.py`, `reconcile.py` (repairs drifted state on read), `snapshot.py` (`/agents/snapshot`) |
| Runtime boundary | `runtime.py`, `runtime_bridge.py` (versioned private RPC), `runtime_events.py` (durable cross-process SSE relay), `runtime_release.py` (idle rolling handoff), `runtime_startup.py` (crash-only recovery) |
| Backends | `backends.py` (adapter selection), `clarp_runner.py` (Claude), `codex_runner.py` + `codex_app_server.py`, `agy_runner.py`, `provider_capabilities.py` (model catalogue), `backend_auth.py`, `backend_usage.py` |
| Conversation read model | `message_store.py`, `conversation.py`, `transcript_log.py`, `codex_transcript.py`, `agy_transcript.py`, `transcript_watcher.py` + `transcript_streamer.py` (live text while a turn runs), `activity.py` |
| Events | `audio_stream.py` (SSE hub + clip janitor), `state_watcher.py`, `eventlog.py` + `telemetry.py` (diagnostics into `telemetry.sqlite`) |
| Voice in | `stt.py`, `whispercpp.py`, `transcription_models.py`, `vocab.py` + `vocab_budget.py` + `vocab_generators.py` + `vocab_compile.py` + `vocab_store.py` + `workspace_vocab.py` (budget-fitted context packs for the transcription prompt, every compile recorded in `vocab_runs`), `stt_providers.py` + `deepgram_stt.py` + `eleven_stt.py` + `cartesia_stt.py` (cloud engines and the engine / turn-taking switches), `hallucinations.py`, `custom_stt_adapters.py` |
| Voice out | `tts_worker.py`, `tts_queue.py`, `tts_engine.py`, `tts_providers.py`, `cartesia_*.py`, `eleven_*.py`, `deepgram_*.py`, `custom_tts_adapters.py`, `voice_markup.py`, `clip_delivery/` (HLS, chunked HTTP, raw PCM), `clip_store.py`, `audio_growing.py` |
| Routing of spoken input | `routing.py` (name matching), `orchestrator.py` (LLM router for hands-free), `herald.py` (which agent's clip plays when several reply) |
| Autonomy | `heartbeat.py`, `dreaming.py`, `team_store.py` + `team_leader.py` + `leader_memory.py`, `background_jobs.py`, `task_plans.py` |
| Native-app surfaces | `device_pairing.py`, `apns.py`, `user_notifications.py`, `artifacts.py`, `media_store.py`, `agent_portraits.py` + `portrait_generation.py`, `personas.py`, `location.py`, `calendar_request.py`, `prompt_history.py` |
| Install and ops | `config.py`, `paths.py`, `xdg.py`, `deployment.py`, `service_manager.py`, `server_update.py`, `managed_skills.py`, `personal_skills.py`, `bonjour.py`, `server_identity.py` |

`bin/clarp-admin.py` is the install, update, pairing, and diagnostics CLI;
`bin/clarp-tui.py` is the setup wizard. `skills/` are Claude Code skills the
server installs for agents so they can publish artifacts, message each other,
and manage the server from inside a conversation.

## Feature tiers

Not everything in `server/lib/` is the chat loop. For an outside reader the
tree is easier to navigate with these tiers in mind; they are also the order
in which features could be split out or put behind configuration.

**Core** (the product does not work without it): persistence, agents and
turns, backends, conversation read model, events, voice in and out, routing
of spoken input, install and ops, and the PWA.

**Optional, on by config** (useful to many users, coupled to the core through
narrow seams): heartbeat and dreaming (autonomous check-ins; both hook into
`message_store` to hide their own traffic), teams and the leader loop,
background jobs and task plans, the orchestrator and herald.

**Native-app surfaces** (only the iOS app calls them today): pairing, push
notifications, artifacts and media, portraits and personas, location and
calendar, prompt history. They are safe to ignore when working on the PWA.

## Clients

- **PWA** (`web/src`, Svelte 5, built by Vite into `static/app/`): one
  conversation store per agent (`stores/conversations.svelte.js`) implements
  the sync algorithm in `docs/protocol.md`; `stores/app.svelte.js` owns the
  agent snapshot and the open chat; `stores/sse.svelte.js` turns server
  events into store updates. Pure logic shared with tests lives in
  `static/lib/` (imported as `@core/*`).
- **iOS** (`ios-native/`, Swift): implements the same core protocol plus the
  native surfaces. It is not part of the open-source release.

## Where things live at runtime

```
~/.config/clarp/config.toml          settings and provider keys
~/.local/share/clarp/state.sqlite    the source of truth
~/.local/share/clarp/telemetry.sqlite diagnostics (24 h detail, 30 d rollups)
~/.local/share/clarp/current/        the installed release (symlink)
~/.cache/clarp/runtime.sock          private server-to-runtime RPC socket
~/.cache/clarp/audio/                synthesized clips and HLS segments
```

Docker nodes keep all of that under one `/data` volume; see `docs/docker.md`.
