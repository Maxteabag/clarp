# Oracle v2 beta

The native client offers **Oracle** and **Oracle v2** under Car Mode Settings →
Oracle version. Oracle remains the default. Existing clients and the original
Realtime/WebRTC endpoints are unchanged.

Oracle v2 requires an updated HTTPS Host and its existing OpenAI API key. The
additive `v2` object in `/oracle/status` advertises availability, model and voice.
`/oracle/v2` is a full-device-authenticated WebSocket endpoint. It pins
`gpt-live-1`, Marin and the Host-owned Luna router. Client commands are limited to
bounded PCM16 audio, interruption and close; the client cannot change prompts,
models, tools or inject arbitrary results.

The Host groups actual input/output transcript fragments into readable context,
checks for newer user speech before applying a backend answer, and dispatches
through existing owner-scoped Oracle work records. Only work admitted by this
voice session is automatically considered for announcements. Stop ends voice,
not already admitted agent work. Explicit cancellation uses the existing agent
stop path. Keys remain on the Host.

The new native audio path uses the existing audio capture/playback adapter.
`oracle_v2.quiet` describes a local silent-output interval; it is not a provider
turn boundary, completed task, or proof that a person heard a result. The native
client waits for its playback queue to drain before changing its speaking state.
No durable delivery acknowledgement is manufactured from that event.

This is an explicit comparison beta. Lab runs found ambiguous identifiers,
unwanted narration and a remaining window in which a correction can arrive
before backend delegation and an older finding can be announced. The conservative
input/output quiet gate reduces timing pressure but is not a proven complete
solution. Keep the original Oracle available while collecting native-device
feedback. Provider/controlled-audio tests are separate from microphone,
Bluetooth, vehicle and physical playback verification.

## Connection lifecycle

The phone owns the session's lifetime; the Host owns one session per device
credential. Both sides are built so that a dropped socket is a short gap, not a
dead session that needs a tap:

- **Reconnect is automatic.** When the socket fails, the Host reports the
  upstream gone, or the upstream session ends, the native client reconnects
  with backoff (0.5 s doubling to 10 s) and keeps the microphone running. Only
  conditions a person must change end the session: credentials (401/403),
  Oracle not configured on the Host (503), microphone permission, or Stop.
- **Same-device reconnects supersede a stale session.** `/oracle/v2` claims
  ownership with a per-session token. A second connection from the same device
  while a live v2 session still holds the claim means the first is stale (a
  dead cellular path, a suspended app), so the Host tells that session to stop
  and waits up to three seconds for its release instead of answering 409. A
  release only frees the claim its own token made, so a late teardown can never
  clear a newer session. A classic-proxy session has no stop hook and is left
  alone.
- **Dead clients are noticed in seconds.** After the upgrade the client socket
  has a 30 s timeout on reads and writes. The phone streams microphone audio
  continuously and sends WebSocket pings every 5 s while its audio is paused,
  so a silent client is gone and its claim is released, and a blocked write to
  a dead peer cannot pin the session until TCP gives up.
- **System audio interruptions pause, not stop.** A call, Siri or another
  app's speech pauses the phone's audio engine; the socket stays open and the
  session resumes when the interruption ends. Route changes (Bluetooth
  connect/disconnect, CarPlay) rebuild the audio engine in place.

Output audio is forwarded while it is audible and for a further 350 ms after
the last audible chunk, so pauses inside a reply keep the phone's playback
buffer fed; sustained silence is still dropped and `oracle_v2.quiet` follows
500 ms after the last audible chunk as before. The phone plays through a
jitter buffer with a 160 ms pre-roll (100 ms after an underrun), which is what
turns network jitter into a slightly later start instead of choppy speech.

Validation: `tests/unit/test_oracle_live.py` and the Oracle cases in
`tests/integration/test_server_di.py` cover the fixed contract, authentication,
real local WebSocket bridge with a fake upstream, audio forwarding and close.
Paid provider experiments remain outside ordinary CI.

