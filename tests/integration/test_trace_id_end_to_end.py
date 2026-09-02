"""Trace ID propagates through every layer of one turn.

Single highest-leverage cross-cutting assertion: a trace_id minted up-front
shows up in the turn row, the trace marker, the sidecar JSON, the clip row,
the eventlog, and the SSE-broadcastable identity carried out to the client.
If this test ever fails, *exactly one* layer dropped the trace — find it,
fix it.

This test would have been impossible to write before the SQLite + sidecar +
shared-protocol refactor because there was no row to assert against.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import secrets

import pytest

from .fake_claude import FakeClaude


@pytest.fixture
def home(tmp_path) -> pathlib.Path:
    h = tmp_path / "home"; h.mkdir()
    return h


def _eventlog_dir() -> pathlib.Path:
    """conftest steers eventlog output to a per-test directory via
    CLAUDE_PWA_LOG_DIR — read it back here so tests stay agnostic to the
    exact filesystem layout."""
    import os
    return pathlib.Path(os.environ.get("CLAUDE_PWA_LOG_DIR", "/tmp"))


def _events_with_trace(
    trace_id: str, directory: pathlib.Path | None = None,
) -> list[dict]:
    d = directory or _eventlog_dir()
    if not d.is_dir():
        return []
    out = []
    for f in d.glob("*.jsonl"):
        for line in f.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("trace_id") == trace_id:
                out.append(row)
    return out


def test_trace_id_propagates_through_every_layer(home):
    """One trace_id, one turn — verify the audit trail is unbroken.

    The /transcribe endpoint normally mints the trace_id and writes it into
    the per-session source marker; we short-circuit that here by passing the
    trace_id straight into the harness, which writes the same marker
    format. The downstream pipeline (UserPromptSubmit hook → DB → tts_queue
    → worker → clip sidecar → eventlog) has no idea where the trace_id came
    from — it just sees the marker.
    """
    trace_id = "trace_" + secrets.token_hex(6)

    with FakeClaude(home=home, session="rachel",
                    persona="Rachel", voice_id="v_rachel") as agent:
        with sqlite3.connect(
                str(home / ".local/share/clarp/state.sqlite")) as settings_db:
            settings_db.execute(
                """INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                ("diagnostics.capture.v1",
                 '{"enabled":true,"categories":["agents","voice"]}', 1),
            )
        agent.user_prompt("test prompt", source="pwa", trace_id=trace_id)
        agent.assistant_text("Sure thing, User.")
        r = agent.stop()
        assert r.returncode == 0, r.stderr
        agent.speak_now("Sure thing, User.")

        # ---- Layer 1: the `turns` row carries the trace_id ----
        turns = sqlite3.connect(
            str(home / ".local/share/clarp/state.sqlite")
        ).execute(
            "SELECT trace_id, source FROM turns ORDER BY started_at DESC"
        ).fetchall()
        assert turns, "no turn was opened"
        assert turns[0][0] == trace_id, (
            f"UserPromptSubmit hook should have opened a turn carrying the "
            f"trace_id from the source marker, got {turns[0][0]!r}"
        )
        assert turns[0][1] == "pwa"

        # ---- Layer 2: the `traces` table has the agent → trace mapping ----
        traces = sqlite3.connect(
            str(home / ".local/share/clarp/state.sqlite")
        ).execute("SELECT trace_id FROM traces").fetchall()
        assert any(row[0] == trace_id for row in traces), (
            "UserPromptSubmit should have stamped traces.trace_id for the "
            "agent so later layers (TTS, /send, /clog) can pick it up"
        )

        # ---- Layer 3: clip sidecar carries the trace_id ----
        clips = agent.clips_on_disk()
        assert len(clips) == 1, f"expected one clip, got {[c.name for c in clips]}"
        sidecar = clips[0].with_suffix(clips[0].suffix + ".json")
        meta = json.loads(sidecar.read_text())
        assert meta.get("trace_id") == trace_id, (
            f"clip sidecar should carry the trace_id end-to-end, "
            f"got {meta.get('trace_id')!r}"
        )

        # ---- Layer 4: clips DB row carries the trace_id ----
        clip_rows = sqlite3.connect(
            str(home / ".local/share/clarp/state.sqlite")
        ).execute("SELECT trace_id, path FROM clips").fetchall()
        assert clip_rows, "the TTS worker should have inserted a clips row"
        assert clip_rows[0][0] == trace_id, (
            f"clips.trace_id should equal {trace_id!r}, got {clip_rows[0][0]!r}"
        )
        assert clip_rows[0][1].endswith(clips[0].name)

        # ---- Layer 5: the eventlog has rows tagged with this trace_id ----
        events = _events_with_trace(
            trace_id, home / ".cache/clarp/logs")
        sources = {e.get("source") for e in events}
        assert events, (
            "no eventlog rows carry this trace_id — the audit trail broke "
            "somewhere"
        )
        # The Stop hook fires AFTER set_trace in pwa_source_flag, so by then
        # eventlog.emit() auto-resolves the trace from the DB. Pin it.
        assert "stop_hook" in sources, (
            f"Stop hook should have emitted under this trace; sources: {sources}"
        )


def test_trace_id_does_not_bleed_between_concurrent_agents(home):
    """Two agents, each with their own trace_id — neither should pick up
    the other's. This pins the bug class that's most likely after a sloppy
    refactor: marker files or trace state shared across agents.
    """
    trace_rachel = "trace_" + secrets.token_hex(6)
    trace_antoni = "trace_" + secrets.token_hex(6)

    with FakeClaude(home=home, session="rachel",
                    persona="Rachel", voice_id="v_r") as rachel:
        with FakeClaude(home=home, session="antoni",
                        persona="Antoni", voice_id="v_a") as antoni:
            rachel.user_prompt("rq", source="pwa", trace_id=trace_rachel)
            rachel.assistant_text("Rachel replying.")
            rachel.stop()

            antoni.user_prompt("aq", source="pwa", trace_id=trace_antoni)
            antoni.assistant_text("Antoni replying.")
            antoni.stop()

            # Each clip's trace must match its own agent. No cross-contamination.
            con = sqlite3.connect(
                str(home / ".local/share/clarp/state.sqlite"))
            rows = con.execute(
                """SELECT a.session, c.trace_id
                     FROM clips c
                     JOIN agents a ON a.agent_id = c.agent_id"""
            ).fetchall()
            for session, trace_id in rows:
                if session == "rachel":
                    assert trace_id == trace_rachel, (
                        f"rachel's clip got trace {trace_id!r}, "
                        f"expected {trace_rachel!r}"
                    )
                elif session == "antoni":
                    assert trace_id == trace_antoni, (
                        f"antoni's clip got trace {trace_id!r}, "
                        f"expected {trace_antoni!r}"
                    )
