# User Notification Policy

Clarp has one user-facing notification decision.

A completed turn notifies User if and only if the turn contains deliberate
user-facing content on a user-facing channel. user-facing content is either
`<speak>...</speak>` voice content or visible plain assistant reply text after
private team blocks, no-op markers, and internal/tool-only output are removed.
When the decision is true, app badge and chat unread fire together. Push is
normally coupled to the same decision, except for the user's per-agent mute setting.

Audio playback is deliberately separate. Muting the phone suppresses TTS
playback only; it does not suppress a notification decision for a completed
turn that contains `<speak>`.

Per-agent mute is also deliberately narrow: it suppresses only the APNs push.
The same `<speak>` turn still emits the `user-notification` event and still
increments per-chat unread, the chat-tab total, and the app-icon badge.

A message sent from the desktop app is the other narrow exception. The
desktop names itself on `/send` (`"client": "desktop"`, kept on the prompt
admission), and every completed turn whose causing user row came from it
gets `push=0` with reason `speak-desktop` or `text-reply-desktop`; badge and
unread stay as they are. The owner is at the desk, so the reply belongs on the
desktop, even after the desktop's presence lease
(docs/desktop-notification-presence.md) has lapsed. The next message from the
phone pushes as usual. Mute wins and keeps its `-muted` reason.

## Server Source Of Truth

The server classifies each `DONE` state into a durable `user_notifications`
row and emits a `user-notification` SSE only for rows where `notify=1`.
APNs also consumes that same row and sends only when `push=1`. Clients do not
re-derive notification policy from raw transcript updates, `last_turn_end`,
audio clips, or state transitions.

The classifier uses the durable user row that caused the turn, not a streamed
live assistant row, because a fast backend can stream assistant text before the
dispatch path finishes updating live provenance.

## User-Facing Boundary

`<speak>` is honored only for these origins:

- `user`: the user asked the agent something and the reply is for the user.
- `leader_tick`: the leader deliberately emits a proactive user-facing update.

`<speak>` is ignored for these origins:

- `agent`
- `schedule`
- `automation`
- `heartbeat`
- `dreaming`

Those channels are worker, automation, or team-system traffic. They may remain
visible as transcript/activity rows, but they must not page or badge the user.

## Notes To The User