## Talking to an agent directly

In a `/oracle/v2` call the user can say "put me through to Theo", "let me talk
to Theo directly" or "switch me to Theo". From then on Theo's replies are
spoken word for word in a voice of Theo's own, and substantive turns go
straight to Theo (the direct-contact path aimed at that agent). "Back to
Oracle" or "switch back" returns to Oracle's voice, Marin. GPT-Live's own
voice activity detection and turn-taking stay in charge throughout.

GPT-Live fixes a session's voice when it starts, so a switch is a new upstream
session (`server/lib/oracle_live_stable.py`, `Conversation.swap_upstream`):

1. The switch is recognised by a deterministic pre-check
   (`oracle_voices.switch_request`) that only accepts a turn made of an
   explicit switch phrase, or by the operator router's `switch_contact` tool.
   The turn is every unrouted user fragment since Oracle last spoke: the
   provider splits one utterance at each pause, and in call 7946a1a7 "can you
   put me through to, can you put me through to" and "Marcus" (1.8 s later)
   were two fragments, so only "Marcus" reached the primary. Leading fillers
   ("yeah", "um"), polite wrappers ("can you", "please") and restarted false
   starts are accepted; any other word before the request ("ask Theo to",
   "tell Marcus to") makes it work. The name must be on the roster, which also
   admits looser forms ("switch to Marcus", "get me Marcus"). A turn that
   stops at "put me through to" waits for the name. "Ask Theo to talk to Lena"
   and "put me through to Theo and ask him about the deploy" are work, not
   switches, and route as before. In direct mode a switch request is never
   also sent to the primary; an unknown name is answered by Oracle.
2. Oracle says "Connecting you to Theo." (skipped when narration is off).
   Nothing else is sent to the old session meanwhile. Oracle's instructions
   forbid it to say the switch happened on its own; the Host confirms it.
3. Once it has gone quiet (0.8 s, at most 5 s), the Host opens a new
   GPT-Live session with the agent's voice, the agent's instructions ("You are
   the voice of Theo ... never claim to be Oracle") and the conversation as
   history (`oracle_memory.startup_history`, about 6.5 KB). Only after that
   session has started does it replace the old one, under the send lock.
4. The phone's socket never closes. The retired session's trailing events,
   including its `session.closed`, are dropped, so the phone does not
   reconnect. The receive loop (`pump_upstream`) reads the current upstream
   on every iteration. Each session has its own `provider_session`, which
   keys its transcript fragments.
5. The Host sends `oracle_v2.contact {agent, session, voice}` (`agent` and
   `session` are null back at Oracle). Clients that don't know it ignore it.
6. Relay parts the old voice never finished speaking are resent to the new
   voice, and parts it did finish are not repeated. A part counts as spoken
   when Oracle went quiet after it and the user did not speak over it.

If the new session fails to open, the call stays on the current session. The
Host logs `oracleV2Swap` and tells the current voice to say plainly that it
could not connect the user and that they are still talking to it.

Two guards keep the user from talking to silence, as in call 7946a1a7, where
Oracle said "Sure, put you through to Marcus" by itself and then nothing
happened for 35 s:

- When Oracle's own words claim a switch ("put you through", "connecting
  you", "switching to") and the Host has started none within 2 s, the Host
  tells Oracle to say it could not connect them and that they are still with
  Oracle (`oracleV2SwitchClaimUnbacked`, journal
  `contact.switch_claim_unbacked`).
- When the user has been talking for 8 s with no delegation and no reply and
  has then paused for 4.5 s, the Host nudges the voice once to answer
  (`oracleV2UnansweredNudge`, journal `turn.unanswered_nudge`).

### Sound cues (earcons)

The Host plays short cues on the phone (`server/lib/oracle_earcons.py`):
24 kHz mono PCM16 sine tones, 120–400 ms, peaking near −18 dBFS, generated in
Python and cached, no audio files.

| Cue | Sound | When |
|---|---|---|
| `switch_started` | rising two-note chime | a switch was requested |
| `connected` | bright chime pitched per agent | the agent's voice is on the line |
| `back_to_oracle` | falling two-note chime | the user is back with Oracle |
| `switch_failed` | low double tone | a swap failed, an unbacked switch claim, an unknown name |
| `handed_off` | very soft tick | work was admitted by an agent |
| `result` | soft bell | an agent's result is about to be read out |

Each cue goes down as `oracle_v2.cue {name, agent?}` followed by its audio as
a `session.output_audio.delta`, which every client already plays; a future
client can render or replace the cue and drop that audio. A cue waits until
Oracle's audio has been silent for 0.6 s, so it never lands inside a reply,
and is followed by `oracle_v2.quiet` when Oracle is not speaking. `[oracle]
earcons = false` in config.toml turns them off for the Host;
`oracle_v2.preferences {earcons: true|false}` turns them off for one call and
the receipt carries the applied `earcons`. Old clients ignore `oracle_v2.cue`
but do hear the cue, and their local "say that again" replay can replay a cue
that ended an utterance.

The expected gap is the new session's start time: about 1.0–1.3 s in the
2026-09-26 probe, plus the 0.8 s quiet wait.

### Voices

Every agent has a voice other than Marin, and each voice matches the gender of
the agent's Cartesia voice (checked against the Cartesia catalog on
2026-09-26). Unmapped agents get a deterministic pick, a hash of the persona
name, from their gender's pool, or from both pools when the gender is unknown.

