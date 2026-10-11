# Codex on another provider: Codex provider profiles (e.g. Azure OpenAI EU)

A Codex agent can be created on a provider profile. Its turns then go to the
profile's provider (for example an Azure OpenAI EU Data Zone deployment)
instead of the OpenAI/ChatGPT account the Codex CLI normally uses. Every other
Codex agent runs as before. The code is in `server/lib/codex_profiles.py`.
The client contract (Host contract 63) is in `docs/compatibility.md` and
`docs/protocol.md`. Below, "EU agent" means an agent on such a profile.

## Configuration

Profiles are configured by the owner, not built into Clarp. Each one is a
table in `~/.config/clarp/config.toml`. Clarp reads it whenever it starts a
profile's process or builds the catalogue, so no reload is needed. Shape:

```toml
[codex_profiles.<id>]
label = "Codex (EU, Azure)"
detail = "Azure OpenAI, EU Data Zone"
base_url = "https://<resource>.openai.azure.com/openai/v1"
models = [{ id = "<deployment>", label = "<name>", efforts = ["low", "medium", "high", "xhigh"] }]
default_model = "<deployment>"
wire_api = "responses"
key_header = "api-key"
keyring_service = "<secret-tool service>"
keyring_key = "<secret-tool key>"
```

The ECIT install adds exactly this table. It is configuration, not code:

```toml
[codex_profiles.azure-eu]
label = "Codex (EU, Azure)"
detail = "Azure OpenAI, EU Data Zone (Sweden Central)"
base_url = "https://irisbot-ticketmaster-ai-sc.openai.azure.com/openai/v1"
models = [{ id = "sol61-eu", label = "GPT-6.1 Sol (EU)", efforts = ["low", "medium", "high", "xhigh"] }]
default_model = "sol61-eu"
wire_api = "responses"
key_header = "api-key"
keyring_service = "psa-reverify"
keyring_key = "azure-openai"
```

- **Keyring fields.** `keyring_service` and `keyring_key` are a reference:
  Clarp runs `secret-tool lookup service <service> key <key>`. The key is never
  in the file.
- **Strict validation.** Clarp rejects a table that:
  - holds a literal secret: any `key`, `token`, `secret`, `password` or similar
    field, or a value that looks like a credential;
  - has any other unknown field, at the top level or in a model;
  - has a base URL that isn't plain `https`, or that carries credentials or a
    query;
  - sets a `wire_api` other than `responses`, or an `Authorization` header;
  - has a `default_model` that isn't one of its `models`.
- **Rejected tables** are logged with the id and the reason, never the values,
  and are never offered or run.
- **Fixed settings.** For every profile Clarp sets analytics, feedback, update
  checks and web search off and `requires_openai_auth = false`. It strips
  `OPENAI_API_KEY`, `CODEX_API_KEY` and every other `OPENAI_*`/`CODEX_*`
  variable. The config can't change these.
- **Availability.** A profile is offered (`available`) only while its table
  is valid and `secret-tool lookup` exits 0. The value is discarded; it is only
  read when the profile's process starts.
- **Profile home.** Each profile runs in `<Clarp data dir>/codex-profiles/<id>`
  (mode 0700).

## Operating rule: text, not voice

Dictation and hands-free routing carry the user's own words. Clarp keeps both
as local as it can for an EU agent: only the Host's own Whisper transcribes
dictation, and routing judgments stay local. Still, **use text input, not
voice, for real cases with this agent.** The catalogue shows this rule in
every profile's `detail` and as `operating_rule`.

## How a turn runs

The profile has its own Codex app-server process, separate from the shared
process that runs every other Codex agent. When Clarp starts that process it
does four things:

1. It writes the profile's `config.toml` (mode 0600). The file sets the model
   provider `azure-eu` with `requires_openai_auth = false`, turns off analytics
   and feedback, disables web search and turns off update checks. It names the
   key's environment variable, never the key.
2. It reads the key from the keyring.
3. It puts the key only into that process's environment, as
   `CLARP_CODEX_PROFILE_API_KEY`. It removes every `OPENAI_*`, `CODEX_*` and
   `AZURE_OPENAI_*` variable inherited from the Host. The profile home never
   holds an OpenAI credential: Clarp refuses to start a profile whose home
   contains `auth.json`, and it never copies or links one in.
4. It sets `CODEX_HOME` to the profile home.