A turn another agent's message started never pages the user (`agent-chatter`
above), and the user may keep its rows out of the chat. An agent that has
something for the user in such a turn says so with a note (`clarp-admin
tell-user`, `POST /agent-notes`): a row of its own chat that is always shown.
The note is classified when it is written, not with its turn: `notify=1` with
reason `agent-note`, badge and unread, and a push under the same rules as a
reply (per-agent mute and the helper rule give `agent-note-muted` with
`push=0`; a quiet agent's note is suppressed as `janitor-maintenance`). The
turn's own completion never takes a note as its content. A note is not spoken.

## Quiet Agents

Janitors are quiet agents (`server/lib/quiet_agents.py`, the one predicate
every path below consults; snapshot rows carry it as `quiet`). A quiet agent's
work never reaches the owner by itself:

- its completed turns are classified `notify=0` with reason
  `janitor-maintenance`, so no push, badge or unread;
- its decisions never push (`apnsDecisionSuppressed reason=quiet-agent`) and,
  with its artifacts, stay out of `GET /attention`, `GET /attention/inbox` and
  the Updates badge;
- it is not a row in the "agents working" Live Activity;
- clients leave it out of unread counts, the attention queue (desktop Ctrl+J,
  `waiting` included) and working summaries.

Its chat stays readable when opened directly. `janitor_failure` items
(`docs/janitor-attention.md`) are the deliberate exception: they are how a
broken Janitor surfaces, and they keep their own quiet path into Updates.

An ordinary agent that is only *named* a janitor (an agent driven by an
`agent_schedules` cron) is not quiet. Making it quiet means converting it to a
Janitor and moving its cron to a `schedule@1` attachment (a Janitor admits only
Janitor runs, so the old schedule would be refused); muting it stops only the
push.

## Push Urgency

A push that fires is not automatically the loud, interrupting kind. Two tiers,
decided by whether the user has something to answer, not by the reply's own
wording:

- **Ordinary reply** (`needs_response=False`): banners, badges and threads
  normally, but arrives with no sound and no vibration, and at the "active"
  interruption level, so it respects Focus and Do Not Disturb like any other
  chat message.
- **Needs a response** (`needs_response=True`): plays the system alert sound
  and uses "time-sensitive", which breaks through Focus. This fires for a
  newly created decision or question (`decision_payload`, always true — a
  decision is by definition something to answer) and for a turn-done push
  whose agent still holds any pending decision when the turn completes
  (`user_notifications.classify_completed_turn` calls
  `artifacts.has_pending_decision`, live at classification time, not frozen in
  the stored row).

`decision_payload` separately narrows interruption-level with
`decision_needs_interruption` (`blocks_progress` or `urgency=="time_sensitive"`
only), so a non-blocking question still gets the sound but does not break
through Focus. Both fields ride the same `aps` payload built by
`apns.turn_done_payload`.

## Client Contract

Native and web clients treat `user-notification` as the unread/badge event.
`transcript-updated` only means "refresh this transcript/cache." It is not a
notification-policy event.

Legacy transcript unread heuristics may remain only as local fallback helpers;
the live path is the server decision.

## Notifications per event (contract 227)

The Rust Host lets the owner choose what notifies them, event by event and per
agent (feature `notification_events`). The one list of events, their defaults,
sounds, interruption levels and the English of every key is
`server-rs/crates/clarp-kernel/src/notify_events.rs`.

The events:

- `reply` (default on): a turn ended with a reply for the user, the decision
  above.
- `needs_you` (on, with sound): a request was made, or a reply came while a
  request of the agent waits.
- `failed` (on): a turn failed of a kind in `failures::NOTIFY` (or its process
  went away under it), and the Host has stopped trying. Told once per turn,
  under `failed-<id of the turn>`. Not for a Stop, a helper, a quiet agent, or a
  turn that was talk between agents or routine. The chat is marked unread.
- `update` (on): an agent's note.
- `job_done` (off): an agent's own job succeeded or failed (not a `sub-agent`
  mirror, not cancelled, not of a helper, a quiet agent or a bound agent).
- `helper_report` (off): a helper reported to its maker. Told in the maker's
  chat.
- `call` (on): a ring. Its global choice is the ring switch.
- `account` (on): the Quota keeper's and the Hotseat's notices.

Who decides: an agent's override, then the global choice, then the default. A
muted agent (and a helper) pushes nothing; mute does not hold rings, which have
their own switch. Off means no push and `alert: false` on the event; badge and
unread are unchanged. The Host applies the choice to every push itself, so it
works with apps that know nothing of it. `job_done` and `helper_report` are
published only when on, so an older app never shows them.

Settings are kept in the Host's settings under `notify.events`, except the
global `call` choice, which is `rings.enabled`.

### What the review of 28 Nov settled

- An agent bound to a model provider of its own sends none of its words to a push service.
  Its failed turn's push says only `Open Clarp to see why.` (`notify.body.failed_withheld`, no
  arguments), with the codes and ids (`error_code`, `error_kind`, `turn_id`,
  `primary_action`) but no sentence, folder or provider message. Its request's push says
  `<name> asks` / `Open Clarp to answer.` with no `options`, no `response_type` and no
  answering category: a tap opens the card.
- A chat read and the end of a request are different pushes. A chat read
  (`read-<session>`) advances the read-through boundary. A
  request's end (`read-decision-<decision_id>`) is sent once for that request, beside any
  other card's, and is no reading of the chat.
