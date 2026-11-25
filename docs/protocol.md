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
once in the Host (`server-rs/crates/clarp-protocol`) and mirrored in
`static/lib/protocol.js` and the web app's client models.

## Vocabulary

| Term | Meaning |
|---|---|
| **agent** | One persona bound to one working directory and one backend CLI (Claude, Codex, or Antigravity). Durable identity is `agent_id`. |
| **session** | The agent's user-facing name, e.g. `rachel`. Unique among live agents. Every core endpoint addresses agents by `session`. |
| **conversation** | The agent's chat as the user sees it. Identified by `conversation_id`, which stays the agent's for as long as the conversation lasts: a relaunch of its backend CLI or a conversation fork does not change it. It changes only for a genuinely new conversation (the agent is reset, or it is a new agent). The backend CLI's own session is `backend_session_id`; it can change under one conversation, and each `/log` row says which one produced it (`session_id`). |
| **message** | One row of the conversation as the server stores it. Identified by `message_id`; ordered by `revision`. |
| **turn** | The message shape `/log` returns. The word is historical; a turn is one message. |
| **revision** | A per-conversation monotonic counter. Every insert or update of a message bumps it. It is the only ordering key a client should rely on. |
| **clip** | One synthesized voice reply, identified by `clip_id`. |
| **focus** | The server-wide "current agent" used for hands-free voice routing. Shared by every client of one server. |
| **sub-agent** | A Clarp helper agent: an agent with `role: "helper"` and a `parent_agent_id`. It has its own chat and can be opened and steered. Counted from the parent's running children. |
| **background process** | A durable background job (`/background-jobs`) that is not merely a helper's mirror: a detached worker, a watcher, a CI wait. Inspect one with `GET /background-jobs/<job_id>`. |

The HTTP service and agent runtime have separate lifetimes. A planned HTTP
restart closes client connections, but the active backend turn continues in
`clarp-runtime`; clients reconnect SSE and resume reading the durable message
cursor. Interruption markers are reserved for a runtime or host failure that
actually terminated work.

## Authentication

The server reads `auth_token` from `config.toml`. When it is empty, every
request is accepted. When it is set, every request must carry one of:

- `Authorization: Bearer <token>` (preferred)
- cookie `claude_pwa_token=<token>` (for `EventSource`, `<img>`, `<audio>`)
- query parameter `?token=<token>` (first visit only; strip it from the URL)

The token is either the administrator token from `config.toml` or a paired
device credential from `POST /pairing/exchange`. A paired device may have
`limited` scope, which allows conversation reads and the core `POST`s
(`/send`, `/transcribe`, `/upload`, `/select`, `/focus`, `/clips/ack`, `/clog`,
`/location`, `/calendar/response`, `/remote-action`). Its `GET`s are a list too:
what an app needs to chat, speak and answer decisions (`/server-info`,
`/events`, `/live`, `/status`, `/agents/snapshot`, `/log`, the turn queue,
attention, artifacts and their catalogs, task plans and the goal ledger, clips,
media, audio, avatars, voices, the speech stream, the transcription models,
providers and retained audio, controller actions and their narration). Every other `GET`, a path no route serves included, and every
`PATCH`, `PUT` and `DELETE` are refused with `403 full device access required`;
so is an artifact's export. `/server-info` answers a limited device a fixed set
of keys, without `default_cwd`. Administrative routes and
Oracle require full scope. Host filesystem browsing (`/agent-file`,
`/agent-files`, `/dirs` and `/past-sessions` with any scope) also requires full
scope: a caller-selected root can contain configuration credentials, even
though the operation is read-only. So do an agent's interactive terminal
(`/terminal/<session>`) and `PATCH /schedules/<id>`.

A limited device may `POST /paired-devices/revoke` with its own `device_id`
(an app that finds it paired the wrong Host ends its own pairing); any other
`device_id` answers `403 full device access required`. Its token stops working
at once.

`GET /pairing/identity` needs no token: it answers exactly `{"server_id",
"name", "contract": {"host"}}`, so an app opening a pairing link can compare the
link's `server_id` before it spends the one-time code. Each source may read it
30 times a minute; more answer `429` with `Retry-After` (feature
`pairing_identity`).

Limited scope is an API permission boundary, not an agent sandbox. It still
allows sending work to agents that run with the host user's permissions.

`GET /` and the static assets are public so a browser can load the shell
before it has a token.

### Cross-origin reads (feature `cross_origin_read`)

