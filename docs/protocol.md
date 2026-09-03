# Client protocol

This is the contract between the Clarp server and any client: the PWA in
`web/`, the native iOS app, or something you write yourself. It has two layers.

1. **Core conversation protocol.** How a client authenticates, discovers agents,
   loads a chat, stays in sync, sends, stops, and plays voice. Every client
   implements this layer and nothing here is optional.
2. **Extension surfaces.** Everything else the server exposes: pairing, push
   notifications, provider sign-in, artifacts, teams, transcription models,
   heartbeat and dreaming settings, and so on. A client implements the ones
   its product needs. The native app implements most of them; the PWA
   implements none.

The wire constants (event type names, agent states, clip statuses) are defined
once in `server/lib/protocol.py` and mirrored in `static/lib/protocol.js` and
the PWA client models. A test fails the build when
they drift.

## Vocabulary

| Term | Meaning |
|---|---|
| **agent** | One persona bound to one working directory and one backend CLI (Claude, Codex, or Antigravity). Durable identity is `agent_id`. |
| **session** | The agent's user-facing name, e.g. `rachel`. Unique among live agents. Every core endpoint addresses agents by `session`. |
| **conversation** | One backend CLI conversation. Identified by `backend_session_id`, also called `conversation_id`. An agent has exactly one live conversation; relaunch or fork starts a new one. |
| **message** | One row of the conversation as the server stores it. Identified by `message_id`; ordered by `revision`. |
| **turn** | The message shape `/log` returns. The word is historical; a turn is one message. |
| **revision** | A per-conversation monotonic counter. Every insert or update of a message bumps it. It is the only ordering key a client should rely on. |
| **clip** | One synthesized voice reply, identified by `clip_id`. |
| **focus** | The server-wide "current agent" used for hands-free voice routing. Shared by every client of one server. |

## Authentication

The server reads `auth_token` from `config.toml`. When it is empty, every
request is accepted. When it is set, every request must carry one of:

- `Authorization: Bearer <token>` (preferred)
- cookie `claude_pwa_token=<token>` (for `EventSource`, `<img>`, `<audio>`)
- query parameter `?token=<token>` (first visit only; strip it from the URL)

The token is either the administrator token from `config.toml` or a paired
device credential from `POST /pairing/exchange`. A paired device may have
`limited` scope, which allows every `GET` and the core `POST`s (`/send`,
`/transcribe`, `/upload`, `/select`, `/focus`, `/clips/ack`, `/clog`,
`/location`, `/calendar/response`) and nothing else.

`GET /` and the static assets are public so a browser can load the shell
before it has a token.

## Core conversation protocol

### 1. Bootstrap: `GET /agents/snapshot`

The one call a client makes to learn everything about the server's agents.
Call it on start, on every SSE (re)connect, and whenever an `agent-roster`
event arrives.

```json
{
  "agents": [
    {
      "agent_id": "…", "session": "rachel", "persona": "Rachel",
      "backend": "claude", "cwd": "/home/me/proj", "model": "", "effort": "",
      "voice_id": "…", "avatar_symbol": "", "avatar_url": "/avatars/<agent_id>?v=<hash>",
      "mcp_servers": [], "heartbeat_enabled": false, "dreaming_enabled": false,
      "muted": false, "archived_at": null,

      "backend_session_id": "…", "conversation_id": "…",
      "head_revision": 812, "last_message_id": "…", "last_message": "…",

      "alive": true, "busy": false, "focused": true,
      "latest_state": "idle", "latest_state_ts": 1756800000000,
      "turn_started_at": 0, "last_activity": 1756800000, "last_turn_end": 1756799000,
      "status_text": null, "activity": { "…": "see agent-activity" },
      "compacting": false, "context_tokens": 12345, "context_window": 1000000,
      "queued_turn_count": 0, "queued_turn_revision": 0, "queue_paused": false,
      "team_ids": []
    }
  ],
  "focus": "<agent_id or null>",
  "roster": ["Rachel", "Mike", "…"],
  "personas": [ { "…": "saved contact definitions" } ],
  "available_mcp_servers": ["…"]
}
```