The process has no OpenAI login and no route back to OpenAI. If Azure refuses
a turn, the turn fails visibly. Clarp skips its OpenAI account handling for
these turns: there is no account switch, no usage-limit reconnect, no usage
recorded against the OpenAI account and no retry on another provider. If the
profile is removed from the config, its agents' turns are refused. There is no
fallback of any kind: to the CLI's account, to another backend, or to the
janitors' model fallbacks, which never take an EU agent in scope.

Clarp stores the profile with the agent when the agent is created, and the
profile never changes. A turn, fork or resume that would cross profiles is
refused. Queued and parked work, including work handed over during a release
drain, re-reads the profile from the agent when it is admitted again.

## What goes to the EU deployment

Everything Codex sends to its model during the agent's turns:

- the user's messages to the agent and the agent's replies;
- Clarp's per-turn instructions and the persona identity;
- tool calls and their results, including file contents and command output
  the agent reads in its working directory;
- the content of the managed skills it opens;
- compacted context. Codex compacts automatically on the same provider.

## What stays on this machine

- Rollouts and transcripts: `<profile home>/sessions/…`. The Codex thread
  index, goals and Codex's own log database stay in the profile home too.
  `~/.codex` is not used. Clarp finds a thread's rollout only in the home of
  the agent that bound the thread: an EU agent never sees `~/.codex`, and a
  default agent never sees a profile home.
- Clarp's database: the messages, turns and states shown in the apps.
- The agent's profile: the `agents.provider_profile` column (schema 118).
  The creating INSERT writes it and a trigger aborts any update that would
  change it, so a restore, repair or resurrection can never move an EU agent
  to the CLI's own account. An unknown or unconfigured profile fails closed:
  its turns are refused, nothing runs on OpenAI, and side channels still treat
  the agent as EU. Schema 118 backfilled the column from the old
  `agent.provider_profile.<id>` settings rows, which are no longer read.

## What never leaves memory

The key exists only in the keyring and in the profile app-server's
environment. It never appears on argv, in `config.toml`, the database, logs,
the eventlog, transcripts, `/status` or error text. Clarp replaces it with
`[redacted]` anywhere that process's stderr or error messages are recorded.
`tests/unit/test_codex_provider_profile.py` checks all of these surfaces for a
sentinel key. A live probe on 2026-10-11 found the real key in none of the
probe's files.

## Leak-path audit: every other path that reads an agent's content

Rule: no Clarp feature sends an EU agent's prompts, replies, tool calls,
tool output or transcript text to a model outside its EU deployment. "EU
agent" means an agent on the `azure-eu` profile. Every Host path that reads
one agent's content and then calls a model is listed below with its
decision. Each decision is pinned by a test that stubs the non-EU model
client and checks that a sentinel from an EU agent never reaches it. The
tests are in `tests/unit/test_codex_profile_leak_paths.py`,
`tests/unit/test_codex_profile_side_channels.py` and
`tests/unit/test_codex_provider_profile.py`.

