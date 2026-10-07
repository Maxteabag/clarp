# Live items

A running turn as a list of **items** that appear, grow and settle in place, pushed
to clients as small ops instead of "go refetch `/log`" pings. This is how Codex,
Claude Code and OpenCode render a live session, and what Clarp clients (iOS,
desktop, web) build their status line, tool rows and thinking rows on.

Feature flag `live_items` in `/server-info` `features`, available from Host
contract **42** (the contract was published at 40). Gate on the feature, and
read it again on every `/server-info`: the Host lists `live_items` (and
`live_activity_push`) only while its live hub is actually serving events. The
hub runs in the agent runtime, which can still be an older release after the
HTTP server was updated, or be restarting; then the flag is absent (also from
`contract.features`) and clients must use the `/log` path and their own
working placeholder. A client that does not see the flag keeps today's path: `transcript-updated` →
`GET /log?after_revision`. Nothing in `/log` or the existing events changes
meaning; everything here is additive.

Machine-checkable parts:

- `contract/schemas/live.json`: the `live` SSE event, its ops, the item shape, the
  `GET /live` snapshot.
- `contract/live/*.json`: recorded live streams (thinking, an explore group, a
  long output tail, a diff, an interrupt, a gap) with the state a client must
  end up in. Every client runs these. The Host's reference reducer
  (`server/lib/live_items.py`) is checked against them by
  `tests/contract/test_live_fixtures.py`.

## 1. Model

```
conversation ─┬─ activity  (one status line: what is happening now)
              ├─ turn      (the open or last turn: started_at, ended_at, status)
              └─ items[]   (ordered by ordinal; each upserted by id)
```

`/log` stays the history and the anti-entropy path. Live items describe the
**open turn** (and the last finished one until its durable rows have landed).
They are never replayed through SSE; a client that missed some asks
`GET /live` (§5).

### 1.1 Item

Every item has:

| field | type | meaning |
|---|---|---|
| `id` | string | Stable, provider-derived (§2). The same id from first sight to the end. |
| `conv` | string | The conversation (`conversation_id`, as in `/log`). |
| `turn_id` | string | The turn's trace id. |
| `kind` | `message` `reasoning` `tool` `plan` `diff` `compaction` | |
| `status` | `pending` `running` `completed` `failed` `interrupted` | §1.2 |
| `ordinal` | integer | Sort key inside the conversation. Increases in creation order; a late item may slot between earlier ones. Sort by it, never by arrival. |
| `started_at_ms` | integer | Host clock, epoch ms, when the item started. Always set. |
| `ended_at_ms` | integer or null | When it settled. `null` while pending/running. |
| `rev` | integer | Item revision, 1 on first upsert, +1 on every op touching the item. |

Duration of any item is `ended_at_ms - started_at_ms`. Clients never time items
with their own clock except to tick a running item's elapsed time from
`started_at_ms` (correct for clock skew with `server_now_ms` from the event).

Per kind:

**message**: assistant text.
- `text`: the display text so far (same transformations as the `/log` row:
  hidden blocks stripped, an incomplete `<speak>`/`<team>` tag held back).
- `phase`: `commentary` (narration between tools) or `final` (the answer).
- `row_id`: the `/log` message id that carries this text. It is stable: the live
  row is finalized in place, so the same id appears in `/log` when the turn is
  imported.

**reasoning**: the model's thinking summary.
- `text`: summary text (Claude `display: summarized`, Codex reasoning summaries,
  OpenCode reasoning parts). Never raw chain of thought.
- `title`: a one-line headline the Host extracts (leading `**bold**` line, else the
  first sentence, ≤ 80 chars). Render collapsed: `Thinking: <title>` while running,
  `Thought for 12s: <title>` when completed.

**tool**: one tool call, updated in place from start to finish.

