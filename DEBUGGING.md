# Debugging Clarp

The server writes every interesting event — HTTP request, herald, audio
clip, state transition, error — into a diagnostics database that sits next
to the state database:

```text
~/.local/share/clarp/state.sqlite       agents, messages, clips, queues
~/.local/share/clarp/telemetry.sqlite   diagnostic_events + rollups (24h detail)
```

`diagnostic_events` is the raw append-only diagnostics table. The `requests`,
`errors`, `trace_paths`, and `voice_latency` views live in the telemetry
database; `clip_lifecycle` lives in the state database.

The entry point opens the state database with telemetry attached as
`telemetry`:

```bash
./scripts/sqlite-query.sh                                                # interactive shell
./scripts/sqlite-query.sh "SELECT * FROM telemetry.trace_paths LIMIT 5"  # one-shot query
```

The JSONL files under `~/.cache/clarp/logs/YYYY-MM-DD.jsonl` mirror the same
rows for DuckDB analysis with `./scripts/query.sh`; SQLite is the live source
of truth.

## Sources

| source              | who writes it                                        |
|---------------------|------------------------------------------------------|
| `server`            | `server.py` (Handler + lib.log → eventlog shim)      |
| `client`            | every `clog()` call from the web client (`web/src/`) via `/clog` |
| `stop_hook`         | `~/.claude/hooks/stop_state.py` (Stop hook)          |
| `userprompt_hook`   | `~/.claude/hooks/pwa_source_flag.py`                 |
| `tts`               | `lib.tts_engine.ElevenLabsEngine`                    |
| `stt`               | `lib.stt.WhisperSTT`                                 |

## Row shape

```json
{
  "ts":         "2026-05-25T13:02:35.123Z",
  "source":    "server",
  "event":     "heraldEmitted",
  "level":     "info|warn|error",
  "request_id":"9577354bbf8a4309",
  "session":      "claude",
  "agent_id":  "agent-…",
  "backend_session_id":"6c708e2a-…",
  "persona":   "Mike",
  "clip_id":   42,
  "clip_url":  "/audio/…",
  "sse_event_id": 101,
  "path":      "/send",
  "status":    200,
  "duration_ms": 1240,
  "trace_id":  "a3f9c001…",
  "detail":    { "free": "form" }
}
```

`ts`, `source`, `event`, `level` are always present. Everything else is
optional. SQLite stores `detail` as JSON text, so query nested fields with
`json_extract(detail, '$.field')`.

### `trace_id` — follow one user turn end-to-end

Every `/transcribe` and `/send` returns a 16-hex `trace_id`. The client echoes
it back on `/send` and stamps it on every `clog()` call until the next turn. The
server writes that same trace into the per-session PWA source marker, so the
Claude Code `UserPromptSubmit` hook keeps the original trace instead of
creating a new one. Later hook/server/client events can therefore be joined
as one path: STT → send → UserPromptSubmit → TTS → broadcast → queue →
play-start → play-ok / play-fail.

```sql
SELECT ts_iso, source, event,
       coalesce(json_extract(detail, '$.msg'), json_extract(detail, '$.text'), '')
FROM telemetry.diagnostic_events
WHERE trace_id = 'a3f9c0014b2d9e7c'
ORDER BY ts;
```

Or use the rollup:

```sql
SELECT * FROM telemetry.trace_paths
WHERE trace_id = 'a3f9c0014b2d9e7c';
```

## Pre-baked views

In `telemetry.sqlite` (`lib/telemetry.py`):

* `diagnostic_events` — the raw rolling table (`ts` in ms, `ts_iso` as text).
* `requests` — every HTTP request: `ts, request_id, trace_id, method, path, status, duration_ms`.
* `errors` — every `level='error'` row with full traceback.
* `trace_paths` — one row per trace with ordered `source:event` path.
* `voice_latency` — send → first clip timings per trace.

In `state.sqlite` (`lib/db.py`):

* `clip_lifecycle` — canonical clip table lifecycle and playback ACK state.

## Cookbook

### Recent activity (last 5 min)

```sql
SELECT ts_iso, source, event, persona, detail
FROM telemetry.diagnostic_events
WHERE ts > (unixepoch('now') * 1000) - 300000
ORDER BY ts DESC
LIMIT 50;
```

### Find errors today

```sql
SELECT ts, source, event, error_type, error_message
FROM telemetry.errors
WHERE substr(ts, 1, 10) = date('now')
ORDER BY ts DESC;
```

### Slow HTTP requests

```sql
SELECT ts, path, status, duration_ms
FROM telemetry.requests
WHERE duration_ms > 500
ORDER BY ts DESC
LIMIT 20;
```

### Replay a conversation

