from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3


_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "live_console.py"
_SPEC = importlib.util.spec_from_file_location("live_console", _SCRIPT)
assert _SPEC and _SPEC.loader
live_console = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(live_console)


def _make_db(path: pathlib.Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript("""
            CREATE TABLE agents (
                agent_id TEXT PRIMARY KEY,
                persona TEXT NOT NULL,
                voice_id TEXT NOT NULL,
                cwd TEXT NOT NULL,
                session TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                deleted_at INTEGER,
                backend TEXT
            );
            CREATE TABLE runtimes (
                runtime_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                session TEXT NOT NULL,
                backend_session_id TEXT,
                started_at INTEGER NOT NULL,
                ended_at INTEGER
            );
            CREATE TABLE state_log (
                state_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                runtime_id INTEGER,
                ts INTEGER NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT
            );
            CREATE TABLE traces (
                agent_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE messages (
                message_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                backend_session_id TEXT,
                source_file TEXT,
                seq INTEGER NOT NULL,
                role TEXT,
                timestamp TEXT,
                text TEXT NOT NULL,
                kind TEXT,
                tool_name TEXT,
                tools_json TEXT NOT NULL DEFAULT '[]',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE tts_queue (
                queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                text TEXT NOT NULL,
                voice_id TEXT NOT NULL,
                session TEXT NOT NULL,
                source TEXT NOT NULL,
                mode TEXT NOT NULL,
                trace_id TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                error TEXT,
                enqueued_at INTEGER NOT NULL,
                claimed_at INTEGER,
                completed_at INTEGER,
                clip_id INTEGER
            );
            CREATE TABLE clips (
                clip_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                runtime_id INTEGER,
                turn_id INTEGER,
                path TEXT NOT NULL UNIQUE,
                voice_id TEXT,
                bytes INTEGER,
                trace_id TEXT,
                created_at INTEGER NOT NULL,
                status TEXT,
                broadcast_at INTEGER,
                queued_at INTEGER,
                play_started_at INTEGER,
                played_at INTEGER,
                error TEXT,
                producer_status TEXT,
                completed_at INTEGER
            );
            CREATE TABLE sse_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                type TEXT NOT NULL,
                session TEXT,
                agent_id TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE diagnostic_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ts_iso TEXT NOT NULL,
                source TEXT NOT NULL,
                event TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                trace_id TEXT,
                request_id TEXT,
                client_id TEXT,
                session TEXT,
                agent_id TEXT,
                backend_session_id TEXT,
                persona TEXT,
                clip_id INTEGER,
                clip_url TEXT,
                sse_event_id INTEGER,
                path TEXT,
                status INTEGER,
                duration_ms REAL,
                detail TEXT
            );
            CREATE VIEW voice_latency AS
            SELECT 'trace-1' AS trace_id, 1000 AS sent_ms, 'hello' AS prompt,
                   1.2 AS first_speak_s, 1.8 AS first_play_s, 4.0 AS done_s;
        """)
        con.execute(
            "INSERT INTO agents VALUES (?,?,?,?,?,?,?,?)",
            ("a1", "Rachel", "v1", "/repo", "rachel-ac21", 1000, None, "claude"),
        )
        con.execute(
            "INSERT INTO runtimes(agent_id, session, backend_session_id, started_at, ended_at) "
            "VALUES (?,?,?,?,?)",
            ("a1", "rachel-ac21", "backend-1", 1100, None),
        )
        con.execute(
            "INSERT INTO state_log(agent_id, runtime_id, ts, kind, detail) VALUES (?,?,?,?,?)",
            ("a1", 1, 1200, "thinking", "{}"),
        )
        con.execute("INSERT INTO traces VALUES (?,?,?)", ("a1", "trace-1", 1300))
        con.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("m1", "a1", "backend-1", "live:x", -900000, "assistant",
             "2026-06-22T00:00:00Z", "streaming hello", "live", None, "[]", 1400),
        )
        con.execute(
            "INSERT INTO tts_queue(agent_id,text,voice_id,session,source,mode,trace_id,status,"
            "error,enqueued_at,claimed_at,completed_at,clip_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a1", "hello audio", "v1", "rachel-ac21", "pwa", "pwa",
             "trace-1", "done", None, 1500, 1510, 1700, 1),
        )
        con.execute(
            "INSERT INTO clips(agent_id,path,voice_id,bytes,trace_id,created_at,status,"
            "broadcast_at,queued_at,play_started_at,played_at,error,producer_status,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a1", "/audio/clip.mp3", "v1", 1234, "trace-1", 1500, "play-ok",
             1510, 1520, 1600, 1800, None, "complete", 1700),
        )
        con.execute(
            "INSERT INTO sse_events(ts,type,session,agent_id,payload) VALUES (?,?,?,?,?)",
            (1510, "audio", "rachel-ac21", "a1", json.dumps({"trace_id": "trace-1"})),
        )
        con.execute(
            "INSERT INTO diagnostic_events(ts,ts_iso,source,event,level,trace_id,session,"
            "agent_id,clip_id,duration_ms,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1000, "2026-06-22T00:00:01.000Z", "server", "send",
             "info", "trace-1", "rachel-ac21", "a1", None, None,
             json.dumps({"text": "hello"})),
        )
        con.execute(
            "INSERT INTO diagnostic_events(ts,ts_iso,source,event,level,trace_id,session,"
            "agent_id,clip_id,duration_ms,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1520, "2026-06-22T00:00:01.520Z", "cartesia", "firstChunk",
             "info", "trace-1", "rachel-ac21", "a1", 1, 42,
             json.dumps({"bytes": 1234, "ttfb_ms": 520})),
        )
        con.execute(
            "INSERT INTO diagnostic_events(ts,ts_iso,source,event,level,trace_id,session,"
            "agent_id,clip_id,duration_ms,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1700, "2026-06-22T00:00:01.700Z", "cartesia", "synth",
             "info", "trace-1", "rachel-ac21", "a1", 1, 700,
             json.dumps({"bytes": 5000, "chunks": 3})),
        )
        con.execute(
            "INSERT INTO diagnostic_events(ts,ts_iso,source,event,level,trace_id,session,"
            "agent_id,clip_id,duration_ms,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1710, "2026-06-22T00:00:01.710Z", "audio_stream", "broadcast",
             "info", "trace-1", "rachel-ac21", "a1", 1, None, "{}"),
        )
        con.execute(
            "INSERT INTO diagnostic_events(ts,ts_iso,source,event,level,trace_id,session,"
            "agent_id,clip_id,duration_ms,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1800, "2026-06-22T00:00:01.800Z", "client", "clipAck",
             "info", "trace-1", "rachel-ac21", "a1", 1, None,
             json.dumps({"status": "play-start"})),
        )
        con.commit()
    finally:
        con.close()