```jsonc
"tool": {
  "name": "Bash",                 // provider tool name
  "call_id": "toolu_01…",         // raw provider id; equals /log tools[].id or display_cells[].id
  "category": "exec",             // exec read list search edit write fetch mcp todo agent other
  "group": "explore:cl:toolu_01…",// set for consecutive read/list/search tools: id of the group's first item
  "label": "npm test",            // deterministic short label: program + args, path tail, query
  "command": "npm test -- --watch=false",  // raw command or primary input, ≤ 2 KB
  "input_preview": {"file_path": "src/a.ts"},  // bounded scalar inputs, ≤ 2 KB
  "explain": null,                // §6
  "output": {"tail": ["…"], "total_lines": 812, "truncated": true, "exit_code": null},
  "diff": {"added": 3, "removed": 1,
           "files": [{"path": "src/a.ts", "added": 3, "removed": 1}],
           "preview": "@@ -10,2 +10,4 @@\n…"}  // unified, ≤ 40 lines
}
```

- `output.tail` holds the **last 50 lines** at most (each ≤ 400 chars).
  `total_lines` counts every line the tool printed; `truncated` is true when the
  tail does not hold all of them. Render 3–5 lines plus `+N lines`.
- `diff` appears on edit/write tools: `added`/`removed` are the unified-diff line
  counts (`Edited src/a.ts (+3 −1)`).
- **Explore groups.** Consecutive tools of category `read`, `list` or `search`
  in one turn share `group`. Render them as one row: `Exploring` while any member
  is running, then `Explored 4 files, 2 searches`. The group id never changes
  while it grows (it is the first member's id). A tool of any other category, or
  a message, ends the group.

**plan**: `plan: {"steps": [{"text", "status": "pending|in_progress|completed"}]}`,
replaced whole on every update (Codex `turn/plan/updated`, OpenCode `todo.updated`,
Claude TodoWrite). One plan item per turn.

**diff**: the turn's aggregated change, `diff` as on a tool (files, totals, a
bounded preview). One per turn (Codex `turn/diff/updated`, OpenCode
`session.diff`).

**compaction**: the context was compacted. `running` while it happens.

### 1.2 Lifecycle

```
pending ──► running ──► completed
                   ├──► failed        (tool error, non-zero exit)
                   └──► interrupted   (turn stopped or the process died)
```

- `pending` exists for tools whose call is known before they run (Claude streams
  the tool_use block before execution). Most items start as `running`.
- Terminal statuses are final. An item never goes from a terminal status back to
  running.
- When a turn ends, every item still pending or running is settled: by its own
  `done` op, or with `interrupted` when the turn was stopped. Clients never see
  a spinner left over from a finished turn.
- A stop (`POST /stop`, a Janitor cancel) or a runtime restart that kills a
  turn always ends it on the live channel: running items `done` with
  `interrupted`, a `turn` op with `status: interrupted`, `ended_at_ms` and
  `worked_ms`, and a `status` op with `state: interrupted`. This holds even
  when the hub never saw the turn (a restarted runtime): it settles it from
  the turn record. Recorded stream: `contract/live/stopped-mid-tool.json`.
- A turn whose provider process dies or is killed without a stop (a crash,
  exit 137) ends with `status: failed` (running items `interrupted`, the
  status line `interrupted`). Recorded: `contract/live/killed-without-stop.json`.
- A new provider turn (a new prompt, a queued prompt starting, a preempting
  prompt) always opens with its own `turn` op. When the previous turn never
  settled (it was preempted), the Host ends it `interrupted` first, in the
  same order clients see. Recorded:
  `contract/live/new-prompt-over-unsettled-turn.json`.
- Item ops always belong to the turn that produced them (`turn_id` on the
  item). Output a killed process flushes after its turn ended is dropped,
  never attached to the newer turn. Steering a running turn keeps it the same
  turn.

### 1.3 Activity (status line)

One record per conversation, replaced whole by every `status` op and also returned
in `GET /live` and on `/agents/snapshot` agents as `live_activity`:

