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

Validation: `tests/unit/test_oracle_live.py` and the Oracle cases in
`tests/integration/test_server_di.py` cover the fixed contract, authentication,
real local WebSocket bridge with a fake upstream, audio forwarding and close.
Paid provider experiments remain outside ordinary CI.