| Path | What it would send | Where | Decision |
|---|---|---|---|
| Tool explainer: live (`live_explain`) and phone (`tool_explanations.request`) | tool commands, inputs, script contents | `codex exec`, CLI fallbacks, TypeSafe | Skipped. Live admission refuses EU agents. Janitor scope resolves no explainer, so no model call is made. |
| Error classification (`turn_dispatch`) | the turn's error text | TypeSafe | Local regex only |
| OpenAI account recovery, usage records, account switch | quota state, model | OpenAI account | Skipped for EU turns |
| Janitors: task labels, heartbeat keeper, scoped janitors (`janitors._in_scope`, `janitor_runner.in_scope`) | recent messages, plans, jobs | janitor model (any CLI or OpenAI) | EU agents are never in scope |
| Label checker (`label_audit`) | status, jobs, recent messages, helper status | TypeSafe | EU agents and EU helpers under ordinary parents are skipped |
| Quota / hotseat janitors | account usage only | — | No agent content; unchanged |
| Dreaming (`dreaming`) | chat snapshot plus a full turn, possibly on another backend | any CLI | EU agents never dream |
| Orchestrator routing context (`build_context_packet`) | recent messages and state of every agent | OpenAI, TypeSafe, CLI | EU agents' messages and state are withheld; only their names stay routable |
| Orchestrator routing of a message to an EU agent (`handle_send`) | the utterance, which is the EU agent's prompt | routing model | No routing model when the target is an EU agent or the message opens with its name |
| Spoken-name matching (`routing`, `send_service`) | the utterance | TypeSafe | Local match for EU names; local-only when the default target is an EU agent |
| Orchestrator spoken status ("what is X doing") | the last reply, 180 characters | TTS provider | EU agent: its state kind only |
| Oracle tools and roster (`oracle_calls`, `oracle_calls_stable`, `oracle_contact`, Gemini Live) | messages, transcript, status | OpenAI Realtime / Live, Gemini Live | EU agents are not in Oracle's roster |
| Oracle attention (`oracle_attention`) | pending decisions, completion replies | same | EU agents' rows are filtered out |
| Oracle delegations (`oracle_delegations.begin`) | the delegated result | same | Refused for EU agents (all engines) |
| Legacy `/oracle/realtime` proxy (the phone runs the tools) | whatever the phone reads back | OpenAI Realtime | Read-tool calls naming an EU agent have their output replaced; EU delegation results are not injected |
| Oracle router (`oracle_router`) | tool and delegation results | `codex exec` / OpenAI | Covered by the roster and delegation guards |
| Podcast detours (`podcast_live.context_for`, three engines) | an episode's transcript and source artifact | OpenAI / Gemini Live, image generation | Refused when the episode or its source belongs to an EU agent |
| Fleet map world and rule design (`viz_normalize.build_fleet_map`) | tool commands, paths, plans, artifacts, prompt excerpts | `codex exec`, `claude -p` | EU agents never enter the map's events or work |
| Team posts (`team_store.capture_assistant_message`) | `<team>` posts, read by teammates' turns and the heartbeat decider | teammates' providers | An EU agent's posts reach only teammates on its profile |
| Agent-to-agent messages: `clarp-admin prompt`/`request`, message replies, any `/send` whose claimed sender or turn identity (`X-Clarp-Turn`) is the EU agent (`send_request`, group-call routing, `turn_dispatch`) | the EU agent's words | the recipient's provider | Refused (403 `provider_profile_boundary`) unless the final target is on the same profile, **when the request identifies itself** (claimed sender or turn header); see "What the Host cannot enforce". Such a message is never re-routed by name or by the orchestrator. Messages into an EU agent stay allowed. The parent follows it through its Clarp status only. |
| Schedules (`POST /agent-schedules`, `PATCH /schedules/<id>`) | a cron prompt in the EU agent's words | the target agent's provider | An EU agent's turn may not create a schedule, or change a schedule's prompt, on an agent outside its profile (403 `provider_profile_boundary`) |
| Bookkeeping delegates (`goal_ledger.enable`, `observe`) | the principal's last messages and goal events | the delegate's provider | `enable` refuses a delegate on another profile; `observe` refuses any existing cross-profile delegation (`provider_profile_boundary`) |
| Request replies recorded into the requester's goal (`peer_requests.plan_reply`) | the EU agent's answer | the requester's provider | Refused (`provider_profile_boundary`) unless the requester is on the same profile |
| Another agent's turn reading over the Host API | `/log` (with or without `session`), `/agents/<x>`, `/artifacts/<id>`, `/artifacts?agent_id=`, `/background-jobs/<id>` | that agent's provider | Refused (403) **when the request carries the caller's turn identity** (`X-Clarp-Turn`) and the owner is a profile agent on another profile |
| Same, lists and events | the artifact list and search, `/attention`, the `/background-jobs` list, `/teams/<id>/messages`, `/agent-schedules`, the `/events` stream | that agent's provider | For an identified caller, every entry or event that names such an agent is dropped, at any depth |
| Same, snapshot | `/agents/snapshot` | that agent's provider | For an identified caller (e.g. the EU agent's parent), the EU agent's row is status only: identity, busy/state, helper state, background job and queue counts, goal status. Never messages, status text, activity or goal text. |
| Same, the user's devices | everything | — | Unfiltered: a request without a turn identity is the user |
| Push notifications to Apple (`apns`, `user_notifications`, relay) | reply preview (≤180 characters), decision question and title | Apple Push Notification service, outside the EU | Every payload about an EU agent starts with `provider_profile` and `privacy_restricted: true`, and its alert text is generic ("New reply", "Needs your answer"). No preview, question, title or subtitle. Background syncs carry the marker too. |
| Live Activity (`live_activity`, pushed through APNs) | the tool label (a file or command), headline | Apple, outside the EU | An EU agent's row carries the same marker and its phase only (a state word); `tool` is null |
| Backend and account fallback (`turn_dispatch`, `codex_app_server`) | the failed turn | the CLI's own OpenAI account, another backend | None exists for an EU turn. No account-pool registration, no usage-limit reconnect, no failover request, no OpenAI usage record. An unconfigured profile refuses the turn. Dispatch refuses an EU agent on any backend but `codex`. Retries go to the same profile process. |
| Janitor model fallbacks (`model_fallbacks`) | janitor packets | any CLI | Only janitor work uses them, and janitors never take an EU agent in scope |
| Clarp-side summarizers, titlers, search, embeddings | — | — | None exist. Every Host module that calls a model is listed in this table: judgments, orchestrator, janitors, explainer, fleet map, Oracle, podcasts, TTS, STT. |
| Speech output (`turn_dispatch`, `tts_queue.enqueue`) | `<speak>` replies | Cartesia, ElevenLabs, Deepgram, Gemini TTS, adapters | EU agents are text-only. Their turns run with `synthesize_audio` off (no voice instructions), and nothing of theirs is ever queued for TTS. |
| Dictation (`transcription_pipeline`) | the prompt audio and transcript, plus the junk filter | cloud STT, adapters, TypeSafe | With an EU agent focused, only the Host's local Whisper transcribes it; otherwise the request is refused (409, type instead). Judgments are local-only |
| Fork / adopt / resume (`agent_fork`, `agent_spec`) | the transcript or a seed | the child's provider | Cross-profile forks and resumes are refused |
| Compaction, interactive terminal | the thread | the CLI's own OpenAI account | Refused |
| Voice preamble, heartbeat, goals, compaction inside the turn | — | the agent's own EU turn | Unchanged (EU) |
| herald, live activity, notifications, leader memory, background jobs, goal ledger, artifacts | — | — | No model call (local) |