```jsonc
"activity": {
  "state": "tool",              // see table
  "tool": {"name": "Bash", "call_id": "toolu_01…", "label": "npm test",
           "item_id": "cl:toolu_01…", "started_at_ms": 1759480000000},  // null unless state=tool
  "running_tools": 1,           // tools running now (parallel calls); show "+N" when > 1
  "headline": "Running npm test",
  "item_id": "cl:toolu_01…",    // the item the status line refers to (scroll/pulse target)
  "since_ms": 1759480000000,    // when this state began
  "turn_id": "tr-…",
  "turn_started_ms": 1759479990000   // null when idle
}
```

| `state` | meaning | busy |
|---|---|---|
| `idle` | no turn running | no |
| `thinking` | the model is working without visible output (or reasoning) | yes |
| `responding` | assistant text is streaming | yes |
| `tool` | a tool is running; `tool` names it with its start time | yes |
| `compacting` | context compaction | yes |
| `waiting` | the agent needs the user (a provider notification) | no |
| `interrupted` | the last turn was stopped or died | no |
| `background` | the turn ended but a helper or background job will wake the agent | no |
| `limited` | parked behind a provider usage limit (account recovery) | yes |

State rules on the Host:
- A tool finishing returns the state to `thinking`, not `tool`. With parallel
  tools, the state stays `tool` naming the newest still-running one.
- `responding` while text deltas arrive; a new tool or reasoning block moves on.
- `interrupted` and `idle` are explicit `status` ops at the end of a turn, so a
  client never infers the end from silence.

The existing `agent-activity` event gains the same facts for clients on the
old path: `state` (above), `call_id`, `started_at_ms` (tool start), and
`turn_started_ms`. `agent-state.kind` keeps its old values: `responding` is still
reported there as `thinking` so older clients stay busy.

### 1.4 Turn

```jsonc
"turn": {"turn_id": "tr-…", "status": "running|completed|failed|interrupted",
         "started_at_ms": …, "ended_at_ms": null, "worked_ms": null, "tool_count": 6}
```

`worked_ms = ended_at_ms - started_at_ms` once settled. Render a settled turn's
work behind `Worked for 1m 12s · 6 tools`.

A `failed` turn adds `"error": {"reason": "account_plan", "message": "This
account's plan does not include gpt-6-astra. …", "detail": "<the provider's own
words>"}` (Host contract 54). Show `message` (and `detail` on demand) with the
fold; never render a failed turn as if it had answered. The same text lands in
`/log` as a `system`-origin row under the prompt.

## 2. Identity

Item ids derive from provider ids so a live item, a re-import and a reconnect agree
without text matching.

| provider | message / reasoning | tool |
|---|---|---|
| Claude | `cl:<message.id>:<content block index>` | `cl:<toolu_…>` |
| Codex | `cx:<item.id>` | `cx:<item.id>` (equals the call_id) |
| OpenCode | `oc:<part id>` | `oc:<callID>` |
| Grok, AGY, DeepSeek and providers without ids | `<backend>:<turn_id>:<n>` (n counts items of the turn in order) | the provider's tool call id when it has one, else the same fallback |

Plan and diff items are `<prefix>:<turn_id>:plan` / `:diff`.

Durable rows follow the same rule: the live assistant row is **finalized in place**
(it keeps its id, its `kind` stops being `live`), and the transcript import adopts
that row instead of inserting a twin and deleting the live one. `replace_required`
is reserved for real history rewrites.

## 3. The `live` SSE event

Delivered only to `/events` connections that asked for it (§4). It has **no SSE
`id:`** and is never replayed.

```jsonc
{"type": "live", "agent_id": "…", "session": "rachel", "conv": "<conversation_id>",
 "epoch": "<Host boot id>", "lseq": 4812, "server_now_ms": 1759480000123,
 "ops": [ … ]}
```

- `lseq` is per conversation and increases by exactly 1 per event.
- `epoch` changes when the Host restarts; `lseq` restarts with it.
- Every op carries `conv`, the item `id` and `kind` (except `status` and `turn`,
  which are per conversation).

