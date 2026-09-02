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
