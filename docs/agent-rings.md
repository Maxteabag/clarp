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
  group call. A classic ring is never upgraded (`live_mode_conflict`). The
  owner's repeat of the original answer request returns the real state: the same
  grant while Live holds (with `join_consumed: true` once the ticket was used; a
  client never opens another socket from it), the classic payload after the
  fallback, `live_call_stale` once retired or for any other answer request.
- **Socket.** `GET /calls/ring/live` (WebSocket). The first frame
  `agent_live.join` presents the grant; all of it is checked and the ticket
  consumed before the upstream opens. Another voice session on the phone is
  refused, never superseded. When the phone is ready (`agent_live.client_ready`)
  and the provider started, the opening is appended once and
  `agent_live.opening_admitted` follows the provider's acknowledgement; no model
  audio reaches the phone before it. Phone audio before it is refused
  (`audio_not_ready`). From admission the Host sends synthetic silence upstream
  (a proposed mechanism from two provider observations) until the first real
  phone frame, hangup, an error or a 15 s budget. An uncertain admission fails
  the call with `live_unavailable`; the opening stays consumed, so no fallback.
- **Close and fallback.** `POST /calls/ring/live/close` and the socket's
  `session.close` retire the call's authority first, then the upstream.
  `POST /calls/ring/live/fallback` switches to the classic call once, only
  while the opening is not yet consumed (no instruction append attempted, so
  never after an uncertain admission) and no user audio went upstream.
- **Deadlines.** A joined call must reach admission (phone ready and provider
  ready) within 15 s, else it fails (`audio_not_ready` or `live_unavailable`);
  pings do not extend it. The provider must acknowledge the opening within 10 s.
- **No tools.** The Live model has no tools, roster, shell or Oracle memory,
  and never reads or changes a global Oracle setting.

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
