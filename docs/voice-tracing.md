# Voice tracing

A permanent, millisecond timeline of every voice exchange: when the user
started and stopped talking, how loud it was, how long each hop took, and
whether the capture was broken. It exists so that "car mode felt slow at
14:32" can be answered from data rather than memory.

Rows live in `voice_events` in `state.sqlite` (`server/lib/voice_events.py`)
and are never pruned. The debugging log in `telemetry.sqlite` is a different
thing: rich, noisy, gone after a day.

## Who writes what

| Producer | Events |
|---|---|
| Client (iOS, PWA) | `listen_start`, `listen_stop`, `speech_start`, `speech_end`, `silence`, `level`, `upload_start`, `upload_end`, `transcript_received`, `barge_in`, `corrupt`, `error` |
| Server `/transcribe` | `transcript` (text, STT time), `retry`, `level` (decoded-audio metrics), `corrupt` (integrity flags), `error` |
| Server `/send` | `send` when the request carries `transcription_id` |
| Server `/clips/ack` | `play_start`, `play_end`, `play_fail` |

## Identity

- **`utterance_id`** ties the client's rows to the server's. Send it as
  `X-Utterance-ID` on `/transcribe` and as `utterance_id` on the following
  `/send`; both fall back to the transcription job id (`X-Transcription-ID`,
  `transcription_id`) when absent. A hands-free turn assembled from several
  captures should use the turn's job id for all of them so the rollup sees
  one utterance.
- **`trace_id`** ties the server side of the turn together: `/transcribe`
  mints it, `/send` echoes it, TTS clips carry it, `/clips/ack` reports it.
  Playback rows only know the trace; the rollup joins them to the utterance
  whose `transcript` or `send` carried the same trace.

## Clocks

Clients send their own wall clock (`ts`, ms since epoch) and, when they have
one, a monotonic clock (`mono_ms`) per event, plus `sent_at` for the batch.
The server stores `ts = client_ts + (received_at - sent_at)` and keeps
`client_ts`, `received_at`, and `clock_offset_ms` raw, so a phone that is 90 s
off still lands on the server's timeline and the correction is auditable.
Offsets beyond six hours are treated as unknown and receipt time is used.

Ship voice events promptly (a few hundred ms, not the 30 s diagnostics
window): the correction handles clock skew, not buffering delay.

## Wire shape

```
POST /voice-events
{
  "source": "ios",                 // ios | pwa
  "client_id": "…",                // stable per install
  "sent_at": 1788400000000,        // client wall clock at send
  "session": "rachel",             // default for events without one
  "events": [
    {"event": "speech_start", "ts": 1788399997000, "mono_ms": 812340,
     "utterance_id": "job-…", "detail": {"silence_ms": 1240}},
    {"event": "speech_end", "ts": 1788399999100, "mono_ms": 814440,
     "utterance_id": "job-…", "duration_ms": 2100,
     "level_db": -19.4, "peak_db": -3.1}
  ]
}
→ {"ok": true, "stored": 2, "clock_offset_ms": -412, "server_now": …}
```

Per-event fields: `event` (required), `ts`, `mono_ms`, `utterance_id`,
`trace_id`, `session`, `duration_ms`, `level_db`, `peak_db`, `text`,
`detail` (object). Levels are dBFS; RMS in `level_db`, peak in `peak_db`.

`/transcribe` accepts `X-Utterance-ID` and `X-Client-Ts` (client wall clock
ms at upload start) next to the existing headers.

```
GET /voice-events?session=&since=&until=&utterance_id=&trace_id=&event=&limit=
→ {"events": [row, …]}                     oldest first, corrected clock

GET /voice-events/utterances?session=&since=&until=&limit=
→ {"utterances": [{utterance_id, session, trace_id, started_at, ended_at,
     speech_ms, silence_before_ms, level_db, peak_db, upload_ms, stt_ms,
     transcript, speech_to_text_ms, text_to_send_ms, send_to_play_ms,
     speech_to_play_ms, play_ms, barge_in, corrupt: [reasons], events}, …]}
```

## Integrity flags

`server/lib/audio_metrics.py` decodes each upload (WAV directly, anything
else through PyAV) and files a `level` row with `duration_ms`, `rms_db`,
`peak_db`, `clip_ratio`, `silence_ratio`, `leading_silence_ms`,
`trailing_silence_ms`, `dc_offset`. A `corrupt` row follows when any of these
hold:

| Reason | Meaning |
|---|---|
| `decode_error` | the bytes did not decode as audio |
| `incomplete_upload`, `upload_read_error`, `bad_size`, `bad_content_length` | the body never fully arrived |
| `too_short` | under 150 ms of audio |
| `clipping` | more than 1% of samples at full scale |
| `too_quiet` | peak below -40 dBFS |
| `silent_upload` | over 97% silence and the transcript is empty |
| `dc_offset` | mean sample beyond ±0.1, a broken capture chain |

Clients add their own `corrupt` rows for what only they can see (zero-byte
recordings, recorder restarts, route changes mid-utterance) with
`detail.reasons`.