The web app is served by one Host. Its all-Hosts list reads the user's other Hosts from its own
origin, which needs CORS on those Hosts. A Host allows it only for the origins on its
`[server] web_read_origins` list (exact `scheme://host[:port]`, the user's own web apps), and
only for two routes:

- `GET /server-info` (the Host's `server_id` and name, for the Host badge and the moved line)
- `GET /agents/snapshot` (the roster: names, activity, `moved`)

For a listed origin those two answer `Access-Control-Allow-Origin: <origin>` and
`Vary: Origin`, on success and on a refusal alike. `OPTIONS` on them is the preflight: 204 with
`Access-Control-Allow-Methods: GET`, `Access-Control-Allow-Headers: Authorization,
X-Clarp-Client` and `Access-Control-Max-Age: 600` when the origin is listed and nothing else is
asked; every other `OPTIONS` is 403 with no CORS header. No credentials mode: the Host never
sends `Access-Control-Allow-Credentials`, and takes such a request with the token in the
`Authorization` header only (a cookie or URL token gets 401). A cross-origin request is never
local, so the token is a paired device's (`POST /pairing/exchange`), with that device's scope.
Every other route and every write stays same-origin. With no list, nothing is allowed
cross-origin.

### Peers' chat lists (feature `peer_snapshot`)

A Host can keep a device token for each of its owner's other Hosts (`clarp-admin peers add`,
pairing by a one-time code made on the peer). `GET /peers/snapshot` (full scope only: a
limited device gets 403) answers `{peers: {<server_id>: {name, url, server_id, reachable, error,
snapshot}}, cache_seconds}`: the Host reads each peer's `GET /server-info` and `GET
/agents/snapshot?include_archived=0` server to server with that token (15 s per read, at most 16 MiB) and returns the peer's
snapshot as the peer gave it. Unreachable: `reachable: false`, `snapshot: null`, an `error` code. Only the peers'
own configured URLs are fetched, never one from the request; redirects are refused; tokens are
never returned. The web app's all-Hosts list reads this, so the browser needs no other Host's
token; opening a peer's chat opens `url` (that Host's own web app).

### Compressed answers (gzip)

A request with `Accept-Encoding` naming `gzip` (and not weighing it `q=0`) gets a whole JSON,
text or script answer of 1 KB or more gzipped, with `Content-Encoding: gzip`; every such answer
carries `Vary: Accept-Encoding`, gzipped or not. The decoded body is byte for byte the plain one.
The Python Host has always done so; the Rust Host does since issue 805, where a snapshot of about
310 agents went from about 1.25 MB to a fraction of it. Never gzipped: event streams (`/events`
and every other answer sent as it is made), WebSocket upgrades, a request with `Range` and a
partial answer, media and files that are no text, answers without a body (`HEAD`, 204, 304) and
answers above 64 MiB. URLSession and OkHttp ask for gzip and decode it themselves; a client that
sets `Accept-Encoding` by hand must decode what it asked for. Through the connection relay the
Host is asked for gzip only where the relay passes the packed body through untouched. The Host's
recorded request durations (diagnostics `requests`) include the packing.

### Unchanged lists: `ETag` and 304 (feature `list_etags`, Rust Host contract 181)

`GET /agents/snapshot` and `GET /artifacts` (any query) answer 200 with a weak `ETag`,
`W/"<32 hex signs>"`; one tag names the answer whether it is sent plain or gzipped. Send it back
in `If-None-Match` on the next fetch of the same URL. While the answer would be byte for byte the
same for the same caller, the Host answers `304 Not Modified` with the tag and no body, and the
client keeps what it holds. The tag is a digest of the answer as it is sent and of who reads it:
the owner (a paired device, the administrator without a turn), a device paired with limited
access, each agent by its turn, an agent's tool without a turn, and another Host each get tags of
their own, so a caller shown fewer agents (feature `bound_privacy`) or fewer fields never holds a
tag that matches the owner's answer. Compare a tag for equality only; it is no revision.

What a 304 saves is the transfer and the packing, not building the answer: the Host builds it to
know whether it changed. Both lists change with the clock as well as by events (agents' activity,
an artifact's state, a plan's `server_now` and `elapsed_ms`), so on a Host whose agents are
working a fetch seldom gets a 304; on a quiet Host most do. Both answers (and their 304s) say
`Cache-Control: private, no-cache`: a client may keep them for itself, and must ask the Host
before each use. An HTTP stack whose cache keeps the answer then sends `If-None-Match` by itself
and hands the app the kept body on a 304. That holds only where the cache keeps it: URLSession's
shared `URLCache` skips a response larger than about a twentieth of its disk size (a full snapshot
or artifact list of a large fleet can be), and OkHttp caches only with a `Cache` configured. An
app gets 304s once its client keeps the response. The iOS build that carries the list cache keeps
these two answers in a memory-only `URLCache` of 32 MB, so an answer of up to about 1.6 MB
(decoded) is revalidated by URLSession itself; a larger one is fetched whole. Any other app
that wants the saving keeps the body and its tag itself and sends `If-None-Match` explicitly. Every other route stays `no-store`. A Host without the feature sends no `ETag` and ignores `If-None-Match`.

## Core conversation protocol

### 1. Bootstrap: `GET /agents/snapshot`

The one call a client makes to learn everything about the server's agents.
Call it on start, on every SSE (re)connect, and whenever an `agent-roster`
event arrives (on a Host with features `roster_fields` and `agent_row`, only for
the events that section names: the others carry their change, or name the one
row to read).

```json
{
  "agents": [
    {
      "agent_id": "…", "session": "rachel", "persona": "Rachel",
      "backend": "claude", "cwd": "/home/me/proj", "model": "", "effort": "",
      "voice_id": "…", "avatar_symbol": "", "avatar_url": "/avatars/<agent_id>?v=<hash>",
      "model_avatar_url": "/static/avatars/models/rachel.opus.png?v=<hash>",
      "mcp_servers": [], "heartbeat_enabled": false, "dreaming_enabled": false,
      "muted": false, "archived_at": null,

      "backend_session_id": "…", "conversation_id": "…",
      "head_revision": 812, "last_message_id": "…", "last_message": "…",

      "alive": true, "busy": false, "focused": true,
      "latest_state": "idle", "latest_state_ts": 1756800000000,
      "turn_started_at": 0, "last_activity": 1756800000000, "last_turn_end": 1756799000000,
      "status_text": null, "activity": { "…": "see agent-activity" },
      "compacting": false, "context_tokens": 12345, "context_window": 1000000,
      "last_compaction": null,
      "queued_turn_count": 0, "queued_turn_revision": 0, "queue_paused": false,
      "backend_quota": null,
      "background_jobs": {"count": 0, "sub_agents": 0},
      "parent_agent_id": null, "role": "agent", "helper_state": null,
      "child_count": 0, "running_children": 0,
      "team_ids": []
    }
  ],
  "focus": "<agent_id or null>",
  "roster": ["Rachel", "Mike", "…"],
  "personas": [ { "…": "saved contact definitions" } ],
  "available_mcp_servers": ["…"],
  "model_avatars": false
}
```

Rules:

- **Compaction says what it did (`compaction_outcome`, R148).** `POST /compact
  {"session": …}` is the only way the Host compacts an agent: it never does so
  by itself, on a timer, at a threshold or at launch. It answers at once
  (`{"ok": true, "status": "started"}`, `"already_compacting"`, or
  `{"ok": false, "error": "<reason in words>"}`; 409). When it cannot start
  at all it still answers in that shape: 503 with `"retryable": true` while
  the runtime is unreachable, 500 otherwise; `compacting` is true while it
  runs, and a send that arrives meanwhile waits and runs after it, in order.
  When it ends, `last_compaction` is `{"outcome", "before_tokens",
  "after_tokens", "after_is_estimate", "detail", "backend", "at"}` (`at` in
  ms; `null` until the agent's first compaction). `outcome` is decided from
  sizes, never from how long it took:
  - Codex: `compacted` needs both sizes, with `after_tokens` <
    `before_tokens`. `before_tokens` is the size Codex last reported for the
    thread (after a runtime restart: the newest token count in the last 64 KB
    of its rollout); `after_tokens` is Codex's own estimate of the new history
    (`after_is_estimate: true`). The compaction runs in the Host's app-server,
    the process that holds the thread.
  - Claude Code: `compacted` on Claude Code's own record of it, the
    `compact_boundary` it writes after `/compact`; `before_tokens` is that
    record's `preTokens`, and `after_tokens` stays `null` (the new size is
    known at the agent's next call, when `context_tokens` shows it; until
    then `context_tokens` is `null`). No record: `not_compacted`.
  - `not_compacted`: refused, failed, stopped, finished without replacing the
    history (Codex), or the conversation did not shrink. `unknown`: a size is missing or it did not finish in time;
    always for a backend that gives the Host no size (Antigravity, Grok).
    `detail` says why in words.
- **Archived agents are optional.** A plain `GET /agents/snapshot` carries
  every agent, archived ones too (`archived_at` set). On a Host that has run
  for a while most agents are archived helpers, and a client pays for them on
  every connect. A client that does not need them at connect asks for
  `GET /agents/snapshot?include_archived=0` (Hosts that advertise the feature
  `snapshot_without_archived`). Only the value `0` does this; `false`, `no`
  or anything else is the full list. `agents` then holds only agents that are
  not archived, and the extra key `archived_count` says how many were left
  out. Everything else is the same as in the full snapshot, and each
  remaining row is the row the full snapshot has. An older Host ignores the
  parameter and answers the full snapshot, so keep filtering on
  `archived_at`. The archived agents are read with `GET /agents/archived`,
  below. A client that opts in must expect:
  - `focus` can name an agent that is not in `agents`: archiving an agent
    does not clear the focus.
  - A row's `parent_agent_id` can name an agent that is not in `agents`: an
    archived agent can still have helpers that are not archived. The key
    `archived_parents` (Host contract 83) says which: it is the list of ids
    of archived agents that some row in `agents` names as its parent. A
    parent id that is neither in `agents` nor in `archived_parents` belongs
    to a deleted agent. So a client that hides a helper under an archived
    parent and shows a helper whose parent is gone can keep doing both
    without loading the archive. The list is empty when there is none, and
    the key is absent from a contract 82 Host, where the two cases cannot be
    told apart until the archive is read.
  - `child_count`, `running_children` and `background_jobs.sub_agents` count
    archived helpers too, and `roster` still holds the names of archived
    agents.
  What the Host leaves out for an archived agent is building its row: no
  transcript read, no message preview, no per-agent statement. It also does
  not check it for a stuck state on the way out, with one exception:
  archiving does not stop a turn, so an archived agent that still looks
  active is checked and repaired as before: its last state is a busy one, it
  has a live process, the turn dispatcher still holds its slot (a slot left
  behind by a turn that died is freed this way), or, on a Host whose turns
  run in a separate runtime process, that runtime holds queued work for it.
  An archived agent that is idle but whose bound session has lost its
  transcript is repaired when the archive is listed or the agent is restored.
- `head_revision` and `conversation_id` are the same values `/log` reports.
  If a client already holds a cached chat for this agent, it is current iff
  the cached `conversation_id` matches and the cached revision equals
  `head_revision`. Anything else means: fetch.
- `busy` means a turn is running. `latest_state` is one of the agent states
  below and is the value to render, not `busy`.
- The snapshot is read-only: calling it never changes server state, so it is
  safe to call as often as needed (the server reconciles stale state on the
  way out).
- `model_avatar_url` is the bundled portrait drawn for the model this agent
  runs on, empty when none is bundled for that persona and family. It is
  offered whether or not the user wants it; `model_avatars` is the Computer's
  preference, so a client can honour a toggle without refetching. The server
  leaves it empty for an agent with a portrait the user chose, so preferring
  it can never replace an uploaded or generated picture.
- `backend_quota` is `null` unless fresh provider evidence says this agent's
  backend cannot serve a new turn. Otherwise it is `{"state": "exhausted",
  "provider_id", "reason" (credits_depleted: the workspace must be refilled |
  usage_limit: wait for the reset), "window" (five_hour | seven_day | unknown), "resets_at"
  (ISO time or null when the provider gave none), "observed_at"}`. An agent never switches provider when its quota runs out (host contract
  26 removed the `fallback_backend` and `fallback_model` fields; older apps
  read them as absent). It is a warning to show before the user sends, never a
  reason to refuse the send: an account switch may still serve the turn.
  The Host refreshes usage every five minutes and sends `agent-roster` with
  `kind: "backend-quota"` when the picture changes.
  On a Rust Host from contract 167 (feature `backend_quota_near`) it can also be
  `{"state": "near", "provider_id", "reason": "usage_limit", "window"
  (five_hour | seven_day), "used_percentage" (0 to 100, below 100), "resets_at"
  (ISO time or null), "observed_at", "failover" (none | unknown)}`, the shape of
  `exhausted` with `used_percentage` and `failover`; and `exhausted` gains
  `used_percentage` too, the share of the window it names (0 to 100), null where
  that is not known (window `unknown`). On both, `resets_at` is UTC to the
  second (`2026-11-23T18:00:00Z`), whatever form the tool gave it in. The account
  this agent runs on is near a limit. It starts at 80% of the five-hour window
  (about an hour of steady work left; the same share as the five-hour
  `provider-limit` warning) and at 90% of the weekly window (80% of a week would
  leave a warning up for more than a day), names the most used window, and
  stands until that window resets or the tool is on another account (on a login
  the Host cannot name, only while the word of it is fresh); at 100% the
  row is `exhausted` instead. It is set only when no other seat of the tool would
  take the turn, since account switching would hide the limit: `failover: "none"`
  means the Host knows no seat would (it switches no accounts, or its seats
  reading shows no other seat below those shares for the model the agent runs,
  its own or the tool's default), `"unknown"` that it cannot know (`--accounts
  follow`, where another Host switches, or a seats reading that is old, failed,
  missing or does not say enough). A Rust Host gives no `near` where it cannot
  know (no new number; before this, it warned with `failover: "unknown"`): a
  warning is for a pool near its end, never for one seat while others may have
  room. Where it switches the seats itself and its reading of them is old or
  missing when a row would turn `near` or `exhausted`, it reads them itself, at
  most once a minute for the tool, and judges again when the reading is in (the
  apps are told by `agent-roster` as below); so `near` carries `failover:
  "none"` only. `exhausted` still stands with `failover: "unknown"` where it
  cannot know: a used-up account is never hidden. The Host looks again every five minutes, a second after a named
  window resets, and after each seats reading or switch made from the desk or
  its switcher (a switch by the owner's selector is seen at the next turn end or
  look), and sends `agent-roster` with `kind: "backend-quota"` when the warning
  appears, changes window or reset (to the minute), grows by a whole percent,
  changes `failover` for a model its agents run, or clears.
  Example: `{"state": "near", "provider_id": "claude", "reason": "usage_limit",
  "window": "five_hour", "used_percentage": 84.0, "resets_at":
  "2026-11-23T18:00:00Z", "observed_at": "2026-11-23T15:12:40Z", "failover":
  "none"}`.
  From contract 176 (feature `quota_pool`) a Rust Host judges `exhausted` by
  the switcher's own choice: the row is `null` while the Host's switcher would
  move a turn of the model the agent runs (its own or the tool's default) to
  another seat now, asked on the switcher's own reading of the seats (what the
  tool said of each seat, including a seat it calls unavailable, models it
  closes, a degraded seat and a seat that may run over its week), with the seat
  in use taken as used up. The seat in use is the one the last switch moved to.
  Otherwise `exhausted` gains `failover` (`none` | `unknown`, as on `near`), and
  where the switcher would move the turn nowhere, `resets_at` is the soonest
  moment a seat that could serve the agent's model regains room (the one in use
  among them), where that is sooner than the reset the tool named; `window` and
  `used_percentage` stay those of the account in use. When that moment comes the
  Host reads the seats again, and the row clears if the seat has room (until that
  reading, `resets_at` stays the moment that passed, never a later one). A seats
  reading that is old or failed never hides a used-up account: the row stands
  with `failover: "unknown"` and the account's own reset.
  By kind of Host: one that switches no accounts (`--accounts none`), or one
  with neither the owner's selector nor an automatic switcher lane for the tool,
  says `exhausted` as before with `failover: "none"`. One beside another Host
  (`--accounts follow`) cannot know the other's seats: `failover: "unknown"`, the
  row stands. One with the owner's selector and no automatic lane reads the seats
  when an app asks for a reading, when a seat a row waits on regains room, and,
  where its reading is old (ten minutes after it was made), when a row would turn
  `near` or `exhausted`; until that reading is in, the row says `exhausted` with
  `failover: "unknown"`, or no `near`. A tool with no pool this Host switches is
  one account and its whole pool, so its `failover` is `none` and `near` shows:
  a tool with neither the selector nor the automatic lane (Antigravity), one the
  owner's seat tool leaves out of its list while it lists another, and every
  tool on a Host without a seat tool.
  `GET /backend-usage` then gives each tool whose seats were read a `pool`
  beside its `windows` (`providers.<tool>.pool`): `{"seats" (in all),
  "with_room" (seats with room for a turn of no model in particular, as the
  switcher reads them), "used_up", "active_alias" (the seat in use, or null),
  "next_alias", "switched" (the last switch of the tool's seats this Host made, by
  its switcher or by hand: `{"from_alias", "to_alias", "at"}`, or null; a switch
  by the owner's selector alone shows as a new `active_alias`), "next_reset_at"
  (the soonest moment a used-up seat regains room,
  or null), "regains_room_at" (only where every seat is used up: when the first
  regains room; else null), "stale", "observed_at"}`, moments in UTC to the
  second. `next_alias` is the seat the Host's switcher would move to were the one
  in use used up (the owner's selector may choose otherwise), null where no
  other seat has room or the reading is stale. A seat that cannot be read is
  counted in neither number; on a `stale` reading (old, failed, or the switcher
  off) `with_room`, `used_up` and `next_alias` are null and `regains_room_at` is
  null, since an old reading says nothing of now. A tool with no seats read has
  no `pool`. The Quota keeper's push that a window of the account in use runs low
  is not sent while another seat would take the turn (its receipt is cancelled,
  "Another seat of the pool has room; the Host moves to it").
- `parent_agent_id` is the agent that created this one (a helper's parent,
  or a fork's source), else `null`. `role` is `agent`, `helper` or `janitor`.
  `helper_state` is `null` unless `role` is `helper`; then it is `running`,
  `reported` (its report reached the parent, or its own session sat idle
  past `[agents] helper_idle_after_minutes`, 30 by default), `ended-unreported`
  (Rust feature `helper_ended_unreported`, its last turn settled without a report
  and it has no queued or background work), `done` (the parent or the user
  accepted it), `failed` or `abandoned` (the parent was deleted or archived;
  helpers are flagged, never deleted with it). On a Rust Host a `reported`
  helper reads `running` while a turn of its runs (a progress message to its
  parent marks it reported while it works on) and `reported` again once that
  turn is over; an `agent-roster` event (`kind: helper-state`) is sent at
  each such change. `GET /agent-helper-state` answers the same. `done` stays
  `done` while the helper works. `child_count` counts live
  agents whose parent is this one and `running_children` those still
  `running`. Nest helpers under their parent; a done helper is archived
  after the grace period (`[agents] helper_archive_grace_hours`, 24 by
  default). `agent-roster` with `kind: "helper-state"` asks for a refetch; on
  a Host with feature `roster_fields` it carries the helper's and its
  parent's changed fields instead ([below](#one-agents-change-in-its-roster-event-and-one-agents-row-features-roster_fields-and-agent_row-rust-host-contracts-182-and-183)).
- `background_jobs` separates the two kinds of work that keep an agent busy
  after its turn ends. `sub_agents` is the number of its helper agents still
  `running` (the same number as `running_children`). `count` is the number
  of active background processes: jobs that are not the watcher job
  `clarp-sub-agent --clarp-agent` registers for a helper (kind `sub-agent`
  whose `detail` or `metadata.helper_session` names one of this agent's
  helpers), so a helper is never counted twice. Either makes `latest_state`
  `background`. Show the numbers from `background_jobs` (a badge);
  `status_text` does not repeat them. When the agent has set no status of
  its own it describes the activity instead: one running helper as
  "slice5-helper: running tests" (the helper's own status, else its latest
  activity summary), several as a roll-up such as "3 working, 1 waiting"
  (`working`, `waiting`, `idle` from each helper's latest state), and a
  process by its latest progress line or title ("CI: build 3/10", the most
  recently active job when there are several). Helpers and a process are
  joined with " · ". Only a process with neither title nor progress falls
  back to a bare "1 background process". Before Host contract 16 `count`
  included the mirror jobs and `sub_agents` counted jobs of kind
  `sub-agent`.
- There is no `/sessions` in this layer. The list of chats is
  `agents[].session` filtered by `archived_at == null`.

#### Archived agents: `GET /agents/archived`

The agents `include_archived=0` leaves out, a page at a time, for an archive
screen. `limit` is a whole number from 1 to 200 written in plain digits
without a leading zero (default 50); `cursor` is the `next_cursor` of the
previous page.

```json
{
  "agents": [ { "…": "the same row as in /agents/snapshot", "archived_at": 1756800000000 } ],
  "next_cursor": "<opaque, null on the last page>",
  "total": 1019
}
```

Rows are in the order most recently archived first. A request without a
cursor asks for a current listing: the Host builds every archived agent's
row and holds that listing for five minutes. A request with a cursor is cut
from the listing the Host holds at that moment, without building anything,
whichever client's first page built it. So the Host builds once per first
page, not once per page, however many clients page at the same time.

A cursor names a place in the order (the last agent of the page before), not
an offset and not a listing. A walk therefore goes on from the same place
when the held listing has been replaced by another client's first page, when
it has expired (the Host builds a new one for that page) and after a Host
restart. What a walk returns:

- A later page shows the listing the Host holds when the page is asked, and
  that listing can be up to five minutes old. Its rows are as they were when
  it was built: an agent restored since then is still returned, with the
  `archived_at` it had; an agent archived since then is not in it; an agent
  restored and archived again is still at its old place with its old
  `archived_at`. Only a first page, any client's, or the five minutes bring
  a current listing.
- No agent is returned twice.
- An agent that is archived for the whole walk is not missed.
- `total` is the number of agents in the listing that answered the page, so
  it can differ from one page to the next.

Start again without a cursor for a current listing.

What a first page costs the Host: it builds every archived agent's row,
which is the work `include_archived=0` saves a connect, and the Host builds
one snapshot or listing at a time, so every snapshot request, with or
without `include_archived=0`, waits while that build runs. Ask for a first
page when the user opens the archive, not on connect and not on a timer.

A `limit` that is not 1 to 200 written that way (`0`, `201`, `007`, `+5`,
`5.0`), or a cursor the Host did not issue, is
`400 {"error": "invalid archived agents query"}`; an empty value counts as
absent. A cursor is good for the Host build that issued it: after a Host
update it can be refused, so on a 400 start again without a cursor. A Host
without the feature answers 404; read the archived rows from the full
snapshot there.

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
| `before=<message_id>` | **Older history**: the `limit` messages older than that message. An older page may hold again a row the newer page had: merge by `id` (rules below). |
| `include_automated=0` | Hide heartbeat, leader, dreaming, and watcher traffic. |
| `include_tool_details=0` | Collapse tool cards to one summary card per message; fetch `/message-tool-details?session=&message_id=` on demand. |
| `coordination=auto\|hide\|show` | Feature `hidden_coordination`: leave coordination between agents out (`hide`), keep it (`show`), or follow the chat's setting (`auto`). Absent: every row, as before. |
| `view=conversation\|session` | Feature `chat_view`: `conversation` returns only the rows of the conversation view (before the page limit) and echoes `view`; `session`, or absent, returns every row as before. |
| `coordination_read=show\|hide` | With `coordination` on a delta: the `visibility` the client's cache was read under. When it differs from the answer's, the answer says `replace_required: true`. |

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
      "reply_to_agent_id": null, "reply_to_name": null, "reply_to_session": null,
      "automation_kind": null,
      "audience": "user | agents"
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
- **Any other `id` is opaque.** The Host's own ids come in forms (`live-…` for
  a row a turn streamed into, and for the first block of a provider message the
  import stores under the same id; `blk-…` for that message's later blocks;
  `msg-…` for a line without a provider message id, which follows its position
  in the transcript), but a client never parses them: compare ids for equality
  only. A row of a provider message keeps its id when the transcript's lines
  shift; a positional `msg-` id changes with its line's position.
- **The spoken part is not the written body.** An assistant row's `text` keeps its voice
  markup. What it marks with `<speak>…</speak>` is the part said aloud (the Host makes it a clip
  where speech is set up). An app shows it as a separate spoken block, apart from the written
  body (iOS: unfolded and foldable, with replay; Android may draw a spoken summary row, for a
  long reply), or leaves it out (the desktop). The written body is the text outside the
  `<speak>` blocks, with the voice's own markup (`<vox>`, `<break>`, emotion tags) taken out. A
  row with nothing written has its spoken words as its answer.
- **A row's text comes before its tools.** An assistant row's `text` is what the agent wrote, and
  its `tools` (and `display_cells`) the calls its message made, in the order it made them: draw
  the text first, then the calls, while the turn runs and once it is settled alike. A message
  that wrote, called a tool and wrote again (Claude, text and `tool_use` blocks in one message)
  is one row on the Rust Host: its `text` holds all its words, the blocks joined by a blank line,
  and its `tools` its calls. Where between the words a call stood is not kept, so it is drawn
  after them; the live turn shows the message's item before its call's item too. A row imported
  from a transcript is split at the message's blocks (`blk-…` rows, above), so there the rows
  stand in the order the agent wrote them.
- **The other agents a row names are the roster's.** The other agents a row
  names (`sender_name` and `sender_session` by `sender_agent_id`,
  `reply_to_name` and `reply_to_session` by `reply_to_agent_id`) are the
  roster's at the moment of reading. They change without the row's revision
  moving: a follower learns of a rename or a removal from the roster's events,
  not from the row. A removed or archived agent is named as it was last, until
  a new agent is created under the removed one's session name: the new agent
  takes over its id, and the old rows then carry the new name.
- **`latest_revision` is the cursor.** Store it with the conversation and pass
  it back as `after_revision` on the next delta. After a delta, set the cursor
  to the response's `latest_revision`, which never exceeds the rows actually
  returned (when `has_more` is true the server holds the cursor back so the
  next call continues the backlog).
- **A goal's check-in is the Host's own (Rust Host contract 163, feature
  `goal_wake_quiet`).** The message a durable goal wakes its agent with is an automated row:
  `automated` true, `automation_kind` `goal` and the text "Goal check-in" in place of the
  prompt, which only the agent's tool reads. Its queue item reads the same, and it cannot be
  reworded. What the agent answers in that turn is an ordinary reply. An agent's check-ins
  among its newest rows that were written before its Host showed them so are sent once more,
  at a newer revision, so a delta gives an app that holds one the row as it is now, to replace
  where it stands; one the app does not hold belongs where its timestamp puts it.
- **A turn on another model provider than its agent's is a failure of its own (Rust Host
  contract 170, feature `codex_own_provider`).** The kind `provider_mismatch`: see
  [Agent lifecycle](#8-agent-lifecycle). It is never tried again.
- **A conversation too long for its model is a failure of its own (Rust Host contract 166,
  feature `prompt_too_long`).** Such a turn fails with the kind `prompt_too_long`, and its
  reason is "This conversation is too long for the model. Compact it or start a new one",
  with the tool's own words after it. It is never tried again and waits for no account.
  Before contract 166 it was `runner_exit`.
- **A turn's end says why it did not complete (Rust Host contract 162, feature
  `turn_end_reason`).** The row that closes a turn carries `turn` (`turn_id`, `status`,
  `started_at_ms`, `ended_at_ms`, `worked_ms`, `tool_count`). When `status` is not
  `completed`, `turn.reason` is the Host's own sentence of why it ended: the failure as its
  user is told it, who stopped it and why, what interrupted it, or that a newer message's
  turn replaced it. It is absent on a completed turn and on ends written before contract
  162. It adds no row: an automated turn that fails stays quiet in the chat.
- **Pages read back with `before` can share rows; merge them by `id`.** A page holds each
  row once, with its newest version, at its place in the conversation. A row that was written
  again after its place (a tool call that ended after later rows were written, a row the Host
  updated) can come again on the older page read with `before`, with the same or a lower
  `revision`. Merge pages by `id` and keep the higher `revision`, as for any two copies of a
  row: a row two pages hold is one row, never shown or counted twice (issue 730).
- **`has_more`** on a tail snapshot or an older-history page means older
  messages exist before the first one returned. On a delta it means more
  changed messages exist after the last one returned; call again.
- **`replace_required: true`** on a delta means the server cannot express the
  change as a delta (the conversation was rebuilt or rewritten). Discard the
  cached chat and load a tail snapshot.
- **`conversation_id`** changing between two responses for the same session
  means a genuinely new conversation: the agent was reset, or it is a new agent.
  Discard the cached chat. A relaunch of the backend CLI or a conversation fork
  never changes it (Rust Host contract 153; before that, and on the Python Host, a
  relaunch could). A Host may keep the id across a reset and say so with
  `replace_required` instead; either way, drop the cache and reload the tail.
- **Every row carries `conversation_id` and `session_id` (Rust Host contract 153,
  feature `conversation_fork`).** `conversation_id` is the agent's, the same value as
  the response's. `session_id` is the backend session that produced the row: the agent's
  own, or its conversation fork's (below). It is absent where the Host does not know it
  (rows written before its tool named a session, rows brought from another Host).
- **Conversation forks (feature `conversation_fork`, Rust Host contract 153, issue 718).**
  In Conversation view, an agent the owner names in the Host's `[conversation_forks]
  agents` setting answers a message of the user's at once while its own session works on
  a turn whose audience is agents: the Host copies the agent's Claude Code session, and
  that copy (the fork) answers in the same chat. Apps draw nothing new unless both the
  feature is listed in `/server-info` and the agent's row in `/agents/snapshot` has
  `conversation_fork: true` (the setting allows that agent to fork).
  - `/log?session=X` returns the fork's rows in the main chat's order, as ordinary rows
    with `audience: "user"`; their `session_id` is the fork's, for every row of a turn the
    fork answered (the user's message included). The fork never appears as a chat or
    roster entry of its own.
  - `/live?session=X` while a fork's turn runs: `turn` and `items` are the fork's turn,
    which is what the user talks to. The agent's own turn is told beside it in
    `main_turn`: `{turn_id, audience, status, started_at_ms, tool_count, activity}`, where
    `activity` has only `state` and, for a tool, `tool.name` (no headline); `null` when the
    agent's own turn is not running. Without a fork's turn, `main_turn` is absent.
  - On the live event stream, changes of the agent's own turn go out as the op
    `{op: "main_turn", conv, main_turn}` (the same object, or `null` when it is no longer
    running or the fork's turn has ended); a reducer that does not know the op leaves it
    alone. The fork's turn and the agent's own are shown from two numberings of events: when
    one takes over from the other, the next event is numbered past what a client holds, so
    it fetches `/live` again. Neither turn is ended for the other.
  - `POST /stop` for the session stops the fork's turn when one runs, in one step, and
    leaves the agent's own turn and its queue as they are (`queue_paused: false`). To stop
    the agent's own turn then, send `target: "main"` in the body. A fork's turn the user
    stopped ends as `interrupted`. `clarp-admin stop` always stops the agent's own turn.
  - Further messages of the user's before the merge go to the same fork. The merge note,
    which tells the agent's own session what the fork was asked, said and did when its next
    turn begins, is the Host's alone: apps show nothing for it.
  - A fork that cannot start, or fails before it answers, loses no row: the user's message
    stays a normal queued message and runs as it would have without a fork. A Stop of a
    fork that has not begun to answer settles its message as stopped; it does not run later.
  - Never two sessions answer the user at once: a message of the user's that arrives while
    the fork's turn runs and the agent's own session is free waits, and the agent's own turn
    for it begins when the fork's turn ends, opening with the note.
  - Known limit: the fork takes the user's message when its first reply or tool call is read.
    If the agent's own turn ends in the moment between the fork's first action and that read
    (or while the Host is away), the message is given back and answered once by the agent's
    own session; the fork's first action (a relay, a note) may then have gone out as well.
- **`missing: true`** with an empty `turns` means the agent has no
  conversation yet. Render it as empty, not as an error.
- **Where a turn's working line goes (feature `live_read_rows`, Rust Host contract 185).** After
  the last row in the live turn's `read_rows` (the messages read into the running turn, oldest
  first; [Live items](live-items.md) §1.4). Where `read_rows` is absent, as before: after the
  turn's own message, before the first row written after the turn began. A message steered into
  a running turn is in `read_rows`, so the working line goes below it; a message that waits for
  the next turn is not, so the working line stays above it.
- **A message that waits enters the conversation where its turn starts (feature
  `waited_send_order`, Host contract 115).** A user row is written when the
  message reaches the agent: at once for a send that starts a turn or is steered into one; for
  a send that waits (the agent is busy and cannot take it), only by the write that starts its
  own turn. Rows are never moved or re-timed: a row's place is its place for good. Such a row
  carries `sent_at` (ISO, the same form as `timestamp`): the time it was sent; `timestamp` is
  when its turn started, or one millisecond after the row above it where that is later (the
  Rust Host, issue 798: every row is later than the row above it in the conversation, so a
  client that orders rows by `timestamp` draws them in the conversation's order even when the
  answer before it ended in the same millisecond; and the end of the turn before it, which
  gives that turn's last row a new `revision`, comes before it in a delta). `sent_at` is absent
  on every row that did not wait. While it waits the
  message is an item of `GET /turn-queue` (see Queue), not a row. A client may rely on this:
  an accepted message is either a queue item or a row, never both and never neither, because
  the write that starts its turn writes the row and takes the item out of the queue in one
  transaction. A local bubble whose `client_msg_id` is in neither was deleted.
- **Delivery state (feature `send_delivery_state`, Host contract 78).**
  A user row whose current `trace_id` has a launch
  receipt carries `delivery: {state, rev, seq, for: {client_msg_id,
  trace_id}}` with `state` one of `pending_launch` (saved, waiting for
  handoff; a display hint, never permission to retry), `stopped` (stopped
  by the user's Stop before handoff; a send the paused queue still holds is run
  by the resume and then reads `handed_over`) or `handed_over` (handoff recorded,
  outcome unconfirmed; not "started"). `handed_over` is recorded only once
  the prompt can have left: a steer the backend refused before sending
  anything leaves or puts the receipt back at `pending_launch` (new `rev`),
  and the send then runs from the queue (Host contract 103). Absent means unknown, and the whole
  block is omitted when the Host cannot observe the same F4-capable runtime
  process before and after its read. Every response carries
  `delivery_stop_generation` (the page's projection epoch) and
  `delivery_scope {server_id, agent_id, runtime_release, runtime_instance}`.
  Optional parameters, independent of `after_revision`:
  `delivery_since=<seq>&delivery_stop_generation=<G>&delivery_runtime_release=<R>&delivery_runtime_instance=<I>`
  returns `delivery_updates` `[{message_id, delivery}]`, `delivery_cursor`,
  `delivery_more` and `delivery_cursor_expired`; a cursor is valid only with
  the exact epoch and runtime scope it was taken under, so any runtime
  restart resets it. Receipts changed by an older runtime stay unknown. `delivery_since=0` (or `delivery_bootstrap=1`)
  starts a bootstrap pass that fixes `delivery_until` and the epoch on its
  first page and must be continued with
  `delivery_bootstrap=1&delivery_since=<cursor>&delivery_until=<B>&delivery_stop_generation=<G>`
  until `delivery_more` is false. Expired means: reload the chat and
  bootstrap again. A `/send` 200 carries no `delivery`: the client learns it
  only from `GET /log`; every 503 body is unchanged. The fields are served
  only when the Host can bind the answering runtime process to the runtime
  unit's main process with one bounded manager read per `/log` response
  (the runtime reports its pid, start time, boot, cgroup and invocation in an
  internal `attestation` status field); otherwise they are omitted.
- A growing assistant reply appears as one message whose `text` and `revision`
  change on successive deltas. Replace it in place.

Provenance. `origin` says what triggered the row. `sender_*` names the
**author** of an incoming `user` row written by another agent (`origin: agent`);
the assistant row that answers it keeps `sender_*` empty and carries the answered
agent in `reply_to_*`. Render that as a compact "Replying to …" marker, never as
the other agent's message: the current agent wrote it, and nothing forwarded it
onward unless the agent itself sent a message.

Coordination between agents (Host contract 108). `audience` says who a row is
for: `agents` when the turn it belongs to was started by another agent's
message, else `user`. Every row of that turn has it: the incoming prompt, the
live and final rows, and the thinking, tool and reply rows the transcript
import brings in. The user's own word (origin `user` or `oracle`) is never
coordination, whatever sender it names. Otherwise a message is another agent's
when its sender is another agent of this Host, or when it arrived as an agent's
(`origin: agent`) with no sender this Host knows (an agent of a peer Host); a
message an agent sends itself is not. A send made from an agent's own turn is
that agent's, whatever origin it claims, and so is a peer Host's (it names itself with `X-Clarp-Peer`, which only the Hosts' own peer senders set and the relay never forwards); addressed to itself it is the agent waking itself (`origin: automation`). A send that names a sender and no origin is an agent's message (`origin: agent`), as it always was. The mark is the flag, and the Host sets it: it reads it from the turn's
trace, never from a row's `origin`, and no sender can set or clear it. An agent
never hides anything by its own say-so. It can only show something, by an
explicit act addressed to the user: a note (`POST /agent-notes`), a decision
card or an artifact, each shown whatever turn made it. A coordination turn the
user steps into (a message of theirs steered into it while it runs) is `user`
from then on: its rows get new revisions, so a delta carries them again, and it
never goes back to `agents`, except on a Rust Host from contract 164 (feature
`turn_audience_returns`): a note that answers the user there gives the rest of the turn back
to the agents, and a later message of the user's makes it `user` again ([Live
items](live-items.md)). An agent's message steered into a turn the user
started is a row of its own trace and is the only row marked. A turn that holds the user's own word is never `agents`, whatever arrives in it. `POST /send` refuses (409) a `trace_id` that already belongs to another message (an admission, a queued send, a turn or a row) unless the request is a retry of that very message: the same `client_msg_id`, sender and origin. Rows from before
contract 108 are `user`. An agent that cannot leave a note (its backend gives its turns no turn token) is never hidden: its coordination turns read `user`, whatever the setting, and a turn marked while it could leave no note stays shown after it moves to a backend that can. Until feature `hidden_coordination` is advertised

**Hidden coordination (feature `hidden_coordination`).** Under `coordination=hide`, or `auto`
on a chat whose effective setting is `hide`, the rows of a turn that is `agents` are left out
before the page limit: a page of N is N rows the user sees, and older pages page past them.
A note is never left out, and neither are the turns of an agent that cannot leave a note.
Nor is a relay of the user's words by the owner's front desk (a message from the verified own
turn of a session in `[agents] front_desk_sessions`): its turn reads `user`, and steered into a
turn that is `agents` it makes that turn `user` from then on, as the user's own message does.
Every answer to a request with `coordination` (the newest page, each older page, each delta)
echoes the policy it was read under: `coordination: {hidden, source, visibility}`, with
`source` one of `global`, `chat`, `request`, and on a Rust Host `helper` (a helper's own chat
with no choice of its own, which shows coordination whatever the Host's choice is). A visible row that follows rows left out carries
`hidden_before: {messages, exchanges, agents: [{agent_id, name}], capped}`. The count runs from
the previous visible row of the chat, not of the page, so a run is counted once, on the same
row, however the pages fall. A tail page or a delta carries `hidden_after` (same shape) for the
rows left out after the newest visible row. A count reads at most 100 messages (or 400 rows of
any kind) of its run, nearest its row (`hidden_after`: nearest the newest visible row), and
says `capped: true` when another message of the run lies beyond them: draw it as "100+
messages between agents". A run of exactly 100 messages is not capped. `messages`, `exchanges`
and `agents` are then what those first rows hold, not the whole run; a page never waits on
counting a long run. `capped` is `false` on a count that holds the whole run. Hidden rows still move the revision: a delta that holds only
hidden rows answers no rows and the new `latest_revision`. To show a hidden run, read the chat
again with `coordination=show` for that visit; its rows come with `audience: agents`.

Client rules: keep a cached transcript with the echo it was read under, and send its
`visibility` as `coordination_read` on deltas; `replace_required` then reloads it when the
setting changed, here or on another device (a change writes nothing to the conversation).
Start the same reload for an open chat when the agent's `coordination_visibility` in the
snapshot or an `agent-roster` event differs from what it was read under. A page read with
`coordination=show` for one visit is never written to the cache.

Live rows follow the same rule (both apps). Under an effective `hide`, a live turn whose
`audience` is `agents` (the `/live` `turn` op) is not drawn; the chat's working line may still
name the agent the turn works with. A live note, decision card or artifact is always drawn, and
a turn whose `audience` is `user` is drawn as before. When a `turn` op changes an open turn's
`audience` to `user` (the user stepped into it), draw it from then on.

**A chat list's preview is the written part.** `last_message` and `last_completed_message` are
the newest row the chat shows, on one line, cut at 80 characters, built from its written part,
the text outside its `<speak>` blocks (see "The spoken part is not the written body" under
`GET /log`); from its spoken words only where nothing is written. The push notification and the
`user-notification` event's `preview` keep the spoken form where there is one.

**What a chat list shows of it (feature `coordination_working_state`).** The snapshot follows
the same rule as `/log` with `coordination=auto`: in a chat whose effective
`coordination_visibility` is `hide`, `last_message`, `last_completed_message`,
`last_message_id` and the chat's place in the list come from the newest row that chat shows
(sought among its newest 200; none: an empty preview), never from a hidden coordination row.
Every agent row carries `working_audience` (`user`, `agents`, or `null` when idle) and
`working_with` (`{agent_id, name}` of the agent whose message started the running turn, or
`null`), so a client that connects mid-turn can say "Working with <Name>". The audience is the
one `/log` gives the turn's rows: a turn of an agent that cannot leave a note reads `user` (and
`working_with` is `null`), as its rows do; every working
`agent-state` carries the turn's `audience`, `sender_agent_id` and `sender_name`, not only the
first one. While such a chat runs a coordination turn, its row's `activity` and
`live_activity`, the `agent-activity` and `agent-state` events of that turn (its `detail` keeps
only `trace_id`, `phase` and the words) and the agents-working push say "Working with <Name>"
and carry no tool, input, label or reasoning title of that turn. A turn waiting for an account
still says so (`account_recovery` is kept). A parent's `status_text` that describes a running
helper follows the helper's setting the same way ("<helper>: Working with <Name>"). The agent's own `status_text` is not veiled: like a
note, it is what the agent chose to tell the user. Under `show` nothing is veiled.

A **note** is an agent's words to the user from its own turn:
`role: assistant`, `kind: "note"`, `origin: "note"`, no `sender_*`,
`audience: user`, carrying the calling turn's `trace_id`. Draw it as the
agent's reply. It is always shown and notifies on its own (`user-notification`
reason `agent-note`); it is not spoken.

Display cells. `display_cells` are backend-neutral activity rows that the
parser has already rendered:

```json
{"id": "…", "kind": "command | patch | exploration | web_search | subagents | …",
 "title": "…", "summary": "…", "status": "running | ok | error | recorded",
 "lines": [{"label": "Task", "text": "…", "kind": "detail | muted | status | output | …"}]}
```

When a turn has cells, `/log` keeps only its edit-shaped `tools` (`Edit`,
`MultiEdit`, `Write`), because the cells already carry the rest. With
`include_tool_details=0` each cell is cut down to `id`, `kind`, `title`,
`summary`, `status`, `ephemeral` and `detail_count`.

Sub-agent cells (`kind: "subagents"`) come from two backends:

- **Codex** (`codex_transcript.py`): one cell per `spawn_agent`,
  `send_input`, `wait_agent`, `resume_agent` and `close_agent` call or
  collab event. The title says what happened ("Spawned agent", "Finished
  waiting"), the summary names the agent (`nickname [role]`), and the lines
  carry the task, thread ids and each agent's status.
- **Claude** (`claude_transcript.py`): one cell per built-in `Agent` (formerly
  `Task`) tool call, in place of a tool card. `id` is the tool_use id and
  `summary` is the call's description. `status` is `running` until the call
  settles: a foreground call settles on its tool result, and a background one
  on the `<task-notification>` Claude writes when the sub-agent stops. Then
  `status` becomes `ok` (completed) or `error` (failed, killed or stopped).
  The title follows the status: "Running agent", "Agent finished",
  "Agent failed" or "Agent stopped". The lines are `Task` (the prompt,
  shortened), `Type`, `Mode` (`Foreground` or `Background`), `Result` or
  `Status`, `Usage` (tool calls and duration, foreground only) and
  `Transcript`. The cell also carries these fields:

  ```json
  {"ephemeral": true, "description": "Map the auth flow",
   "subagent_type": "Explore", "background": true, "agent_id": "a1dac2d0…",
   "transcript_path": "/home/…/.claude/projects/<project>/<parent-id>/subagents/agent-a1dac2d0….jsonl"}
  ```

  `ephemeral: true` means the sub-agent runs inside the parent's Claude
  process and dies with it, for example when Clarp restarts the session
  after a usage limit. Label these so they are not mistaken for detached
  Clarp helpers, which survive restarts. A cell whose parent died stays
  `running`, because no notification ever arrives. `transcript_path` is the
  sub-agent's own transcript, found through the `toolUseId` in its
  `.meta.json` or through its agent id, and is empty when the file does not
  exist. The Host does not import sub-agent transcripts as conversations.
  They appear only as these cells.

#### Conversation and session views

Rust Host feature `chat_view` (issue 702, contract 145). Every `/log` row carries `view`,
`conversation` or `session`, beside `audience`:

- `conversation`, in a turn the user sees (`audience` `user`): the user's own message (a user
  row of origin `user` or `oracle`); an agent's note (`kind` `note`: `POST /agent-notes`,
  `clarp-admin tell-user`); a row the Host writes for the user (origin `system`); a turn's
  final reply; an assistant text row that no known turn wrote (`trace_id` empty) whose `phase`
  is `final`.
- `session`: everything else: assistant text in between (commentary), tool rows, prompts the
  Host or another agent sent (schedules, wakes, delivered answers, another agent's message), and
  every row of a turn with `audience` `agents`.

A helper's chat (an agent whose `role` is `helper`) is the exception (feature
`helper_conversation`, contract 175; an older Host tags these turns `session`): a turn another
agent began is that chat's conversation, whichever agent sent the message (its parent, a peer, or
another agent's message read into a running turn). Its prompt (a user row of origin `agent`) is
`conversation`, and its answers anchor by the rules below exactly as a user's turn's do, with
`session_rows`; its tool rows and commentary stay `session`. The turn's `audience` stays `agents`,
and in a helper's chat `view=conversation` does not leave such turns out as coordination. A turn
the Host began by itself for the agents (a self-prompt, a scheduled wake, a goal's continuation)
is not one another agent began: its answers stay `session` in a helper's chat too. A chat is a
helper's from the first time it is read as one, and stays one when the agent's role changes
afterwards (a relaunch as an agent, a janitor), so a row that entered its Conversation view never
leaves it: in every read, also the whole log, and with the helper's default coordination
visibility (`show`, source `helper`) under a global `hide`. Agent turns from before the agent became a helper join its Conversation view then. A helper's chat that moved to
this Host keeps them too: a prompt it brought along (an imported user row of origin `agent`) is
`conversation` in it, and a chat that is or was a helper's says so in its row of
`GET /agents/snapshot` (`helper_chat: true`, present only then, for a helper's own chat as well; feature `helper_chat`, contract 177), which a move takes along, so it
stays a helper's chat on the new Host whatever the agent's role is.
Every other chat keeps agent turns out of the Conversation view as above, and `view=session` is
unchanged.

The conversation view only grows (feature `conversation_view_grows`, contract 174): a row that
has entered `view=conversation` never leaves it, so a client that reads only that view never has
to remove a row. A turn's **answers** enter it as the turn runs. An assistant text row (words,
no tool name, not a note) whose message called no tools is `session` while what it is can still
change, and enters once it can no longer: when the turn's next row is other words, when a
message is read into the running turn (steering: any message read into it, the user's, another
agent's or a relayed one, but not one the agent's conversation fork read), or when the turn
ends. If work comes next (a tool call, or a plan the tool changes; one it tells again with the
same steps, as Codex closes its plan after its last words, is no work), the words were said along
the way (`phase` `commentary`) and do not enter as an answer. Words whose own message called
tools announced work: they never enter as an answer, and whatever comes next, other words
included, makes them `commentary` in `/log` as the live turn shows them from when their tool
began (issue 836; rows written before keep the phase they have). A plan counts as work from contract
174 on: a Codex turn written before it whose words came before a plan and then more words has
the first words as an answer too. Each message the turn answers keeps
its answer, also one given before a follow-up was read into the turn; two replies with no work
between them are both answers. When the turn ends (settles; a turn a follow-up replaced has
none), what came after its last answer is settled: its newest words enter as the turn's last
anchor, also when work followed them (a turn that ended on words along the way shows those, with
`phase` `final`: the answer of its turn, as the live turn showed it at its end),
else its last note or user prompt; if nothing after the last answer was said, the last answer
counts that work. An anchor carries `session_rows`, how many of the turn's rows since the anchor
before it the conversation view leaves out (with the rows of a message that had no answer of its
own), so a client offers "Show work" there and opens that turn in the session view. A row is
given again, with a new revision, when it enters or its count grows: the row or the end that
decided it brings it, also to a delta (`after_revision`) and to a reader of `view=conversation`
alone, so a row a client holds as `session` arrives again as `conversation`; never the other
way round. The Rust Host derives the anchors and their revisions from its turn records, also for
turns written before contract 174: on a Host that lists `conversation_view_grows` for the first
time, turns already read gain answers whose revisions lie below a client's cursor, so a client
drops what it holds of a conversation view once, and reads it anew.

`GET /log?view=conversation` returns only `conversation` rows, filtered before the page limit,
for a snapshot, a delta and an older page alike, and echoes `view`. With `view=session`, or
without `view`, every row comes as before. The two combine with `coordination` and
`include_automated`.

The agent's choice is kept on the Host: every snapshot agent row carries `chat_view`
(`conversation` or `session`, default `session`). `POST /agent-chat-view {session, chat_view}`
stores it and answers `{ok: true, session, chat_view}`, and broadcasts `agent-roster` with
`kind: chat_view`, `session` and `chat_view`. An agent's own turn or a peer Host is refused
`403 agents_may_not`; another value is `400`. A client may still override the view locally.
In conversation view, the brief the Host adds to each of the agent's turns says that its final
reply and its posts reach the user and its text in between does not.

#### Moving an agent to another backend

Rust Host feature `agent_backend_transit` (issue 720, contract 158). An agent moves to another
tool (`claude`, `codex`, `agy`, `grok`, `opencode`, and `agy-acp` with feature
`backend_agy_acp`) and keeps its conversation.

**What can be chosen.** `GET /agent-model-options?session=S` carries, on each tool's row
(`providers.<backend>`), `available` (bool) and, when false, `unavailable_reason`, one of
`not_installed` (the tool is missing on this Host), `signed_out` (no login file), `usage_limit`
(with `until_ms` when the Host knows the reset), `not_supported` (a tool an agent cannot be moved
to: `deepseek`, `muse`) or `error` (with `unavailable_detail`). `until_ms` and
`unavailable_detail` are null otherwise. Each model has `default` (bool): the model the tool
runs when none is named (the owner's configured default where it is listed, else the first).
When the tool itself listed which models its login runs (the row's `source.kind` is
`cli_probe`, as Codex does), only those are listed: the bundled catalogue's other models, which
that login may not run, are left out, and `POST /agent-transit` refuses them as
`unknown_model`. A row whose tool was only seen running one model (`cli_cache`, Claude Code)
keeps the catalogue's models beside it.

**Start.** `POST /agent-transit {session, backend, model, effort?, request_id}` answers
`202 {transit_id}`. The same `request_id` for the same agent answers the same `transit_id`
and starts nothing. `model` is an id the tool's row lists (`""`: the tool's default). `effort`
absent keeps the agent's effort where the target takes it. Refusals, after which nothing has
started: `400 {error: "unknown_backend" | "unknown_model" | "same_backend_and_model"}`,
`400 {error: "backend_unavailable", reason, until_ms?, unavailable_detail?}`,
`400 {error: "invalid effort for backend", valid_efforts}`, `409 {error: "turn_running"}`,
`409 {error: "messages_waiting"}`, `409 {error: "transit_running"}`, `404` for no such agent.

**Progress.** The event `agent-transit` `{agent_id, session, transit_id, backend, model, phase,
error, previous_backend, previous_model, carried}`; `phase` is `reading`, `translating`,
`registering`, `switching`, then `done` or `failed`; `error` is text on `failed`, else null.
On `done`, `carried` is `{turns, user, assistant, omitted, summary}` (`omitted`: the oldest
things said that did not fit the target's budget and were not carried as they were; `summary`:
who summarized them, `agent` or `model`, else null; `summary` since contract 161), else null.
Where the conversation does not fit, `reading` lasts while the summary is written (up to about
seven minutes). A change of the model
alone (same backend) goes straight to `switching` and `done`, with `carried` null.
`GET /agent-transit?session=S&transit_id=T` answers the same object at its latest phase; an
unknown id answers 404. The Host keeps these in memory: after a restart it has forgotten the
finished ones. While a change runs, the agent's snapshot row carries `transit` `{transit_id,
phase, backend, model}`, null otherwise; an `agent-roster` with `kind: agent-transit` is sent
when one begins and when it ends.

**Same store (Rust Host contract 165, feature `backend_agy_acp`).** A move between `agy` and
`agy-acp`, one program with one store of conversations, keeps the tool's own conversation and
carries nothing (only this pair: `deepseek` and `opencode` move as any two tools do): no
summary turn, no record at the head. `reading` and the carrying phases pass at once, `carried`
on `done` is all zeros, and the row reads "Now on <Backend> · <Model>. The same conversation
goes on." The agent's next turn continues that conversation natively (`session/resume` over
ACP, `--conversation` on `agy`). Should the tool no longer have it, that turn finds out: `agy`
by its store (the conversation's file is gone), `agy-acp` by agy's own answer that it does not
know it, which fails that turn ("Antigravity no longer has this conversation") and marks the
conversation. The next turn then begins a new conversation that opens with the Host's record of
the old one, as any move to a tool without a writer does.

**Result.** On `done` the agent's row carries the new `backend`, `model` and `effort`, and the
tool's session changes inside the Host. An agent set to no model (`""`) shows the model that answered last;
after a move it shows none until the new tool's first answer, not the old tool's. `conversation_id` stays: an app keeps its cached chat.
Persona, voice, goals, jobs and history stay. The Host writes one row into the conversation,
`role` `system`, `origin` `system`, `kind` `agent-transit`, in the conversation view:
"Now on <Backend> · <Model>. The conversation came along." (with ", its oldest N turns as a
summary." when some did not fit and were summarized, ", except its oldest N turns." when they
were left out, "its oldest turn" for one), "Now on <Model>." for a model change, or on failure
"Couldn't move to <Backend>: <reason>. Still on <Old backend> · <Old model>." The reason
is in a person's words ("the conversation couldn't be imported into OpenCode", "a turn
started"); what the tool itself said is in the event's `error` and the Host's log. On failure
nothing else changes.

What is carried: the user's words and the agent's answers of each turn of the current
conversation (since the agent was last started again), in order, from the Host's own record,
with the tools the agent used and what they gave back (feature `agent_backend_transit_tools`,
contract 161; a Host without it carries only the text). Claude, Codex and Grok get a native
session written where the tool keeps its sessions; the agent's next turn resumes it. Claude
gets each call as a `tool_use` block and its result as a `tool_result` block, Codex as a
`function_call` and `function_call_output`, Grok as a quoted block after the answer's text
(`[Tool call: <name>]`, `Input:`, `Result:` with each line quoted, `[End of tool call]`).
Antigravity (the Host never opens its database) and OpenCode (whose free models refuse turns
on a session handed to its import) get none: the agent's next turn there begins a new
conversation that opens with the Host's record of the old one, its calls as the same quoted
blocks (its most recent 60,000 bytes for Antigravity, which takes it on its command line).
A call's input and result are each cut past 8 KB, with
a line that says how many bytes were. The Host's record keeps the first 4,000 characters of a
result; for a call made in Claude the whole result is read from Claude Code's own transcript,
and otherwise a shortened one says that the rest was not carried. Attachments come as what the
user's message holds of them, the absolute path of each file, which the agent can open with
its own file tool; a picture a tool returned is named (with its file where the call names one),
never inlined. Not carried: thinking (it holds for the model that thought it; Claude signs its
own), the old tool's compaction summaries, system and developer prompts, and sub-agent
sessions.

How much is carried (contract 161): about 60% of what the target's model takes in, reckoned at
three characters a token (Claude 200,000 tokens, or 1,000,000 for a `[1m]` model; Codex
272,000; Grok 256,000; Antigravity 1,000,000; OpenCode 128,000; a model's `context_tokens` in
the catalogue wins, and for Codex the `model_context_window` its own `config.toml` sets, in the
active profile or at the top, for the model it runs, or for the tool's default). What does not fit is replaced by one summary at the head of what is
carried, written by the agent itself just before the move: one more turn on its old tool,
filed as nobody's message (`origin` trigger `transit-summary`, hidden like any automated turn),
that only reads (the Host's own tools refuse it helpers, jobs and new agents, and Claude Code
refuses it file edits) and is never carried as a turn. Where the agent cannot (its turn fails,
or takes more than four minutes and is stopped), a cheap model writes it from the Host's
record: Claude's Haiku, else Codex. Failing both, the newest part goes under a note that the
older turns were left out. The head says which it is.

`clarp-admin agent transit --session S --backend B [--model M] [--effort E]` starts a change,
waits until it is done or failed, and prints its last state (exit 1 when it failed).

#### Pair conversations: `GET /agent-conversations`

Agent-to-agent exchanges are also readable as one conversation per pair,
independent of direction, retries or renames. Grouping uses stable agent IDs:
`conversation_id` is `pair:<lower agent_id>:<higher agent_id>`.

```
GET /agent-conversations
→ {"conversations": [{"conversation_id": "pair:…:…", "agent_ids": ["…", "…"],
    "participants": [{"agent_id", "session", "name", "avatar_url", "archived"}],
    "title": "C++ Agent & Hugo", "message_count": 4, "latest_revision": 812,
    "latest_activity": 1788750466681,
    "latest_message": {"message_id", "text", "timestamp", "sender_agent_id",
                       "sender_name", "delivery", "revision"}}]}
GET /log?session=pair:…:…&limit=100[&after_revision=N|&before=<message_id>]
```

The pair `/log` shares the ordinary response shape and revision clock. Each turn
carries `sender_*` = the author. A delivered prompt is `role: user`,
`delivery: "sent"`, with `recipient_agent_id`. The recipient's answer is
`role: assistant`, `delivery: "private"`, with `reply_to_*` naming the agent it
answers; it lived only in that agent's own chat. Private user chats with either
agent are never included, and the projection never moves or relays transcript
rows. Sending into a pair conversation is not supported.

### 3. Stay in sync: `GET /events` (SSE)

The Rust Host advertises `agent_state` (feature number 156) in
`GET /server-info` for the existing `agent-state` and `agent-activity`
events below. Both event types are part of the base protocol, including on
older Hosts that omit the feature advertisement.

With `stalled_turn`, the agent row, `activity` and `live_activity` in the
snapshot and the entering and clearing `agent-activity` events expose
`stalled_since` and `stalled_action`. From Rust Host contract 157,
`stalled_since` is the positive Unix millisecond timestamp of the tool's
last output, or of its start if it has never printed. A stall is still detected
only after the silence threshold while a user or Oracle message waits.
The fields are null when the tool is unstalled.

A turn that waits for this machine's network (a Rust Host, issue 752: its tool lost its
connection to the provider and the Host finds no network) is shown as a turn that waits for an
account, in other words: `agent-activity` `state` `limited` with `summary` "Waiting for the
network", `agent-state` `detail.account_recovery` `"waiting"` with `detail.message` "Waiting for
the network", and `live_activity` `limited` with that `headline`. Show the words as sent; do not
read `limited` alone as a usage limit. No contract number goes with it (see
[compatibility](compatibility.md)).

A turn that waits for an account on a Rust Host from contract 176 (feature `quota_pool`) shows the
reset of the tool's pool, not only of the account that ran out: while another seat of the pool
would take the turn (the Host is moving to it), its words carry no reset ("Waiting for a claude
account with available usage"); where no seat has room, the reset is the moment the first seat
regains room, where that is sooner ("... · resets 18:27"). The same reset is the one a turn that
waited in vain fails with ("Usage limit reached · resets 18:27"). These are the words of
`agent-state` `detail.message` (with `detail.account_recovery: "waiting"`), of `agent-activity`
`summary` (phase `account_recovery`) and of `live_activity.headline` (state `limited`). On a Host
with the owner's selector a failed turn asks it at once, so a wait with no reset is short; on one
with only the automatic switcher lane the wait lasts until the switcher's next check, which can be
minutes after idle runs. Which reset is shown is judged for the tool's default model.

One `text/event-stream` per client. Every event is a JSON object with a
`type` field and, when the server recorded it durably, an SSE `id:` line.

Connection rules:

- Send `Last-Event-ID` (browsers do this automatically). The server replays
  every durable event after that id before going live, paging until caught up,
  so a reconnect loses nothing. Without it the server replays the last few
  minutes.
- After the replay the server always sends `{"type": "agent-roster"}`. Treat
  it as "call `/agents/snapshot` now".
- The server sends a `heartbeat` event (`event: heartbeat`) every few seconds when nothing else happens. If nothing arrives
  for 25 s, reconnect with exponential backoff (250 ms to 5 s).
- The server closes the stream when a client falls too far behind. Reconnect;
  the replay fills the gap.

Event types and payloads:

| `type` | Fields | Client action |
|---|---|---|
| `transcript-updated` | `agent_id`, `session`, `backend_session_id`; `head_revision` and `conversation_id` (feature `roster_fields`: as the agent's snapshot row has them) | The conversation changed. Fetch a delta for that session; with feature `roster_fields`, not when the cache already holds `head_revision` of that `conversation_id`. The server rate-limits this per session to about 4/s; it is a wake-up, not the data. |
| `agent-state` | `agent_id`, `session`, `kind`; the state watcher adds `persona`, `ts`, `detail`, `status_text`; a dispatch adds `trace_id`, `client_msg_id`, `queue_started`, `queue_remaining`, and `audience` ("agents" for a turn another agent's message started, else "user"; contract 108) with `sender_agent_id` and `sender_name` for such a turn when the sender is an agent of this Host, so an app can say whom the agent is working with; a janitor cancel adds `origin` | Patch the agent's `latest_state`. `kind` is an agent state (below). `thinking` starts a turn; `done`, `idle`, `stopped`, `interrupted` end one. |
| `agent-activity` | `agent_id`, `session`, `persona`, `kind`, `phase`, `status`, `tool`, `action`, `summary`, `file_path`, `ts`, `state`, `call_id`, `started_at_ms`, `turn_started_ms`, `stop_actor`, `stop_actor_verified`, `stop_reason`, `repair_actor`, `repair_actor_verified`, `repair_reason`, `released_trace` | A tool call or phase change inside a turn. Show it as a transient activity row until the next delta lands. `status` ∈ running, ok, error, recorded. `state` is the status-line state of [live-items.md](live-items.md) §1.3 (`responding` while text streams; a finished tool is `thinking` with `phase: tool_finished`); `call_id` is the provider tool call id, `started_at_ms` the tool's start, `turn_started_ms` the open turn's start. Contract 61: an interrupted state from a Stop that recorded its actor adds `stop_actor`, `stop_actor_verified` and `stop_reason`; a `slot_repair` audit state adds `repair_actor`, `repair_actor_verified`, `repair_reason` and `released_trace`. They are absent otherwise, and absent means unknown. |
| `agent-roster` | `kind` ∈ created, relaunched, forked, deleted, persona-created, persona-updated, persona-deleted, portrait-selected, backend-quota, contact-assigned, agent-renamed, janitor-changed, janitor-attention, stale-work, voice-changed, voice-profile, coordination-changed, agent-moved, chat_view, agent-transit (absent on the reconnect nudge); optional `session`, `persona`, `voice_id`, `backend`, `name`, `voice_profile` (feature `voice_profile`), `coordination_visibility` (with `coordination-changed`: the new effective value, for that `session` or, without one, the Host's; a Rust Host follows a change of the Host's value with one event for each chat without a choice of its own that does not follow it, a helper's own chat, carrying that chat's value, so apply the events in order), `chat_view` (with `chat_view`: that `session`'s new chat view, feature `chat_view`), `agent_id`, `fields` and `parent` (feature `roster_fields`: the changed fields of the agent's snapshot row, and with `helper-state` its maker's) | Refetch the snapshot; with feature `roster_fields`, patch the rows with `fields` and `parent` instead, and with feature `agent_row` read the one row where the kind allows it (see [the roster's events](#one-agents-change-in-its-roster-event-and-one-agents-row-features-roster_fields-and-agent_row-rust-host-contracts-182-and-183)). `created`, `relaunched`, `forked` for a session you have cached means that session's conversation is new: discard the cache. |
| `agent-focus` | `session`, `agent_id` | Server-wide focus moved (someone called `/select`, or hands-free routing picked an agent). Update `focused` flags. A client may follow focus or ignore it; the PWA follows, the native app follows only in hands-free mode. |
| `agent-transit` | `agent_id`, `session`, `transit_id`, `backend`, `model`, `phase` ∈ reading, translating, registering, switching, done, failed, `error` (text on failed, else null), `previous_backend`, `previous_model`, `carried` (on done; its `summary` since contract 161) | Rust feature `agent_backend_transit` (contract 158): how a move of the agent to another backend goes. Show progress, then the result; see [Moving an agent to another backend](#moving-an-agent-to-another-backend). |
| `queue-updated` | `agent_id`, `session`, `queue_depth`, `queue_paused`, `queue_started`, `queue_revision`, optional `client_msg_id`, `queue_change`, `text`, `origin`, `sender`, `read_together` | The agent's pending-turn queue changed (a send while busy was queued, started, or the queue was paused by `/stop`). `queue_change` (Host contract 115, feature `waited_send_order`): `edited` (with the item's new `text`) or `deleted`, for the item `client_msg_id`. An event that names an item (edited, deleted, a retired goal wake) also carries its `origin` and `sender` as `GET /turn-queue` does (R182). `read_together` (Rust Host contract 169, feature `queued_read_together`), on the event a message joined the queue with: as on its `GET /turn-queue` item. |
| `delivery-updated` | `agent_id`, `session`, `message_id` | F4 (feature `send_delivery_state`, Host contract 78): a user send's delivery state may have changed. An invalidation hint only, carrying NO state: clients re-read `GET /log` (`delivery_since`) and merge from there, and never merge the hint itself into history or cursors. Replay emits only these three fields (plus `type`, `event_id`, `ts`): any older stored `delivery` field is dropped, and a stored event whose identity is missing or invalid is left out of replay entirely (stored history is not rewritten; replay continues past it). |
| `user-notification` | `notification_id`, `agent_id`, `session`, `persona`, `done_ts`, `source_message_id`, `cause_message_id`, `origin`, `push`, `badge`, `unread`, `muted`, `preview`, `reason` | The server decided this completed turn deserves the user's attention. Badge and mark unread from this event only; never infer it from state changes. An agent's note (`POST /agent-notes`) notifies with `reason` `agent-note` (`agent-note-muted` when muted), `origin: note`, `source_message_id` the note's row, empty `cause_message_id` (contract 108). |
| `audio` | `clip_id`, `url`, `name`, `session`, `agent_id`, `persona`, `trace_id`, `streamable`, `delivery`, `stream_url`, `playlist_url`, `complete_url`, `audio_format`, `preview`; a cross-agent clip adds `herald`; a cached phrase adds `voice_id`, `phrase_key`, `cached_phrase`; a replayed clip adds `voice_id`, `replay`; a message replay adds `ts`; a clip of a listen-first opening turn adds `call_id` and `opening` (ring id, token, epoch, kind and admission id; see agent-rings.md) | A voice clip is ready. See §6. |
| `tts-error` | `session`, `agent_id`, `persona`, `message`, `error`, and with the speech_outcome feature `reason`, `provider`, `trace_id` | A reply was not spoken; tell the user instead of playing silence. With speech_outcome: once per turn and reason, `trace_id` names the turn whose reply it is (write the line under that reply), `reason` is an open set (below). |
| `server-version` | `version` | Server restarted on a new version; reload the client when it differs from the last one seen. |
| `remote-action` | `action` ∈ record, record-toggle, stop-agent, controller-event; `ts`; a controller event adds `controller_event_id`, `button`, `controller_event`, `duration_ms`, `age_ms`, `queued` and optionally `controller_id` | A live shortcut/controller asked the client to act. These input events are never replayed after reconnect. |
| `provider-limit` | `schema_version`, `provider_limit_event_id`, `episode_id`, `provider_instance_id`, `provider_id`, `window_id`, `kind`, `threshold_id`, `used_percentage`, `resets_at`, `observed_at`, `freshness`, `source`, `dedupe_key` | A provider usage window crossed a threshold, hit its limit, or recovered. `schema_version` precedes `type` on the wire. Extension surface; ignore if not implemented. From Rust Host contract 176 (feature `quota_pool`) a `hard_limit` is not sent while another seat of the tool's pool would take the turn (the Host moves to it), and when sent it carries `failover` (`none` or `unknown`) and `pool_regains_at` (ISO, UTC to the second: when the first seat of the pool regains room, where none has; else null). `resets_at` stays the window's own. A `hard_limit` not sent for that reason is held and sent, with those fields, if the pool then has no seat for the turn while the limit lasts and the tool is still on the seat it is of; a switch away from that seat lets it go. A `warning` follows the rule of the agents' `near` rows (no new number): it is sent, with `failover: "none"`, only where no other seat of the pool would take the turn from an account near its limit; while one would, or where that cannot be known (a seats reading that is old, failed or missing, `--accounts follow`), it is held, and sent once it is known that none would while the tool is still on that seat and the window has not reset; a switch away from that seat lets it go, and it is never sent on not knowing. `recovered` and `unknown` are sent as before. |
| `artifact-updated` | `session`, `agent_id`, `artifact_id` | An artifact was published or resolved; refetch `/artifacts/<id>`. Extension surface. |
| `media-changed` | `session`, `agent_id`, `asset_id`, `change` ∈ created, deleted, favorite; `favorite` (only with change favorite); `at` (ms) | A chat's gallery changed (feature `media_manage`): re-read or patch `GET /media`; on `deleted` drop cached bytes of the image. Extension surface. |
| `attention-updated` | `attention_count` | The number of items awaiting the user changed. Extension surface. |
| `background-job-updated` | `change_revision`, `observed_at`, `job_id`, `session`, `agent_id`, `status`, `job` | A durable background job changed; `job` is the full row. Extension surface. |
| `location-request` | `session` | An agent asked for the user's location; show the one-tap share prompt for that session. Extension surface. |
| `oracle-handoff` | `handoff_id`, `parent_handoff_id`, `host_id`, `principal`, `generation`, `revision`, `direction`, `mode`, `state`, `reason`, `oracle_call`, `agent`, `oracle`, `issued_at`, `server_now`, `ttl_ms`, `expires_at` | An Oracle call is being put through to an agent's hands-free session or back to Oracle. Apply the fence and phase rules in [oracle-handoff.md](oracle-handoff.md); act only for your own `principal`. Extension surface. |
| `group-call` | `call_id`, `host_id`, `principal`, `state`, `reason`, `revision`, `started_at`, `ended_at`, `floor`, `participants`, `change`, `server_now` | A group call changed (started, a participant added, removed, held, resumed or given the floor, or the call ended). Apply only for your own `principal` and a `revision` newer than the one held; `GET /calls` is the snapshot. See [group-calls.md](group-calls.md). Extension surface. |
| `agent-ring` | `ring_id`, `session`, `agent_id`, `persona`, `reason`, `opening_line`, `opening_audio_url`, `state`, `created_at`, `expires_at`, `ended_at`, `answered_by`, `call_id`, `server_now`, `conversation_mode` (agent-live rings only, and only to a Live-capable client: an iPhone's VoIP registration or an Android phone's push row declares `agent_live_v1`); a listen_first ring also `opening_mode` and `opening` (state and epoch) | An agent is calling the user (`state: ringing`), or the call was `answered`, `declined`, `missed` or `cancelled`. Show an incoming call while ringing; answer with `POST /calls/ring/answer`. `GET /calls/ring` is the snapshot. A listen_first ring lets the user speak first (feature `agent_ring_listen_first`). See [agent-rings.md](agent-rings.md). Extension surface. |
| `lane-settings-updated` | `codex_chat_lane`, `claude_chat_lane`, `muse_chat_lane`, `most`, `memory_mb`, `idle_minutes`; from contract 186 also `pressure_cpu_percent`, `pressure_io_percent`, `pressure_memory_percent` | The owner changed the settings of lanes on this Host (feature `lane_settings`, Rust Host contract 178). Read `GET /lane-settings` again, for what the lanes hold as well. See [the owner's settings of lanes](#the-owners-settings-of-lanes-lane-settings-feature-lane_settings-rust-host-contract-178). |
| `controller-actions-updated` | `revision` | The controller custom actions or their gesture mappings changed. Refetch `GET /controller-actions` when `revision` is newer than the one held. Ephemeral. See [controller-actions.md](controller-actions.md). |
| `calendar-request` | `request_id`, `session`, `title`, `start`, `end`, `time_zone`, `location`, `notes`, `url`, `all_day`, `calendar` | An agent asked the app to write an Apple Calendar event. Extension surface. |
| `goal-updated` | `agent_id`, `session`, `goal` | The agent's standing objective changed; `goal` is the new goal or null. Extension surface. |
| `heartbeat` | `ts` | Sent as `event: heartbeat` (no `id:`) after about 10 s without other events. The stream is alive; nothing to do. EventSource clients listen with `addEventListener("heartbeat", …)`; others ignore it as an unknown type. |
| `live` | `agent_id`, `session`, `conv`, `epoch`, `lseq`, `server_now_ms`, `ops` | Only with `GET /events?live=…` and feature `live_items`: item ops for the open turn. Never replayed, no `id:`; recover a gap with `GET /live?session=`. See [live-items.md](live-items.md). |
| `orchestrator-decision` | `decision_id`, `trace_id`, `action`, `kind`, `target_session`, `confidence`, `reason` | Hands-free routing picked (or declined to pick) an agent for an utterance. Informational. |

`controller-event` is the simulation/relay form of a physical controller
input. `POST /remote-action` with `action=controller-event` also carries
`controller_event_id`, `button` (`primary` or `secondary`),
`controller_event`, `duration_ms`, `age_ms`, `queued`, and optionally
`controller_id`. Controller events are live input, never durable state: the
Host deliberately excludes every `remote-action` from SSE reconnect replay.
Clients must additionally reject `queued=true` or `age_ms>0` to prevent an old
press from acting after a Bluetooth reconnect.

Valid `controller_event` values are `down`, `up`, `single-click`,
`double-click`, `hold`, four `swipe-*` directions, and the reserved
`rotate-clockwise` / `rotate-counterclockwise` pair. Phone-direct Flic Duo
integration supports the button and swipe events. Flic officially requires a
Hub for Hold & Twist, so rotation remains a separately negotiated experimental
input rather than a production-critical command.

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
  "transcription_id": "<optional; the X-Transcription-ID the client sent to /transcribe>",
  "client": "<optional; the app's short name, e.g. desktop>"
}
```

Rules:

- **`client_msg_id` is the idempotency key.** Mint one per message, reuse it
  on retry, and paint the optimistic bubble with id `u-<client_msg_id>`. The
  server files the durable user row under that id and ignores a repeated
  request with the same id. Omit it only if you accept the server minting one
  from `trace_id`, in which case you cannot confirm delivery. The id is one per
  Host, not per agent: a send under an id another agent already holds is
  refused with 409 and the text body `client_msg_id is held by another
  agent`, with nothing written (Host contract 129). Mint a new id once and
  send again.
- If the agent is busy with a turn that is not a person's (another agent's
  message, a schedule, an automation or a goal's wake, a janitor; not the
  tool's compaction), a user's or the Oracle's plain send (no
  `queue_if_busy`) never waits behind it (Rust Host contract 187, feature
  `owner_preempts`, issue 839): that turn is stopped at once, ending as
  stopped for a newer message, and the send runs next, ahead of everything
  that waits. Behind it the Host leaves the agent a message of origin
  `made-way` (`u-made-way-<turn>`) that quotes the cut message and asks the
  agent to take that work up again. It runs after what already waits, and a
  Stop holds it like other background work. A heartbeat, a leader's tick,
  dreaming or a watcher come again by themselves and get no such message.
  A turn that carries the user's own words (the user's turn, a front desk's
  relay) is not cut: there the rules below hold. Where the owner lets the
  agent answer through a conversation fork (feature `conversation_fork`), the
  fork answers beside the running turn as before. On an older Host the send
  waits or is steered as below.