Rules:

- `head_revision` and `conversation_id` are the same values `/log` reports.
  If a client already holds a cached chat for this agent, it is current iff
  the cached `conversation_id` matches and the cached revision equals
  `head_revision`. Anything else means: fetch.
- `busy` means a turn is running. `latest_state` is one of the agent states
  below and is the value to render, not `busy`.
- The snapshot is read-only: calling it never changes server state, so it is
  safe to call as often as needed (the server reconciles stale state on the
  way out).
- There is no `/sessions` in this layer. The list of chats is
  `agents[].session` filtered by `archived_at == null`.

### 2. Load a chat: `GET /log`

```
GET /log?session=rachel&limit=100
GET /log?session=rachel&limit=100&after_revision=812
GET /log?session=rachel&limit=100&before=<message_id>
GET /log?…&include_automated=0&include_tool_details=0
```

| Parameter | Meaning |
|---|---|
| `session` | required |
| `limit` | 1–5000, default 100 |
| *(none of the below)* | **Tail snapshot**: the newest `limit` messages in display order. |
| `after_revision=N` | **Delta**: every message whose `revision > N`, ascending by revision. Includes messages that changed, not only new ones. |
| `before=<message_id>` | **Older history**: the `limit` messages older than that message. |
| `include_automated=0` | Hide heartbeat, leader, dreaming, and watcher traffic. |
| `include_tool_details=0` | Collapse tool cards to one summary card per message; fetch `/message-tool-details?session=&message_id=` on demand. |

Response:

```json
{
  "conversation_id": "…",
  "turns": [
    {
      "id": "u-<client_msg_id>  |  <server id>",
      "role": "user | assistant",
      "text": "…",
      "timestamp": "2026-09-02T05:41:00.123Z",
      "revision": 812,
      "kind": null,
      "tool_name": null,
      "tools": [ … ],
      "display_cells": [ … ],
      "origin": "user | agent | heartbeat | leader_tick | dreaming | watcher | schedule",
      "sender_agent_id": null, "sender_name": null, "sender_session": null,
      "automation_kind": null
    }
  ],
  "latest_revision": 812,
  "has_more": false,
  "replace_required": false,
  "missing": false,
  "latest_ts": "…", "cwd": "…", "file": "…", "includes_automated": true
}
```

Rules:

- **Identity is `id`.** Two turns with the same `id` are the same message; the
  one with the higher `revision` wins. Never match messages by text or position.
- **A user message's `id` is `u-` + the `client_msg_id` the client sent.** So
  the optimistic bubble the client paints and the durable row the server files
  have the same id, and confirmation is an id lookup.
- **`latest_revision` is the cursor.** Store it with the conversation and pass
  it back as `after_revision` on the next delta. After a delta, set the cursor
  to the response's `latest_revision`, which never exceeds the rows actually
  returned (when `has_more` is true the server holds the cursor back so the
  next call continues the backlog).
- **`has_more`** on a tail snapshot or an older-history page means older
  messages exist before the first one returned. On a delta it means more
  changed messages exist after the last one returned; call again.
- **`replace_required: true`** on a delta means the server cannot express the
  change as a delta (the conversation was rebuilt or rewritten). Discard the
  cached chat and load a tail snapshot.
- **`conversation_id`** changing between two responses for the same session
  means the agent started a new conversation (relaunch or fork). Discard the
  cached chat.
- **`missing: true`** with an empty `turns` means the agent has no
  conversation yet. Render it as empty, not as an error.
- A growing assistant reply appears as one message whose `text` and `revision`
  change on successive deltas. Replace it in place.

### 3. Stay in sync: `GET /events` (SSE)

One `text/event-stream` per client. Every event is a JSON object with a
`type` field and, when the server recorded it durably, an SSE `id:` line.

Connection rules:

- Send `Last-Event-ID` (browsers do this automatically). The server replays
  every durable event after that id before going live, paging until caught up,
  so a reconnect loses nothing. Without it the server replays the last few
  minutes.
