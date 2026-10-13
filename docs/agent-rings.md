# Agent rings: an agent calls its owner

Status: **implemented on the Host, contract 63, feature `agent_rings`.**

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

The iOS app has no CallKit or PushKit today, and the push relay sends alert
pushes only, so a time-sensitive alert is the most reliable route that works
on every install. CallKit's full-screen ring would need a VoIP token, the
`voip` background mode and VoIP push support in Audio Central.

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