- A request held for the desk is pushed after three minutes only if its chat was not opened
  or read, it is still pending at the same revision, and it has not run out. Only a request
  the desk actually held back is re-offered.
- A call switched off for an agent holds back a ring that begins to ring. What became of a
  ring that was pushed (answered, declined, missed, cancelled) still follows it.
- Every route that changes the choices (`POST /notification-settings`,
  `POST /calls/ring/settings` with `enabled`, `POST /agent-mute`) goes one change at a time,
  each with a version of its own. A change of the ring switch together with other choices is
  whole or not at all: the ring switch is set first and set back if the rest cannot be kept.
- A failed turn whose alert is off (switched off, or a muted agent's) is told as
  `chat-unread` (the same fields, `alert: false`), not as `user-notification`: an app from
  before contract 227 pops up every `user-notification`. `job_done` and `helper_report` are
  published only when on. A `reply` or `update` whose alert is off is still a
  `user-notification` with `alert: false`, because every app marks the chat unread from it.
  A desktop from before the `alert` gate pops it up anyway; so the desktop with that gate is
  released before the Host feature is switched on.

### Settings document

`GET /notification-settings`, the answer to `POST /notification-settings`, and
the SSE `notification-settings-changed` (with `"type"` added):

```json
{
  "events": [
    {"id": "reply", "message_key": "notify.event.reply", "default": true, "on": true},
    {"id": "needs_you", "message_key": "notify.event.needs_you", "default": true, "on": true},
    {"id": "failed", "message_key": "notify.event.failed", "default": true, "on": false},
    {"id": "update", "message_key": "notify.event.update", "default": true, "on": true},
    {"id": "job_done", "message_key": "notify.event.job_done", "default": false, "on": false},
    {"id": "helper_report", "message_key": "notify.event.helper_report", "default": false, "on": false},
    {"id": "call", "message_key": "notify.event.call", "default": true, "on": true},
    {"id": "account", "message_key": "notify.event.account", "default": true, "on": true}
  ],
  "agents": {
    "a1": {"session": "theo", "muted": false, "events": {"job_done": true}},
    "a2": {"session": "ola", "muted": true, "events": {}}
  },
  "version": 3
}
```

`agents` lists the agents that are muted or have an override. `version` grows
with every change through this route.

`POST /notification-settings` takes `{"events": {"failed": false}}` (every
agent; booleans only) or `{"agent": "theo", "events": {"job_done": true,
"reply": null}}` (one agent by session or id; `null` follows the global choice).
Refusals (nothing is changed): 400 `bad json`, 400 `events_required`, 400
`unknown_event`, 400 `invalid_choice`, 400 `invalid_agent`, 404
`unknown_agent`, with `{"error": <code>, "message": <words>}`. The owner's apps
and the agents' tools may change it.

### `user-notification` (SSE)

Every kind carries `event` and `alert`; `reply`, `needs_you` and `update` also
`message_id` (the row a tap opens, the same as `source_message_id`):

```json
{"type": "user-notification", "event": "reply", "alert": true, "notification_id": "pn-…",
 "agent_id": "a1", "session": "vera", "persona": "Vera", "origin": "user", "reason": "text-reply",
 "cause_message_id": "m-8", "source_message_id": "m-9", "message_id": "m-9",
 "preview": "The build is green.", "done_ts": 1795010000000, "badge": true, "unread": true,
 "push": true, "muted": false}
```

A failed turn:

```json
{"type": "user-notification", "event": "failed", "alert": true, "notification_id": "failed-…",
 "agent_id": "a1", "session": "vera", "persona": "Vera", "origin": "user",
 "reason": "failed:usage_limit", "error_kind": "usage_limit", "error_code": "usage_limit",
 "turn_id": 41, "cause_message_id": "m-8", "source_message_id": "", "title": "Vera couldn't finish",
 "preview": "Your Codex account is out of usage until 14:00. Try again then, or switch model.",
 "time_ms": 1795017600000, "primary_action": {"id": "retry_at", "at_ms": 1795017600000},
 "title_loc_key": "notify.body.failed_title", "title_loc_args": ["Vera"],
 "body_loc_key": "error.usage_limit_until", "body_loc_args": ["Codex", 1795017600000],
 "done_ts": 1795010000000, "badge": true, "unread": true, "push": true, "muted": false}
```

A job (`job_status` `succeeded` or `failed`; `notification_id` is
`job-<job id>-<run>-<status>`) and a helper's report:

```json
{"type": "user-notification", "event": "job_done", "alert": true, "notification_id": "job-j1-2-succeeded",
 "agent_id": "a1", "session": "theo", "persona": "Theo", "origin": "job", "reason": "job:succeeded",
 "job_id": "j1", "job_status": "succeeded", "cause_message_id": "", "source_message_id": "",
 "preview": "Finished: Nightly build", "body_loc_key": "notify.body.job_succeeded",
 "body_loc_args": ["Nightly build"], "done_ts": 1795010000000, "badge": false, "unread": false,
 "push": true, "muted": false}
{"type": "user-notification", "event": "helper_report", "alert": true,
 "notification_id": "helper-a7-1795010000000", "agent_id": "a1", "session": "theo", "persona": "Theo",
 "origin": "agent", "reason": "helper_report", "helper_agent_id": "a7", "helper_session": "theo-h1",
 "cause_message_id": "", "source_message_id": "", "preview": "Scout reported back",
 "body_loc_key": "notify.body.helper_reported", "body_loc_args": ["Scout"],
 "done_ts": 1795010000000, "badge": false, "unread": false, "push": true, "muted": false}
```

A request made (event `needs_you`), published when the owner wants questions
from that agent (not muted, not quiet). It carries what the push carries to
answer it from a button: `decision_id`, `response_type`, `revision`, and
`options` where the push offers buttons (an approval's `accepted` and
`rejected` with the card's labels, or a choice of two to four):

```json
{"type": "user-notification", "event": "needs_you", "alert": true, "notification_id": "decision-d1",
 "agent_id": "a1", "session": "vera", "persona": "Vera", "origin": "decision", "reason": "decision",
 "decision_id": "d1", "artifact_id": "art1", "response_type": "single_choice", "revision": 1,
 "options": [{"id": "a", "label": "Ship"}, {"id": "b", "label": "Wait"}, {"id": "c", "label": "Drop"}],
 "title": "Release?",
 "preview": "Which way?", "cause_message_id": "", "source_message_id": "", "done_ts": 1795010000000,
 "badge": false, "unread": false, "push": true, "muted": false}
```

### At the desk

While a desk is present the phones get no pushes, as before, with one
exception. A request goes to the desk at once (the event above). If it is still
pending three minutes later and its chat was not opened or read on any device in
the meantime (`agent-focus`, `chat-read`), the phones get it after all, desk or
no desk. The wait is a timer on the Host's runtime, not a poll. It does not
outlive a Host restart: a request held across one stays at the desk.

`chat-read`: `{"type": "chat-read", "session": "vera", "agent_id": "a1", "at_ms": 1795010000000}`.

### Push payloads

What a push carries beyond what it always did:

- `event`, `agent_id`, and `message_id` where there is a row.
- `aps.relevance-score`: `needs_you` and `call` 1.0, `failed` 0.8, `reply` 0.5,
  the others 0.3.
- `title_loc_key`, `title_loc_args`, `body_loc_key`, `body_loc_args` at the top
  level, only on texts the Host made (the empty-reply body, "X answered" and
  "X asks" of a bound agent, call titles, failures, jobs, helper reports). The
  English stays in `aps.alert`. The keys are not put in `aps.alert` (`loc-key`),
  because an app without the key would show the key itself; the app's
  extension translates from the top-level keys. Arguments are `{1}`, `{2}` in
  the English templates.
- `aps.category`: `agent-turn` for `reply`, `needs_you` after a reply, and
  `update`; `turn-failed` for `failed`; for a request `decision-approval`,
  `decision-choice-<n>` (a single choice of 2 to 4 options), or `input-request`
  (text). A choice of more than four has no category. Rings keep `agent-call`.
- For a request: `revision` (always, so a button can resolve it with
  `expected_revision` without reading the card), `response_type`, and `options`
  `[{id, label}]`: the choices with a `decision-choice-<n>` category, or with
  `decision-approval` the yes and no, `[{"id": "accepted", "label": <yes_label>},
  {"id": "rejected", "label": <no_label>}]`, the card's words or `Yes` and `No`
  where it gave none. Every app titles its buttons with these labels. A bound
  agent's request sends no choices, and its yes and no are `Yes` and `No`.
- Reply and note pushes use the existing English preview on this Host. A bound
  agent's push says only that the agent answered (`notify.body.answered`).
- `failed` is the failure as every app draws it (`docs/turn-errors.md`,
  `turn_error::explained`): `body_loc_key` is `error.key` and `body_loc_args`
  the values of `turn_error::placeholders(key)` from `error.params` (`time` as
  epoch ms), the English body is `error.text.en`, and it adds `error_code`,
  `time_ms` where a time is told, and `primary_action` (`{id}`, with `at_ms` for
  `retry_at`; never the text to copy). `message_id` is the user's message a
  retry sends again (`POST /turn-retry`), absent where there is none. It also
  adds `error_kind` and `turn_id`; `job_done` adds `job_id` and
  `job_status`; `helper_report` adds `helper_agent_id` and `helper_session`.

A reply (APNs):

```json
{"aps": {"alert": {"title": "Vera", "body": "The build is green."}, "content-available": 1,
         "interruption-level": "active", "mutable-content": 1, "thread-id": "vera",
         "relevance-score": 0.5, "category": "agent-turn"},
 "kind": "user-notification", "session": "vera", "persona": "Vera", "needs_response": false,
 "notification_id": "pn-…", "server_instance_id": "host-1", "avatar_url": "https://…",
 "event": "reply", "message_id": "m-9", "agent_id": "a1"}
```

A request with three choices (APNs):

```json
{"aps": {"alert": {"title": "Vera", "body": "Which way?", "subtitle": "Release?"},
         "content-available": 1, "interruption-level": "active", "mutable-content": 1,
         "thread-id": "vera", "sound": "default", "relevance-score": 1.0,
         "category": "decision-choice-3"},
 "kind": "user-notification", "session": "vera", "persona": "Vera", "needs_response": true,
 "notification_id": "decision-d1", "server_instance_id": "host-1", "event": "needs_you",
 "agent_id": "a1", "reason": "decision", "response_type": "single_choice",
 "options": [{"id": "a", "label": "Ship"}, {"id": "b", "label": "Wait"}, {"id": "c", "label": "Drop"}],
 "revision": 1, "decision_id": "d1", "artifact_id": "art1"}
```

An approval (APNs):

```json
{"aps": {"alert": {"title": "Vera", "body": "Merge the release branch?", "subtitle": "Release"},
         "content-available": 1, "interruption-level": "active", "mutable-content": 1,
         "thread-id": "vera", "sound": "default", "relevance-score": 1.0,
         "category": "decision-approval"},
 "kind": "user-notification", "session": "vera", "persona": "Vera", "needs_response": true,
 "notification_id": "decision-d2", "server_instance_id": "host-1", "event": "needs_you",
 "agent_id": "a1", "reason": "decision", "response_type": "approval",
 "options": [{"id": "accepted", "label": "Merge"}, {"id": "rejected", "label": "No"}],
 "revision": 1, "decision_id": "d2", "artifact_id": "art2"}
```

A failed turn (APNs):

```json
{"aps": {"alert": {"title": "Vera couldn't finish",
                   "body": "Your Codex account is out of usage until 14:00. Try again then, or switch model."},
         "content-available": 1, "interruption-level": "active", "mutable-content": 1,
         "thread-id": "vera", "relevance-score": 0.8, "category": "turn-failed"},
 "kind": "user-notification", "session": "vera", "persona": "Vera", "needs_response": false,
 "notification_id": "failed-…", "server_instance_id": "host-1", "event": "failed", "agent_id": "a1",
 "message_id": "m-8", "title_loc_key": "notify.body.failed_title", "title_loc_args": ["Vera"],
 "body_loc_key": "error.usage_limit_until", "body_loc_args": ["Codex", 1795017600000],
 "error_kind": "usage_limit", "error_code": "usage_limit", "turn_id": 41,
 "time_ms": 1795017600000, "primary_action": {"id": "retry_at", "at_ms": 1795017600000}}
```

On Android every top-level field goes into the FCM data as a string (lists and
objects as compact JSON), with `title`, `body`, `subtitle`, `category`,
`thread_id`, `interruption_level` and `sound` lifted from `aps`, plus `badge`
and `unread` for turns, `collapse_id`, `sent_at` and `payload_version`:

```json
{"payload_version": "1", "title": "Vera couldn't finish",
 "body": "Your Codex account is out of usage until 14:00. Try again then, or switch model.",
 "interruption_level": "active", "category": "turn-failed", "thread_id": "vera",
 "kind": "user-notification", "session": "vera", "persona": "Vera", "needs_response": "false",
 "notification_id": "failed-…", "server_instance_id": "host-1", "event": "failed",
 "agent_id": "a1", "message_id": "m-8", "title_loc_key": "notify.body.failed_title",
 "title_loc_args": "[\"Vera\"]", "body_loc_key": "error.usage_limit_until",
 "body_loc_args": "[\"Codex\",1795017600000]", "error_kind": "usage_limit",
 "error_code": "usage_limit", "turn_id": "41", "time_ms": "1795017600000",
 "primary_action": "{\"at_ms\":1795017600000,\"id\":\"retry_at\"}", "badge": "true", "unread": "true",
 "collapse_id": "failed-…", "sent_at": "1795010000000"}
```

A ring adds `event: "call"`, `aps.relevance-score`, `title_loc_key`
(`notify.body.call_ringing`, `_missed`, `_declined`, `_answered`,
`_cancelled`) with the agent's name, and `body_loc_key`
`notify.body.call_incoming` where the reason is not shown.

### The silent read push

After `POST /chat-read`, a chat opened on a device (`agent-focus`), or a request
answered, dismissed or withdrawn, every phone gets:

```json
{"aps": {"content-available": 1}, "kind": "read", "session": "vera",
 "server_instance_id": "host-1", "decision_id": "d1"}
```

`decision_id` only for a request. APNs: push type `background`, priority 5,
collapse id `read-<session>` for a chat read and `read-decision-<decision_id>` for a request's end. FCM: the same as string data, priority NORMAL.

For a chat read, compare `(read_through_ms, read_through_seq)` with the displayed
notification's `(event_at_ms, event_seq)` as integer pairs in lexicographic order.
Scope comparisons to the same Host instance, session and opaque `order_epoch` (one per Host
process lifetime). The Host samples its clock and allocates the sequence together. During a
backward clock step it retains the last millisecond and advances the sequence. APNs and SSE use JSON integers. FCM uses decimal strings. Record the
maximum boundary. Remove a displayed notification or suppress a late alert only when its
order is at or below that boundary. Preserve it if either order is missing or malformed or
the epochs differ. Use the sequence to break ties within a millisecond. Thus reading A,
displaying B and then receiving A's delayed read leaves B visible. A read with `decision_id`
clears only the matching card and leaves the chat boundary unchanged.
