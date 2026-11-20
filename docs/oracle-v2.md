# Oracle v2 beta

The native client offers **Oracle** and **Oracle v2** under Car Mode Settings →
Oracle version. Oracle remains the default. Existing clients and the original
Realtime/WebRTC endpoints are unchanged.

Oracle v2 requires an updated HTTPS Host and its existing OpenAI API key. The
additive `v2` object in `/oracle/status` advertises availability, model and voice.
`/oracle/v2` is a full-device-authenticated WebSocket endpoint. It runs the
Host's configured GPT-Live model (`[openai] live_model`: `gpt-live-1.1` by
default, or `gpt-live-1`; the one `v2.model` advertises), Marin and the
Host-owned Luna router. Client commands are limited to
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

## Standby

Standby is built for the WebRTC call creation (`POST /oracle/v2/calls`) in API
mode only and ships off: `[openai] live_idle_timeout_s = 0`. Set to 5 to 60, it
sends `session.wake_policy {type: speech}` and `session.idle_timeout_s`; the
provider then pauses the session after that many idle seconds and the app gets
`oracle_v2.standby {active: true}` (and `{active: false}` on waking, see
docs/protocol.md). The Host's own idle close is unchanged: 180 s after the last
audible input, output or transcript it closes the session (`oracle_v2.idle`). So
standby pays the standby rate ($0.02 a minute) from the timeout to 180 s, then
the Host closes. No WebSocket path and no agent Live call sends standby.
`oracle_v2.standby` and the usage's `active_seconds`/`paused_seconds` came with
Host contract 110.

Measured on real calls (gpt-live-1.1, wake on speech, idle 20 s; issue 473,
https://github.com/Maxteabag/clarp/issues/473#issuecomment-7043365799):

- Before any speech the session sleeps: nothing is spoken and no event comes.
- The first wake sends no `session.resumed`, so the app gets no
  `oracle_v2.standby {active: false}` for it. It got no `{active: true}` before
  it either, so its state stays right.
- After the idle time `session.paused` arrives. A later wake by speech sends
  `session.resumed`.
- A result the Host appends while the session is paused
  (`session.commentary.append`, `session.instructions.append`) does not wake it,
  is not acknowledged and is not spoken. It is spoken at the user's next speech.
  So with standby on, an agent result is not heard at once: it waits for the
  user to speak.
- The provider refuses standby on a WebSocket `session.start`:
  `invalid_request_error`, code `unknown_parameter`, param `session.wake_policy`
  ("Unknown parameter: 'session.wake_policy'."). Nothing was billed.
- Usage at close reports both parts: 73 s active and 15 s paused, 88 s in all,
  priced by the Host as active and standby time.

Cost and limits of the WebRTC path: the provider bills a WebRTC create for at
least 15 seconds, credited against the duration once the session runs, so a call
shorter than that, or a create whose sideband never attaches, costs 15 s that
the Host's usage estimate does not show. The provider cuts a connected call at
2 hours, and one still ringing at 3 minutes.

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

Putting the user through to an agent is a Host capability, not something the
Host infers from speech. `POST /oracle/connect`, or `clarp-admin oracle
connect <agent|oracle>` (managed skill `clarp-oracle-contact`), hands the
call to always-on hands-free with that agent's own Clarp session and TTS
voice; the agent, or the user opening Oracle again, brings them back. Any
Clarp agent decides for itself when to do it; in direct-to-primary mode the
request simply reaches the primary like any other turn. The wire contract,
with its phases, acknowledgements and fencing, is
[oracle-handoff.md](oracle-handoff.md).

The Host never pattern-matches what the user or Oracle said, and never tells
Oracle what to say. GPT-Live handles turn-taking, interruptions, repetition,
language and tone. Where the Host must tell Oracle a fact (a handoff failed,
the user is back from an agent), it sends one neutral data item and Oracle
phrases it.

### Sound cues (earcons)

The Host plays short cues on the phone (`server/lib/oracle_earcons.py`):
24 kHz mono PCM16 sine tones, 120–400 ms, peaking near −18 dBFS, generated in
Python and cached, no audio files. Every cue is fired by a Host event, never
by text.

| Cue | Sound | When |
|---|---|---|
| `connected` | bright chime pitched per agent | a handoff to that agent was offered |
| `back_to_oracle` | falling two-note chime | the user is back with Oracle after a handoff |
| `switch_failed` | low double tone | a handoff did not happen |
| `handed_off` | very soft tick | work was admitted by an agent |
| `result` | soft bell | an agent's result is about to be relayed |

Each cue goes down as `oracle_v2.cue {name, agent?}` followed by its audio as
a `session.output_audio.delta`, which every client already plays; a future
client can render or replace the cue and drop that audio. A cue waits until
Oracle's audio has been silent for 0.6 s, so it never lands inside a reply,
and is followed by `oracle_v2.quiet` when Oracle is not speaking. `[oracle]
earcons = false` in config.toml turns them off for the Host;
`oracle_v2.preferences {earcons: true|false}` turns them off for one call and
the receipt carries the applied `earcons`. Old clients ignore `oracle_v2.cue`
but do hear the cue.