- After the replay the server always sends `{"type": "agent-roster"}`. Treat
  it as "call `/agents/snapshot` now".
- The server writes `: ping` comments every few seconds. If nothing arrives
  for 25 s, reconnect with exponential backoff (250 ms to 5 s).
- The server closes the stream when a client falls too far behind. Reconnect;
  the replay fills the gap.

Event types and payloads:

| `type` | Fields | Client action |
|---|---|---|
| `transcript-updated` | `agent_id`, `session`, `backend_session_id` | The conversation changed. Fetch a delta for that session. The server rate-limits this per session to about 4/s; it is a wake-up, not the data. |
| `agent-state` | `agent_id`, `session`, `persona`, `kind`, `ts`, `detail`, `status_text` | Patch the agent's `latest_state`. `kind` is an agent state (below). `thinking` starts a turn; `done`, `idle`, `stopped`, `interrupted` end one. |
| `agent-activity` | `agent_id`, `session`, `persona`, `kind`, `phase`, `status`, `tool`, `action`, `summary`, `file_path`, `ts` | A tool call or phase change inside a turn. Show it as a transient activity row until the next delta lands. `status` ∈ running, ok, error, recorded. |
| `agent-roster` | `kind` ∈ created, relaunched, forked, deleted, persona-created, persona-updated, persona-deleted, portrait-selected; `session` | Refetch the snapshot. `created`, `relaunched`, `forked` for a session you have cached means that session's conversation is new: discard the cache. |
| `agent-focus` | `session`, `agent_id` | Server-wide focus moved (someone called `/select`, or hands-free routing picked an agent). Update `focused` flags. A client may follow focus or ignore it; the PWA follows, the native app follows only in hands-free mode. |
| `queue-updated` | `agent_id`, `session`, `queue_depth`, `queue_paused`, `queue_started`, `queue_revision`, optional `client_msg_id` | The agent's pending-turn queue changed (a send while busy was queued, started, or the queue was paused by `/stop`). |
| `user-notification` | `notification_id`, `agent_id`, `session`, `persona`, `done_ts`, `source_message_id`, `cause_message_id`, `origin`, `push`, `badge`, `unread`, `muted`, `preview`, `reason` | The server decided this completed turn deserves the user's attention. Badge and mark unread from this event only; never infer it from state changes. |
| `audio` | `clip_id`, `url`, `name`, `session`, `agent_id`, `persona`, `trace_id`, `streamable`, `delivery`, `stream_url`, `playlist_url`, `complete_url`, `audio_format`, `preview` | A voice clip is ready. See §6. |
| `tts-error` | `session`, `agent_id`, `persona`, `message`, `error` | Synthesis failed; tell the user instead of playing silence. |
| `server-version` | `version` | Server restarted on a new version; reload the client when it differs from the last one seen. |
| `remote-action` | `action` ∈ record, record-toggle, stop-agent | A shortcut asked the client to act (native and PWA both honour it). |
| `provider-limit`, `artifact-updated`, `attention-updated`, `background-job-updated`, `location-request`, `calendar-request` | see extension surfaces | Ignore if the client does not implement the surface. |

Agent states (`kind` / `latest_state`): `thinking`, `tool`, `compacting` are
**busy**; `idle`, `done`, `stopped`, `interrupted`, `waiting`, `background`,
`spawned` are not. `waiting` means the agent needs the user (a permission
prompt); `interrupted` means a turn died and was not recovered; `background`
means an out-of-band job is running and the agent is otherwise idle.

### 4. Send a message: `POST /send`

```json
{
  "session": "rachel",
  "text": "…",
  "client_msg_id": "<uuid the client mints>",
  "trace_id": "<optional; from a preceding /transcribe>",
  "synthesize_audio": true,
  "hands_free": false,
  "queue_if_busy": false,
  "transcription_id": "<optional; from /transcribe>"
}
```

Rules:

- **`client_msg_id` is the idempotency key.** Mint one per message, reuse it
  on retry, and paint the optimistic bubble with id `u-<client_msg_id>`. The
  server files the durable user row under that id and ignores a repeated
  request with the same id. Omit it only if you accept the server minting one
  from `trace_id`, in which case you cannot confirm delivery.
- If the agent is busy the new turn **preempts** the running one unless
  `queue_if_busy` is true, in which case it queues and a `queue-updated`
  event follows.
- `hands_free: true` is for dictation: the server may route the text to a
  different agent (name routing or the orchestrator) and answers with the
  session it chose.

Response `200`:

```json
{ "ok": true, "session": "rachel", "dispatch": "codex",
  "queued": false, "trace_id": "…",
  "orchestrator": { "action": "route | fallback | …", "decision_id": "…", "decision": {} } }
```

`dispatch` names the backend that took the turn (`claude`, `codex`, …);
`queued` is true when the turn waits behind a running one.

`200` means accepted, not delivered. Delivery is proven when a `/log` response
contains a turn with id `u-<client_msg_id>`. A client that wants a delivery
indicator should treat a send as failed if that id has not appeared within
about 20 s.

### 5. Stop: `POST /stop`

`{"session": "rachel"}`. Terminates the running turn, records an
`interrupted` state, pauses the queue, and broadcasts `agent-state` and
`queue-updated`. Response: `{"ok": true, "terminated": n}`.

Related: `GET /turn-queue?session=` lists queued turns; `POST /turn-queue/<id>/send`
releases one.

### 6. Voice

**In:** `POST /transcribe` with the audio body (`Content-Type` of the
recording, e.g. `audio/webm`), headers `X-Hands-Free: 1` for dictation,
`X-Transcription-ID: <uuid>` for idempotent retries, `X-Transcription-Model`
to pick a model. Response includes `text`, `trace_id`, and `transcription_id`;
pass both ids on the following `/send`.

**Out:** an `audio` event announces a clip. Pick a source in this order:

1. `playlist_url` (HLS) when present: assign it to an `<audio>` element.
2. `stream_url` of the form `/clips/<id>/stream` (progressive HTTP): assign it.
3. `url` (`/audio/<file>.mp3`): the complete file, always playable.

Acknowledge playback with `POST /clips/ack` `{"clip_id": …, "status":
"queued | play-start | play-ok | play-fail", "error": "…", "trace_id": "…"}`.
`GET /clips/recoverable?session=` returns recent clips a client that was
suspended may still want to play.

### 7. Focus: `POST /select`

`{"session": "rachel"}` makes that agent the server-wide focus (hands-free
routing target) and broadcasts `agent-focus`. Clients that follow focus must
apply the broadcast, not re-post `/select`, or two clients loop.

### 8. Agent lifecycle

- `GET /agents` — session-keyed map of live agents (a subset of the snapshot).
- `POST /agents` — create, relaunch, or fork: `{"name", "voice_id",
  "backend", "cwd", "model", "effort", "session" (to relaunch),
  "resume_session_id", "fork_session_id", "avatar_symbol", "personality"}`.
  Answers `{"session", "persona", …}` and broadcasts `agent-roster`.
- `DELETE /agents/<session>` — release the agent (soft delete).
- `POST /agent-mute`, `/agent-archive`, `/agent-heartbeat`, `/agent-dreaming`,
  `/agent-voice` — per-agent toggles: `{"session", "<flag>": bool}`.
- `GET /past-sessions?backend=&cwd=` — resumable backend conversations for
  the start dialog.
- `GET /agent-model-options` — the backend catalogue. `providers.<id>` is
  everything a chooser needs for one CLI: `label`, `detail`, `badge` (a
  bundled mark name), `symbol` (icon fallback), `brand` (`field_top`,
  `field_bottom`, `tint_dark`, `tint_light` as `#rrggbb`), `sort_index`,
  `hidden`, `installed`, the capability flags `supports_fork`,
  `supports_resume`, `supports_steer`, `supports_compact`, `supports_mcp`,
  `supports_routing`, `supports_auth`, `supports_usage`, `login_kind`
  (`none` | `device_code` | `cli` | `api_key`), `effort_ui` (`picker` |
  `hidden` | `folded_into_model`) with `effort_help`, and `models[]` with
  `id`, `label`, `default_effort`, `supported_efforts`, and provenance.
  Clients render this list rather than a bundled enum: cards are the
  installed, unhidden rows (plus the backend an existing agent runs on),
  and every Fork / Resume / Compact / MCP control follows its flag. A flag
  the Host does not send means the client's old default, never "no".
