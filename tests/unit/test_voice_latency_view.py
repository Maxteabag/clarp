"""The voice_latency debug view in telemetry.sqlite: one row per turn with the
exact spoken prompt and the latency milestones used to diagnose slow replies."""
import json

from lib import telemetry


def _ev(con, *, trace, ts, source, event, detail=None):
    con.execute(
        "INSERT INTO diagnostic_events "
        "(ts, ts_iso, source, event, level, trace_id, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (ts, "t", source, event, "info", trace,
         json.dumps(detail) if detail is not None else None),
    )


def test_voice_latency_surfaces_prompt_and_milestones():
    con = telemetry.conn()
    tr = "trace-xyz"
    # Prompt dispatched at t=1000ms; first synth at 14s; playback at 15s; done at 42s.
    _ev(con, trace=tr, ts=1_000,  source="server",      event="send",
        detail={"text": "Sam, what's the weather?"})
    _ev(con, trace=tr, ts=14_000, source="tts_worker",  event="synthOk")
    _ev(con, trace=tr, ts=15_000, source="client",      event="clipAck",
        detail={"status": "play-start"})
    _ev(con, trace=tr, ts=42_000, source="stop_hook",   event="done")
    con.commit()

    row = con.execute(
        "SELECT prompt, first_speak_s, first_play_s, done_s "
        "FROM voice_latency WHERE trace_id = ?", (tr,)
    ).fetchone()

    assert row["prompt"] == "Sam, what's the weather?"
    assert row["first_speak_s"] == 13.0
    assert row["first_play_s"] == 14.0
    assert row["done_s"] == 41.0


def test_voice_latency_null_milestones_when_silent():
    """A turn that never produced audio still appears, with NULL latencies —
    exactly the 'agent went quiet' case we want to spot."""
    con = telemetry.conn()
    tr = "trace-silent"
    _ev(con, trace=tr, ts=5_000, source="server", event="send",
        detail={"text": "are you there?"})
    con.commit()

    row = con.execute(
        "SELECT prompt, first_speak_s, done_s FROM voice_latency WHERE trace_id = ?",
        (tr,)
    ).fetchone()
    assert row["prompt"] == "are you there?"
    assert row["first_speak_s"] is None
    assert row["done_s"] is None
