"""Reviewer B's intentionally red reproductions of the approved high findings.

These are WIP fix-lane inputs, not a passing characterization suite. All data
is synthetic. Existing tests cover successful delivery and ordinary lifecycle
paths; these cases add partial delivery, cold history, and concurrent ownership
changes. Each assertion expresses the user-visible invariant, not source shape.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from lib import agents, config, db


def _agent(tmp_path, session="repro"):
    aid = agents.create_agent(persona="Repro", voice_id="voice", cwd=str(tmp_path),
                              session=session)
    agents.start_runtime(aid, session)
    agents.bind_backend_session(aid, "synthetic-native")
    return aid


def _voice_config():
    return config.Config(tts_provider="cartesia", tts_fallback="elevenlabs",
                         cartesia_api_key="synthetic", eleven_api_key="synthetic",
                         cartesia_voices={"Repro": "synthetic-cartesia"})


def test_vp01_partial_primary_cannot_prepend_fallback_audio(tmp_path, monkeypatch):
    from lib import tts_worker as worker
    delivered = []
    target = tmp_path / "clip.mp3"

    def primary(**kw):
        kw["out_path"].write_bytes(b"partial-primary")
        kw["on_chunk"](0, b"partial-primary")
        raise worker.CartesiaError("synthetic disconnect after first chunk")

    def fallback(**kw):
        kw["out_path"].write_bytes(b"complete-fallback")
        kw["on_chunk"](0, b"complete-fallback")
        return len(b"complete-fallback")

    monkeypatch.setattr(worker, "cartesia_synthesize", primary)
    monkeypatch.setattr(worker, "synthesize_streaming", fallback)
    try:
        worker._synthesize(cfg=_voice_config(), row={"voice_id": "voice", "text": "synthetic"},
                           agent={"persona": "Repro"}, out_path=target,
                           on_chunk=lambda _i, chunk: delivered.append(chunk), trace_id="repro-vp01")
    except worker.CartesiaError:
        # Failing the clip after partial delivery is coherent; replaying its
        # entire text into an already-started stream is not.
        pass
    assert b"".join(delivered) in (b"partial-primary", b"complete-fallback")


def test_vp02_raw_pcm_honors_configured_provider_fallback(tmp_path, monkeypatch):
    from lib import tts_worker as worker
    called = []

    def primary(**_kw):
        raise worker.CartesiaError("synthetic provider unavailable before audio")

    def fallback(**kw):
        called.append(True)
        kw["out_path"].write_bytes(b"fallback")
        return 8

    monkeypatch.setattr(worker, "cartesia_synthesize_raw_pcm", primary)
    monkeypatch.setattr(worker, "synthesize_streaming", fallback)
    error = None
    try:
        worker._synthesize(cfg=_voice_config(), row={"voice_id": "voice", "text": "synthetic"},
                           agent={"persona": "Repro"}, out_path=tmp_path / "clip.pcm",
                           on_chunk=lambda *_: None, trace_id="repro-vp02",
                           delivery_fields={"delivery": "raw-pcm"})
    except worker.CartesiaError as exc:
        error = exc
    assert called and error is None, "raw-pcm bypassed the configured fallback"


def test_vp03_audio_retention_prunes_expired_pcm_and_sidecar(tmp_path):
    from lib.audio_stream import AudioStream
    from lib.clip_delivery.raw_pcm import RawPcmDelivery
    aid = _agent(tmp_path)
    delivery = RawPcmDelivery()
    session = delivery.begin(audio_dir=tmp_path / "audio", agent=agents.get_by_agent_id(aid),
                             voice_id="voice", session="repro", source="pwa", text_len=9,
                             trace_id="repro-vp03")
    session.target_path.write_bytes(b"\0" * 16)
    session.finalize(total_bytes=16)
    sidecar = session.target_path.with_suffix(".pcm.json")
    assert sidecar.exists()
    for path in (session.target_path, sidecar):
        os.utime(path, (1, 1))
    AudioStream(tmp_path / "audio")._prune_audio()
    assert not session.target_path.exists() and not sidecar.exists()


def test_aut03_clock_jump_does_not_fail_verified_alive_worker(tmp_path):
    from lib import background_jobs
    _agent(tmp_path)
    job = background_jobs.upsert(session="repro", job_id="synthetic-worker", title="Synthetic work",
                                 heartbeat_timeout_ms=600_000, worker_pid=12345,
                                 worker_start_token="synthetic-identity")
    probes = []
    background_jobs.reconcile_stale(job_id=job["job_id"],
        now_ms=job["updated_at"] + 3_600_000,
        process_probe=lambda pid, token: probes.append((pid, token)) or True)
    assert probes == [(12345, "synthetic-identity")]
    assert background_jobs.get(job["job_id"])["status"] == "running"


def test_aut05_heartbeat_cannot_preempt_user_arriving_after_idle_check(tmp_path, monkeypatch):
    from lib import turn_dispatch as td
    from lib.dispatch_adapters import DispatchAdapters
    from test_drain_pause_parity import World
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    try:
        world = World(tmp_path)
        original = agents.is_busy

        def race(aid):
            idle = not original(aid)
            if idle:
                world.send("user-turn")
            return not idle

        monkeypatch.setattr(agents, "is_busy", race)
        adapter = DispatchAdapters(world.service.ctx, lambda _ctx: world.service)
        adapter.heartbeat("mike", "synthetic heartbeat")
        world._collect()
        assert world.started == ["user-turn"]
        assert world.agent in world.provider.live
    finally:
        td.reset_for_tests()


def test_rm02_cold_log_does_not_parse_on_request_thread(tmp_path, monkeypatch):
    import threading
    from lib import backends, conversation, transcript_incremental
    _agent(tmp_path)
    path = tmp_path / "synthetic.jsonl"
    path.write_text("{}\n")
    monkeypatch.setattr(backends, "find_session_jsonl", lambda *_: path)
    request_thread = threading.get_ident()
    parse_threads = []

    def record_import(**_kw):
        parse_threads.append(threading.get_ident())
        return {"rows": 0, "parse_s": 0.0, "store_s": 0.0}

    monkeypatch.setattr(transcript_incremental, "import_transcript", record_import)
    result = conversation.load_conversation(session="repro", background_import=True)
    assert result["conversation_id"] == "synthetic-native"
    assert request_thread not in parse_threads, "cold /log performed transcript import synchronously"


def test_aut01_stop_pauses_goal_despite_concurrent_checkpoint(tmp_path, monkeypatch):
    from lib import goal_ledger, task_plans
    aid = _agent(tmp_path)
    plan = task_plans.create(session="repro", title="Synthetic goal",
        items=[{"id": "step", "title": "Synthetic step"}],
        goal={"criteria": ["Synthetic evidence"], "limits": "No deployment", "enroll": True})
    mutate = task_plans._goal_mutate
    raced = []

    def checkpoint_before_pause(plan_id, *, revision, action, data):
        if not raced:
            raced.append(True)
            mutate(plan_id, revision=revision, action="checkpoint", data={
                "progress": "Synthetic checkpoint", "next_work": "Synthetic next step"})
        return mutate(plan_id, revision=revision, action=action, data=data)

    monkeypatch.setattr(task_plans, "_goal_mutate", checkpoint_before_pause)
    error = None
    with goal_ledger.acting_as("user"):
        try:
            task_plans.pause_recovery_for_agent(aid, "Synthetic user Stop")
        except ValueError as exc:
            error = exc
    current = task_plans.get(plan["plan_id"])
    assert error is None and current["status"] == "paused", "concurrent revision defeats Stop goal pause"