- If the agent is busy, a user's send is steered into the running turn when
  its backend can take it. A Codex turn takes it at once, and its `/log` row
  follows at once. A Claude turn (Claude Code 2.1.321 or later) takes it while
  the turn has not ended: the follow-up is written to the running CLI, which
  reads it at its next boundary, after the running or next tool call, inside
  the same turn, or right after the turn's result, answering it in the same
  process. On a Rust Host with feature `agy_acp_steering` (contract 168) an
  `agy-acp` turn takes it the same way: Antigravity is given it inside the same
  turn, or right after the turn's answer in the same process, and it is answered
  there. Only words go so: a send that names a file the user attached (a picture,
  a sound, a PDF), or that begins with `/`, waits for its own turn. Until the CLI reads it the send answers `queued: true` and is a
  queue item that can no longer be edited or deleted; its `/log` row appears
  when it is read, with a `queue-updated` event. A send that cannot be steered
  (another backend, a Claude or `agy-acp` turn that has already ended, a terminal session)
  waits for the running turn and runs as the next turn as soon as it ends; a
  `queue-updated` event follows, as for a send with `queue_if_busy`. A send
  that waits is a queue item until its turn starts and only then a `/log` row
  (Host contract 115, feature `waited_send_order`).
  A send of another origin (a schedule, an automation) waits in the
  same way: no send of theirs ends a running turn; only the user's Stop does,
  and, from contract 187, a person's send to a turn that is not a person's
  (above). A message
  from another agent (`origin: "agent"` with a `sender`) never preempts: it steers a steerable turn (Codex) and otherwise
  queues as if `queue_if_busy` were true. `/stop` pauses the queue to
  hold its backlog. The user's next fresh message (a plain user or Oracle send,
  whether it starts a turn or is taken as a follow-up or a steer) ends the
  pause, and what the Stop held runs again in the queue's order: the user's own
  messages first, oldest first, then the rest. On an idle agent the fresh
  message starts at once and the held work follows it; on a busy agent the
  fresh message waits too and takes its place in that order, behind the user's
  older held messages. A Send Now of a held item, another
  agent's message, a goal wake or a schedule does not end it, and a Stop with
  nothing after it stays in force, across restarts too.
  A peer message, a queued user or Oracle send, and a goal wake the user
  resumed also run past it, but they go through the queue, so when nothing
  else is held each ends the pause as it starts (logged as
  `queuePauseEndedByDrain` with its origin). Every other `queue_if_busy`
  send, including an agent send that asks for the queue, waits for
  `/turn-queue/resume`.