```sql
SELECT ts_iso, source, event,
       coalesce(json_extract(detail, '$.msg'), json_extract(detail, '$.text'), '') AS text
FROM telemetry.diagnostic_events
WHERE session = 'claude'
  AND ts_iso BETWEEN '2026-05-25T13:00:00.000Z' AND '2026-05-25T14:00:00.000Z'
ORDER BY ts;
```

### Find missing trace IDs

```sql
SELECT source, event, count(*) AS rows
FROM telemetry.diagnostic_events
WHERE (trace_id IS NULL OR trace_id = '')
  AND ts > (unixepoch('now') * 1000) - 86400000
GROUP BY source, event
ORDER BY rows DESC;
```

### Audio playback diagnostics

```sql
SELECT clip_id, trace_id, clip_url, status, producer_status,
       broadcast_at, queued_at, play_started_at, played_at, completed_at, error
FROM clip_lifecycle
ORDER BY created_at DESC
LIMIT 30;
```

### Find clips that never played

```sql
SELECT *
FROM clip_lifecycle
WHERE broadcast_at IS NOT NULL
  AND played_at IS NULL
  AND error IS NULL
ORDER BY created_at DESC;
```

## What the transcriber was sent, and what it heard

Every `/transcribe` compiles a vocabulary payload and records it in
`vocab_runs` (state database) before inference; the transcript and model
latency are written to the same row afterwards. The run id travels in the
`transcribe` event's detail and in the `/transcribe` response as
`vocab_run_id`, and `/vocab/run?trace_id=…` returns the row.

```sql
-- The prompt behind the last ten transcripts, with what was dropped and why
SELECT run_id, session, provider, model, used || '/' || capacity AS budget,
       form, transcript, latency_ms, payload, dropped_json
FROM vocab_runs ORDER BY run_id DESC LIMIT 10;

-- Which packs are earning their budget
SELECT json_extract(value, '$.pack') AS pack, count(*) AS terms_sent
FROM vocab_runs, json_each(vocab_runs.included_json)
WHERE created_at > (unixepoch('now') * 1000) - 86400000
GROUP BY pack ORDER BY terms_sent DESC;

-- Runs where the model never answered
SELECT run_id, session, created_at FROM vocab_runs
WHERE transcript = '' AND latency_ms = 0 ORDER BY run_id DESC LIMIT 20;
```

With `transcription.retain_audio` on (POST `/transcription-providers`
`{"retain_audio": true}`), the clip itself is kept under
`~/.cache/clarp/heard/<trace_id>.<ext>` with a JSON sidecar, capped at 500
clips or 14 days. `/transcription-audio?trace_id=…` serves it, and
`/vocab/run` adds `audio_url` when one exists, so a garbled transcript can be
listened to next to the exact prompt that produced it.
## Audio corruption diagnostics

Every time playback goes wrong on a client, the web app records what went
wrong, where in the clip, and what the world looked like at that instant. The
monitor lives in `static/lib/audio-faults.js`, is attached in
`web/src/stores/audio.svelte.js`, and reports through `/clog` as two events:

* `client:audioFault` — one row per fault. `detail.kind` is one of
  `stall`, `decode-error`, `load-fail`, `load-timeout`, `play-rejected`,
  `premature-end`, `end-timeout`, `time-jump`, `rate-drift`, `aborted`.
* `client:audioClipSummary` — one row per clip, faulty or not, so healthy
  clips give a baseline (stall counts, latency, whether sound was reached).

Each record carries three blocks in `detail`:

| block        | what it holds                                                                 |
|--------------|-------------------------------------------------------------------------------|
| `element`    | playhead (`current_s`, `position_pct`), `buffered_ahead_ms`, `ready_state`, `network_state`, `rate`, `volume`, `element_muted`, MediaError name |
| `latency`    | `broadcast_to_queued_ms`, `queued_to_play_start_ms`, `play_start_to_sound_ms` |
| `conditions` | network (`online`, `net_type`, `net_rtt_ms`), `visibility`, `focused`, battery, `sse_open`, `mic_recording`/`mic_capturing`/`mic_level`, `queue_len`, `machine_state`, adapter version |

Stalls are measured from the element's `waiting`/`stalled` event to the next
`playing`; anything under 250 ms is counted in the summary but not reported as
a fault. A stall record also carries `at_stall_start` — the element snapshot
from the moment the stall began — because by the time it ends the buffer has
usually recovered.

`mic_level` is the same band-limited energy the voice-activity detector uses
(0 when the mic is off). The browser exposes no output loudness for an
`<audio>` element without routing it through Web Audio, which changes the
playback path on iOS, so output level is reported as the element's `volume`
and `element_muted` only.

Two views flatten the JSON in `telemetry.sqlite`:

```sql
-- Every fault today, newest first
SELECT ts, kind, clip_id, delivery, at_s, duration_s, stall_ms,
       buffered_ahead_ms, ready_state, net_type, net_rtt_ms, visibility,
       mic_recording, mic_level, queue_len
FROM telemetry.audio_faults
WHERE substr(ts, 1, 10) = date('now')
ORDER BY ts DESC;

-- Which kinds of fault happen, and under which delivery
SELECT kind, delivery, count(*) AS n, round(avg(stall_ms)) AS avg_stall_ms
FROM telemetry.audio_faults
GROUP BY kind, delivery ORDER BY n DESC;

-- Faulty share of clips per delivery path over the last 24h
SELECT delivery, count(*) AS clips,
       sum(CASE WHEN ok THEN 0 ELSE 1 END) AS faulty,
       round(avg(stall_total_ms)) AS avg_stall_ms,
       round(avg(play_start_to_sound_ms)) AS avg_to_sound_ms
FROM telemetry.audio_clip_health
WHERE ts > (unixepoch('now') * 1000) - 86400000
GROUP BY delivery;

-- Was the server side to blame? Join the clip's producer state.
SELECT f.ts, f.kind, f.at_s, f.stall_ms, c.producer_status, c.bytes, c.error,
       c.broadcast_at, c.completed_at
FROM telemetry.audio_faults f
JOIN clip_lifecycle c ON c.clip_id = f.clip_id
ORDER BY f.ts DESC LIMIT 30;

-- Did the provider pause? Server-side chunk gaps (tts_worker:synthChunkGap)
-- next to the client stalls for the same clip.
SELECT g.ts_iso, g.clip_id, json_extract(g.detail, '$.gap_ms') AS server_gap_ms,
       json_extract(g.detail, '$.chunk_idx') AS chunk_idx,
       f.kind, f.stall_ms AS client_stall_ms, f.at_s
FROM telemetry.diagnostic_events g
LEFT JOIN telemetry.audio_faults f ON f.clip_id = g.clip_id
WHERE g.source = 'tts_worker' AND g.event = 'synthChunkGap'
ORDER BY g.ts DESC LIMIT 30;

-- Per-clip producer pacing summary (tts_worker:synthPacing)
SELECT ts_iso, clip_id, json_extract(detail, '$.delivery') AS delivery,
       json_extract(detail, '$.chunks') AS chunks,
       json_extract(detail, '$.first_chunk_ms') AS first_chunk_ms,
       json_extract(detail, '$.max_gap_ms') AS max_gap_ms,
       json_extract(detail, '$.gaps_over_threshold') AS gaps_over,
       json_extract(detail, '$.outcome') AS outcome
FROM telemetry.diagnostic_events
WHERE source = 'tts_worker' AND event = 'synthPacing'
ORDER BY ts DESC LIMIT 30;

-- The whole story of one bad turn
SELECT ts_iso, source, event,
       coalesce(json_extract(detail, '$.kind'), json_extract(detail, '$.msg'), '') AS what
FROM telemetry.diagnostic_events
WHERE trace_id = (SELECT trace_id FROM telemetry.audio_faults ORDER BY ts DESC LIMIT 1)
ORDER BY ts;
```

Reading a record: a `stall` with `buffered_ahead_ms = 0`, `network_state = 2`
and a poor `net_type` is the network not keeping up; the same stall with a
healthy buffer and `visibility = hidden` is the browser throttling a
backgrounded tab; a `decode-error` at a fixed `at_s` across clips of one
`delivery` points at the producer; `time-jump` and `rate-drift` are the
element itself misbehaving (seen on iOS after an interruption).

## File layout

```
~/.cache/clarp/
  logs/
    2026-05-25.jsonl     JSONL mirror for DuckDB tooling
    2026-05-24.parquet   compacted yesterday (zstd)
    ...
  logs/                  structured JSONL event log
  current-session        selected app session
  source-markers/<sid>   fresh PWA source marker consumed by hook
  worker-pids/<uuid>.pid per-backend-session Stop-hook worker pid
```

## Rotation

Compaction runs daily at 04:00 UTC via
`clarp-logrotate.service` + `.timer` (in `~/.config/systemd/user/`).
Force a rotation manually:

```bash
systemctl --user start clarp-logrotate.service
journalctl --user -u clarp-logrotate -e
```

## Adding a new event

Anywhere in server code:

```python
from lib import eventlog
eventlog.emit("server", "myNewThing",
              session=session, persona=name,
              duration_ms=elapsed,
              detail={"foo": 1, "bar": "two"})
```

For client code, just call `clog('myNewThing', 'detail text')` — already
batched and routed through `/clog` into eventlog.

For new persistent live views, add them to the SQLite migration in
`server/lib/db.py`. Keep `scripts/views.sql` only for legacy JSONL/DuckDB
queries.

## Tips

* All timestamps are UTC, millisecond precision.
* `json_extract(detail, '$.path')` reads nested JSON fields.
* Use `coalesce(json_extract(detail, '$.foo'), '')` for paths that may not exist.
* When schemas evolve, run `./scripts/sqlite-query.sh ".schema events"`.
