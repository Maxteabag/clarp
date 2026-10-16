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

## Opening receipts

Feature `opening_receipts` (Host contract 68). The phone acknowledges the
opening line of a ring it answered on `POST /clips/ack`, with
`clip_id: "ring-opening-<ring_id>"` (or the same value in `opening_id`) and an
optional `opening_attempt` (a logical playback attempt, default 1; transport
retries keep it). Only the device that answered the ring may report it.

| `status` | Receipt class |
|---|---|
| `queued` | attempted |
| `play-start` | started |
| `play-progress` | progress: the first observed media-clock advance (openings only) |
| `play-ok` | terminal_ok |
| `play-fail` | terminal_error (with `error`) |
| `held` | held |

A receipt says what the phone reported, never that anything was audible.
Classes are accepted in any order, because the phone may coalesce pending
acknowledgements. The same ring, attempt and class is recorded once; a repeat
answers `updated: false`. Receipts are voice-timeline rows (`opening_<class>`,
`trace_id` = ring id). A receipt that arrives after the call ended is kept as
history (`historical: true`) and never plays, dispatches or changes the ring or
the call. Refusals: 400 `bad opening_id`, `conflicting clip ids`,
`bad opening_attempt`, `bad status`; 401 limited device; 403 `not your ring`;
404 `unknown opening`; 409 `ring not answered`. Integer clip ids and every
other string keep their old behaviour.

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