Ops:

```jsonc
// create or patch an item. Fields absent stay unchanged, null clears, objects merge, other values replace.
{"op": "upsert", "conv": "…", "id": "cx:call_9", "kind": "tool", "rev": 1,
 "item": {…full item on first sight, changed fields after…}}

// grow a field without resending it
{"op": "append", "conv": "…", "id": "cl:msg_01:2", "kind": "message", "rev": 7,
 "field": "text", "chunk": " more words"}
{"op": "append", "conv": "…", "id": "cx:call_9", "kind": "tool", "rev": 4,
 "field": "tool.output", "lines": ["PASS src/a.test.ts"], "total_lines": 813}

// settle an item
{"op": "done", "conv": "…", "id": "cx:call_9", "kind": "tool", "rev": 5,
 "status": "completed", "started_at_ms": …, "ended_at_ms": …,
 "item": {"tool": {"output": {"exit_code": 0}}}}     // optional final patch

// replace the status line
{"op": "status", "conv": "…", "activity": {…§1.3…}}

// the turn started or settled
{"op": "turn", "conv": "…", "turn": {…§1.4…}}
```

Apply rules (the reference reducer implements exactly these):

1. **Epoch.** An event whose `epoch` differs from the one you hold: stop applying
   events for that conversation, fetch `GET /live` and replace your live state
   with it (keep showing the old state until it arrives).
2. **Order.** `lseq <= held` → duplicate, ignore. `lseq == held + 1` → apply all
   ops in order, then hold `lseq`. `lseq > held + 1` → gap: fetch `GET /live`.
   With nothing held yet (just opened), fetch `GET /live` first and apply events
   with `lseq` above the snapshot's. While a fetch is outstanding, ignore events
   and do not ask again.
3. **Item revision.** An op whose `rev` is not the held item's `rev + 1` (or not 1
   for an unknown item, except `upsert` which may create with any rev) means you
   missed something: fetch `GET /live`.
4. `append` `text`: concatenate `chunk`. `append` `tool.output`: add `lines` to
   the tail, keep the last 50, set `total_lines`, `truncated = total_lines >
   len(tail)`.
5. `done`: set `status`, `started_at_ms`, `ended_at_ms`, then merge `item`.
   `turn` with a `turn_id` different from the held turn starts a new turn: drop
   the previous turn's items from the live state (their durable rows are in
   `/log`).
6. Unknown op names and unknown fields are ignored.

Pacing: the Host coalesces text appends to at most one event per ~100 ms per
conversation and output tails to ~4 per second. Starts, `done`, `status` and `turn`
ops are sent at once. Voice and TTS never wait for this pacing.

## 4. Subscribing

```
GET /events?live=rachel,mike        item ops for those sessions, status/turn ops for all
GET /events?live=*                  item ops for every conversation (desktop dashboards)
GET /events?live=                   status/turn ops only (the agent list)
```

`live` accepts session names or agent ids. Without the parameter a connection
gets no `live` events at all (old clients). For a conversation outside your
list the event carries only its `status` and `turn` ops; its `lseq` still counts
the ops you did not get, so do not track `lseq` (or fetch `/live`) for those: use
them only to update the agent's status line.

The same conversation's events reach every connection with the same `lseq`: a
connection that lists the chat gets every op, one that does not gets the
summary-only copy (status and turn ops) under that same `lseq`. A client with
more than one `/events` connection (or one that changes its `live` list by
reconnecting) must remember which connection carries a chat's item ops and
apply item ops, and check `lseq`, only from that one; a summary copy must
never advance the held `lseq` for a chat it renders items for. Change the set by reconnecting; that
is cheap because `live` is not replayed and the other events resume from
`Last-Event-ID` as before. Subscribe to the open chat and the few cached ones,
not the whole fleet: a 100-agent Host must not push every token to every phone.

## 5. Gap recovery: `GET /live`

```
GET /live?session=rachel
```