- While a turn process of the agent is still running, no second one starts:
  every send waits until it exits, even after the Host recorded that turn as
  finished.
- `synthesize_audio: true` asks for the answer to be spoken: the agent's tool is
  told how to speak, and what it marks with `<speak>` becomes clips. Send it when
  the app's voice output is on. Without it, or with `false`, the answer is text
  only, whoever sent the message (Rust Host contract 171, feature
  `spoken_when_asked`; an older Host speaks a user's send that leaves the
  field out, so send it either way). A person's words in a call that is still
  on (with `call_id`, or the greeting with `call_opening`) are spoken unless the
  send says `false`. The user's messages read together, and a message of the
  user's that steers a running turn, follow the newest of them: each carries the
  app's setting when it was sent, so turning voice off takes effect with the
  next message. Another sender's message read into a turn can only ask for
  speech, and so can any message read into a call's turn, which stays spoken. A message that asks for speech, read into a turn whose tool was not
  told how to speak, tells it then, so its answer is written for the ear.
  Answers to forms (`POST /artifacts/<id>/submit`) take the same field with the
  same rule. The one exception: in hands-free dictation the router of spoken
  messages speaks its own short questions and confirmations ("Was that for
  Nadia?") whatever the send says, because the user started a voice
  conversation with it; the agent's answer still follows the flag.
- `hands_free: true` is for dictation: the server may route the text to a
  different agent (name routing or the orchestrator) and answers with the
  session it chose.
- `client` names the app that sent it (lowercase letters, digits and `-`, at
  most 32; anything else is ignored). The desktop app sends `"desktop"`: the
  reply to that message still badges and marks the chat unread, but is not
  pushed to the phone (reason `speak-desktop` / `text-reply-desktop`).
  Feature `desktop_send_quiet_push`.

Response `200`:

```json
{ "ok": true, "session": "rachel", "dispatch": "codex",
  "queued": false, "trace_id": "…",
  "orchestrator": { "action": "route | fallback | …", "decision_id": "…", "decision": {} } }
```

`dispatch` names the backend that took the turn (`claude`, `codex`, …);
`queued` is true when the turn waits behind a running one.

A routed send (`hands_free: true`, or `POST /orchestrator/route-delegation`, which
answers the same way) replayed with the same `client_msg_id` answers what the first
answered: the same `orchestrator.action`, `session`, `dispatch`, `decision_id` and
`decision`, and the FIRST request's `trace_id`; the model is not asked again. A send
first answered before this answers `decision_id` and `decision` null and its own
`trace_id`, as before. Same fields, so no contract change.

`200` means accepted, not delivered. Delivery is proven when a `/log` response
contains a turn with id `u-<client_msg_id>`. A client that wants a delivery
indicator should treat a send as failed if that id has not appeared within
about 20 s, unless the answer said `queued: true`: such a send waits as an item
of `GET /turn-queue` (with the same `client_msg_id`) and becomes a `/log` row
only when its turn starts, however long that takes. Draw it as waiting, at the
end of the chat, while it is in the queue; drop it if it is in neither the queue
nor `/log` (it was deleted).

### 5. Stop: `POST /stop`

`{"session": "rachel"}`. Terminates the running turn, records an
`interrupted` state, pauses the queue (until the user's next fresh message or a
resume), and broadcasts `agent-state` and `queue-updated`. Response:
`{"ok": true, "terminated": n}`.

On the Rust Host (issue 747) the user's Stop lets the user's messages that were
already waiting run, oldest first, and they do not end the pause: after they ran,
the queue is still paused until the user's next fresh message or a resume. The
response's `queue_paused` is the queue as the Stop left it, and `GET /turn-queue`
says the same afterwards. The stopped turn's `interrupted` state is told once on
the event stream (one `agent-state` event per Stop).

**The Stop in one sentence** (Rust Host contract 184, feature `stop_receipt_text`, issue
811). The response adds `text`, one sentence for people built from what the Stop left, read
in the same step as `queue_paused`:

- nothing waits: `"Stopped. You can keep chatting."`;
- the queue holds messages for the user: `"Stopped. 1 message waits; send one to go on."`, or
  `"Stopped. N messages wait; send one to go on."`;
- the user's own waiting messages go on by themselves: `"Stopped. The message that waits goes
  on next."`, or `"Stopped. The N messages that wait go on next."`.

N counts the waiting messages an app lists (`GET /turn-queue`). Every app that learns of the
Stop without having sent it gets the same sentence as `stop_text`: on the agent's `activity`
(the snapshot's row and the `agent-activity` event) for as long as the Stop is its latest
state, counted as the queue is when the activity is told, and in the `detail` of the Stop's
`agent-state` event. Show this line in place of the fields it is built from; `queue_paused`,
`goals_paused`, `stop_actor`, `stop_actor_verified` and `stop_reason` stay for apps that act on
them. A Host without the feature sends neither field.

Related: `GET /turn-queue?session=` lists queued turns; `POST /turn-queue/<id>/send`
releases one. Each item is `{id, text, enqueued_at, synthesize_audio, client_msg_id,
sent_at, kind}` (the last three from Host contract 115): `sent_at` is ISO,
`kind` is `waiting` (a send that met a busy agent) or `later` (its sender asked
it to wait). Each item also carries `status` (optional; `queued`, `claimed` or `started`): a
`started` item is already being read, by a steer into the running turn or by its own turn,
and is no longer waiting; it leaves the list once its row is written. Each item also says who it is from: `origin`, the stored value (an open set:
`user`, `oracle`, `agent`, `automation` for a goal wake or a schedule, `janitor`,
`heartbeat`, ...), and `sender`, `{agent_id, name}` when an agent sent it (`name` is `null`
once that agent is gone), else `null`. A user's send from another device is
`origin: user` with `sender: null`. An app that finds no `origin` is talking to an older
Host and draws only its own sends (R182, issue 491). `PUT /turn-queue/<id>` (`{"text"}`) edits and `DELETE /turn-queue/<id>`
deletes an item of either kind until its turn starts; after that both answer
404. Each sends `queue-updated` with the item's `client_msg_id`, `origin`, `sender` and
`queue_change`: `edited` (with the new `text`) or `deleted`. A goal wake the Host retires
sends `queue-updated` with its `client_msg_id`, `origin` and `sender` too.

**The app's audio setting where a message is changed or an answer is given** (Rust Host
contract 171, feature `spoken_when_asked`). `PUT /turn-queue/<id>` takes an optional
`synthesize_audio` (bool) beside `text`, and `POST /turn-queue/<id>/send` takes an optional
body `{"synthesize_audio": bool}` (an empty body is still accepted; a body that is not JSON
answers 400 `invalid JSON body`). The value sent replaces the item's own flag, so send the
app's current setting: the one the item was queued with is stale if voice was turned on or off
since. Absent or `null`, the item keeps its flag. A Send now that is refused because a turn
still runs (409) has already applied the flag, the item waits with it, and `queue-updated` is
sent. An item a turn gave back to the queue, whose row is already in the conversation, keeps
its own flag on a Send now. `POST /decisions/<id>/resolve` takes an optional `synthesize_audio` (bool):
the message the answer becomes for the agent is spoken only when it is `true`; absent or
`null`, it is not. Any of the three answers 400 `synthesize_audio must be a boolean` for another type.
Older Hosts ignore the field: on Send now and reword they use the item's flag, and on a
resolve the answer is not spoken.

**The user's word goes first** (feature `queue_user_first`). An agent takes one
queued item each time a turn ends. An item that carries the user's own word is
taken before the others: a send whose origin the Host resolved as `user` or
`oracle` (a send made from an agent's own turn never is, whatever origin it
names); the Host's delivery of a card the user answered in the app, or that a
session in `[agents] decision_on_behalf_sessions` answered on the user's
behalf (not one that expired); and a message sent from the own turn (its
verified `X-Clarp-Turn`) of a session the owner named in `[agents]
front_desk_sessions`, the front desk that relays the user's words. Such a
relay keeps origin `agent` and its sender: it reads and notifies as an agent's
message; it goes first in line, and under hidden coordination it is never left out. A request that only
names a front desk, without that session's turn token, gets nothing. Among themselves such items keep the order they were sent; every
other item keeps its order behind them. An item already taken once (its launch
failed, or the release drain handed it over) is not overtaken. Nothing else
changes: a Stop's pause still holds what it held, the release drain's fence
still holds everything, and the user's items are first when the queue moves
again. `GET /turn-queue` lists `items` in the order the Host takes them, so a
user's item that arrives moves the others one place down; `revision` and the
`queue-updated` event's `queue_revision` change with it. Other agents' messages
still run: the user's items in front of them are only the few the user sent.

**The user's waiting messages are read together** (Rust Host contract 169, feature
`queued_read_together`). When a turn starts for a message of the user's (origin `user` or
`oracle`, said to the agent, not another agent's words) and more such messages wait directly
behind it, the turn takes all of them: its tool is given one prompt that says the user sent
these while the agent was busy and holds each message in the order sent, numbered `[1]`, `[2]`,
and so on, with nothing else added. Each of them leaves the queue as a started item
(`queue-updated` with `queue_started: true` and its `client_msg_id`) and is a `/log` row of
its own, in order after the first and before the answer, with its original `sent_at`. The
answer is one turn: its rows and its end are on the first message's turn. The run stops at the
first item that is not such a message (an agent's message, one left for later, one sent now,
one a Stop holds, one in a call), which keeps its own turn. A retry of the turn gives the tool
the same prompt again, and no takeover replaces such a turn. When the turn ends before its tool
did anything for them (wrote words of an answer or used a tool, in any attempt), by anyone's Stop but the user's own (an agent's, a routine's, a newer
message sent now) or because its process was lost, the messages it took after the first go back
to the head of the queue, in order (behind a message sent now): each is an item of
`GET /turn-queue` again, announced by `queue-updated`, while its `/log` row stays where it was,
and the turn that takes them again writes no second row; `PUT /turn-queue/<id>` refuses to
reword such an item (409), so its row and what the tool is given stay the same words. A turn that
had begun its work, and the user's own Stop, end them with the turn, as a message steered into it
ends. While a turn runs that cannot take such a message now (the tool cannot be steered,
as on Antigravity, or on Codex without its chat lane, or a steer was not possible), its `GET /turn-queue` item carries
`read_together: true`, as does the `queue-updated` event it joined the queue with: draw it as
queued, to be read with the user's other waiting messages when the turn is over. It is also
`true` while a turn waits for its next attempt: such a message heads the next turn, with the ones
behind it. The field is
`false` on every other item and absent on other events; an older Host sends neither, and takes
one item per turn.

