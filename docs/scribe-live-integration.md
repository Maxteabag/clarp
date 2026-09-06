# Live Scribe transcription integration (in progress)

The Host provides `POST /transcription/realtime-session`.
It accepts an empty JSON object and requires authenticated full-device access,
even on a Host otherwise configured without authentication. A successful reply
contains a single-use token, `scribe_v2_realtime`, `pcm_16000`, and manual commit
strategy. Responses are not cacheable. Stored ElevenLabs credentials never leave
the Host. This endpoint alone does not change recording or transcription behavior.

The native client must preserve native turn detection and the durable voice job
queue. Provider segment commits are not turn-end events. Current capture can
retract an eager turn and replay the earlier audio plus a pause into a subsequent
capture. Saved WAVs also receive whole-clip gain normalization. Consequently,
matching a stream to a WAV by timestamp, text prefix, or hash of streamed PCM is
unsafe. An explicit capture identity must travel with the file and the result.

Native integration requirements:

- Attach an immutable stream/capture identity before the first microphone frame;
  serialize boundary callbacks with audio frames. Keep Host/session routing pinned.
- Buffer a bounded amount of startup audio while obtaining the single-use token.
  Cancel and use the persisted WAV on overflow, auth/network failure, or malformed
  provider output. Never promote a partial transcript after a failure.
- Accumulate provider committed segments and acknowledge every explicit commit.
  Finish only at the native capture boundary, with a deadline and WAV fallback.
- Persist completed results against the exact durable audio segment. Preserve
  segment assembly, retraction, cancellation generations, retry, herald processing,
  transcript provenance, retention settings, and replay diagnostics.
- Add a selectable live engine only once both endpoints of this contract work.
  Keep existing clients and batch model selections compatible.

Tests for the session endpoint cover authentication, response caching, malformed
input, and sanitized provider failures. No external API calls are made by tests.
`POST /transcription/live-result` accepts an authenticated native final transcript,
its WAV as `audio_base64`, boolean `hands_free`, and a required `transcription_id`.
The result is client-supplied, not independently re-verified with ElevenLabs. Like
normal authenticated user text, it passes through Host voice-command handling.
Audio and final text are bound into the idempotency fingerprint. It uses the
existing retention and trace path. A configured long-form escalation can replace
the live text with its chosen model. Provenance is `native_scribe_realtime`.

`elevenlabs:scribe_v2_realtime` is presented as Scribe Live. An old client or a
recovered WAV sent to `/transcribe` under that model falls back to batch Scribe v2.
Live sessions currently send no vocabulary hints; their budget is explicitly zero.

Native build, end-to-end integration and deployment are not complete.
