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

## Lock-screen privacy

The ring alert says who is calling ("Theo is calling", body "Incoming
call"). The reason shows on the lock screen only when the owner opts in:
`POST /calls/ring/settings {"show_reason": true}`. The app still receives
the reason in the payload.

## Listen-first (feature `agent_ring_listen_first`, contract 67)

The normative contract is the agreed listen-first v5 (§11, then §10, §9, §9.1).
A ring may ask the phone to let the user speak first:

- `POST /calls/ring` takes `opening_mode`: `say` (the default, exactly the
  ring above) or `listen_first`. Any other value is 400 `unknown opening_mode`.
  `opening_line` stays required and is still voiced at ring time: it is the
  line a phone that cannot listen first plays. `clarp-admin call ring
  --listen-first` sends it; without the flag the request is unchanged.
- `POST /calls/ring/answer` takes `opening_mode: "listen_first"` from a phone
  that can. On a listen_first ring it is an ACK only: the call starts, nothing
  is spoken, synthesized, written to history or run, and the answer's
  `opening` is `{mode, ring_id, call_id, token, owner_nonce, epoch: 1,
  state: "pending", silence_ms: 1250, session, persona, server_id, intent:
  null}`. Without `opening_mode` the answer is today's, byte for byte; with any
  other value (or on a say ring) it is today's answer plus `opening.mode:
  "say"`. A listen_first ring answered the legacy way is `fallback`: the line
  plays, nothing is dispatched.
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