## What the Host cannot enforce

**Every agent runs as the same Unix user.** This was confirmed in review:
- The turn header (`X-Clarp-Turn`) is optional.
- The config's admin token is accepted from any local request.
- `peter` has passwordless `sudo` and `docker`.

So any agent can:
- read the Clarp database, `config.toml` and the EU profile's rollouts straight
  from disk;
- leave out its turn header (`env -u CLARP_PROVIDER_TURN clarp-admin …`) and
  be treated as the user.

The 403s and filters above therefore hold **only for requests that identify
their turn**. They stop accidents and well-behaved tools, not an agent that
tries. The real control is machine or Unix-user isolation: run EU work on a
machine, or under a user, where no non-EU agent runs. This is a CISO-level
limit; Clarp claims no isolation beyond it.

Also outside the Host:
- **Clients.** On-device features of the apps, such as iOS speech recognition
  or any client-side summaries, are outside the Host. The `privacy_restricted`
  marker lets the iOS notification extension skip enrichment and intent
  donation; that behaviour is the app's.
- **What the user chooses to send.** The user typing EU content into an
  ordinary agent, or pasting it into a team, a decision or Oracle.

## Capability keys

`interaction_capabilities` (in snapshot agent rows) reports `can_speak`,
`can_cloud_dictation` and `can_voice_target` as false for a profile agent.
Speech, cloud dictation and turn audio read the same function, so what
clients are told is what the Host enforces.

## What the EU Data Zone does and does not guarantee

Microsoft Learn, "Understanding deployment types in Microsoft Foundry Models"
(updated 2026-08-12), says:

- Data Zone deployment types process prompts and responses only within the
  specified data zone. For the European Union, this is the Azure EU Data
  Boundary.
- The EU Data Boundary can include EFTA countries such as Norway and
  Switzerland, as well as EU member states. Microsoft can add regions to a
  data zone without prior notice.
- Data stored at rest stays in the designated Azure geography, which is the
  resource's region.

The Data Zone does not cover:

- data in Clarp or on this laptop;
- anything listed in "What the Host cannot enforce".

The ECIT `azure-eu` deployment was verified through Azure Resource Manager by
Nadia on 2026-10-11:

| Item | Value |
|---|---|
| Subscription | `21a4fbcd-1d08-4ce7-9aa4-61553cbc8750` |
| Resource group | `rg-irisbot-ticketmaster` |
| Account region | swedencentral |
| Deployment | `sol61-eu` |
| SKU | `DataZoneStandard`, capacity 300 |
| Model | `gpt-6.1-sol`, version 2026-09-29 |

**Do not use customer data until the CISO confirms that Azure OpenAI is on the
customer DPA sub-processor list.** Until then, use synthetic data only.

## Operations

- The profile is shown as `available` only when the key is in the keyring.
  The `clarp-runtime` user service reaches the keyring over the session bus
  (`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`). If the keyring is
  locked or the key is missing, the turn fails with "no key in the keyring".
- A rotated key takes effect when the profile's app-server restarts, which
  happens at a runtime restart. Switching OpenAI accounts (`codex login`,
  Hotseat) does not recycle profile processes: they are not OpenAI accounts.
- Managed skills are linked from the profile home (`skills` → Clarp's
  managed Codex skills directory).
