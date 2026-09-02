#!/usr/bin/env python3
"""Read-only terminal console for claude-pwa agent/debug state.

This intentionally talks to SQLite directly instead of the browser UI. It is
for agents and humans who need a compact answer to: where is this turn stuck?
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import time
from typing import Any


DEFAULT_DB = pathlib.Path(
    os.environ.get(
        "CLAUDE_PWA_DB",
        str(pathlib.Path.home() / ".local" / "share" / "clarp" / "state.sqlite"),
    )
)
DEFAULT_TELEMETRY_DB = pathlib.Path(os.environ.get(
    "CLARP_TELEMETRY_DB",
    str(pathlib.Path.home() / ".local/share/clarp/telemetry.sqlite")))


def _connect_readonly(db_path: pathlib.Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def _rows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.Error as exc:
        return [{"error": str(exc), "sql": " ".join(sql.split())}]


def _one(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    try:
        row = con.execute(sql, params).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as exc:
        return {"error": str(exc), "sql": " ".join(sql.split())}


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r["name"]) for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _ms_to_local(value: Any) -> str:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return "-"
    if ms <= 0:
        return "-"
    stamp = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).astimezone()
    return stamp.strftime("%H:%M:%S")


def _short(value: Any, limit: int = 96) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _json_loads(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError:
        return raw


def _agent_filter_where(session: str | None, trace: str | None) -> tuple[str, tuple[Any, ...]]:
    if session:
        return "a.session = ?", (session,)
    if trace:
        return "tr.trace_id = ?", (trace,)
    return "a.deleted_at IS NULL", ()


def snapshot(db_path: pathlib.Path = DEFAULT_DB, *,
             telemetry_path: pathlib.Path = DEFAULT_TELEMETRY_DB,
             session: str | None = None,
             trace: str | None = None, limit: int = 8) -> dict[str, Any]:
    """Collect a read-only diagnostic snapshot from SQLite."""
    with _connect_readonly(db_path) as con:
        telemetry_schema = "main"
        if telemetry_path.is_file():
            con.execute(
                "ATTACH DATABASE ? AS telemetry",
                (f"file:{telemetry_path}?mode=ro",))
            telemetry_schema = "telemetry"
        agents_cols = _table_columns(con, "agents")
        backend_expr = "a.backend" if "backend" in agents_cols else "'claude'"
        where, params = _agent_filter_where(session, trace)
        agents = _rows(con, f"""
            WITH latest_state AS (
                SELECT sl.*
                FROM state_log sl
                JOIN (
                    SELECT agent_id, max(state_id) AS state_id
                    FROM state_log GROUP BY agent_id
                ) pick ON pick.state_id = sl.state_id
            ),
            live_runtime AS (
                SELECT r.*
                FROM runtimes r
                JOIN (
                    SELECT agent_id, max(runtime_id) AS runtime_id
                    FROM runtimes WHERE ended_at IS NULL GROUP BY agent_id
                ) pick ON pick.runtime_id = r.runtime_id
            )
            SELECT a.agent_id, a.persona, a.session, a.cwd,
                   {backend_expr} AS backend,
                   r.backend_session_id, r.started_at AS runtime_started_at,
                   ls.kind AS state, ls.ts AS state_ts, ls.detail AS state_detail,
                   tr.trace_id
            FROM agents a
            LEFT JOIN live_runtime r ON r.agent_id = a.agent_id
            LEFT JOIN latest_state ls ON ls.agent_id = a.agent_id
            LEFT JOIN traces tr ON tr.agent_id = a.agent_id
            WHERE {where}
            ORDER BY a.created_at DESC
            LIMIT ?
        """, (*params, limit))

        agent_ids = [a["agent_id"] for a in agents if a.get("agent_id")]
        trace_ids = [trace] if trace else [a.get("trace_id") for a in agents if a.get("trace_id")]
        primary_agent = agent_ids[0] if agent_ids else None
        primary_trace = trace_ids[0] if trace_ids else None

        messages = _rows(con, """
            SELECT message_id, agent_id, backend_session_id, seq, role, kind,
                   timestamp, text, updated_at
            FROM messages
            WHERE (? IS NULL OR agent_id = ?)
              AND (? IS NULL OR backend_session_id = ?)
            ORDER BY updated_at DESC, seq DESC
            LIMIT ?
        """, (
            primary_agent, primary_agent,
            agents[0].get("backend_session_id") if agents else None,
            agents[0].get("backend_session_id") if agents else None,
            limit,
        ))

        tts = _rows(con, """
            SELECT queue_id, agent_id, session, trace_id, status, clip_id,
                   enqueued_at, claimed_at, completed_at, error,
                   substr(text, 1, 180) AS text
            FROM tts_queue
            WHERE (? IS NULL OR agent_id = ?)
              AND (? IS NULL OR trace_id = ?)
            ORDER BY queue_id DESC
            LIMIT ?
        """, (primary_agent, primary_agent, primary_trace, primary_trace, limit))

        clips = _rows(con, """
            SELECT clip_id, agent_id, trace_id, path, bytes, status,
                   producer_status, created_at, broadcast_at, queued_at,
                   play_started_at, played_at, completed_at, error
            FROM clips
            WHERE (? IS NULL OR agent_id = ?)
              AND (? IS NULL OR trace_id = ?)
            ORDER BY clip_id DESC
            LIMIT ?
        """, (primary_agent, primary_agent, primary_trace, primary_trace, limit))

        sse = _rows(con, """
            SELECT event_id, ts, type, session, agent_id, payload
            FROM sse_events
            WHERE (? IS NULL OR session = ?)
              AND (? IS NULL OR agent_id = ?)
            ORDER BY event_id DESC
            LIMIT ?
        """, (session, session, primary_agent, primary_agent, limit))
        for row in sse:
            if "payload" in row:
                row["payload"] = _json_loads(row["payload"])

        events = _rows(con, f"""
            SELECT event_id, ts, ts_iso, source, event, level, trace_id, session,
                   agent_id, clip_id, duration_ms, detail
            FROM {telemetry_schema}.diagnostic_events
            WHERE (? IS NULL OR session = ?)
              AND (? IS NULL OR agent_id = ?)
              AND (? IS NULL OR trace_id = ?)
            ORDER BY event_id DESC
            LIMIT ?
        """, (
            session, session,
            primary_agent, primary_agent,
            primary_trace, primary_trace,
            limit,
        ))
        for row in events:
            if "detail" in row:
                row["detail"] = _json_loads(row["detail"])

        latency = _one(con, f"""
            SELECT *
            FROM {telemetry_schema}.voice_latency
            WHERE (? IS NULL OR trace_id = ?)
            ORDER BY sent_ms DESC
            LIMIT 1
        """, (primary_trace, primary_trace))

        streaming_latency = _one(con, f"""
            WITH sent AS (
                SELECT trace_id, min(ts) AS ts
                FROM {telemetry_schema}.diagnostic_events
                WHERE event = 'send'
                  AND (? IS NULL OR trace_id = ?)
                GROUP BY trace_id
                ORDER BY ts DESC
                LIMIT 1
            ),
            first_chunk AS (
                SELECT trace_id, min(ts) AS ts,
                       json_extract(detail, '$.bytes') AS bytes,
                       json_extract(detail, '$.ttfb_ms') AS ttfb_ms
                FROM {telemetry_schema}.diagnostic_events
                WHERE source = 'cartesia'
                  AND event = 'firstChunk'
                  AND trace_id = (SELECT trace_id FROM sent)
                GROUP BY trace_id
            ),
            synth_done AS (
                SELECT trace_id, min(ts) AS ts,
                       duration_ms,
                       json_extract(detail, '$.bytes') AS bytes,
                       json_extract(detail, '$.chunks') AS chunks
                FROM {telemetry_schema}.diagnostic_events
                WHERE source = 'cartesia'
                  AND event = 'synth'
                  AND trace_id = (SELECT trace_id FROM sent)
                GROUP BY trace_id
            ),
            audio_broadcast AS (
                SELECT trace_id, min(ts) AS ts
                FROM {telemetry_schema}.diagnostic_events
                WHERE source = 'audio_stream'
                  AND event = 'broadcast'
                  AND json_extract(detail, '$.type') = 'audio'
                  AND trace_id = (SELECT trace_id FROM sent)
                GROUP BY trace_id
            ),
            play_start AS (
                SELECT trace_id, min(ts) AS ts
                FROM {telemetry_schema}.diagnostic_events
                WHERE event = 'clipAck'
                  AND json_extract(detail, '$.status') = 'play-start'
                  AND trace_id = (SELECT trace_id FROM sent)
                GROUP BY trace_id
            )
            SELECT
                sent.trace_id,
                sent.ts AS sent_ms,
                first_chunk.ts AS first_chunk_ms,
                synth_done.ts AS synth_done_ms,
                audio_broadcast.ts AS audio_broadcast_ms,
                play_start.ts AS play_start_ms,
                round((first_chunk.ts - sent.ts) / 1000.0, 2) AS first_chunk_s,
                round((synth_done.ts - sent.ts) / 1000.0, 2) AS synth_done_s,
                round((audio_broadcast.ts - sent.ts) / 1000.0, 2) AS audio_broadcast_s,
                round((play_start.ts - sent.ts) / 1000.0, 2) AS play_start_s,
                round((synth_done.ts - first_chunk.ts) / 1000.0, 2) AS chunk_win_s,
                round((play_start.ts - first_chunk.ts) / 1000.0, 2) AS first_chunk_to_play_s,
                first_chunk.bytes AS first_chunk_bytes,
                first_chunk.ttfb_ms AS cartesia_ttfb_ms,
                synth_done.bytes AS total_bytes,
                synth_done.chunks AS total_chunks,
                synth_done.duration_ms AS cartesia_duration_ms
            FROM sent
            LEFT JOIN first_chunk ON first_chunk.trace_id = sent.trace_id
            LEFT JOIN synth_done ON synth_done.trace_id = sent.trace_id
            LEFT JOIN audio_broadcast ON audio_broadcast.trace_id = sent.trace_id
            LEFT JOIN play_start ON play_start.trace_id = sent.trace_id
        """, (primary_trace, primary_trace))

    return {
        "db": str(db_path),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "filter": {"session": session, "trace": trace, "limit": limit},
        "agents": agents,
        "messages": messages,
        "tts_queue": tts,
        "clips": clips,
        "sse_events": sse,
        "diagnostic_events": events,
        "voice_latency": latency,
        "streaming_latency": streaming_latency,
    }


def render_text(data: dict[str, Any]) -> str:
    lines: list[str] = []
    filt = data.get("filter", {})
    lines.append("claude-pwa live console")
    lines.append(f"db: {data.get('db')}")
    lines.append(
        "filter: "
        + ", ".join(f"{k}={v}" for k, v in filt.items() if v not in (None, ""))
    )
    lines.append("")

    lines.append("Agents")
    agents = data.get("agents") or []
    if not agents:
        lines.append("  none")
    for a in agents:
        if a.get("error"):
            lines.append(f"  ERROR {a['error']}")
            continue
        lines.append(
            f"  {a.get('persona') or '?'} session={a.get('session') or '-'} "
            f"backend={a.get('backend') or '-'} state={a.get('state') or '-'} "
            f"trace={a.get('trace_id') or '-'}"
        )
        lines.append(
            f"    agent_id={a.get('agent_id') or '-'} "
            f"backend_session={a.get('backend_session_id') or '-'} "
            f"cwd={_short(a.get('cwd'), 90)}"
        )

    lines.append("")
    lines.append("Recent Messages")
    for m in data.get("messages") or []:
        if m.get("error"):
            lines.append(f"  ERROR {m['error']}")
            continue
        lines.append(
            f"  seq={m.get('seq')} {m.get('role') or '-'} "
            f"kind={m.get('kind') or '-'} updated={_ms_to_local(m.get('updated_at'))}"
        )
        lines.append(f"    {_short(m.get('text'), 140)}")
    if not data.get("messages"):
        lines.append("  none")

    lines.append("")
    lines.append("TTS Queue")
    for q in data.get("tts_queue") or []:
        if q.get("error"):
            lines.append(f"  ERROR {q['error']}")
            continue
        lines.append(
            f"  #{q.get('queue_id')} status={q.get('status')} clip={q.get('clip_id') or '-'} "
            f"trace={q.get('trace_id') or '-'} enq={_ms_to_local(q.get('enqueued_at'))} "
            f"done={_ms_to_local(q.get('completed_at'))}"
        )
        lines.append(f"    {_short(q.get('text'), 140)}")
        if q.get("error"):
            lines.append(f"    error={_short(q.get('error'), 140)}")
    if not data.get("tts_queue"):
        lines.append("  none")

    lines.append("")
    lines.append("Clips")
    for c in data.get("clips") or []:
        if c.get("error"):
            lines.append(f"  ERROR {c['error']}")
            continue
        lines.append(
            f"  #{c.get('clip_id')} producer={c.get('producer_status') or '-'} "
            f"playback={c.get('status') or '-'} bytes={c.get('bytes') or '-'} "
            f"play_start={_ms_to_local(c.get('play_started_at'))} "
            f"played={_ms_to_local(c.get('played_at'))}"
        )
        lines.append(f"    trace={c.get('trace_id') or '-'} path={_short(c.get('path'), 110)}")
        if c.get("error"):
            lines.append(f"    error={_short(c.get('error'), 140)}")
    if not data.get("clips"):
        lines.append("  none")

    lines.append("")
    lines.append("Recent SSE")
    for s in data.get("sse_events") or []:
        if s.get("error"):
            lines.append(f"  ERROR {s['error']}")
            continue
        payload = s.get("payload") if isinstance(s.get("payload"), dict) else {}
        trace = payload.get("trace_id") if isinstance(payload, dict) else None
        lines.append(
            f"  #{s.get('event_id')} {_ms_to_local(s.get('ts'))} "
            f"type={s.get('type')} session={s.get('session') or '-'} "
            f"trace={trace or '-'}"
        )
    if not data.get("sse_events"):
        lines.append("  none")

    lines.append("")
    lines.append("Recent Diagnostics")
    for e in data.get("diagnostic_events") or []:
        if e.get("error"):
            lines.append(f"  ERROR {e['error']}")
            continue
        detail = e.get("detail") if isinstance(e.get("detail"), dict) else {}
        status = detail.get("status") if isinstance(detail, dict) else None
        lines.append(
            f"  #{e.get('event_id')} {e.get('ts_iso') or _ms_to_local(e.get('ts'))} "
            f"{e.get('source')}:{e.get('event')} level={e.get('level')} "
            f"trace={e.get('trace_id') or '-'} clip={e.get('clip_id') or '-'} "
            f"dur={e.get('duration_ms') or '-'} status={status or '-'}"
        )
    if not data.get("diagnostic_events"):
        lines.append("  none")

    latency = data.get("voice_latency")
    if latency and not latency.get("error"):
        lines.append("")
        lines.append("Voice Latency")
        lines.append(
            f"  trace={latency.get('trace_id') or '-'} "
            f"first_speak={latency.get('first_speak_s') or '-'}s "
            f"first_play={latency.get('first_play_s') or '-'}s "
            f"done={latency.get('done_s') or '-'}s"
        )
        if latency.get("prompt"):
            lines.append(f"    prompt={_short(latency.get('prompt'), 140)}")

    streaming = data.get("streaming_latency")
    if streaming and not streaming.get("error"):
        lines.append("")
        lines.append("Streaming Latency")
        lines.append(
            f"  trace={streaming.get('trace_id') or '-'} "
            f"first_chunk={streaming.get('first_chunk_s') or '-'}s "
            f"synth_done={streaming.get('synth_done_s') or '-'}s "
            f"audio_event={streaming.get('audio_broadcast_s') or '-'}s "
            f"play_start={streaming.get('play_start_s') or '-'}s"
        )
        lines.append(
            f"    chunk_win={streaming.get('chunk_win_s') or '-'}s "
            f"first_chunk_to_play={streaming.get('first_chunk_to_play_s') or '-'}s "
            f"cartesia_ttfb={streaming.get('cartesia_ttfb_ms') or '-'}ms "
            f"chunks={streaming.get('total_chunks') or '-'} "
            f"bytes={streaming.get('total_bytes') or '-'}"
        )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only terminal console for claude-pwa agent/audio state."
    )
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB)
    parser.add_argument(
        "--telemetry-db", type=pathlib.Path, default=DEFAULT_TELEMETRY_DB)
    parser.add_argument("--session", help="Filter by app session, e.g. rachel-ac21")
    parser.add_argument("--trace", help="Filter by trace id")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--watch", type=float, default=0.0,
        help="Refresh every N seconds until interrupted",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 2

    while True:
        data = snapshot(
            args.db, telemetry_path=args.telemetry_db,
            session=args.session, trace=args.trace, limit=args.limit)
        if args.json:
            print(json.dumps(data, indent=2, sort_keys=True))
        else:
            if args.watch:
                print("\033[2J\033[H", end="")
            print(render_text(data))
        if not args.watch:
            return 0
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
