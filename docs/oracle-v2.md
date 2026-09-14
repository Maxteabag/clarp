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