**An agent that moved to another Host** (feature `agent_moved`). When another Host (the
Rust Host) has imported an agent and taken up its native session, it marks the agent moved:
`POST /agent-moved {"session", "moved_to": {"host", "id"}}`, and takes the move back with
`"moved_to": null` (an import rolled back, an agent that returns). Only a full device or the
administrator may: a request with an agent's turn token or a peer Host's header answers
`403 {"error": "agents_may_not"}`, a limited device `403`. Each answers
`{"ok", "session", "changed", "moved"}`; the same call again changes nothing (`changed: false`),
and a mark to another place while moved answers `409 {"error": "already_moved"}`.
Setting a mark answers `409 {"error": "runtime_not_ready"}` and sets nothing while the
runtime that owns turns does not report the `agent_moves` capability in its status (a
runtime of an older release would ignore the mark); taking a move back is always allowed.
While an agent is moved no turn of it starts here, by any door: `POST /send` (and every relay
through it: `clarp-admin prompt`, a parent steering a helper, a peer Host's request) and
`POST /turn-queue/{id}/send` answer `409 {"error": "agent_moved", "moved_to": {host, id},
"moved_at"}`, and the Host's own wakes (schedules and self-prompts, goal wakes, janitor wakes,
decision deliveries) are refused at the same admission. Compaction and the interactive
terminal are refused too. Items already queued stay queued, are not dispatched, and run in
their order when the move is taken back. A turn already running when the mark is set is not
cut. Snapshot rows carry `moved`: `{"to": {"host", "id"}, "at"}`, or `null`; an `agent-roster`
event with `kind: agent-moved` follows each change.

### 6. Voice

**In:** `POST /transcribe` with the audio body (`Content-Type` of the
recording, e.g. `audio/webm`), headers `X-Hands-Free: 1` for dictation,
`X-Transcription-ID: <uuid>` for idempotent retries, `X-Transcription-Model`
to pick a model. Response includes `text` and `trace_id`; it carries no
`transcription_id`. The transcription id is the client's own `X-Transcription-ID`
(the Host mints none when the header is left out). Pass `trace_id`, and that id
when you sent one, on the following `/send`.
When the engine cannot be handed the agent's language set (a set such as `no,en` goes only
to Google Chirp or ElevenLabs Scribe), the Host stays within what the owner chose for
transcription: its selected engine when that one takes the set, else its own engine when its
model declares the agent's languages (a model that declares none: unless it is a `.en`
model). An engine this recording already went to and that failed is not sent it again.
The response then adds `engine_fallback`:
`{"from", "to", "reason": "language_set", "languages"}` (`languages`: the codes the engine was
handed, or `"detected"`). Otherwise it answers `422`
`{"error": "language_set_unsupported", "message", "engine", "languages", "engines", "reasons"}`,
where `engines` are the engines that can take a set and `reasons` says in words why none of the
owner's engines could take over (the Host's own engine being off or unavailable is one such
reason, never a 500): show `message`, it says what to change. A
recording is never sent to a provider the owner did not choose for transcription.
An agent's engines are tried in order, each once, and never the same recording to one provider
twice: a provider the recording reached (it answered, or failed after the audio left) is not asked
again with another of its models. A refusal before any audio left (a language set the engine cannot
take, no key, an unknown model) does not use up its provider, so its next model may still try.

In a group call (feature `call_transcription_policy`), a clip captured for a
participant also sends `X-Clarp-Call-Id: <call_id>` and
`X-Transcription-Session: <that participant>`. The Host checks them before any
transcription and refuses with no transcript: 400 `bad call id`, 404 `unknown
call`, 403 `not your call` (another device or Host), 409 `call ended` (+
`call_id`, `reason`), 409 `not a call participant` (+ `session`). A valid one
answers with `stt`: `{language: {value, source}, engines: {value, source},
effective_engine, attempts: [{engine, outcome, language, ms|reason}],
language_adapted, requested_model}`, sources `agent | call_policy | device |
global`; `attempts[].language` is the language the Host handed at that
engine's boundary (`auto` = no constraint handed, not a detection; `null`, or
no `language` on a skip = the boundary was unobserved, never a statement that
the engine was or was not invoked). `attempts[].outcome` (`ok`, `error`, `skipped`) and a
skip's `reason` (such as `language_incompatible` or `provider_already_sent`) are open sets: a
client shows a value it does not know as plain words and keeps the answer. With a call policy
configured, when no
engine can take the call language the clip is refused, with no provider call:
409 `{"ok": false, "error": "no compatible engine", "language", "engines",
"attempts"}`. A stored call policy that can never work is refused the same
way for every call clip: 409 `{"ok": false, "error": "call policy invalid",
"call_policy_error", "language", "engines", "attempts": []}`. A configured
language set skips ElevenLabs Scribe, which would drop it. Multilingual or
unknown local engines never receive the code, so they are skipped when a call
policy is configured. Without a call policy,
in-call transcription is unchanged. The transcription settings take `call_languages` and
`call_engines`, refuse a combination that can never work, and read back
`call_policy_error`. A settings save writes only the keys it names (an
omitted key is left as stored); an explicit `null` for `engine` or
`turn_taking` is refused with 400. Without the header nothing changes. See
[group-calls.md](group-calls.md).

**Out:** an `audio` event announces a clip. Pick a source in this order:

1. `playlist_url` (HLS) when present: assign it to an `<audio>` element.
2. `stream_url` of the form `/clips/<id>/stream` (progressive HTTP): assign it.
3. `url` (`/audio/<file>.mp3`): the complete file, always playable.

Acknowledge playback with `POST /clips/ack` `{"clip_id": …, "status":
"queued | play-start | play-ok | play-fail", "error": "…", "trace_id": "…"}`.
`GET /clips/recoverable?session=` returns recent clips a client that was
suspended may still want to play.

**Oracle standby: `oracle_v2.standby`** (Host contract 110). An Oracle call
created over WebRTC with an API key may run with GPT-Live standby when the Host
sets `[openai] live_idle_timeout_s` (5 to 60; it ships at 0, off). The Host then
relays the provider's `session.paused` and `session.resumed` as one event on
the call's event stream:

```json
{"type": "oracle_v2.standby", "active": true}
```

`active: true` means the provider paused inference after that many seconds
without an input or output transcript; `active: false` means speech woke it and
it is ready again (the provider also says this at the first wake). The call stays
connected throughout and keeps its session: **keep sending microphone audio while
in standby**, because only speech wakes it (muting the microphone does not pause
it, and a muted call cannot wake). The first answer after a wake may take a little
longer. No other path sends this event: the default Oracle and the WebSocket
Oracle hold a WebSocket, and an agent Live call never uses standby. A client that
does not know the event ignores it; nothing else in the call changes.

`oracle_v2.usage` adds `active_seconds` and `paused_seconds` (numbers or null):
present only when the provider reports the breakdown; `voice_estimate_usd` then
prices active time at $0.05 and standby time at $0.02 a minute.

### 7. Focus: `POST /select`

`{"session": "rachel"}` makes that agent the server-wide focus (hands-free
routing target) and broadcasts `agent-focus`. Clients that follow focus must
apply the broadcast, not re-post `/select`, or two clients loop.

### 8. Agent lifecycle

- `GET /agents` — session-keyed map of live agents (a subset of the snapshot).
- `POST /agents` — create, relaunch, or fork: `{"name", "voice_id",
  "backend", "cwd", "model", "effort", "replace_sid" (to relaunch),
  "session" (an id asked for a new agent, honoured only when unused),
  "resume_session_id", "fork_session_id", "avatar_symbol", "personality",
  "parent", "role"}`. `parent` is the creating session or agent id and
  `role` is `agent` (default) or `helper`; a helper needs a parent, and a
  fork without `parent` records its source agent. Self-parenting and cycles
  answer 409 (`self_parent`, `parent_cycle`); an unknown parent 404.
  Answers `{"session", "persona", …}` and broadcasts `agent-roster`. A
  relaunch of an agent whose turn is running stops that turn first (its
  `agent-state` is `interrupted`, detail `source: "restart"`), so its answer
  never goes into the conversation the relaunch ends; unlike a Stop it holds
  nothing, and what waited behind the turn runs in the new conversation.