def test_snapshot_collects_agent_audio_and_event_state(tmp_path):
    db = tmp_path / "state.sqlite"
    _make_db(db)

    snap = live_console.snapshot(
        db, telemetry_path=tmp_path / "missing-telemetry.sqlite",
        session="rachel-ac21", limit=5)

    assert snap["agents"][0]["persona"] == "Rachel"
    assert snap["agents"][0]["state"] == "thinking"
    assert snap["messages"][0]["text"] == "streaming hello"
    assert snap["tts_queue"][0]["status"] == "done"
    assert snap["clips"][0]["producer_status"] == "complete"
    assert snap["sse_events"][0]["payload"]["trace_id"] == "trace-1"
    first_chunk = next(
        e for e in snap["diagnostic_events"]
        if e["source"] == "cartesia" and e["event"] == "firstChunk"
    )
    assert first_chunk["detail"]["bytes"] == 1234
    assert snap["voice_latency"]["first_play_s"] == 1.8
    assert snap["streaming_latency"]["first_chunk_s"] == 0.52
    assert snap["streaming_latency"]["chunk_win_s"] == 0.18


def test_render_text_is_terminal_friendly(tmp_path):
    db = tmp_path / "state.sqlite"
    _make_db(db)

    rendered = live_console.render_text(
        live_console.snapshot(
            db, telemetry_path=tmp_path / "missing-telemetry.sqlite",
            session="rachel-ac21", limit=5)
    )

    assert "claude-pwa live console" in rendered
    assert "Rachel session=rachel-ac21" in rendered
    assert "TTS Queue" in rendered
    assert "#1 status=done" in rendered
    assert "cartesia:firstChunk" in rendered
    assert "Streaming Latency" in rendered
    assert "chunk_win=0.18s" in rendered