| Agent | Voice | Agent | Voice |
|---|---|---|---|
| Theo | meridian | Lena | gleam |
| Omar | vesper | Nadia | willow |
| Caleb | stone | Priya | quartz |
| Adam | ripple | Yuki | delta |
| Josh | cinder | Domi | delta |
| Sam | beacon | Freya | coral |
| Marcus | cedar | Bella | sage |
| Felix | ash | Elli | shimmer |
| Diego | verse | | |
| Antoni | ballad | | |
| Mike | echo | | |

Pools: masculine meridian, vesper, stone, ripple, cinder, beacon, cedar, ash,
verse, ballad, echo; feminine gleam, willow, quartz, delta, coral, sage,
shimmer. With seven feminine voices and eight feminine personas, Yuki and Domi
share delta. tempo and bossa are left out because they are Portuguese-language
voices.

Override the mapping in `config.toml`. Names are case-insensitive. A value
that is `marin` or not a known voice is logged (`oracleAgentVoiceIgnored`)
and ignored:

```toml
[oracle.agent_voices]
Theo = "ash"
Nadia = "coral"
```

Probe, 2026-09-26, with the Host's OpenAI key: for each voice, one session was
started on `gpt-live-1` with that voice and closed before any audio was sent.

| Voice | Result |
|---|---|
| marin, meridian, vesper, stone, ripple, cinder, beacon, gleam, willow, quartz, delta | `session.started` |
| cedar, ash, coral, sage, verse, ballad, alloy, echo, shimmer (older Realtime voices) | `session.started`, voice echoed back in the session |
| cove (the subscription voice) | `error` forbidden, "Voice session access denied." |
| an invented name | `error` forbidden, the same message |

The invented name is rejected, so a started session means the voice is
accepted, not silently replaced. How the older voices actually sound on
gpt-live-1 was not heard; alloy is left out of the pools because it is
gender-neutral.

### Limits and follow-ups

- Host WebSocket path only. The WebRTC path, where the phone talks to OpenAI
  directly (`OracleRealtimeClient+AppAPI.swift`), cannot switch voices. That
  is a follow-up.
- The subscription (`gpt-live-1-codex`) engine is not covered.
- The contact is not saved across a reconnect. After a dropped socket the
  call comes back as Oracle, with the conversation intact.
- Mid-part accuracy: a part the user spoke over is resent in full. The
  new voice cannot know how far the old one got.