- `GET /voices?for=<session>` — voice catalogue with availability.
- `GET /dirs?path=` — directory completion for the start dialog.
- `POST /clog` — batched client diagnostics `{"events": [{"event", "detail",
  "trace_id", "clip_id"}]}`; the server files them next to its own logs.

### The sync algorithm

A client that follows these steps never shows a duplicated, missing, or
stale message:

```
on start / reconnect / agent-roster:
    snapshot = GET /agents/snapshot
    for each agent with a cached chat:
        if cached.conversation_id != agent.conversation_id: drop cache
        else if cached.revision < agent.head_revision: fetch delta

open chat(session):
    if no cache: GET /log?session&limit=100  → store turns, latest_revision, conversation_id, has_more
    else: fetch delta

fetch delta(session):
    r = GET /log?session&after_revision=<cursor>
    if r.replace_required or r.conversation_id != cached.conversation_id: reload tail
    else: upsert r.turns by id; cursor = r.latest_revision; if r.has_more: fetch delta again

on transcript-updated(session): debounce ~100 ms, then fetch delta(session)
on agent-roster created/relaunched/forked for a cached session: drop cache, reload

send(text):
    id = new uuid
    show turn {id: "u-"+id, role: user, text, optimistic: true}
    POST /send {client_msg_id: id, …}
    delivered when a /log response contains id "u-"+id
```

One fetch per session at a time. If a wake-up arrives while a fetch is in
flight, run one more fetch after it completes rather than starting a second.

## Extension surfaces

These are the routes only the native app uses today. A new client may adopt
any subset. Payload shapes are documented in the handler docstrings in
`server/server.py` and the modules named here.

| Surface | Routes | Server module |
|---|---|---|
| Device pairing | `POST /pairing/exchange`, `GET /paired-devices`, `POST /paired-devices/revoke`, `GET /devices`, `GET /server-info` | `device_pairing.py`, `server_identity.py` |
| Push notifications | `POST /devices` (APNs token), `GET /notification-avatars/…`, `/attention` | `apns.py`, `user_notifications.py` |
| Provider sign-in and usage | `/backend-auth`, `/backend-auth/login`, `/backend-auth/login-code`, `/backend-auth/logout`, `/backend-usage` | `backend_auth.py`, `backend_usage.py` |
| Files and media | `POST /upload` (headers `X-Session`, `X-File-Name`, `X-Upload-ID`), `/media`, `/agent-files`, `/agent-file` | `upload_results.py`, `media_store.py`, `agent_files.py` |
| Artifacts and decisions | `/artifacts`, `/artifacts/<id>`, `/decisions`, `/decisions/<id>/resolve`, `/task-plan` | `artifacts.py`, `task_plans.py` |
| Portraits and personas | `/agent-portraits`, `/agent-portrait-generation`, `/personas`, `/personas/update`, `/personalities/settings` | `agent_portraits.py`, `portrait_generation.py`, `personas.py` |
| Teams | `/teams`, `/teams/<id>`, `/teams/<id>/members`, `/teams/<id>/messages`, `/team-nudging` | `team_store.py`, `team_leader.py` |
| Autonomy | `/heartbeat/settings`, `/agent-heartbeat/status`, `/dreaming/settings`, `/dreaming/runs`, `/automation-settings`, `/herald/settings`, `/orchestrator/settings`, `/orchestrator/route-delegation` | `heartbeat.py`, `dreaming.py`, `herald.py`, `orchestrator.py` |
| Transcription | `/transcription-capabilities`, `/transcription-guidance`, `/transcription-models/install`, `/transcription-models/remove` | `transcription_models.py`, `vocab.py` |
| Voice providers | `/tts/providers`, `/voice-catalog`, `/voice-preview`, `/cartesia-voices`, `/cartesia-voice-preview` | `voice_catalog.py` |
| Background jobs and updates | `/background-jobs`, `/server-update`, `/status`, `/compact`, `/managed-skills`, `/diagnostics/settings` | `background_jobs.py`, `server_update.py`, `managed_skills.py` |
| Location and calendar | `/location`, `/location/request`, `/calendar/request`, `/calendar/response` | `location.py`, `calendar_request.py` |
| Prompt history | `/identity/prompt-history` | `prompt_history.py` |
| Voice timeline | `POST /voice-events`, `GET /voice-events`, `GET /voice-events/utterances`; `/transcribe` headers `X-Utterance-ID`, `X-Client-Ts` | `voice_events.py`, `audio_metrics.py`, `docs/voice-tracing.md` |
| Diagnostics | `/crash` (MetricKit), `/diagnostics/health` | `ios_diagnostics.py`, `health.py` |

