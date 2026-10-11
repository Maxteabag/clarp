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

## Clarp features that skip profile agents

These features send an agent's content to a model provider other than the
agent's own. They skip profile agents:

- the tool-call explainer: the live path and janitor scope;
- janitors: task labels, the heartbeat keeper and other scoped janitors;
- the label checker;
- dreaming;
- the orchestrator's routing context. The agent's name stays routable, but
  its messages and state are not sent;
- Oracle: its roster, delegation, pending decisions and completion
  notifications;
- fleet-map rule design for unknown commands;
- remote error classification;
- compaction through `codex resume` and the interactive terminal. Both would
  run on the CLI's own account.

`tests/unit/test_codex_profile_side_channels.py` pins these.

## Still outside the EU deployment

- **Speech.** When the agent speaks, `<speak>` text goes to the configured TTS
  vendor (Cartesia, ElevenLabs and others). Dictation goes to the configured
  speech-to-text vendor. For EU-only work, use the agent in text, or set
  `[tts.agents.<name>]` to an EU-hosted or local voice.
- **Hands-free routing.** With the orchestrator on, a spoken or typed
  utterance that is not addressed to a session goes to the routing model
  (OpenAI by default) to decide which agent receives it. Send to the agent's
  chat directly.
- **Agent-to-agent traffic.** Team posts, `clarp-admin prompt` and replies to a
  parent go to whichever agents receive them. Keep EU work among profile
  agents.

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
