# Agent rings: an agent calls its owner

Status: **implemented on the Host, contract 64, feature `agent_rings`.**

An agent that needs the user now, by voice, can ring the owner's phone. The
phone shows an incoming call from that agent with the reason. When the user
answers, the agent speaks first, in its own voice ("Camera 2 shows a possible
water leak at pump 01. Shall I find an available field worker?"), and the
conversation carries on as an ordinary group call (docs/group-calls.md):
live, two-way and hands-free.

## Principles

- **A ring is a request; answering is the user's.** The Host never opens a
  microphone or starts audio by itself. It records the ring, alerts the
  phone, and starts a group call only when the phone answers.
- **Reuse the group call.** An answered ring is `POST /calls` with the agent
  as the only participant and the floor. Routing, replies, the call header and
  the call screen are the group call's. The ring adds only its reason and
  opening line to the agent's call header.
- **Speak first without a turn.** The opening line is voiced at ring time with
  the agent's own TTS voice (`ctx.tts.synthesize`), so answering plays it at
  once, with no model turn in between. When synthesis fails the ring still
  rings; the phone then shows the line as text and starts listening.

## Policy

| Rule | Refusal |
|---|---|
| The owner can turn rings off (`clarp-admin call rings off`, `POST /calls/ring/settings {"enabled": false}`). On by default. | 403 `agent calls are turned off` |
| An agent proven by its turn (`X-Clarp-Turn`) rings only as itself. The owner's own tools (no turn proof) name the agent. | 403 `an agent can only ring as itself` |
| Janitors, archived and deleted agents cannot ring. | 403 `this agent cannot call` |
| A muted agent cannot ring. | 403 `this agent is muted` |
| One ring at a time. | 409 `the user is already being called` |
| An agent waits 60 s between rings. | 429 `wait before calling again` with `retry_after_s` |
| At most 10 rings per hour on the Host. | 429 `too many calls this hour` |

Every request, refused or not, every outcome and every switch change is
appended to the audit trail (`GET /calls/ring/settings` → `audit`, newest
first, the latest 200 kept) and logged as `agentRingAudit`. Only the owner's
devices are ever rung: the push goes to this Host's registered phones.

## States

```
ringing ──answer──▶ answered   (a group call starts; call_id is set)
   │ ├──decline──▶ declined
   │ ├──cancel───▶ cancelled   (the agent hung up)
   │ └──45 s─────▶ missed
```

A ring that is overdue becomes `missed` on a timer and also on any read, so a
restart never leaves a ring that rings forever. Every change emits an
`agent-ring` event, pushes the new state under the same collapse id (the
incoming call is replaced by "Missed call from Theo"), and posts a quiet
notice in the agent's chat ("Call: The user declined your call about: …").

## Endpoints

Full-device authentication, as for group calls.

| Method and path | Body | Effect |
|---|---|---|
| `POST /calls/ring` | `{"reason", "opening_line", "agent"?}` | Ring. Reason up to 200 characters, opening line up to 600. Answers `{"ok", "ring", "delivery", "summary"}`. |
| `GET /calls/ring` | `?ring_id=` | `{"ring": record or null, "settings"}`: that ring, or the one ringing now. |
| `POST /calls/ring/answer` | `{"ring_id"}` | Start the group call. Answers `{"ok", "ring", "call", "opening": {"text", "audio_url", "session", "persona"}, "changed"}`. A retried answer from the same device returns the same call; another device gets 409 `the call was answered elsewhere`. |
| `POST /calls/ring/decline` | `{"ring_id"}` | Decline. |
| `POST /calls/ring/cancel` | `{"ring_id"}` | The calling agent hangs up (another agent gets 403). |
| `GET`/`POST /calls/ring/settings` | `{"enabled"}` | The owner switch, with the audit trail. |

A ring that is no longer ringing answers 409 `the call is over` with the
`ring`.

The record:

```json
{"ring_id": "rng_<32 hex>", "session": "theo-97e5", "agent_id": "a-theo",
 "persona": "Theo", "reason": "Possible water leak at pump 01",
 "opening_line": "Camera 2 shows a possible water leak at pump 01. Shall I find an available field worker?",
 "opening_audio_url": "/audio/1760000000000__theo-97e5.mp3", "state": "ringing",
 "created_at": 1760000000000, "expires_at": 1760000045000, "ended_at": null,
 "answered_by": "", "call_id": "", "server_now": 1760000000100}
```

## The push

`kind: "agent-ring"` with `ring_id`, `ring_state`, `session`, `persona`,
`reason`, `opening_line`, `opening_audio_url`, `expires_at` and
`notification_id`. The ring itself is `interruption-level: time-sensitive`
with the default sound and `category: "agent-call"`, whose actions the app
registers: **Answer** (opens the app straight into the call, the opening line
first) and **Decline**. A plain tap answers too. Later states are `active`
(missed) or `passive` alerts without the category.

Only a live ring answers. Tapping a ring that is over (missed, declined,
cancelled, overdue, or answered on another device) opens the agent's chat and
nothing else: declined, expired or dismissed rings never start the microphone
or play audio, and whatever owns audio on the phone keeps it.

The iOS app has no CallKit or PushKit today, and the push relay sends alert
pushes only, so a time-sensitive alert is the most reliable route that works
on every install. CallKit's full-screen ring would need a VoIP token, the
`voip` background mode and VoIP push support in Audio Central; it is a
follow-up, not part of this feature.

## For agents

```bash
clarp-admin call ring --reason "Possible water leak at pump 01" \
  --say "Camera 2 shows a possible water leak at pump 01. Shall I find an available field worker?" --wait
clarp-admin call ring --agent rk-sander --oracle --context-file /abs/event.json --reason … --say …
clarp-admin call ring-status [RING_ID]
clarp-admin call ring-cancel RING_ID
clarp-admin call rings on|off|status      # the owner's switch
```

`--wait` returns when the call is answered, declined or missed. When it is
answered, the user has heard the opening line and their reply arrives as the
next turn, with a `[Group call]` header that says why you called.

## CallKit (VoIP)

A phone that supports CallKit registers its PushKit VoIP token with
`POST /calls/ring/voip-token` `{"token": hex, "environment": "production"|"sandbox"}`
(one token per principal; an empty token forgets it). A live ring is then
pushed as VoIP:

- `apns-push-type: voip`, `apns-topic: <bundle>.voip`, `apns-priority: 10`,
  `apns-expiration` at the ring's `expires_at`, `apns-collapse-id: ring-<id>`.
- Payload: `kind: agent-ring`, `ring_id`, `ring_state: ringing`, `session`,
  `persona`, `reason`, `expires_at`, `server_instance_id`. No alert.
- Signed with the Host's own APNs key (`[apns] key_path`). The Audio Central
  relay carries no VoIP pushes, so a Host without a direct key sends only
  the alert.
- When a VoIP push is delivered the ring alert is skipped (it would be a
  duplicate). Answered, declined, missed and cancelled are never VoIP: iOS
  requires every VoIP push to report a new CallKit call. The phone ends its
  CallKit call itself, from the `agent-ring` event, `GET /calls/ring?ring_id=`
  or `expires_at`.

## Agent-live rings (feature `agent_ring_live`)

A ring may instead be held as a live voice call on OpenAI GPT-Live
(`gpt-live-1`, voice `marin`, public API only; never another model, voice or
provider, never a silent fallback). The wire is WIRE-v2; the compatibility row
lists every field and fixed string.

- **Ring.** `POST /calls/ring` with `conversation_mode: "agent_live_v1"` and the
  producer's `event_context` (CONTEXT-v1: an explicit allowlist with fixed
  bounds; `opening_text` equals `opening_line`). The Host resolves the
  operator-approved profile under `<data>/clarp/agent-live/profiles/<profile_id>/`
  (`profile.json` binds `agent_session`, a `display_name`, the approved
  `stories` and each file's sha256), and freezes profile texts, event and
  opening in a snapshot on the ring. Nothing is trimmed: a ring that cannot be
  built is refused with a fixed `live_*` code. The same event retried with the
  same content gets the same ring back. Nothing is pre-voiced.
- **Delivery.** The VoIP push goes only to phones that registered
  `supported_conversation_modes` with `agent_live_v1`; without one the ring is
  refused (`live_client_required`). GET, SSE and both pushes name
  `conversation_mode`; a classic ring has no such field.
- **Answer.** Only `conversation_mode: "agent_live_v1"` answers a Live ring
  (any other answer, every older app included, is 409 `live_client_required`
  and the ring keeps ringing). It hands back the call grant (`alv_` call id,
  epoch, attempt, owner nonce, one-use join ticket for 60 s) and starts no
  group call. A classic ring is never upgraded (`live_mode_conflict`).
- **Socket.** `GET /calls/ring/live` (WebSocket). The first frame
  `agent_live.join` presents the grant; all of it is checked and the ticket
  consumed before the upstream opens. Another voice session on the phone is
  refused, never superseded. When the phone is ready (`agent_live.client_ready`)
  and the provider started, the opening is appended once and
  `agent_live.opening_admitted` follows the provider's acknowledgement. Until
  then the Host sends silence upstream (the provider takes an append only while
  input audio flows) and refuses phone audio (`audio_not_ready`).
- **Close and fallback.** `POST /calls/ring/live/close` and the socket's
  `session.close` retire the call's authority first, then the upstream.
  `POST /calls/ring/live/fallback` switches to the classic call once, only
  before the opening was admitted and before user audio went upstream.
- **No tools.** The Live model has no tools, roster, shell or Oracle memory,
  and never reads or changes a global Oracle setting.

## Lock-screen privacy

The ring alert says who is calling ("Theo is calling", body "Incoming
call"). The reason shows on the lock screen only when the owner opts in:
`POST /calls/ring/settings {"show_reason": true}`. The app still receives
the reason in the payload.

## Listen-first (feature `agent_ring_listen_first`; contract number booked at landing)

The normative contract is the agreed listen-first v5 (§11, then §10, §9, §9.1).
A ring may ask the phone to let the user speak first:

- `POST /calls/ring` takes `opening_mode`: `say` (the default, exactly the
  ring above) or `listen_first`. On a classic ring any other value is 400
  `unknown opening_mode`. A Live ring (`conversation_mode: agent_live_v1`)
  takes no listen-first opening (its opening is the Live model's): any sent
  value other than `say`, `listen_first` included, is the existing 400
  `live_mode_invalid` and nothing is stored. `opening_line` stays required
  and is still voiced at ring time: it is the line a phone that cannot listen
  first plays. `clarp-admin call ring --listen-first` sends it; without the
  flag the request is unchanged.
- `POST /calls/ring/answer` takes `opening_mode: "listen_first"` from a phone
  that can. On a listen_first ring it is an ACK only: the call starts, nothing
  is spoken, synthesized, written to history or run, and the answer's
  `opening` is `{mode, ring_id, call_id, token, owner_nonce, epoch: 1,
  state: "pending", silence_ms: 1250, session, persona, server_id, intent:
  null}`. Without `opening_mode` the answer is today's, byte for byte; with any
  other value (or on a say ring) it is today's answer plus `opening.mode:
  "say"`. A listen_first ring answered the legacy way is `fallback`: the line
  plays, nothing is dispatched. `opening_mode` is chosen when the ring is
  placed; the answer never renegotiates it. An answer's `opening_mode` only
  picks how a listen_first ring's opening is handed back, and a Live ring's
  answer ignores it (the Live grant, no listen-first opening; no refusal).
- The phone owns timing (readiness, VAD onset, the 1.25 s deadline). It reports
  through `POST /calls/ring/opening {token, epoch, claim, request_id,
  capture_gen, at_ms}` with `claim` one of `listening`, `speech_started`
  (latches the greeting; later silence claims get 409 `speech latched`),
  `speech_abandoned` (`reason`: `empty_capture` or `capture_aborted` only),
  `silence` (needs `at_ms` and `watermark_ms` at or past the deadline),
  `no_mic`, `not_ready`, and `renew` (`expected_epoch`, `owner_nonce`; a
  compare-and-set that bumps the epoch and rotates the nonce, before any
  resolution only). It answers `{ok, opening, changed}`; the same
  `request_id` answers again with `changed: false`.
- The greeting is the phone's ordinary captured-turn `/send` with `call_id`
  and `call_opening: {token, epoch}`. It goes to the calling agent only (no
  selection, no orchestrator) with the header "They answered and spoke first",
  and the response carries `opening`. A retry of the same `client_msg_id` is
  the same greeting, even across a renewal.
- A silence claim (or no_mic, not_ready, speech_abandoned) admits ONE
  initiative turn: prompt admission and queued row `call-open:<ring_id>`,
  origin `automation`, whose whole input is the call header and why the agent
  speaks first. It is never a user row; the reply is the agent's own words.
- Refusals dispatch nothing: 400 `bad claim`, 403 `not the answering device`,
  404 `unknown opening`, 409 `stale epoch`, `owner changed`, `already
  resolved` (+`outcome`), `opening retired` (+`reason`), `wrong call`,
  `speech latched`.

Atomicity. The opening lives in its own settings row per ring
(`agent_ring.opening.<ring_id>`), written only inside SQLite transactions.
The initiative's intent is `intended` (no row yet), `admitted`, `cancelled`
or `started`. The admitter inserts the admission and queued row only while the
intent is still `intended`, in the same transaction; a greeting or speech
onset cancels an intended intent, or withdraws an admitted, unclaimed row
(`status = 'withdrawn'`), in one transaction; the dispatcher claims the row
(`queued` to `claimed`) and starts the intent in one transaction before the
turn runs. Whichever commits first wins; a greeting after the claim is an
ordinary call utterance (`opening.greeting: "ordinary"`). Recovery (Host start
and every read of the ring) admits only an `intended` intent.

Claimed once. The dispatcher claims an initiative for a launch at most once.
Only its own failure before the backend was invoked puts the row back
(`queued`, intent `admitted`), to be claimed once more through the same fence.
Any other way back (a stale-claim reset, a released or retried claim, a claim
left by an earlier Host process, whatever the clock says) cannot tell whether
the turn reached the backend: the opening and its intent become `unknown` and
the row `status = 'unknown'`, never counted as queued, claimed or done and
never run again. It is never listed by `GET /turn-queue` or counted in any
queue depth; editing, deleting or sending it (`PUT`, `DELETE`,
`POST /turn-queue/<id>/send`) is refused with 409 `opening launch unknown`,
and the row stays as the record. The opening's `state: "unknown"` is how a
client sees it. A newer Stop or retire still wins. A row named `call-open:…`
is an initiative only when its prompt admission is the Host's own (`call`
channel, same id and trace); a client's message under such an id stays that
client's ordinary message, and the opening it collides with becomes `unknown`.

Retirement. The call ending or being superseded, the device being revoked, or
another Host instance retires the opening: later claims and greetings get 409
`opening retired`. A committed opening keeps its outcome but loses its output.
A replacement ring gets a new token.

Audio. Clips of an opening turn carry `call_id` and `opening: {ring_id, token,
epoch, kind: "initiative"|"greeting_reply", admission_id}`; ordinary clips are
untagged. The Host skips voicing an initiative clip once its intent is
cancelled or a greeting arrived after it started, and any opening clip once
retired (best effort; the phone's fence decides).

The `agent-ring` event and `GET /calls/ring` carry `opening_mode` and
`opening` (state and epoch, never the token or nonce) for listen_first rings
only; a say ring's event is unchanged.

### Host identity

A listen-first ring and its opening remember the host generation they were
made under: the kernel boot id plus the instance token in
`clarp/backups/instance.generation` (created at process start; rotated only by
the supported restore). Every opening step reads it again, once before and
once inside its transaction, and never creates, rotates or repairs it.

- `POST /calls/ring` with `opening_mode: "listen_first"` while the identity
  cannot be read: 409 `listen-first opening is unavailable: host identity
  unprovable; ring with opening_mode say`. A say ring is unaffected.
- Answering a listen-first ring placed on another boot or instance, or whose
  identity cannot be proven: 409 `the call is over`; the ring ends `missed`
  (the cause stays internal), no call starts and no opening is made. The miss
  is announced like any missed ring: the `agent-ring` event and the replacing
  push are sent, and devices on the event stream dismiss the ring. A push alone
  does not end the original CallKit call (the terminal VoIP push uses another
  call id), and a missed push may leave a notification in the list. The agent
  gets no "did not answer" notice. Replaying an answered ring's opening (its
  token) is refused when the ring or the opening is on another identity, or the
  opening is retired or unknown; the say-shaped answer is unchanged.
- A failure after the call has started (saving, recording the opening, an
  audit write) ends that call once, best effort, and the error is returned. If
  ending it fails, the call can stay up and an opening already recorded stays
  usable until the call ends.
- An opening still in flight on another identity becomes `unknown` and is
  retired with the cause (`host_rebooted`, `db_restored`,
  `host_identity_unprovable`); its queued or claimed row becomes `unknown`,
  never withdrawn, and nothing runs. An opening that already finished is kept
  as stored, and the step is still refused.
- Equal identities mean only "not shown different". A restore applied by an
  older Host (downgrade, restore, upgrade) does not rotate the token, so it is
  not detected; that sequence is unsupported. A deleted token yields a new one
  at the next start, which makes every older opening unknown.

## Follow-ups (after the 23 Oct 2026 demo)

Reply latency on an answered call: measured by rk-integration (Sonnet, low
effort, everything the reply needs pre-loaded, no tool call before speaking),
the agent's first spoken words come about 3.7 s after the user stops talking,
and the full answer in under 10 s. That was accepted for the demo. The floor
is on the Host: about 1 s to start or resume the provider CLI for each turn,
then the first token, then TTS waiting for a finished text block. Two ways
under 3 s, both deliberately not built before the demo:

- **Acknowledgement clip.** At ring time, voice one more short line in the
  agent's own voice (for example "Et øyeblikk."), the same way as the opening
  line, and return it in the answer as `ack_audio_url`. The phone plays it
  once, right after the user's first utterance in an answered-ring call;
  the real reply follows through the normal queue. It hides the wait but
  does not shorten it. Make it an opt-in per ring (`clarp-admin call ring
  --ack "Et øyeblikk."`), off by default. Build it only after a yes from
  Peter or Koko: Peter has banned fillers elsewhere.
- **Warm provider process for the call.** Keep the agent's provider process
  running (stream-json) while its call is live, so each turn skips the
  spawn/resume. A related lever: start TTS on the first finished sentence
  instead of the whole text block. Both are runtime and audio changes in
  Avana's area, to take up with Avana after the demo.

## A phone that switches rings off for itself

`POST /calls/ring/device` `{"receive_rings": false}` switches agent rings off for the calling phone only (feature
`agent_ring_device_optout`; `{"receive_rings": true}` switches them back on; `GET /calls/ring/device` reads the state).
Both answer `{"ok": true, "receive_rings": bool, "alerts_suppressed": bool, "state": "on"|"off"|"partial"}`. Only a
paired, full-scope, non-revoked device sets its own state (403 `ring_device_required` otherwise); any other body is
400 `ring_device_invalid`.

A phone with rings off gets no agent ring of any kind, classic or Live: no VoIP push, no `agent-ring` SSE event, no
ring from `GET /calls/ring`, and no ring alert push on the alert tokens it owns. The ring it answered stays its own.
A Live ring is admitted only for a capable phone that still receives rings (else 409 `live_client_required`).

Alert tokens are owned by the device that registered them: `POST /devices` binds a token to the authenticated paired,
full-scope device (never a body field or the session) and refuses with 409 `device_token_owned_elsewhere` a token that
another device owns. Legacy tokens registered before this are unbound and still get ring alerts, so rings-off reads
`partial` while any live unbound token remains: show it as pending, never as off. A revoked device keeps its
provenance and gets no ring alerts; a re-paired phone with a new device id cannot take the old id's token until an
explicit owner release is designed. Provenance means token possession by an authenticated device, not proof of the
physical phone.


## User-initiated Live calls (contract 75)

`agent_call_live` advertises `POST /calls/live`. Snapshot agents carry
`call_mode: "agent_live_v1"` for an operator opt-in, otherwise `"classic"`.
An unreadable, ambiguous or overflowing profile registry yields `"unavailable"`:
clients must refuse unknown modes and Live without the feature, never silently
start Normal. Discovery does not check or open the provider.

A paired full-scope Live-capable phone posts exactly
`{agent: <raw snapshot agent_id>, request_id, client_nonce}`. The Host requires
one profile bound to the agent session with `user_call_default: "live"`, a
nonempty `user_call_opening`, and pinned `stories.user_call` file keys. It builds
a truthful `user_call` / `user-initiated` context with no event. This returns the
existing answered Live envelope directly; no answer hop, GET discovery, push,
chat notice, incoming CallKit or group call. The public ring has `origin:
"user_call"`; grant fields and gpt-live-1/marin remain unchanged.

The principal's request id binds the raw agent and nonce. Exact replays return
the same grant; conflicts fail before another ring is persisted. Its persisted
join deadline is 60 seconds, checked at the deadline without extending on replay.
An expired unjoined call is retired; a joined call is not subject to that expiry.
Only an authenticated, valid one-use WebSocket join opens the provider. Existing
socket and close controls apply. User calls refuse classic fallback; incoming
Live rings retain their existing explicit fallback. Normal `/calls`, group calls,
and CLI `--oracle --context-file` rings are unchanged. This feature changes no
schema and does not authorize a production profile edit or a real call.
