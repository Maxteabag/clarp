# Codex on Azure OpenAI EU: the `azure-eu` provider profile

A Codex agent can be created on the provider profile `azure-eu`. Its turns go
to Azure OpenAI instead of the OpenAI/ChatGPT account the Codex CLI normally
uses. Every other Codex agent runs as before. The code is in
`server/lib/codex_profiles.py`. The client contract (Host contract 63) is in
`docs/compatibility.md` and `docs/protocol.md`.

| | |
|---|---|
| Resource | `irisbot-ticketmaster-ai-sc` (Sweden Central) |
| Endpoint | `https://irisbot-ticketmaster-ai-sc.openai.azure.com/openai/v1`, Responses API |
| Deployment | `sol61-eu` (gpt-6.1-sol) |
| Key | user keyring: `secret-tool lookup service psa-reverify key azure-openai` |
| Codex home | `<Clarp data dir>/codex-profiles/azure-eu` (mode 0700) |

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
   `AZURE_OPENAI_API_KEY`. It removes `OPENAI_API_KEY`, `CODEX_API_KEY` and the
   other OpenAI variables from that environment.
4. It sets `CODEX_HOME` to the profile home.

The process has no OpenAI login and no route back to OpenAI. If Azure refuses
a turn, the turn fails visibly. Clarp skips its OpenAI account handling for
these turns: there is no account switch, no usage-limit reconnect, no usage
recorded against the OpenAI account and no retry on another provider.

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
  `~/.codex` is not used.
- Clarp's database: the messages, turns and states shown in the apps.
- The `agent.provider_profile.<agent_id>` setting.

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
| Another agent's turn reading over the Host API (`do_GET`, `/agents/snapshot`) | `/log`, artifacts, jobs, conversation and message previews | that agent's provider | Refused (403) when the caller's turn identity (`X-Clarp-Turn`) is on another profile. The snapshot shows such a caller an EU agent's identity and state only. The user's devices see everything. |
| Speech output (`tts_queue.enqueue`) | `<speak>` replies | Cartesia, ElevenLabs, Deepgram, Gemini TTS, adapters | EU agents are text-only: nothing is enqueued |
| Dictation (`transcription_pipeline`) | the prompt audio and transcript, plus the junk filter | cloud STT, adapters, TypeSafe | With an EU agent focused, only the Host's local Whisper transcribes it; otherwise the request is refused (409, type instead). Judgments are local-only |
| Fork / adopt / resume (`agent_fork`, `agent_spec`) | the transcript or a seed | the child's provider | Cross-profile forks and resumes are refused |
| Compaction, interactive terminal | the thread | the CLI's own OpenAI account | Refused |
| Voice preamble, heartbeat, goals, compaction inside the turn | — | the agent's own EU turn | Unchanged (EU) |
| herald, live activity, notifications, leader memory, background jobs, goal ledger, artifacts | — | — | No model call (local) |

## What the Host cannot enforce

- **Same Unix user.** Every agent runs with full access as the same OS user.
  An ordinary agent can read the Clarp database or the EU profile's rollouts
  straight from disk and pass them to its own provider. The Host only guards
  its own API. To exclude this, keep EU work off any machine where
  non-EU agents work on files that hold EU content, or run them in an OS
  sandbox.
- **Live event stream.** The SSE event stream is not filtered per caller. An
  agent subscribing to it would receive live text. No agent or skill does
  this today.
- **Bypassing turn identity.** A request without `X-Clarp-Turn`, made with a
  device token from an agent's shell, is treated as the user.
- **Clients.** On-device features of the apps (iOS speech recognition,
  client-side TTS or summaries, if any) are outside the Host. Push
  notifications carry a reply preview to Apple's push service; this is not a
  model.
- **What the user chooses to send.** The user typing EU content into an
  ordinary agent, or pasting it into a team, a decision or Oracle.

## What the EU Data Zone does and does not guarantee

Microsoft Learn, "Understanding deployment types in Microsoft Foundry Models"
(updated 2026-08-12), says:

- Data Zone deployment types process prompts and responses only within the
  specified data zone. For the European Union, this is the Azure EU Data
  Boundary.
- The EU Data Boundary can include EFTA countries such as Norway and
  Switzerland, as well as EU member states. Microsoft can add regions to a
  data zone without prior notice.
- Data stored at rest stays in the designated Azure geography: Sweden, for
  this resource.

The Data Zone does not cover:

- data in Clarp or on this laptop;
- the vendors in "Still outside the EU deployment";
- whether the deployment really is a Data Zone SKU. This was not checked from
  Clarp: the resource is not visible from the tenant used here. Confirm that
  `sol61-eu` is `DataZoneStandard` (or `DataZoneProvisionedManaged`) in the
  portal.

**Do not use customer data until the CISO confirms that Azure OpenAI is on the
customer DPA sub-processor list.** Until then, use synthetic data only.

## Operations

- The profile is shown as `available` only when the key is in the keyring.
  The `clarp-runtime` user service reaches the keyring over the session bus
  (`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`). If the keyring is
  locked or the key is missing, the turn fails with "no key in the keyring".
- A rotated key takes effect when the profile's app-server restarts, for
  example after a credential change or a runtime restart.
- Managed skills are linked from the profile home (`skills` → Clarp's
  managed Codex skills directory).