## Compatibility policy

The core conversation protocol is **additive-only**. Fields and event
types are added, never renamed, removed, or retyped. A change that cannot
be expressed additively ships as a new event type or a new endpoint, and
the old one keeps working until `min_app_version` (below) moves past
every client that used it. The database schema follows the same rule
(`server/lib/db.py`).

Clients **must ignore** unknown event types and unknown fields on known
types, and must treat a missing optional field as the documented default.
A client that follows this rule keeps working when the server grows; a
client that refuses unknown traffic breaks on every minor release.

`/server-info` `capabilities.features` is the **only** feature gate. No
client sniffs versions to decide whether a surface exists.

An old client is guaranteed:

- the nine core endpoints: `GET /server-info`, `GET /agents/snapshot`,
  `GET /log`, `GET /events` (SSE), `POST /send`, `POST /stop`,
  `POST /transcribe`, `POST /select`, `POST /clips/ack`
- the eleven core event types: `transcript-updated`, `agent-state`,
  `agent-activity`, `agent-roster`, `agent-focus`, `queue-updated`,
  `user-notification`, `audio`, `tts-error`, `server-version`,
  `remote-action`
- the sync algorithm ("The sync algorithm" below): revision cursors,
  `replace_required`, `conversation_id` changes, `has_more` paging
- clip URL precedence: `playlist_url`, then `stream_url`, then `url`
- delivery: a send is delivered if and only if its `u-<client_msg_id>`
  appears in `/log`. HTTP 200 means accepted, not delivered.

The one place a version *is* negotiated is the App Store app, which cannot be
upgraded in lockstep with the server. `GET /server-info` carries
`clarp_version` (the server's release, from `pyproject.toml`) and
`min_app_version` (`server_identity.MIN_APP_VERSION`, the oldest app it still
speaks to). The app compares those with its own version and
`HostCompatibilityPolicy.minimumHostVersion` and shows a one-time "update the
Host" or "update the app" dialog per (Host, Host version, app version). A
server that predates these fields is treated as older than any minimum.

Inside that window, features are negotiated per surface rather than by
version. `/server-info` also carries `capabilities.features`, the product
surfaces this Host implements (`teams`, `oracle`, `dreaming`, …,
`server_identity.FEATURES`), so a client hides an entry point the Host
lacks instead of showing a dead toggle; a Host that sends no block hides
nothing. The same idea drives the per-surface catalogues: backends
(`/agent-model-options`, above), sign-in rows (`/backend-auth` lists every
registry adapter whose `login_kind` is not `none`, with that `login_kind`
on the row), and routing providers (`/orchestrator/settings` returns
`providers[]` with `id`, `label`, `detail`, `kind` `backend` | `api`,
`catalog_backend` naming the `/agent-model-options` row that supplies its
models, `installed`, and `effort_options`; a `POST` with a provider outside
that list is a 400). Adding a CLI is a server adapter with its
presentation and flags; no client release is needed for it to look
intentional.
