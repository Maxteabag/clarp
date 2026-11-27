---
name: clarp-agent-admin
description: Create or refine Clarp agent personas, including identity, personality, voice, and avatar direction.
---

# Clarp Agent Administration

Use the server's agent creation flow for the durable persona record. A good
persona has a distinct name, a concise behavioral description, a voice that is
not already occupied, and recognizable artwork.

## Workflow

1. Resolve the target server and session with `clarp-sessions`.
2. Preserve the user's explicit name, backend, directory, and voice choices.
3. Write personality guidance as concrete behavior, tone, and decision style;
   avoid biography that does not affect responses.
4. Prefer the Cartesia catalog exposed by the server over hard-coded voice IDs.
   Preview candidates and do not silently reuse a voice marked as occupied.
5. Use the app's symbol avatar when image generation is unavailable.
6. If an image-generation tool is available and the user requests artwork,
   generate a square, face-readable avatar with no text. Do not assume an
   OpenAI key exists on the Clarp server: image generation belongs to the
   current agent/tool environment. Publish the result with `clarp-media` and
   let the user approve it before replacing an established avatar.

## Creating a personality

A personality is one live call. It needs no deploy and no restart. Make it from your own
turn (or a job your turn started) with the tool, which presents the token of the agents'
tools and never the owner's:

```
clarp-admin persona create NAME [--voice-id VOICE_ID] [--personality TEXT] \
    [--avatar-symbol SYMBOL] [--no-picture]
```

It needs a Rust Host with the feature `tools_owner_free` (contract 193); on an older Host
it says "not available". Outside a turn it is refused: the Host must know which agent asks.

- `--voice-id` is a JSON *string*, not an object: `{"cartesia":"<uuid>"}`. Copy the
  chosen voice's `selection_value` verbatim. Without one the persona is given a free voice.
- `--personality` is at most 4000 characters, and takes precedence over any built-in
  clause for that name.
- A picture is drawn for a persona made without a symbol, unless `--no-picture`. An image
  of your own goes on the agent afterwards, from its gallery (`clarp-admin portrait`, see
  `clarp-avatar-generation`).

Then `clarp-admin agent create NAME [--cwd DIR] [--backend B]` starts a session that
inherits the persona's voice, personality and picture.