```jsonc
{"conv": "<conversation_id>", "session": "rachel", "agent_id": "…",
 "epoch": "…", "lseq": 4812, "server_now_ms": …,
 "activity": {…}, "turn": {…} | null,
 "items": [ …every item of the open turn (or the last turn), full, ordered by ordinal… ],
 "tool_explanations": {"enabled": true, "detail_level": 2}}
```

Replace your live state for that conversation with it, hold its `lseq`, and
continue with events whose `lseq` is greater. 404 for an unknown session.

## 6. Tool explanations on items

A Host setting (feature `tool_explanation_setting`, Host contract 43) decides
whether tool rows carry a plain-language explanation:

```
GET  /tool-explanations/settings  → {"enabled": true, "detail_level": 2}
POST /tool-explanations/settings    {"enabled": false}            (partial update; same response)
```

- `enabled` defaults to **true** (today's behaviour), `detail_level` 0–4 to 2
  (Balanced). It is shown on `/agents/snapshot` as top-level `tool_explanations`
  and in `GET /live`.
- **On**: the Host explains each new tool item and fills `tool.explain` with a
  follow-up `upsert`: `{"text": "Runs the test suite once", "level": 2,
  "status": "ready"}` (`pending` first when it has to wait for the model, `failed`
  when it gives up). Template and learned answers usually arrive in the same
  event as the tool start.
- **Off**: no explainer runs (no model call, nothing queued), `tool.explain` stays
  `null`, and `POST /tool-explanations` answers every item `disabled`.
- Rendering: the tool row's first line is always the deterministic `label` (verb +
  label + elapsed + status). With `explain.text` present it is the secondary line,
  in a reserved one-line slot so the row never changes height when it arrives;
  while it is coming (`explain.status` `pending`, or no text yet on a running
  tool) the slot reads a faint `Explaining…`.
  With the setting off, the secondary line is the raw `tool.command`. Clients
  keep their per-device detail level for the existing `POST /tool-explanations`
  path; items use the Host level.

## 7. What clients must do

1. Check `live_items` in `/server-info`. Without it, use `/log` polling as before.
2. Open `/events?live=<open and cached sessions>`. On opening a chat call
   `GET /live?session=…` once, then apply `live` events by §3.
3. Render the open turn from items, sorted by `ordinal`: message text, a one-line
   reasoning row, tool rows updated in place by id, explore groups as one row, a
   plan card, a diff summary. Keep expand/collapse state keyed by item id.
   Place the turn where it happened: after its prompt (the user row whose
   `trace_id` is the `turn_id`) and every row from before `turn.started_at_ms`,
   before the first other row written after it (a message sent once it
   settled, or queued while it runs); the turn's own rows never move it.
   No item goes above another row written before the item began: a turn
   whose items go on after newer prompts (one the Host failed to settle)
   splits at them, each prompt above what started after it.
4. Status line from `activity`: `● Running npm test · 0:12 +1`, `◌ Thinking:
   <reasoning title>`, `Responding`, `Compacting`, nothing when `idle`. Tick
   elapsed from `tool.started_at_ms` / `turn_started_ms`; never start a timer
   on your own.
5. When a `/log` row arrives whose `id` equals a message item's `row_id`, or whose
   `tools[].id` / `display_cells[].id` equals a tool item's `tool.call_id`, the
   durable row takes over that item's place. Do not delete and re-insert.
6. Settled turn: fold its work behind `Worked for <worked_ms>`; failed or
   interrupted items and every message (commentary included: text written
   before a tool call is a reply to the user) stay visible. The fold survives the takeover: the Host
   lands each tool as its own `/log` row, so while the turn is held keep the
   tool and reasoning rows (one row per tool, inside the fold or, when failed,
   outside it) and do not show those tool rows again as activity; the answer's
   durable row shows in the answer's place.
7. Ignore unknown kinds, ops and fields.

## 8. Live Activity pushes (feature `live_activity_push`, Host contract 45)