- **A Codex agent on a model provider of its own (Rust Host contract 170, feature
  `codex_own_provider`).** `POST /agents` also takes `codex_home` (an absolute directory) and
  `codex_provider` (the name its `config.toml` defines the provider under), both or neither,
  for a `codex` agent, and `cloud_speech` (bool). `POST /agent-provider` `{"session",
  "codex_home"?, "codex_provider"?, "cloud_speech"?}` changes them later: what it leaves out
  stays, empty or null `codex_home` and `codex_provider` let the binding go, null
  `cloud_speech` returns to the default. It answers `{"ok", "session", "backend",
  "codex_home", "codex_provider", "cloud_speech"}`, and 400 `invalid_codex_provider` (with a
  `message`) when half a binding is given, the agent is no Codex agent, or the home is no
  directory. Each turn of a bound agent runs with `CODEX_HOME` set to that home and
  `model_provider` set to that provider (exec, chat lane and goal lane alike). A turn whose
  home's `config.toml` does not set `model_provider = "<codex_provider>"` and define
  `[model_providers.<codex_provider>]` is refused before Codex starts; a turn whose Codex
  reports another provider for its thread is stopped at once. Both end the turn as the
  failure kind `provider_mismatch`, never tried again, with the reason "This agent's turn was
  stopped: it ran, or would have run, on another model provider than the one it is bound to.
  Check its Codex home and provider" and the Host's words of what it found below it. A bound agent
  takes any model name (its provider's deployments), runs on its `config.toml`'s model when
  it names none, takes its sessions (`resume_session_id`) from its own home, and cannot be
  moved to another tool (`POST /agent-transit` answers 409 `own_provider`). `cloud_speech`
  defaults to true, and to false for an agent with `codex_provider`; when false nothing the
  agent says reaches a speech provider in the cloud: its lines are not spoken, nothing is
  made in its voice, `POST /preview` for it answers 409, live calls and live rings for it
  answer 409 `cloud_speech_off`, the Oracle is told none of its words, and what is said to it is heard only by the Host's own engine (`POST /transcribe` and a call's clips answer 409 with a sentence where there is none; `GET /stt/stream` answers 409). The Host's own model calls leave a bound agent out: no janitor scope holds it, its tools are explained without a model, and the goal keeper (also of a bound helper), label checker, router and other agents of a call leave its words out. Pushes, questions and the Live Activity of a bound agent carry only a neutral line ("<name> answered", "<name> asks", "<name> is working"). An exec turn whose rollout in the agent's home names no provider within 3 seconds of its first item, or is not there at all within 30 seconds, is stopped, and one whose provider is still not verified when its process is gone (Codex may flush its rollout after the turn's end) fails closed as `provider_mismatch`, its answer not shown; this only detects, the launch settings are what prevent. What other agents read of a bound agent: see the next item (Rust Host contract 179, feature `bound_privacy`; before it, every agent read every word). The snapshot row
  carries `codex_home` and `codex_provider` when the agent is bound, `cloud_speech` (the
  value in effect) when it is bound or the choice was set, and `readers` (below).
- **Who reads a bound agent's words (Rust Host contract 179, feature `bound_privacy`).**
  Its words are read by its owner (a paired device, from any page it reads; the
  administrator's token presented without `X-Clarp-Turn`), by itself and by every agent
  bound to the same `codex_home` and `codex_provider`, and by the agents its owner lets in,
  listed as agent ids in `readers` on its snapshot row. Its forks and the helpers made with
  it as their parent (`POST /agents` with `parent`, `clarp-admin agent create --parent`)
  are bound as it is, unless the request binds them itself; a helper of it on another tool
  is refused (409 `own_provider`). Every other reader sees that it is there, its name and
  its state, and none of its words. Such a reader is a request that presents another
  agent's `X-Clarp-Turn` (any token beside it), an agent's tool token without a turn, or a
  request with `X-Clarp-Peer` (another Host; a Rust Host sends it on its peer reads). For
  such a reader, on every `GET`:
  - A read that names the agent (`session` or `agent_id` in the query, its session in the
    path, an artifact it made, a plan of its) is refused: `403 {"error":
    "bound_conversation", "message": "<name> is bound to a model provider of its own …"}`.
    This covers `GET /log`, `/live`, `/turn-queue`, `/agent-goal`, `/task-plan(s)`,
    `/goal-ledger`, `/artifacts`, `/message-tool-details` and the rest.
  - Every JSON answer of a read of agents' words (`GET /agents/snapshot`,
    `/agents/archived`, `/attention`, `/background-jobs`, `/teams/<id>/messages`, `/calls`,
    `/status`, `/live`, `/orchestrator/decisions`, `/controller-actions`,
    `/oracle/delegations`, `/goal-ledger/delegations` and others) has each object that is
    the agent's with every text emptied, lists of texts emptied, and `words_withheld: true`;
    ids, sessions, `persona`, states, kinds, addresses and times stay. An object is the
    agent's where one of these names it: `session`, `agent_id`, `agent_session`,
    `source_*`, `sender_*`, `requester_agent_id`, `requested_session`, `target_*`,
    `delegate_*`, `principal_*`, `owner_*`, `answer_session`, `reply_to_*`, `actor_*`,
    `helper_session`.
  - `GET /events`: each event of the agent is blanked the same way, judged frame by frame
    against the fleet as it is then, so a stream opened before an agent was bound is
    filtered from then on, and a reader the owner stops reads no more.
  - Answers that cannot be blanked (files, terminals, recordings, clips, gallery images,
    sockets, the vocabulary mined from conversations) are given where the read names only
    agents this reader may read: `/agent-file` and `/agent-files` and `/stt/stream` by their
    `session`, `/terminal/<session>`, `/clips/<n>/…` by the agent whose speech the clip is,
    `/media/<asset>` by the chat the image was shown in. Where the read names no agent the
    reader may read, or the route cannot say whose words it serves (`/dirs`,
    `/past-sessions`, `/server-terminal`, `/audio/<name>`, `/media/`,
    `/transcription-audio`, `/calls/ring/live`, `/oracle/v2`, `/vocab/*`, a clip still
    being made), it is refused while the Host holds a bound agent this reader may not read.
    A read of words that names readable agents and answers other than JSON (an artifact's
    page) is given; one that names none is refused the same way. A route the Host has not
    classed is treated as one that cannot say whose words it serves.
  - The agents' own tools ask the same: `clarp-goal`/`clarp-agent-tasks` about it, and
    `clarp-agent-artifacts list|wait|form-events|report-history|attention` about it, exit
    77 with `bound_conversation`; JSON a tool prints is blanked as above.
  - A message it sends to such an agent (`clarp-admin prompt`, a request's reply, a helper's
    report) arrives as "<name> sent you a message. Its words are withheld: …", and the send
    answers `words_withheld: true`. A delayed prompt it schedules (`--delay`) for another agent
    goes out under its name when it is sent, whatever `--from` says; one to itself on this
    Host keeps its `--origin` (with `--server` it goes out under its name, and is refused). `clarp-admin prompt --server` from
    it (or naming it as the sender) is refused (409 `bound_conversation`). Its `<team>`
    updates reach such members' briefs (and a leader's tick) as "<name> posted an update
    (words withheld: …)"; an update whose sender or reader the Host cannot place is
    withheld the same way (the user's own posts always read).
  - `POST /agent-provider` takes `add_readers` and `remove_readers` (lists of sessions or
    ids) and answers `readers` (sessions). From the owner the change is made at once. From
    an agent (its turn), `add_readers` makes one decision card per agent, in the asking
    agent's chat ("Let X read B's conversation?"), listed in the answer as
    `asked: [{reader, artifact_id, decision_id}]`. An agent's `remove_readers` is 403
    `owner_only`; an agent asking a reader of an agent that is not bound is 409 `not_bound`.
  - Its binding is its owner's. On `POST /agent-provider`, a change of a bound agent's
    `codex_home`, `codex_provider` (letting it go included) or `cloud_speech` asked by
    anyone but the owner is not made: a card in the asking agent's chat ("Unbind B from its
    provider?" or "Change B's provider?") is listed under `asked` as
    `{binding, artifact_id, decision_id}`. A caller with no turn to ask from is 403
    `owner_only`. The owner's own call changes it at once. `POST /agents` that starts a
    bound agent again in place (`replace_sid`) with another binding is 409 `own_provider`
    for anyone but the owner.
  - The cards: the Host keeps what each card asks under the decision's id, never on the
    card, and acts only on a card it keeps such a question for: a card an agent made itself
    with a payload or reference of its choosing changes nothing. It acts once, when the
    owner's answer changes the card (a resolve sent again does nothing), and only on his
    own tap in an app: an answer given on his behalf by any agent (`on_behalf_session`,
    the front desk included) is not taken. Asking the same question again answers the card
    that still waits; once that card is answered or gone, a new one is made.
    `clarp-admin agent provider --session B --reader X[,Y] | --no-reader X` is the tool.
  - Not covered:
    - Anything run with the administrator's token and no `X-Clarp-Turn` reads as the owner.
      That includes every agent tool run outside a turn (the tool shim falls back to the
      installed file's administrator token: a timer, a delayed prompt as it is sent, a
      durable background job, a subprocess without the turn's environment), and the scripts
      of the `clarp-agent-admin` and `clarp-adopt` skills, which read `[server].auth_token`
      and present no turn. Such a run reads a bound agent and may rebind it as the owner.
    - An agent on the same machine that reads the Host's configuration (its administrator
      token) or the agent's files.
    - A Python peer Host, which sends no `X-Clarp-Peer` on its reads and is answered as a
      paired device.
    - `POST` answers that carry words (`/task-plan/action`, `/decisions/<id>/resolve`) are
      not blanked; an agent's tool token cannot reach them.
- `POST /agent-helper-state` — `{"session", "state": "done"|"failed"|"running",
  "by"?}` marks a helper. `by` is the marking agent and must be the helper's
  parent; without it the mark is the user's. An agent-origin `/send` from a
  helper to its parent moves it to `reported` on its own, and one from the
  parent back to a reported helper returns it to `running`.
- `POST /agent-notes` — `{"session", "text", "note_id"?}` (`clarp-admin
  tell-user`): an agent's note to the user, written into its own chat as a
  row of its running turn (see Provenance: a note). Accepted only from that
  agent's own running turn, proven by its turn token (`X-Clarp-Turn`): a call
  without one answers 403 for every caller (a device, a peer Host, the Host's
  own token), so does another agent's token, and a call outside a running turn
  answers 409. `note_id` (up to 64 of
  `a-z0-9-_`) makes a retry answer the same note. Answers `{"ok", "session",
  "message_id", "trace_id", "created", "notified", "reason"}`. At most 8000
  characters (413); a note with no words answers 400.
- `GET /coordination-settings` → `{"coordination_visibility": "show" | "hide"}`, and
  `POST /coordination-settings` `{"coordination_visibility"}` (a full device): the Host's
  choice whether coordination between agents is shown, default `hide` (out of the way unless
  the user asks for it; earlier releases defaulted to `show`; a stored choice is kept). A string that may
  gain values. `POST /agent-coordination` `{"session", "visibility": "show" | "hide" |
  "default"}`: one chat's own choice (`default` follows the Host, except in a helper's own
  chat on a Rust Host, where it is `show`: every turn of a helper is started by another
  agent, so under `hide` its chat would be empty); it answers
  `{"ok", "session", "coordination_visibility", "coordination_override"}`. Each change is
  written, then an `agent-roster` event (`kind: coordination-changed`) carries the new
  effective value. An event without `session` is the Host's value, which a helper's own chat
  without a choice of its own does not follow: on a Rust Host each such chat is told its own
  value in an event with its `session` right after. The next snapshot's agent rows carry it too
  (`coordination_visibility`, the effective value, and `coordination_override`, the chat's
  own choice or null). A change writes nothing to any conversation. A request that carries
  a turn token (`X-Clarp-Turn`) or comes from a paired peer Host (`X-Clarp-Peer`) is refused
  on both POSTs with `403 {"error": "agents_may_not"}`: an agent may not hide coordination
  from the user.
- `GET /agent-helper-state?session=` — `{"agent_id", "session", "role",
  "parent_agent_id", "helper_state", "helper_completed_at", "archived_at"}`
  for one agent, so a watcher can poll without the snapshot. The Rust Host
  includes `backend` with feature `helper_state_backend` (contract 152).
- `DELETE /agents/<session>` — release the agent (soft delete).
- `POST /agent-mute`, `/agent-archive`, `/agent-heartbeat`, `/agent-dreaming`,
  `/agent-voice` — per-agent toggles: `{"session", "<flag>": bool}`.
- On Rust Hosts with `agent_api_status`, a mute or archive change emits
  `agent-roster` with `kind: "mute-changed"` or `"archive-changed"` and the session.
  The resulting state and the agent's creation time (`created_at`, epoch milliseconds)
  are available in `GET /agents/snapshot`. A nonexistent explicit working directory in
  `POST /agents` is refused with 400 `cwd_not_found`. Model options report
  `authenticated` when `GET /backend-auth` can establish whether the harness is signed in.
