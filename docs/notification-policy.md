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