The phone's "agents working" Live Activity is kept current by APNs pushes
from the Host, built from the same status the live stream carries.

```
POST   /devices/live-activity  {"token": "<hex>", "activity_id": "act-1", "kind": "agents-working",
                                "environment": "sandbox" | "production"}   -> {"ok": true, "activity_id"}
DELETE /devices/live-activity  {"activity_id": "act-1"} or {"token": "<hex>"} -> {"ok": true, "removed": n}
```

Register again when ActivityKit rotates the token (same `activity_id`
replaces it); drop it when the activity ends on the phone. Tokens APNs reports
dead are dropped by the Host.

Each push: `apns-push-type: liveactivity`, topic `<bundle>.push-type.liveactivity`,

```jsonc
{"aps": {"timestamp": 1759480000, "event": "update", "stale-date": 1759480900,
         "content-state": {
           "working": 2,            // agents thinking, responding, in a tool, compacting or limited
           "needs_you": 4,          // equals the apps' Updates badge, plus waiting agents with nothing filed
           "decisions": 3,          // the badge part: unarchived pending decisions and questions,
                                    // blocking attention artifacts, and Janitor failures
           "lead": ROW | null,      // the working agent whose turn started last
           "agents": [ROW, …],      // at most 5: needing you first, then newest work
           "updated_at_ms": 1759480000123}}}
// ROW = {"agent_id", "session", "persona", "state", "headline", "tool", "started_at_ms", "turn_started_ms"}
```

- `needs_you` counts what the Updates badge counts (unarchived pending decisions
  and questions, artifacts whose attention bucket is `blocking`, Janitor
  failure items) plus agents in `waiting` that have none of those filed, so
  an agent is never counted twice.
- `persona` is the agent's display name, by the same rule as the app:
  `clarp-memory-expert` and `CLARP-MEMORY-EXPERT` read `Clarp Memory Expert`;
  `ECIT Rachel` stays as it is.
- `headline` is never null. A tool reads like the live rows: `Running npm test`,
  `Editing AppModel.swift`, `Reading parser.py`, `Searching tokenize`
  (`Listing`, `Writing`, `Fetching`, `Planning`, `Delegating`, `Calling`), with
  file paths shortened to the file name. Otherwise it is the state's word:
  `Thinking` (or the reasoning headline), `Responding`, `Compacting`,
  `Waiting for you`, `Waiting for the usage limit`, `Interrupted`,
  `Background work`.
- `started_at_ms` is the running tool's start, else when the state began.

Use `started_at_ms` (the tool's start, else the state's) or `turn_started_ms`
with a system timer. The Host never sends `end`: when nothing works it sends
`working: 0`, and the app decides whether to end the activity. Cadence:
routine changes at most every 15 s at priority 5; a rising `needs_you` at once
at priority 10; a turn starting or ending or a new lead at once (≥ 2 s apart).

## 9. Settled turns in `/log` (feature `log_turn_summary`)

So a client can fold a finished turn (`Worked for 1m 12s · 6 tools`) from
history alone:

- Every row a turn writes carries the turn's `trace_id` (= `turn_id`): the
  prompt, commentary, tool rows and the answer. Imports backfill it for Claude,
  Codex, OpenCode and AGY from the prompt the rows answer, else by time from
  the Host's turn record. Rows no known turn wrote keep `trace_id: ""`.
- Assistant text rows carry `phase`: `commentary` when tool work follows it in
  the same turn, else `final` (Codex reports its own phase). Other rows have
  `phase: null`.
- Once the turn has ended, its last row carries `turn`, shaped like
  `GET /live`'s: `{turn_id, status: completed|failed|interrupted,
  started_at_ms, ended_at_ms, worked_ms, tool_count}`. A running turn has no
  `turn` on any row. The summary moves if later rows of the turn are imported.
- Tools (`tools[].status`) and display cells an interrupted turn left
  `running` or `recorded` read `interrupted`.
- All of this bumps row revisions, so a delta (`after_revision`) brings it.