`POST /personas` with `avatar_base64`, `POST /personas/update` and `DELETE /personas/<name>`
stay the owner's (his apps, or his own command with `CLARP_HOST_TOKEN`). Do not read
`[server].auth_token` from config.toml to call them: the Host logs every call made with the
owner's token from no app and no turn, and an agent's tools are moving off it (#795).

For a disposable agent on the Rust Host, use `clarp-admin agent create NAME --test`
or send `"test": true` in `POST /agents`. The experience watcher skips that agent.

For a Codex agent that must run only on a model provider of its own (its own
`CODEX_HOME`, with no way back to the default provider), on a Rust Host with the
feature `codex_own_provider`, see "A Codex agent on a model provider of its own" in
the `clarp-sessions` skill (`--codex-home`, `--codex-provider`, `--cloud-speech`).

## Scheduled Jobs

Configure recurring autonomous turns for agent sessions:

```bash
# List scheduled jobs for a session
clarp-admin schedule list --session <session>

# Add a scheduled job (standard 5-part cron or @daily, @hourly, @weekly)
clarp-admin schedule add <session> \
  --name "Morning Briefing" \
  --cron "0 8:30 * * 1-5" \
  --prompt "Check Outlook mail & calendar and summarize key tasks."

# Toggle enable/disable
clarp-admin schedule toggle <schedule_id> --enable
clarp-admin schedule toggle <schedule_id> --disable

# Remove a schedule
clarp-admin schedule remove <schedule_id>
```

Schedules are exposed on `GET /agents/snapshot` under `agent.schedules` and can be toggled directly from the Clarp PWA and native iOS apps.

Never register a new personality in the server source — the roster, Cartesia
voice, and personality tables in `lib/config.py`, or an avatar in
`static/avatars/`. That path is for built-ins only, and none of it takes
effect until the server restarts.

## Choosing a voice

On Rust Hosts advertising `agent_token_api` (contract 216), use the local
`CLARP_TOOL_TOKEN` as the bearer for `/cartesia-voices`, `/agents/<session>`,
`/agent-voice` and `/personas/update`. Send the current turn's
`CLARP_PROVIDER_TURN` in `X-Clarp-Turn` for attribution. These requests retain
the agent's identity and the endpoint's usual privacy and ownership checks.

`GET /cartesia-voices` returns the English catalog: `name`, `tagline`,
`description`, `gender`, `country`, `preview_url`, `selection_value`, and
`taken_by`. Play `preview_url` for the shortlist and let the user pick; do not
choose a voice on description alone. `taken_by` is computed from running
agents only, so also check the personas list for a voice already claimed by a
personality with no live session.

You may also leave `voice_id` out. Every Host that advertises feature
`voice_pool` (Host contract 123 and later) gives a new agent or contact made
without a voice an unused one. That holds on every route: `POST /agents`,
`clarp-admin agent create`, adoption, `POST /personas`, minted and assigned contacts.
On contract 135 and later (feature `voice_for_helpers`), these rules also apply:

- An English agent gets its Cartesia voice from the Host's curated list
  (`server/lib/cartesia_voice_pool.json`: native English accents, no novelty
  voices, one per name). Agents in other languages get one from Cartesia's
  catalogue in their language.
- A voice is unused when no agent holds it, archived agents included, and no
  contact holds it. When the pool runs out, the Host first reuses a voice that
  only archived agents hold, then the least recently used voice, and logs
  `voicePoolExhausted`.
- A helper (`--role helper`) speaks with its parent's voice. A fork gets its own.
  A Janitor has none, because it never speaks.

To see which voice an agent got, read its `voice_profile.speech` row in
`GET /agents/snapshot`: `provider_label`, `voice_id` and `voice_label` (for
example "Voice: Brooke (Cartesia)"). When nothing would be heard,
`silent_reason` says why.

A script that reads `GET /agents/snapshot` or `GET /artifacts` again and again
can send the last answer's `ETag` back in `If-None-Match`: a Rust Host that
lists the feature `list_etags` answers `304` with no body while nothing changed
for that reader. Other Hosts send no tag and answer in full, so keep the body
you have and fall back to it only on a 304. While agents work, both lists change
every few seconds, so a 304 is mostly for quiet periods.
To read one agent's row rather than the whole fleet, use `GET /agents/<session>`
on a Rust Host that lists feature `agent_row` (contract 183) in
`GET /server-info`: it answers that agent's row exactly as the snapshot has it,
and 404 `agent_not_found` for a session the Host does not have. A Python Host
and older Rust Hosts do not have it: read `GET /agents/snapshot` there. On a
Host with feature `roster_fields` (contract 182), an `agent-roster` event of
kind `helper-state`, `mute-changed`, `chat_view` or a beginning
`agent-transit` carries `fields` (the changed fields of that row) and, for a
helper, `parent` (its maker's counts and state), so a watcher of `/events`
needs no read for it; and `transcript-updated` carries the conversation's
`head_revision` and `conversation_id`, so a reader that already holds that
revision skips `GET /log`.
On a Host with feature `compact_resume` (contract 201), a watcher that opens
`/events?compact=1` and resumes with `Last-Event-ID` (or falls behind) is sent
`agent-roster` with `resync: true` first, then only the newest `transcript-updated` per
chat and `agent-activity` per agent of what it missed; on
`truncated: true` read the snapshot again. Without `Last-Event-ID` there is
no replay. Without `compact=1` the stream is as before.
On a Host with feature `events_gzip` (contract 198), a watcher of `/events`
whose HTTP client decodes gzip as it streams can add `gzip=1` to get the same
events in a fraction of the bytes; without the feature, or without decoding
gzip, leave it out.
On a Host with feature `events_rows` (contract 199), add `rows=1`: a
`transcript-updated` then carries `rows` and `rows_latest`, and a watcher that
holds its `rows_from` revision or more merges them instead of reading `GET /log`.
With feature `events_rows_options` (contract 207) add `rows_automated=0` and
`rows_coordination=auto` when you read `/log` that way, so the carried rows
match your own reads.

On a Host with feature `events_live_change` (contract 209), open the stream
with `live_change=1` to follow other chats without reconnecting: its first
event gives `stream_id`, and `POST /events/<stream_id>/live` with
`{"live": ["<session>"]}` (same token) switches the set; `live-changed` marks
where it takes effect.
On a Host with feature `events_chat_previews` (contract 212), a watcher of
many chats adds `chat_previews=1&visible=<sessions shown whole>` beside an
explicit `live=` (empty `live=` for the list alone; without `live` the stream
stays ordinary): chats not both followed and visible come as `agent-roster`
`kind: "preview"` patches of their list fields (at most every 5 s) instead of
every transcript, activity and state event; send `{"live": [...], "visible":
[...]}` to the same POST to change both at once (omit `visible` to keep it).

Never expose provider API keys to the phone or place them in prompts. Creating
or editing a persona does not authorize deleting its conversation history.

## Archiving and restoring agents

On a Rust Host with `archive_by_anyone` (contract 200), any agent may archive or restore any
agent from its own turn (or a job its turn started):

```bash
clarp-admin agent archive --session <session>            # put it away
clarp-admin agent archive --session <session> --restore  # take it back
```

An archived agent keeps everything and can still be written to. It carries `archived_by`
(`{"kind": "agent", "session", "agent_id"}`, `{"kind": "owner"}`, `{"kind": "host-token"}`
for the Host's token from a script, or `{"kind": "host"}`)
while it is away, so the apps show who archived it, and the Host logs every archive and
restore. Without a verified turn the command is refused. On an older Host (contract 193 to
199) it works only for yourself and your own helpers.

## Organizing agents into teams

For requests to group the current fleet, base assignments on current evidence:

1. Resolve active agents with `clarp-admin sessions` or authenticated
   `GET /agents/snapshot`.
2. Use the `fleet-activity` skill for recent tool destinations and inspect the
   latest user requests when purpose is ambiguous. Exclude archived agents
   unless the user asks to organize archives.
3. Prefer a small hierarchy of stable projects or responsibilities. Put agents
   in leaf teams and use empty parent teams as containers. Avoid assigning one
   agent to several teams merely because it touched several repositories.
4. Keep communication and leadership disabled unless explicitly requested;
   teams are presentation-only by default.
5. Reconcile through the HTTP API, never by writing SQLite directly.

Write a JSON array in parent-before-child order, using session slugs for members:

```json
[
  {"name":"Product","color":"moss","members":[]},
  {"name":"iOS","parent":"Product","color":"gold","members":["marcus","theo"]}
]
```

Preview, then apply:

```bash
python3 scripts/reconcile_teams.py plan.json --require-all-active
python3 scripts/reconcile_teams.py plan.json --apply --require-all-active
```

The helper creates or updates named teams and replaces direct membership only
for teams in the plan. It preserves unrelated teams. `--require-all-active`
refuses a plan that omits an active agent. Success requires final verification
with no omitted or multiply assigned active agents.

## Create a designer contact and delegate an implementation

Use `GET /agents/snapshot` field `personas` for saved contacts; `GET /personas`
is not a supported listing route on the verified Host. Check both personas and
active agents before creation. For voice conflicts, parse each saved `voice_id`
JSON and compare its Cartesia ID, not the entire serialized string: older records
may contain multiple providers.

Read `GET /agent-model-options` and select an exact ID from
`providers[BACKEND].models`. `POST /agents` accepts `model` at creation; verify the
stored override with `clarp-admin model --session SESSION` before dispatch. Preserve the
returned session slug. A creation response alone does not prove a model turn ran.

For multi-step delegated engineering, commit a durable handoff in the project,
create an isolated worktree from that commit, and create one plan for the returned
session using `clarp-agent-tasks`. Send the handoff path, returned plan ID, scope,
evidence requirements and report-back destination with `clarp-admin prompt` and
an explicit `--from` session. Verify the destination's `/log` contains the message
and an actual response/tool activity. Native Clarp tracks the running turn;
separately detached workers still require the background-jobs skill.

Generated portrait art belongs in the persona API, not built-in source files.
Convert the selected generated image to a bounded square JPEG for the API, retain
the original, and publish through `clarp-media-publish` for the agent gallery.