- `GET /past-sessions?backend=&cwd=` (full scope) — resumable backend conversations for
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
  A row with `runs_through` (DeepSeek's `opencode`) is another card of that row's program and
  sign-in. On a Rust Host with feature `backend_agy_acp` (contract 165) the list has `agy-acp`,
  "Antigravity (ACP)": `agy` driven over the Agent Client Protocol, `runs_through` `agy`, with
  `agy`'s models and sign-in. An agent can be made on it or moved to and from it like any
  backend. `supports_steer` is false, as for Claude: with feature `agy_acp_steering`
  (contract 168) a message sent while its turn runs is read into that turn all the same (see
  [Send a message](#4-send-a-message-post-send)); without it, it waits for the next turn.
- `GET /voices?for=<session>` — voice catalogue with availability.
- `GET /dirs?path=` — directory completion for the start dialog. Answers `{"base", "matches"}`: a path that
  ends in a slash lists the visible directories inside it (absolute paths only); any other path completes its last
  name in its parent. At most 20 matches. A directory that is not there answers no matches.
- `POST /clog` — batched client diagnostics `{"events":[{"event","detail","level","trace_id","clip_id"}]}`.
  With `client_events_kept` (Rust Host contract 146), a bounded store accepts the batch
  regardless of diagnostics settings and answers `{ok,captured,kept,full_capture}`.
  `captured` confirms persistence to the bounded store. `kept` counts the events it took.
  `full_capture` indicates that diagnostics also captured events from the batch.
  Errors, crashes, exceptions, faults, fatal events and hangs keep the preceding 30 minutes
  from their device and stay up to seven days. Ordinary events expire after 30 minutes.
  Capacity is 50,000 events or 50 MiB in total and 20,000 events or 20 MiB per device.
  Oldest events leave first. Details are redacted and capped at 4 KiB. `POST /crash` adds a
  `metrickit_report` row with its counts and file name.
- `GET /client-log?device=&since=&level=&event=&limit=` — full device access required.
  `since` accepts a duration such as `30m` or epoch milliseconds. `level=error` includes
  fault, critical and fatal. `level=warning` also includes warn. The newest matching
  events are returned oldest first in `{events,count}`. Limit defaults to 200 and
  cannot exceed 5,000. `clarp-admin client-log` reads them as JSON lines.

### One agent's change in its roster event, and one agent's row (features `roster_fields` and `agent_row`, Rust Host contracts 182 and 183)

An `agent-roster` event that names one agent once said only `{type, kind, session}`, and an app
read the whole snapshot for it (issue 809). On a Rust Host that lists these features:

- **`roster_fields` (contract 182).** An event of these kinds carries `agent_id` and `fields`:
  the fields of the agent's snapshot row its change moved, with their values as that row has
  them when the event is numbered. Patch the row and read nothing.
  - `helper-state`: `fields` `helper_state`, `archived_at`; and `parent`, the maker's
    `{session, agent_id, fields}` with `fields` `child_count`, `running_children`,
    `background_jobs`, `latest_state` and `status_text` (the maker's line names its helpers
    while it has nothing of its own to say). Patch both rows. A helper whose maker the Host no
    longer has carries no `parent`.
  - `mute-changed`: `muted`.
  - `chat_view`: `chat_view` (the same value as the event's `chat_view`).
  - `agent-transit` as a change of backend begins: `transit`. The event at its end (`done` or
    `failed`) carries no `fields`: the agent's backend, model and what follows from them
    changed; read the row.
  - `transcript-updated` (not a roster event) carries `head_revision` and `conversation_id` as
    the agent's row has them when the event is sent. A client whose cache holds that
    conversation at that revision or later drops the event and reads nothing; otherwise it
    fetches the delta as before. A change that comes while the Host holds the event back (it
    sends at most about four a second for an agent) is in the next one.
  An event of these kinds without `fields` (an agent the Host no longer has, or an older Host)
  is read as before. The values are made by the code that makes the snapshot's rows, so a
  patched row equals a fresh read.
- **`agent_row` (contract 183).** `GET /agents/{session}` answers one agent's row (or a
  janitor's), exactly as `GET /agents/snapshot` has it, put away or not: about 3 kB in place of
  the whole list. A session the Host does not have is 404 `{"error": "agent_not_found",
  "message"}`. A limited device is refused it (403), as it is every route its scope does not
  list: it reads the snapshot. Read it, in place of the
  snapshot, for an event of these kinds, whose change stays in that agent's row:
  `portrait-selected`, `agent-moved`, `stale-work`, `coordination-changed` with a `session`,
  `agent-transit` without `fields`, and a carried kind about a row the app does not hold (one
  put away that `include_archived=0` left out).
- **The snapshot is read** for every other kind (`created`, `forked`, `relaunched`, `deleted`,
  `archive-changed`, `agent-renamed`, `contact-assigned`, `voice-changed`, `voice-profile`,
  `backend-quota`, the personas' and janitors' kinds) and for the reconnect nudge: their change
  reaches other rows or the snapshot's head (a maker's counts, its helpers' states, the voices
  shared, the names in `roster`, `archived_count`).
- **Bound privacy (feature `bound_privacy`).** A reader who may not read an agent's words is
  given its event and its row as the snapshot gives that reader its row: names, states, numbers
  and times kept, words blank, `words_withheld: true`. The event and its `parent` are each
  judged by their own `session`, frame by frame as the stream is sent. `head_revision` and
  `conversation_id` on `transcript-updated` are a number and an id, which such a reader's
  snapshot row has too. A maker's `status_text` names a working helper bound otherwise than the maker (one
  that some reader of the maker may not read) by its session alone, never with its line
  (issue 819), so neither the maker's row nor `parent.fields` carries that helper's words. `GET /agents/{session}`
  of such an agent answers 200 with that row, never 403.

### The sync algorithm

A client that follows these steps never shows a duplicated, missing, or
stale message:

```
on agent-roster with fields (feature roster_fields):
    patch the row of session with fields; patch the row of parent.session with parent.fields
on agent-roster of a one-row kind (feature agent_row), or of a carried kind for a row not held:
    row = GET /agents/{session}; replace that row (404: drop it)
on start / reconnect / any other agent-roster:
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

on transcript-updated(session): if it names head_revision and conversation_id and the cache
    holds that conversation at that revision or later: nothing; else debounce ~100 ms, then fetch delta(session)
on agent-roster created or reset for a cached session: drop cache, reload
    (a relaunch or a conversation fork keeps conversation_id: fetch delta)

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
any subset. Payload shapes are documented with the Host's handlers in
`server-rs/crates/clarp-host/src`. The last column names the module of the
retired Python Host each surface came from, kept as a record.

| Surface | Routes | Server module |
|---|---|---|
| Device pairing | `GET /pairing/identity`, `POST /pairing/exchange`, `GET /paired-devices`, `POST /paired-devices/revoke`, `GET /devices`, `GET /server-info` | `device_pairing.py`, `server_identity.py` |
| Push notifications | `POST /devices` (APNs token, or an Android FCM token with `"platform": "android", "provider": "fcm"`: [push-android.md](push-android.md); an FCM registration may declare `"supported_conversation_modes": ["agent_live_v1"]`, which makes the phone Live-capable for `POST /calls/live` and Live rings, feature `agent_live_fcm`: [Live calls](push-android.md#live-calls-feature-agent_live_fcm-host-contract-94)), `DELETE /devices`, `GET /devices/push-tokens` ([unregistering](push-android.md#unregistering-and-revocation)), `POST`/`DELETE /devices/live-activity` (an ActivityKit token; or `"platform": "fcm"`, the agents-working summary for an Android phone, feature `live_activity_fcm`: [agents working](push-android.md#agents-working-feature-live_activity_fcm-host-contract-95)), `GET /notification-avatars/…`, `/attention` | `apns.py`, `fcm.py`, `user_notifications.py` |
| Harness sign-in and usage | `/backend-auth` (`?harnesses=all` lists every harness), `/backend-auth/login`, `/backend-auth/login-code`, `/backend-auth/logout`, `/backend-usage` | `backend_auth.py`, `backend_usage.py` |
| Hotseat account status (read-only snapshot, `accounts_hotseat`) | `/hotseat/status` | `hotseat_status.py` |
| Model providers inside a harness (OpenCode, DeepSeek card) | `GET /harness-providers?harness=`, `GET /harness-providers/models?harness=&provider=`, `POST /harness-providers/login` (`key`, or `method` for OpenCode's own device/OAuth flow), `/harness-providers/login-code`, `/harness-providers/logout`; full-device scope; keys go to OpenCode's own `auth.json` and are never returned (see `docs/compatibility.md` row 75) | `harness_providers.py` |
| Files and media | `POST /upload` (headers `X-Session`, `X-File-Name`, `X-Upload-ID`), `/media`, `/media/<id>/favorite`, `DELETE /media/<id>`, `/media/delete`, `/media/deleted`, `/agent-files`, `/agent-file` | `upload_results.py`, `media_store.py`, `agent_files.py` |
| Artifacts and decisions | `/artifacts`, `/artifacts/<id>`, `/decisions`, `/decisions/<id>/resolve`, `/task-plan` | `artifacts.py`, `task_plans.py` |
| Portraits and personas | `/agent-portraits`, `/agent-portrait-generation`, `/personas`, `/personas/update`, `/personalities/settings`, `/avatar-settings` | `agent_portraits.py`, `portrait_generation.py`, `personas.py`, `model_avatars.py` |
| Teams | `/teams`, `/teams/<id>`, `/teams/<id>/members`, `/teams/<id>/messages`, `/team-nudging` | `team_store.py`, `team_leader.py` |
| Autonomy | `/heartbeat/settings`, `/agent-heartbeat/status`, `/dreaming/settings`, `/dreaming/runs`, `/automation-settings`, `/herald/settings`, `/orchestrator/settings`, `/orchestrator/route-delegation` | `heartbeat.py`, `dreaming.py`, `herald.py`, `orchestrator.py` |
| Transcription | `/transcription-capabilities`, `/transcription-guidance`, `/transcription-models/install`, `/transcription-models/remove` | `transcription_models.py`, `vocab.py` |
| Voice providers | `/tts/providers`, `/voice-catalog`, `/voice-preview`, `/cartesia-voices`, `/cartesia-voice-preview` | `voice_catalog.py` |
| Background jobs and updates | `/background-jobs`, `GET /background-jobs/<job_id>` (see below), `DELETE /background-jobs/<job_id>` (cancel), `/server-update`, `/status`, `/compact`, `/managed-skills`, `/diagnostics/settings` | `background_jobs.py`, `server_update.py`, `managed_skills.py` |
| Location and calendar | `/location`, `/location/request`, `/calendar/request`, `/calendar/response` | `location.py`, `calendar_request.py` |
| Prompt history | `/identity/prompt-history` | `prompt_history.py` |
| Voice timeline | `POST /voice-events`, `GET /voice-events`, `GET /voice-events/utterances`; `/transcribe` headers `X-Utterance-ID`, `X-Client-Ts` | `voice_events.py`, `audio_metrics.py`, `docs/voice-tracing.md` |
| Diagnostics | `/crash` (MetricKit), `/diagnostics/health` | `ios_diagnostics.py`, `health.py` |
| Tool explanations | `POST /tool-explanations`, `GET /tool-explanations/stats` (see below) | `tool_explanations.py`, `tool_explanation_learning.py`, `docs/tool-explanations.md` |

### Saving a voice provider: `POST /tts/providers`

`{"provider", "fallback", "voice"}` saves the choice and answers the provider
status plus `restart_scheduled`. Every provider but Cartesia needs a Host
restart; the Host restarts its own service half a second after the answer.

Only the installed Host does that (Host contract 99). Any other Host still saves, and answers
`restart_scheduled: false` with
`restart_refusal: {"reason": "not_in_unit" | "no_definition" | "foreign_directories" | "disposable_host", "message": "…"}`.
`restart_refusal` is absent whenever a restart was scheduled or none was
needed. The same Hosts answer `POST /server-update` with
`409 {"ok": false, "message": "This Host is not the installed Clarp service: …"}`.

### An agent's voice and hearing: `voice_profile` (feature `voice_profile`, Host contract 109)

Each agent row in `GET /agents/snapshot` that is not archived carries `voice_profile` (an
archived agent speaks to nobody; its row has none, in the snapshot and in `GET /agents/archived`
alike): what the agent is heard saying and how it hears, as the Host
resolves it. The Host computes it with the same functions that speak and transcribe, so a
client shows it and derives nothing from `voice_id`.

```json
"voice_profile": {
  "speech": {"provider": "cartesia", "provider_label": "Cartesia Sonic",
             "voice_id": "…", "voice_label": "Brooke", "origin": "own",
             "streaming": true, "silent_reason": null, "shared_with": 0},
  "hearing": {"engines": [{"id": "elevenlabs:scribe_v2", "provider": "elevenlabs",
                           "model": "scribe_v2", "label": "ElevenLabs Scribe v2"}],
              "engine_origin": "own", "device_may_override": false,
              "device_cloud_languages": null,
              "languages": ["no", "en"], "language_origin": "own",
              "configured_languages": ["no", "en"]}
}
```

- `speech.provider` is the provider the next clip is spoken with (a per-agent override
  and the configured fallback included), or `"none"`. `voice_id` is null when nothing is
  heard; `silent_reason` then says why. `voice_label`, `provider_label` and each engine's `label` are a real name or null, never
  an empty string; a client shows its own words for null.
- `speech.origin`: `own` (the agent's or its contact's own voice) or `default` (the
  provider's one default voice, the same for every agent without its own); null when nothing
  is heard (`voice_id` null, `silent_reason` given).
- `speech.voice_label` comes from the Host's own tables and a names file it fills in the
  background, never from a request: Cartesia's catalogue name and ElevenLabs' name for a voice
  in use (null until the Host has fetched them, and for a voice the provider does not know),
  ElevenLabs' built-in voices, Gemini voices in the voice picker's words (a prebuilt voice by
  its name, a Voice Design voice as `<Contact> (designed)`, the contact it is stored under in
  `[gemini_tts.voices]`; null for an id in no map), Google's and Deepgram's voice names.
- An archived agent has no `voice_profile`: not on its snapshot row, and not in the creation
  answer or a roster event about it.
- The rule the Host holds: no voice of its own =
  the provider's default voice where the provider has exactly one, silence (`no_voice`) where
  it has none; a migrated agent must not fall silent where it spoke before.
- `speech.shared_with`: how many other active agents speak with the same provider and
  voice (0: its own). Say it when it is not 0.
- `speech.streaming`: whether audio begins before the whole clip is made.
- `hearing.engines`: in the order they are tried, each only after the one before fails.
  `hearing.languages`: ISO codes the first engine is handed; empty means it detects the
  language itself (a multilingual local model, or ElevenLabs Scribe given a set).
- `hearing.engine_origin`, `hearing.language_origin`: `own` (this agent's setting) or
  `default` (the Host's).
- `hearing.configured_languages` (Host contract: next free at merge): the language set chosen
  for this agent, else the Host's default set, as ISO codes, whatever the first engine is
  handed. Show it as "listens for": an agent on ElevenLabs Scribe with `no,en` reads
  `languages: []` (Scribe detects) but `configured_languages: ["no","en"]`. `language_origin`
  says whose set it is (`own` or `default`), except when the first engine is a local model:
  then `languages` is the model's own and `language_origin` is `default`.
- `hearing.device_may_override`: the one rule for a client. When it is true (the engine
  is the Host's default), a phone that sends its own `X-Transcription-Model` is heard
  with that model instead; such a client shows its own setting.
- `hearing.device_cloud_languages` (null unless `device_may_override` is true): the codes a
  hosted engine chosen on the device is handed for this agent (its own set, else the Host's
  default). When the phone's own model applies, show the phone's engine, and with it these
  languages for a hosted engine, or for a local or adapter model the `languages` of that
  model's row in `GET /transcription-capabilities`; never this profile's `languages` beside
  the phone's engine. A local model chosen on the device ignores the agent's languages (its
  language is fixed when it loads: English for a `.en` model, detected otherwise), and a
  custom adapter is handed no language at all.
- `GET /transcription-capabilities` (and `GET /transcription-providers`): each hosted model
  row has `accepts_language_set`, whether it can be handed a set such as `no,en`. An engine
  that cannot refuses a recording for an agent with a set, so a client can warn before
  recording ("this model cannot hear this agent's languages").
- **`origin`, `engine_origin`, `language_origin` and `silent_reason` are open sets.** The
  documented values are `own` and `default` (a later Host adds `parent`), and for
  `silent_reason`: `no_voice` (no voice for the provider), `voice_unavailable` (the agent has a
  voice of its own the provider refuses, such as a Google voice off its list, or a Cartesia or
  ElevenLabs voice the provider has deleted (it answered 404 when the Host looked it up; the
  Host logs `voiceGone` once); with feature
  `voice_provider_maps`), `provider_off` (the Host speaks with no provider), `not_configured` (the provider
  has no credential or is refused) and, with feature `voice_pool`, `voiceless_role` (a role
  that is never heard: a Janitor).
  A client shows plain wording for a value it does not know; it never fails on one.

The snapshot head carries `voice_defaults` (the same `speech` and `hearing` shapes for an
agent not yet created: `speech.voice_id` is null and `speech.assignment` says how its voice
will be chosen: `pool` with feature `voice_pool`) and `voice_profile_version`, which changes whenever a
Host-wide input of any agent's profile changes.

Freshness: `voice_profile` is also in the `POST /agents` answer's `agent` and in the
`agent-roster` events `created`, `relaunched`, `forked`, `contact-assigned`,
`agent-renamed` and `voice-changed` (after `POST /agent-voice`). A change that can alter
every agent (`POST /tts/providers`, `POST /transcription-providers`, a configuration
edit) sends `agent-roster` with `kind` `voice-profile`: re-read the snapshot.

### A stored voice and its edits (feature `voice_provider_maps`; Host contract: next free at merge)

A stored `voice_id` (agent or contact) is a provider map, compact sorted JSON such as
`{"cartesia":"<uuid>","gemini":"Kore"}`; `{}` means no voice of its own. A bare value stored
before this release is still read as an ElevenLabs id; `clarp-admin voice migrate` brings
stored values to the map shape and writes only rows that sound the same under every provider.

- `POST /agent-voice` `{session, voice_id}` without `provider`: the id goes under the provider
  its form names (a uuid: Cartesia; a Chirp 3 HD name: Google; 20 letters and digits:
  ElevenLabs; otherwise ElevenLabs), merged into the agent's map. With `provider`, as before.
- `POST /personas/update` merges `voice_id` into the stored map per provider. Optional
  `base_voice_id`: the map the app last read. A provider whose incoming value equals the base
  keeps the stored value (another app may have changed it since); one in the base but not in
  the incoming map is removed. Without `base_voice_id` nothing is removed by its absence; an
  empty value for a provider removes it. Only the incoming value is read by its form: an edit
  never moves a stored value to another provider.

### A working voice for every new agent and contact (feature `voice_pool`)

A new agent (`POST /agents`, an adopted session, an anonymous launch, an assigned or minted
contact, the first-install roster) and a new contact (`POST /personas`) end with a voice that
is heard under the Host's configured provider and under its configured fallback. The Host keeps
every voice the request gave and adds, for each of those providers it has none for, a free
voice from its pool (`[cartesia] pool` when set; else, for an English agent, the curated list
in `server-rs/crates/clarp-kernel/src/cartesia_voice_pool.json` less the voices Cartesia no longer offers, and for
another language Cartesia's catalogue in that language; the ElevenLabs and Gemini
catalogues). A free voice is one no agent (archived ones included) or contact speaks with; when
there is none, one is reused (below). With this feature an app may create an agent or contact
with no voice at all and trust the Host; a phone's `auto-<uuid>` stand-in voice is replaced.
The stored value is always in the one shape (a bare Cartesia uuid on create becomes
`{"cartesia":"<uuid>"}`). A `voice_id` of `{}` means "no voice given", as `""` does: a new
agent then takes its contact's voice, and a relaunch keeps its own. `POST /personas/update` fills
a contact stored in the one shape the same way a contact assignment does (a pool voice for each
of those providers it has none for, an empty map included). Existing agents are not changed. A Janitor gets no voice: no app can
send it a message, so it never speaks. Renaming an agent keeps the voice it spoke with.

With feature `voice_for_helpers` (Host contract 135): a helper (`role` `helper`) is created
with its parent's voice for each provider it has none for, the voice the parent is heard with,
and takes a pool voice only where the parent has none; a helper is heard only when the user
opens its chat, and giving each of them a voice of its own would empty the pool within days.
A fork gets a pool voice like any new agent. When every pool voice is taken, one is reused: a
voice that only archived agents hold first (reusing a voice the user still hears is worse),
then the least recently used one (the voice whose holders' newest clip, else their creation,
is oldest); the Host logs `voicePoolExhausted`. At start the Host writes every stored voice in
the one shape where that changes nothing anyone hears under any provider (the rows
`clarp-admin voice migrate --apply` writes), and logs `voiceShapeMigrated` with the rows it
left because they would sound different. The same pass copies each agent's own transcription
engine and language settings, today matched by its name or session, under its agent id
(`transcription.agent_engines`, `transcription.agent_languages`); an existing entry under the
id is kept, and nothing is removed.

### A reply that is not spoken: `tts-error` (feature `speech_outcome`, Host contract 109)

When a reply's speech ends without audio, the Host sends `tts-error` with `reason`, `provider`
and `trace_id`, also when no provider had a voice for the agent (before this feature that case
sent nothing). It is sent once per turn and reason, however many clips of that reply fail.

- `reason` is an open set: `no_voice`, `voice_unavailable`, `not_configured`, `provider_off` (the same words and causes
  as `voice_profile.speech.silent_reason`), `provider_failed` (the provider did not make the clip)
  and `host_failed`. A client shows plain words for a value it does not know.
- `message` is a sentence built from the reason and the provider's name ("Not spoken: Cartesia
  has no voice for Rachel."), never from an error's text; Google's own failures keep their
  words. `error` now carries the reason, not provider-internal text.
- `trace_id` is the turn whose reply was not spoken: an assistant row of `GET /log` with the same
  `trace_id` is that reply, so write the line under it ("Not spoken: this agent has no voice").
  No retry and no notification.
- Lines spoken outside a turn (announcements, notes) have no reply row and send nothing yet.
- An older client shows `message` where it shows `tts-error` today (a status line).

### The owner's settings of lanes: `/lane-settings` (feature `lane_settings`, Rust Host contract 178)

A Rust Host can keep an agent's tool running between its chat turns (a lane: Codex's app-server,
a kept Claude Code, a kept `muse serve`). The owner switches lanes on per tool and sets two limits that all lanes of
the Host share. A full-scope device or the Host's own token reads and changes them; an agent's
own tools (`CLARP_AUTH_TOKEN` in a turn) may read them. A limited device may do neither.

`GET /lane-settings` answers:

```json
{
  "codex_chat_lane": true,
  "claude_chat_lane": false,
  "muse_chat_lane": false,
  "most": 8,
  "memory_mb": 2200,
  "idle_minutes": 15,
  "ranges": {
    "most": {"min": 1, "max": 32},
    "memory_mb": {"min": 256, "max": 16384},
    "idle_minutes": {"min": 1, "max": 240}
  },
  "held": {
    "lanes": 2,
    "memory_mb": 412,
    "unanswered": 0,
    "tools": {
      "codex": {"lanes": 1, "busy": 0, "memory_mb": 140},
      "claude": {"lanes": 1, "busy": 1, "memory_mb": 272},
      "muse": {"lanes": 0, "busy": 0, "memory_mb": 0}
    }
  }
}
```

- `codex_chat_lane`, `claude_chat_lane`, `muse_chat_lane`: whether that
  tool's chat turns run on lanes (off unless set).
- `most`: how many lanes the Host keeps, of every tool together. `memory_mb`: what they may hold
  together, in MiB of proportional set size. A lane starts only when both allow it; the one idle
  longest leaves first. `idle_minutes`: a lane with no turn leaves after this long.
- These are the values the Host uses now (its defaults where the owner set none). A Host
  configured by hand may hold a value outside `ranges`; show it as it is.
- From Rust Host contract 180 (feature `lane_auto_limits`, issue 792) the limits follow the
  machine's memory by default:
  - `limits`: `"auto"` (the default) or `"fixed"`.
  - In auto, a lane starts while the memory the machine has free (`MemAvailable`), less the new
    lane's typical size, stays at or above `reserve_percent` of its memory (20 unless set, range
    5-50), however many lanes run: from contract 186 auto has no count (`ceiling` is still told,
    with its range, and bounds nothing; a `POST` that names it changes nothing). Idle lanes leave
    first, as they do
    for the fixed limits, and each one that leaves gives back what it weighed. Idle lanes also
    leave when the kernel reports memory pressure between starts. A lane started in the last
    minute, or one that has not weighed itself, is counted at its tool's typical size until it
    has grown, so a burst of starts cannot overshoot the reserve. `most` and `memory_mb` are not
    used in auto, and are kept for `"fixed"`.
  - A `POST` that changes `most` or `memory_mb` switches the Host to `"fixed"` with that value,
    unless the same request names `limits`: the owner setting a count or a budget chose fixed
    limits. An app that only knows `lane_settings` (contract 178) needs no change for it.
  - In fixed, `most` and `memory_mb` bound the lanes as before.
  - `held` gains `available_mb` (what the machine has free now), `reserve_mb` (what auto keeps
    free) and `room_mb` (what is left for lanes), each `null` where the Host cannot read its
    machine's memory. In auto, a machine whose memory cannot be read keeps the fixed limits.
  - `POST` takes `limits` (`"auto"` or `"fixed"`, else `400` `wrong_type`), `reserve_percent` and
    `ceiling` as it takes the other limits. They are the top-level keys `lane_limits`,
    `lane_reserve_percent` and `lane_ceiling` of the Host's `config.toml`.
  - Show the auto settings, and what is free, only where the Host lists the feature.
- From Rust Host contract 186 (feature `turn_pressure_admission`) the Host holds the turns nobody
  waits on while the machine is under pressure, read from the kernel's `/proc/pressure`:
  - The machine is under pressure when io `some` avg60 reaches `pressure_io_percent` (30 unless
    set), cpu `some` avg60 reaches `pressure_cpu_percent` (60), or memory `full` avg10 reaches
    `pressure_memory_percent` (10). It is calm again only once every one of the three is below
    80% of its threshold, so a reading that wavers about a threshold does not flap. A machine
    without the pressure files is never under pressure.
  - While it is, a turn whose origin is not the owner's word waits before its tool is started:
    a helper's, a janitor's, another agent's message, a goal's wake, a schedule or another
    trigger. The owner's own turns, and the Oracle's carrying his words, never wait. A turn that
    waits starts when the machine is calm again, or ten minutes after it began to wait, whichever
    comes first; one at a time, the pressure read again between two. A message of the owner's
    that would queue behind a waiting turn lets it go at once. A Stop of a waiting turn ends it at
    once. A waiting turn shows as a turn that has started and has said nothing yet.
  - What the owner waits on through another agent is his for one hop: a message another agent
    sends (`/send` with its turn's token, as `clarp-admin prompt` does) or a fork it opens
    (`POST /agents/<session>/fork`, `clarp-admin agent fork`) while that agent runs one of the
    owner's own turns is marked when it is accepted, and the turn it starts never waits. What
    that turn sends on is not marked, nor is anything sent from a helper's own turn. The mark is
    kept with the message (`owner_relayed` in the agent's log, never read from a request).
  - The hold is of work, not of processes: a waiting turn is held whether it would run on a lane
    that lives or start its tool anew. Once let go while the machine is still under pressure (at
    the ten minutes), it uses a lane that runs, and starts none: its turn runs in a process of
    its own where no lane runs, and no lane starts after it. The owner's turns may start a lane
    at any time. A native goal's lane, which its turn needs, still starts.
  - `live`: the processes that live now, apart from the turns that start a tool anew:
    `{"lanes", "busy", "idle", "tools": {"codex"|"claude"|"muse": {"lanes", "busy", "idle"}}}`,
    as `held` counts them. Many live lanes are fine; what loads the machine is tools started anew.
  - `cold_starts_last_hour`: the turns that started without a lane in the last hour, `total` and
    `tools` by the agent's tool (`{"claude": 3, "scripted": 1}`), counted since the Host started.
  - The CPU figure is the Host's own cgroup's (its `cpu.pressure`), so the agents' own heavy work
    in their turns does not count; the machine's `/proc/pressure/cpu` only where the cgroup
    cannot be read. io and memory are the machine's.
  - `pressure`: what was read now. `cpu_source`: `"host-cgroup"` or `"system"`. `cpu`, `io` and
    `memory` each
    `{"some": {"avg10", "avg60"}, "full": {"avg10", "avg60"} | null}` in percent, or `null`
    where the Host has no such file; `high`; and `thresholds`
    (`cpu_some_avg60`, `io_some_avg60`, `memory_full_avg10`). `held_for_pressure`: how many turns
    wait now, across the Host.
  - `pressure_cpu_percent`, `pressure_io_percent` and `pressure_memory_percent` are limits like
    the others, with their `ranges` (5-95), set by `POST` and kept as top-level keys of the same
    names in the Host's `config.toml`. Setting one does not switch the lanes to `"fixed"`.
  - The `lane-settings-updated` event carries the three thresholds, never `pressure`,
    `held_for_pressure`, `live` or `cold_starts_last_hour`, which are read anew.
  - Show the pressure, and its thresholds, only where the Host lists the feature.
- `held`: the lanes that run now, from each lane's own reading of its memory. `busy` counts those
  that hold a turn. A lane that has not weighed itself yet counts at its tool's typical size, as
  the budget counts it. The lanes are asked all at once, and the Host waits for them at most about
  two seconds together. A lane that has not answered by then is left out of `lanes` and
  `memory_mb` and counted in `unanswered`. Reading this never starts a lane.

`POST /lane-settings` takes any of `codex_chat_lane`, `claude_chat_lane`, `muse_chat_lane` (booleans), `most`,
`memory_mb`, `idle_minutes` (whole numbers within `ranges`), and answers as `GET` does after the
change. A key that is missing or `null` is left as it is. Other keys (`held`, `ranges`) are
ignored. A value equal to the one the Host uses now is no change: it is not checked against its
range and not written. So an app may send back what it read with one value changed, also where the
owner set a value outside `ranges` by hand, and a default the owner never set is not written into
the file. Every value that changes is checked before anything is written: one refusal changes
nothing. Refusals have a `code`:

- `400` `{"ok": false, "code": "out_of_range", "error": "most must be between 1 and 32", "setting": "most", "range": {"min": 1, "max": 32}}`;
- `400` `wrong_type` (with `setting`, and `range` for a number);
- `400` `nothing_to_change` (no known key named);
- `400` `bad_json` (the body is not a JSON object);
- `403` `{"error": "agents_may_not"}` for a request that names a turn (`X-Clarp-Turn`) or a peer
  Host. An agent's own tools get `403` from their token's scope;
- `409` `config_unreadable` when the Host's `config.toml` is not TOML, or holds a table under one
  of these keys. It is not written over;
- `500` `config_unwritable`.

The settings are the top-level keys of the Host's own `config.toml` (`codex_chat_lane`,
`claude_chat_lane`, `muse_chat_lane`, `codex_lane_most`, `lane_memory_mb`, `codex_lane_idle_minutes`). A change is
written into that file, with every other line, table and comment of it kept, and replaces the
file in one rename. It takes effect at each tool's next turn: a lane switched off leaves before its
agent's next turn, and a lower limit applies when the next lane starts. When something changed,
every client is then sent `lane-settings-updated` with the new settings (without `held`). Read `GET /lane-settings` again
on it. An older Host, or the Python Host, does not list `lane_settings` and answers `404`.

### Inspecting a background process: `GET /background-jobs/<job_id>`

Returns 404 for an unknown job, otherwise:

```json
{
  "job": { "…": "the same row as /background-jobs, plus progress_text, progress_at, log_path, worker_cwd, started_trace_id" },
  "handle": "bg1:2:sub-agent-stream-a",
  "is_process": true,
  "owner_session": "nadia", "owner_agent_id": "…",
  "started_trace_id": "0123456789abcdef",
  "helper_session": "",
  "progress": {"text": "running tests 3/10", "at": 1756800000000},
  "timeline": [
    {"event_id": 41, "observed_at": 1756799000000, "status": "running", "note": "", "change": "status"},
    {"event_id": 57, "observed_at": 1756800000000, "status": "running", "note": "running tests 3/10", "change": "progress"}
  ],
  "log": {"path": "/var/tmp/clarp-sub-agents/stream-a.log", "available": true, "reason": "",
          "text": "…last 64 KB…", "size": 181234, "truncated": true}
}
```

- `started_trace_id` is the trace of the owner's turn that registered this
  generation, or empty when it was registered between turns.
  `helper_session` names the helper agent this job mirrors (then
  `is_process` is false), else it is empty.
- `timeline` is the job's change feed, oldest first, at most 200 entries:
  `change` is `status` when `status` differs from the entry before,
  `progress` when the entry carries a progress `note`, else `update`.
- `log` is the tail of the file the worker registered with `clarp-agent-bg
  SESSION job-log HANDLE /abs/path`: the last 64 KB, starting on a line
  boundary when `truncated`. It is read only when the fully resolved path is
  a regular file under the worker's working directory, the owner agent's
  cwd, or `/var/tmp`; otherwise `available` is false and `reason` is one of
  `no_log`, `not_absolute`, `outside_allowed_roots`, `missing`,
  `not_a_regular_file`, `unreadable`. A limited device gets `reason:
  "forbidden"` and no text.
- Progress written with `clarp-agent-bg SESSION job-progress HANDLE "text"`
  also arrives live as `background-job-updated`, whose `job` carries
  `progress_text`. Cancel stays `DELETE /background-jobs/<job_id>`. Since Host
  contract 23 (feature `background_job_cancel_generation`) it accepts
  `?expected_generation=N` or a JSON body `{"expected_generation": N}` (the query wins): the cancel applies only if the job is still run N,
  checked atomically with the update. A different run answers `409
  generation_mismatch` with `expected_generation` and `current_generation` and
  changes nothing; see docs/compatibility.md row 23 for every response.
- The Host's stale-work reconcile writes its own notes, prefixed
  `reconcile:`. An agent's running job with no worker PID to verify whose
  heartbeat and progress have been silent for `[agents]
  job_stale_after_minutes` (15) gets a `reconcile: heartbeat stale` note,
  and after `job_heartbeat_grace_minutes` (15) more fails with
  `terminal_reason: "heartbeat_lost"` (`worker_freshness: "stale"`,
  `outcome_state: "unknown"`, like `heartbeat_expired`). A helper moved to
  `reported` for idleness is noted on the job that mirrors it.
- An agent that is idle with nothing counted has its own `status_text`
  cleared once it is older than `[agents] custom_status_ttl_hours` (2); a
  declared `background` state it left behind settles to `idle`, and
  `agent-roster` with `kind: "stale-work"` asks for a refetch. Janitor
  labels keep their own validity window.

### Delivery admission in `GET /status`

`delivery_admission` is process-local health of the F4 delivery admission
(reset when the Host restarts; never used to admit anything). It has fixed
keys: `scope` ("process"), `reset_on` ("process_restart"), `gate`
(`inflight_limit` 1, `rate_per_s` 2, `burst` 2, `probes_per_log_response` 1,
`inflight_now`, `tokens_now`), `probes` (`started`, `reaped`, `bound`,
`last_duration_ms`), `omitted` (one counter per fixed reason),
`unexpected_by_type` (at most 16 exception type names plus `_other`), `budget`
(`deadline_ms` 2000, `caps_ms` {`o1` 500, `b` 1000, `b_reap_reserve` 250,
`o2` 500}, and `last_ms` / `max_ms` per phase `o1`, `b`, `snapshot`, `o2`,
`total`: bounded health metadata, not a benchmark) and
`reconcile` (`passes`, `laps`, `last_lap_ms`, `failures`,
`consecutive_failures`, `last_failure_type`, `refused`, `findings` per fixed
kind). The optional delivery part of one `GET /log` has ONE monotonic 2.0 s deadline
from its start (after the core read): the first runtime observation (shared
status of at most 1 s, else one RPC of at most 0.5 s), the manager read (at most
1.0 s plus its 0.25 s kill / reap reserve, both inside the deadline), the read
snapshot (its SQL is interrupted past the deadline) and the fresh second
observation (one RPC of at most 0.5 s); each is clamped to what is left. When
nothing is left before a step, or a step times out or fails, every delivery
field is omitted and the step is counted (`deadline_exhausted`,
`o1_unavailable`, `o2_unavailable`); nothing is retried and no partial field or
cursor is returned. A first observation that misses the shared status first
takes the request's manager-read permit (the same 1 in flight / 2 per second
gate), so no status RPC starts while that gate is exhausted; its result refreshes
the shared status only if that is free at that moment and holds nothing newer.
While the shared status is busy (another status read in flight) the delivery
fields are omitted (`status_contended`) rather than waiting or reading twice. A
failed observation is logged at most once per step and exception type per 10 s
(type name only); every one is counted. The `start_mismatch` reason counts any main start time the Host could not
confirm equal: a different start, a process that is gone, or an unreadable
`/proc` entry (fail-closed; it is not evidence of a different process). Reading
it does no database, manager, runtime or file access. It is at
most 8 KiB; past that it is `{"overflow": true, "serialized_bytes", "limit",
"scope"}`.

### Runtime status in `GET /status`

`runtime` always carries the same keys: `available`, `reason`, `release_id`,
`fenced`, `sealed`, `draining`, `drain`, `active_turns` and `observed_at`
(epoch ms). `fenced` is the graceful drain's soft admission fence (no turn
starts, arrivals are held durably), `sealed` the hard fence of the moment
before the handoff, and `draining` is either. `drain` is the graceful release
drain of [runtime-restarts.md](runtime-restarts.md):
`{"mode": "natural | fenced", "phase": "idle | waiting_idle | draining |
backoff | handed_over", "running_release",
"target_release", "attempt", "fenced_since", "deadline", "next_attempt_at",
"blockers": {kind: [agent ids]}, "last_blockers", "last_outcome",
"last_event": {"kind": "fenced | retargeted | reverted | withdrawn | sealed |
seal_missed", "at", "target_release", "reason"}}` with times in epoch ms.
`waiting_idle` is the natural drain (the default) waiting, with nothing
fenced, for a moment with no blockers; `seal_missed` means its brief seal
check found something and lowered the fence at once. `blockers` is non-empty
only while the phase is `draining` or `waiting_idle`; the previous attempt's
are `last_blockers`. A runtime from before the drain reports
`{"phase": "unsupported"}`, and an embedded Host `{"phase": "idle"}`.

When the runtime does not answer its status call, the block is explicitly
unavailable, never idle: `available` false, `reason` one of `timeout`,
`unreachable`, `protocol_error`, `empty_status`, `error`; `fenced`, `sealed`,
`draining` and `active_turns` are `null` (unknown); and `drain` is
`{"phase": "unavailable", "last_known", "last_known_at"}`, the last real drain
block this Host saw and when. It is diagnostic: no client contract depends on
it, and clients must tolerate it missing. Contract 62 (`runtime_drain_status`)
introduced this shape; before it `draining` meant sealed.

### Read-only HTML reports: `html_form` with `read_only`

An `html_form` artifact (see `docs/html-forms/README.md`) may be a read-only
report. Create it with `POST /artifacts`:

```json
{"session": "solu", "type": "html_form", "title": "Audit findings",
 "summary": "Three issues", "artifact_id": "report-audit-findings-1a2b3c4d5e6f",
 "payload": {"content": "<!doctype html>…", "version": "1", "read_only": true}}
```

- `read_only` is an optional boolean; absent means `false`, and existing forms
  are unchanged. When it is `true`, `answer_schema` may be omitted and is
  stored as `{"type":"object","properties":{},"additionalProperties":false}`;
  any other schema is rejected with 400.
- The artifact carries `read_only: true` at the top level as well as in
  `payload`, so `representation=flat-v1` clients see it too.
- `POST /artifacts/<id>/submit` for a read-only report returns 400
  `{"error": "read-only report; it does not accept answers"}`.
- The HTML is self-contained and rendered by the same confined form viewer;
  it is never served on the Host origin. Clients before contract 19 show it as
  a form with an empty answer set. Feature `html_reports`.

### Moving a render spec: `GET /artifacts/<id>/export`

For the administrator's token only, and only when it comes in the `Authorization` header: the
same token in a query string or a cookie is refused (403), as any paired device is, limited or
full, and the client header plays no part. For a `render_spec` it answers
`{"kind": "render_spec", "render_spec": {"artifact": ..., "stored": ..., "submission": ...}}`:
the row as `GET /artifacts` lists it, the stored payload as kept (the spec, its catalog and
schema identity, its content identity), and the accepted submission or `null` (its
`submission_id`, `version`, `answers`, `accepted_at`, `payload_hash`, `synthesize_audio`,
`delivery_status` and `delivered_at`). Another kind is refused with 409 and its `kind`; the body
is shaped so a kind can be added later. The answers are the user's: no log line carries them.

### Which turn published an artifact: `source_trace_id`

An artifact row carries `source_trace_id`: the trace of the turn that published it, or `''`.
The Host sets it from the request's turn token (`X-Clarp-Turn`, which the publishing tools
send) and only when that turn belongs to the artifact's own agent and is that agent's running
turn when the artifact is created. A card published after its turn ended, by a job the turn
started for example, has `''` and is placed by time. A Codex agent through the shared
app-server has no per-turn token, so its artifacts have `''` too. A later revision of the
artifact keeps the trace it was created with. `source_message_id` stays `''`.

### A chat's gallery: list, favourite, delete (feature `media_manage`)

The images of one chat are what `GET /media?session=<session>` lists. A Host that does
not advertise `media_manage` answers `404` on the three writes below, and its rows have
no `favorite` key: show no heart and no bin there.

**The row.** Every asset row (in `GET /media`, in the favourite answer, in a publish
answer) carries, besides its other keys, `favorite` (bool), `favorite_at` (ms the mark
was set, else `null`) and `used_as`: a list of `{"kind": "portrait", "agent_id",
"persona"}` for each agent whose live portrait is this picture (made from this image, or
the very same file), usually empty.

**The list.** `GET /media?session=…` takes, all optional: `limit` (1 to 500, default
100), `before` (the `next_before` of the previous answer, an opaque string; a string the
Host did not write answers `400 {"error": "bad before"}`) and `favorites=1` (only
favourites, newest mark first, paged with the same `before`). The answer is
`{"session", "assets", "next_before", "total", "favorites_total"}`: `next_before` is
`null` on the last page, `total` counts the chat's images that are not deleted and
`favorites_total` its favourites. Order without `favorites`: newest first by
`created_at`, ties by `asset_id` (descending). A deleted image is never listed.

**Favourite.** `POST /media/<asset_id>/favorite` with `{"favorite": true}` or
`{"favorite": false}` answers `200` with the asset's row. Setting what is already set
answers the same row and leaves `favorite_at` as it was. `400` for a body without a
boolean `favorite`, `404` for an unknown id, `410` (below) for a deleted image, `422
{"error": "not_an_image"}` for a file, audio or other asset that is not an image.

**Delete.** `DELETE /media/<asset_id>` answers `200 {"asset_id", "deleted": true,
"deleted_at", "still_used_as"}`; `still_used_as` is the row's `used_as` (a portrait made
from the image is not affected: it keeps showing). Repeating it answers the same `200`
with the first `deleted_at`. `404 {"error": "not_found"}` for an unknown id, `422
{"error": "not_an_image"}` as above. Several at once: `POST /media/delete` with
`{"asset_ids": [...]}` (1 to 200 ids, else `400`) answers `200 {"results": [...]}`, one
entry per id in the order sent: the delete answer above, or `{"asset_id", "deleted":
false, "error": "not_found" | "not_an_image"}`. One bad id does not stop the others.

The image's bytes leave the Host's disk. Images are stored by content, so when another
image that is not deleted, a portrait or an avatar holds the very same file, the file
stays for it and goes with the last of them. The row is marked deleted first and the
file removed after; a crash between the two leaves the file, and the next delete of the
same id removes it.

**Afterwards.** `GET /media/<asset_id>` (also the media url of an artifact that points at
the image) answers `410 {"error": "removed", "asset_id", "deleted_at"}` for ever: show
"Image removed" and offer no retry. `404` stays an id the Host never had; any other
status that is not `200` is "not reachable", retry as before. The chat's `/log` row is not
rewritten: it keeps its `clarp-media://asset/<id>` markdown. Drop a cached copy of the
image on the `410` or on the event below.

**A device that was away.** The events stream does not promise to replay
`media-changed` after a reconnect, and a device with the bytes in its own cache never meets
the `410`. So ask `GET /media/deleted?since=<ms>` (optional `&session=…`, without it every
chat; optional `limit`, 1 to 1,000, default 1,000) when the app starts and whenever the
events stream comes back, then drop the cached bytes of every id listed. It answers `200
{"deleted": [{"asset_id", "session", "deleted_at"}, …], "next_since": <ms> | null,
"server_now": <ms>}`: the images deleted strictly after `since`, oldest first by
`deleted_at`, ties by `asset_id`. A page never ends inside a millisecond: when the limit
falls among images deleted in the same millisecond, the page takes all of that millisecond,
so a page may exceed `limit` by the size of one millisecond. `next_since` is the
`deleted_at` (an integer of ms) of the page's last image when more remain, else `null`; ask
again at once with `since=next_since`: it never repeats an image, never skips one and always
advances. `since` is a Host time: when `next_since` is `null`, keep the answer's
`server_now` for the next time (use `since=0` on the very first call). Every image deleted
after `server_now` comes in a later answer; one deleted in the millisecond of the read may
come twice. The Host keeps the record of a deleted image for good. `400` for a missing or
negative `since` or a `limit` that is not a number from 1 to 1,000. Any device that may read `GET /media`
may read this.

**Freshness.** `media-changed` (see the event table) with `change: "created"` is sent
once for every image added to a chat's gallery, whatever made it: published through
`POST /media`, a podcast companion's concept image once it is saved, or an image a
portrait generation run kept, when the run ends. `"deleted"` is sent once per image a
delete removed, also from the batch call, and for each image a portrait generation run
discarded, when the run ends. `"favorite"` (with `favorite`) for each favourite change that
changed something. Each is sent after the change has committed; a save that rolls back
sends nothing. `at` is the change's time in ms. `created` is a hint, idempotent per
`asset_id`: a client may receive it more than once for the same image (rarely: a late write
to a long-finished portrait run after the Host has stopped tracking that run) and must patch
or re-read by `asset_id`, never append.

**Who may.** A full device and the administrator. A limited device is refused the three
writes with `403 full device access required`; it still reads `GET /media`, new keys
included. A request carrying a turn token (`X-Clarp-Turn`, an agent's) or coming from a
paired peer Host (`X-Clarp-Peer`) is refused `403 {"error": "agents_may_not"}`. Publishing
is unchanged.

### Artifact list summaries: `representation=summary-v1`

`GET /artifacts`, `GET /pinned-artifacts` and `GET /attention/inbox` accept
`representation=summary-v1` when the Host advertises the feature
`artifact_summary_v1` (introduced at contract 73; gate on the feature, not the number). It is `flat-v1` (no `payload`) with
exactly two bodies left out, each replaced by an explicit marker. The rows,
their order, count, filters, paging, archived and pinned rows, `decision` and
every action field are exactly `flat-v1`'s.

- An `html_form` has **no** `content` key; it carries
  `"content_omitted": {"reason": "summary", "bytes": <UTF-8 length>,
  "sha256": "c1:<hex>", "version": <payload version or null>,
  "updated_at": <ms>}`. `sha256` is over the UTF-8 bytes of the decoded
  `content` string, so a client can verify hydrated content. `bytes` may be
  `null` with `"size_unknown": true`, and `sha256` `null` with
  `"hash_pending": true` (content with no UTF-8 encoding, or the request's
  hash budget spent); the client then hydrates before treating it as ready.
- A plan's `plan.goal` has **no** `history` key; it carries
  `"history_omitted": {"reason": "summary", "count": N, "last_at": <ms or
  null>, "digest": "h1:<hex>"}`. The digest is opaque: compare it for
  equality, never recompute it. It changes whenever the history does, even
  when the plan's revision does not; `null` with `"digest_pending": true`
  means hydrate before use.
- Hydrate an item with `GET /artifacts/<id>?representation=flat-v1`: the full
  item without the duplicate `payload`. Without that exact value (none, or any
  other) the route returns the full legacy item, as an older Host always does,
  so decode both shapes. 404 `{"error": "artifact not found"}` means deleted.
- An older Host answers `summary-v1` with 400 `unsupported artifact
  representation` (the inbox: `invalid attention query`). Gate on the
  feature; fall back to `flat-v1` only when it is absent or on that exact 400.
- What the Host bounds, and what it does not. Each request may spend at most
  64 MiB of UTF-8 encoding on markers for cache misses (counting a non-ASCII
  body's size and hashing it both count), checked before any body is encoded
  and exceeded by at most one counting chunk; a marker it cannot afford is
  pending, never wrong. The
  Host's marker caches are bounded by entries and by the bytes they hold. An
  unchanged list rebuild hashes nothing while its rows' identities fit those
  caches; a larger list keeps a stable resident share and rehashes only the
  excess each time, and rows past the budget stay pending (hydrating them
  returns the real content). None of this limits the response
  itself, which is as large as its rows, like any list response. A plan that
  reaches the projection without its stored goal text gets `digest_pending`.

### A checkpoint in a goal's history (Rust Host, issue 805)

`goal.checkpoint` (`GET /task-plans`, `GET /task-plan`, plan artifacts) holds the owner's current
`progress` and `next_work` whole. An entry of `goal.history` of kind `checkpoint` keeps of each
text longer than 280 characters only its first 280 characters followed by `…`, with
`progress_bytes` / `next_work_bytes` (its whole size in UTF-8 bytes) and `progress_sha256` /
`next_work_sha256` (the hex SHA-256 of its UTF-8 bytes) beside it, both of the text without the
blanks around it, as `goal.checkpoint` keeps it; an entry's text equals the current checkpoint's
when the digests match. Shorter texts are kept as they are. Entries written
whole before this are read the same way. Read the whole of an earlier text from the goal's
documents, not from its history.

### Goal keeper, paused steps and waivers (feature `goal_updater`, Host contract 96)

The Goal keeper Janitor (built-in, paused until enabled) keeps durable goals
current beside the agent doing the work
([goal-keeper.md](architecture/goal-keeper.md)); the agent is never prompted.
In every task plan (`GET /task-plans`, `GET /task-plan`, plan artifacts):

- `goal.updater` (or absent): the keeper's own checkpoint, `{at, progress,
  next_work, model, source_refs, changed_steps, through_ms, through_event, by,
  janitor_agent_id}`. Show it beside `goal.checkpoint` (the owner's), as the
  newer of the two by `at`, labelled as the Goal keeper's. Its step changes are
  in `items[].status` and in `goal.history` as kind `updater`. Its writes do
  not change `revision`.
- `plan.updater`: the Goal keeper as this goal sees it, `{enabled,
  janitor_agent_id, last_run_at, last_error, goal_error, retry_at}`. `enabled`
  is the Janitor's own switch (it ships paused); `last_run_at`/`last_error` are
  its latest run; `goal_error`/`retry_at` this goal's last failed update.
  With feature `goal_keeper_receipts` (Host contract 97), `last_receipt` (or
  null) is the Goal keeper's newest receipt for this goal:
  `{run_id, outcome, reason, steps, model, input_tokens, output_tokens,
  duration_ms, error, created_at}`, where `outcome` is one of `updated`,
  `unchanged`, `held_by_evidence_rule`, `refused_owner_changed`, `failed`,
  `skipped`, `cancelled` (and, with feature `goal_keeper_stalled`, Host contract
  98, `stalled`: the goal is active but stalled, reason `idle`, `usage_limit` or
  `provider_error`) and `steps` lists `{item_id, from, to, note}`. `error`
  is a short code, never free text, and a receipt for a goal the keeper did not
  write has empty `progress`/`next_work`.
  Every run leaves one receipt per goal it handled; `GET /janitors/<session>/runs` for
  the Goal keeper shows them under `receipts`, and `clarp-goal receipts
  PLAN_ID [LIMIT]` reads a goal's newest. The updater's ledger event
  (`kind: updater`) carries the message, job and goal-event refs it read in
  `source_refs`.
- `items[].paused`: true for an `in_progress` step of a paused goal. It is not
  running and `elapsed_ms` does not grow; show it as paused, not active.
- `goal.criteria[].waived` (or absent): `{at, reason, by: "user"}`, the user set the
  criterion aside. Completion accepts it without evidence; show the reason.

`POST /task-plan/action` takes `action: "waive"` or `"unwaive"` with `data`
`{criterion_id, reason}` (`reason` required to waive); a present `X-Clarp-Turn` that does not verify answers 403. Only the user may waive or
restore: the store refuses both for any other actor, including a request to this
route that carries an agent's `X-Clarp-Turn` and the owner's own `clarp-goal act`
(an agent could otherwise waive its own criteria and complete without evidence).
A paused goal takes
step updates and a `checkpoint` without `continuation` (it stays paused); a
checkpoint with a `continuation` answers 409 and saves nothing. The Janitor
itself is listed and controlled like any other (`/janitors`), template
`goal-keeper`, effect `goal_update`.

### Tool explanation hit rate: `GET /tool-explanations/stats`

How tool calls were explained over a window, for watching the learning cache
work. Query `window` is `24h` (default), `7d` or `30d`; `bucket` is `hour`
(default) or `day`. Anything else returns 400. Buckets start at UTC multiples
of their size; the last one is the bucket containing `now`.

```json
{
  "window": "24h", "bucket": "hour", "now": 1790000000000,
  "tiers": ["template", "learned", "exact_cache", "jev", "llm", "failed", "miss", "disabled"],
  "buckets": [
    {"start": 1789916400000,
     "counts": {"template": 40, "learned": 22, "exact_cache": 0, "jev": 1, "llm": 9,
                "failed": 1, "miss": 0, "disabled": 3},
     "lookups": 73, "hits": 62, "hit_rate": 0.8493, "mean_latency_ms": 180}
  ],
  "totals": {"counts": {"…": "per tier over the window"}, "lookups": 900, "hits": 700, "hit_rate": 0.7778,
             "learned_entries": 412, "learned_parameterised": 380, "learned_exact_only": 32,
             "learned_by_producer": {"llm": 405, "jev": 7}, "learned_hits": 5100,
             "jev_picks": 12, "llm_calls": 150}
}
```

- Each explained call is one decision, counted in the tier that answered it:
  `template` (a scripted template), `learned` (the permanent learned table),
  `exact_cache` (the 24-hour exact cache), `jev`, `llm`, `failed`, `miss`
  (not admitted: queue full or too many views) or `disabled`. A call with
  several parts counts once, in its most expensive part's tier.
- `hits` is `template + learned + exact_cache`; `lookups` is every tier but
  `disabled`; `hit_rate` is `hits / lookups`, or null without lookups.
  A rising `hit_rate` is the cache learning.
- `learned_*` totals describe the learned table at the current prompt and
  template versions; `jev_picks` and `llm_calls` count decisions in the
  window. `clarp-admin explanations stats` prints the same. Feature
  `tool_explanation_stats`.

## Compatibility policy

The core conversation protocol is **additive-only**. Fields and event
types are added, never renamed, removed, or retyped. A change that cannot
be expressed additively ships as a new event type or a new endpoint, and
the old one keeps working until `min_app_version` (below) moves past
every client that used it. The database schema follows the same rule.

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

The one place compatibility *is* negotiated is the App Store app, which
cannot be upgraded in lockstep with the server. `GET /server-info` carries
`contract.host` (the contract this Host implements, `HOST_CONTRACT`),
`contract.min_ios` (the oldest iOS client contract it still serves,
`MIN_IOS_CONTRACT`) and `contract.features` (the Host contract at which each
capability became available). The app carries its own `ClientContract.current`
and `ClientContract.minimumHost`, sends `X-Clarp-Client: ios/<build>
contract=<n>` on every request, and the Host echoes its verdict back as
`client.status` (`compatible`, `client_outdated`, `unknown`). The app shows a
persistent red banner while either floor is unmet and an amber one while a
feature the user has turned on is missing from the Host. Every bump is a row
in `docs/compatibility.md`; `server-rs/tools/tests/test_rust_contract.py` keeps the
table honest. `clarp_version` and `min_app_version` remain for apps that
predate the contract; a server that sends no `contract` block is treated as
contract 0 and therefore older than any app minimum.

Inside that window, features are negotiated per surface rather than by
version. `/server-info` also carries `capabilities.features`, the product
surfaces this Host implements (`teams`, `oracle`, `dreaming`, …,
`FEATURES` in `server-rs/crates/clarp-host/src/edge.rs`), so a client hides an entry point the Host
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

### Local encrypted transport

`GET /server-info` and the pairing exchange's `server` object include optional
`local_connection`: `enabled`, HTTPS `port`, `certificate_sha256` (leaf DER SHA-256),
`urls` (local HTTPS origin candidates) and `service_type` (`_clarps._tcp`). Clients
learn pins only over an already trusted authenticated HTTPS connection, validate
the exact certificate before sending credentials, and use `server_id` to confirm
the paired identity. Bonjour metadata does not authorize certificate trust. The
local endpoint uses the same device tokens and protocol without a relay prefix.

### Server home terminal

`GET /server-terminal` upgrades to a WebSocket for an authenticated full-device
owner. It runs the server user's login shell with the server home as its working
directory. Frames and resize messages use the same protocol as `/terminal/<session>`.
Disconnect closes only this shell process group; no agent binding, dispatch, queue
or provider inference is created. Feature: `server_home_terminal` (Host contract 48).
