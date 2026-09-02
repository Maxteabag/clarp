# Oracle Mode and CarPlay architecture

## Product boundary

This change adds two independent, default-off Car Mode settings:

- **Oracle Mode** replaces the existing chained capture/transcribe/agent-TTS
  loop while Car Mode is visible with one OpenAI Realtime speech session.
  Oracle is the only audible voice. It delegates text-only turns to existing
  Clarp agents and speaks their durable results with attribution.
- **CarPlay experience** selects the deliberately reduced, voice-first layout
  on iPhone so it can be compared with the existing favorites cockpit. The
  same state model drives a native `CPVoiceControlTemplate` scene when the
  bundle is signed with Apple's conversational CarPlay entitlement.

Turning either setting off restores the current behavior. Existing Car Mode
routing, recording, durable transcription jobs, audio queues, and agent voice
providers remain untouched and remain the degraded/offline fallback.

## Authority and data flow

```text
iPhone microphone <-> Oracle Realtime WebSocket proxy <-> OpenAI Realtime
                              |
                              +-- function call -> iOS Oracle coordinator
                                                       |
                                                       +-- authenticated Clarp API
                                                               |
                                                               +-- durable text-only agent turn
                                                               +-- durable Oracle delegation row
                                                               +-- durable assistant result
                                                       |
                              <--- attributed result injection --+
```

- The OpenAI API key never leaves the Computer. The iPhone connects to an
  authenticated Clarp WebSocket; the Computer opens the upstream OpenAI
  WebSocket and proxies protocol messages. All `/oracle/*` routes fail closed
  unless a full-device credential was actually validated; legacy auth-disabled
  server mode cannot expose the billable proxy or delegation authority.
- The iPhone probes and starts Oracle only for an HTTPS Computer URL, and the
  audio socket itself must be WSS. Existing HTTP/LAN profiles remain usable by
  ordinary Car Mode but never receive Oracle credentials or microphone PCM.
- Agent turns keep normal Clarp session, Computer, transcript, queue, and
  idempotency authority. Oracle requests `synthesize_audio=false`, preventing
  a second voice from competing with the Realtime session.
- One Realtime session is anchored to one HTTPS Clarp Computer. Its Oracle
  roster, delegation tools, cancellations, and durable result polling are
  intentionally limited to agents on that Computer, so the proxy can verify
  every injected result against its own owner-scoped database. Secure
  cross-Computer result proofs are a separate future protocol, not trusted
  client text in this release.
- A delegation ID is stable across retries. The server stores it before
  dispatch and records the exact assistant result by turn trace. Unacknowledged
  results survive app, network, and server restarts.
- The Realtime tool call returns an acceptance receipt immediately. Long agent
  work never holds a model function call open. When a durable result arrives,
  the iPhone adds an attributed, explicitly untrusted user-data item to the same
  Realtime conversation and asks Oracle to speak it with tools disabled.
- Consequential external actions remain subject to the receiving agent's
  ordinary approval and permission boundaries. Oracle itself receives only
  narrow roster/status/delegate/cancel tools.

## Runtime lifecycle

1. Entering Car Mode does nothing new unless Oracle Mode is enabled.
2. Oracle startup stops existing Car Mode capture and queued playback, obtains
   microphone permission, opens one authenticated Realtime proxy, configures
   semantic turn detection and interruption, and starts full-duplex PCM.
3. User speech interrupts locally buffered Oracle audio immediately as well as
   cancelling the server response through Realtime VAD.
4. Leaving Car Mode, disabling Oracle, an audio interruption, route loss, or a
   terminal WebSocket error stops capture/output and releases the audio session.
5. Pending agent work remains in Clarp. The next Oracle session receives every
   unacknowledged result before accepting new work.

## CarPlay boundary

CarPlay is a separate template scene, not a conversion of the SwiftUI page.
The phone UI remains available and testable without CarPlay. The dashboard
scene shows only listening, working, speaking, stopped, and error states.

Apple controls actual dashboard availability. The current App Store profile
does not contain `com.apple.developer.carplay-voice-based-conversation`.
Therefore source, scene delegate, state model, and on-phone preview ship without
activating the entitlement in the normal signing file. After Apple grants the
capability, the App ID/profile and source entitlement must be updated together;
until then the existing TestFlight signing path must remain valid.

## Verification contract

- Server migration and store tests cover idempotent dispatch, durable results,
  acknowledgement, failure, and restart recovery.
- WebSocket proxy tests cover authentication boundary, bidirectional frames,
  close, ping/pong, upstream failure, and absence of API-key leakage.
- Portable Swift tests cover settings defaults, mode transitions, tool
  resolution, result de-duplication, and CarPlay presentation states.
- Static checks cover scene metadata and the entitlement/profile boundary.
- The complete app must pass the repository debug and Release Linux gates.
- Real CarPlay launch/audio behavior remains an Apple-entitlement and physical
  vehicle validation gate; no source-only test may be reported as that proof.
