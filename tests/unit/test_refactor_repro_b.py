"""Reviewer B's intentionally red reproductions of the approved high findings.

These are WIP fix-lane inputs, not a passing characterization suite. All data
is synthetic. Existing tests cover successful delivery and ordinary lifecycle
paths; these cases add partial delivery, cold history, and concurrent ownership
changes. Each assertion expresses the user-visible invariant, not source shape.
"""
from __future__ import annotations

import os
import io
import json
import pathlib
import threading
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


def test_http01_limited_device_cannot_reach_interactive_terminal(tmp_path, monkeypatch):
    import server as host
    from lib import terminal_ws
    _agent(tmp_path)
    handler = host.Handler.__new__(host.Handler)
    handler.path = "/terminal/repro"
    handler._request_device_scope = "limited"
    handler._request_auth_validated = True
    handler._request_principal = "synthetic-device"
    handler._authorized = lambda: True
    denied, reached = [], []
    handler._json_error = lambda status, _message: denied.append(status)
    monkeypatch.setattr(terminal_ws, "serve_terminal", lambda *_: reached.append(True))
    handler.do_GET()
    assert denied == [403] and not reached, "limited device reached the interactive terminal"


def test_rm01_appending_one_record_does_not_rescan_unchanged_prefix(tmp_path, monkeypatch):
    from lib import transcript_incremental as importer
    aid = _agent(tmp_path)
    path = tmp_path / "synthetic.jsonl"
    def record(i):
        return json.dumps({"type": "user", "uuid": f"synthetic-{i}",
            "timestamp": f"2026-01-01T00:00:{i % 60:02d}Z",
            "message": {"role": "user", "content": f"synthetic-{i}"}}) + "\n"
    path.write_text("".join(record(i) for i in range(256)))
    monkeypatch.setenv("CLARP_TRANSCRIPT_SHADOW", "0")
    open_path = pathlib.Path.open
    scanned = []

    class CountingReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __iter__(self):
            for line in self.stream:
                scanned.append(len(line.encode() if isinstance(line, str) else line))
                yield line
        def __getattr__(self, name):
            return getattr(self.stream, name)

    def counted_open(self, *args, **kwargs):
        stream = open_path(self, *args, **kwargs)
        if self == path and (not args or "r" in str(args[0])):
            return CountingReader(stream)
        return stream

    monkeypatch.setattr(pathlib.Path, "open", counted_open)
    importer.import_transcript(backend="claude", path=path, agent_id=aid,
                               backend_session_id="synthetic-native")
    scanned.clear()
    tail = record(256)
    with open_path(path, "a") as stream:
        stream.write(tail)
    importer.import_transcript(backend="claude", path=path, agent_id=aid,
                               backend_session_id="synthetic-native")
    assert sum(scanned) <= len(tail.encode()) + 4096, "append rescanned the unchanged transcript prefix"


def test_rto01_early_stable_oracle_failure_releases_device_claim(tmp_path, monkeypatch):
    from lib import oracle_live_stable as stable, oracle_realtime
    principal = "synthetic-rto01-device"
    monkeypatch.setattr(stable.config, "load", lambda: config.Config(openai_api_key="synthetic"))
    monkeypatch.setattr(stable.oracle_strategy, "contact", lambda *_a, **_k: "")
    monkeypatch.setattr(stable.oracle_voice_context, "load", lambda *_: None)
    monkeypatch.setattr(stable.oracle_memory, "open_thread", lambda *_a, **_k: None)
    upstream = SimpleNamespace(close=lambda: None, send=lambda *_: None)
    monkeypatch.setattr(stable, "_connect_voice", lambda *_a: (upstream, "openai"))
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set())
    monkeypatch.setattr(stable, "AgentTools", lambda *_a: tools)

    reached = []
    def fail_register(_conversation):
        reached.append(True)
        raise OSError("synthetic identity write failure")

    monkeypatch.setattr(stable.oracle_handoffs, "bind", lambda *_: None)
    monkeypatch.setattr(stable.oracle_handoffs, "register", fail_register)
    monkeypatch.setattr(stable.oracle_handoffs, "unregister", lambda *_: None)
    handler = SimpleNamespace(headers={"Upgrade": "websocket", "Connection": "Upgrade",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}, path="/oracle/v2?handoff=hands_free",
        ctx=SimpleNamespace(), wfile=io.BytesIO(), connection=SimpleNamespace(settimeout=lambda *_: None),
        _request_auth_validated=True, _request_device_scope="full", _request_principal=principal,
        _stop_agent_session=lambda *_a, **_k: (0, lambda *_: None))
    error = None
    try:
        try:
            stable.serve(handler)
        except Exception as exc:
            error = exc
        assert reached, "probe did not reach the handoff registration failure"
        assert principal not in oracle_realtime._ACTIVE_PRINCIPALS, (
            f"early Oracle failure leaked voice claim; cleanup error={type(error).__name__}")
    finally:
        token = oracle_realtime._ACTIVE_PRINCIPALS.get(principal)
        if token:
            stable.release_connection(principal, token)


def test_rto02_herald_grant_preserves_following_user_instruction(tmp_path, monkeypatch):
    from lib import transcription_pipeline as pipeline, stt_providers
    from lib.herald import HeraldManager
    from test_transcription_pipeline import StubSTT, Recorder, _ctx, _run
    monkeypatch.setattr(stt_providers, "selected_engine", lambda: "server-default")
    monkeypatch.setattr(stt_providers, "is_cloud_model", lambda _engine: False)
    monkeypatch.setattr(stt_providers, "long_form_model_for", lambda _seconds: "")
    monkeypatch.setattr(pipeline, "_cache_dir", lambda: tmp_path / "cache")
    herald = HeraldManager(stream=SimpleNamespace(broadcast=lambda *_: None),
        tts=SimpleNamespace(), agents=lambda: {"rachel": {"name": "Rachel"}})
    herald._pending.add("rachel")
    out = _run(_ctx(StubSTT(text="OK Rachel, rerun the failing tests"), herald=herald), Recorder())
    assert out.status == 200
    assert "rerun the failing tests" in out.payload["text"], "herald grant swallowed the following instruction"


def test_rto03_low_confidence_router_cannot_stop_agent(tmp_path, monkeypatch):
    from lib import backends
    from lib.orchestrator import OrchestratorService
    from test_orchestrator import _ctx
    aid = _agent(tmp_path, "mike")
    interrupted = []
    monkeypatch.setattr(backends, "interrupt_any", lambda target: interrupted.append(target))
    service = OrchestratorService(_ctx(tmp_path), model_call=lambda *_: {
        "kind": "agent_control", "control_action": "stop", "target_session": "mike",
        "confidence": 0.3, "addressing": False, "reason": "Synthetic ambiguous input"})
    outcome = service.handle_send(text="synthetic ambiguous request", requested_session="mike",
        trace_id="repro-rto03", hands_free=True, synthesize_audio=False,
        dispatch=lambda **_: pytest.fail("ambiguous control must not dispatch work"))
    assert outcome is not None
    assert not interrupted, f"low confidence router stopped agent {aid}"


def test_aut04_computer_job_cancel_does_not_dispatch_to_default_agent(tmp_path, monkeypatch):
    import server as host
    from lib import background_jobs, turn_dispatch as td
    from test_drain_pause_parity import World
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    try:
        world = World(tmp_path)
        job = background_jobs.upsert_computer(computer_id="synthetic-computer", job_id="synthetic-install",
                                              title="Synthetic model install")
        handler = host.Handler.__new__(host.Handler)
        handler.server = SimpleNamespace(ctx=world.service.ctx)
        handler._expected_generation = lambda: (True, job["generation"])
        handler._json_ok = lambda payload: payload
        monkeypatch.setattr(host, "TurnDispatchService", lambda _ctx: world.service)
        receipt = handler._handle_background_job_cancel(job["job_id"])
        assert receipt["job"]["status"] == "cancelled"
        world._collect()
        assert world.started == [], "computer-owned cancellation was dispatched to the default agent"
    finally:
        td.reset_for_tests()


def test_aut06_repeated_backend_failure_does_not_retry_dream_forever(tmp_path, monkeypatch):
    from lib import dreaming
    aid = _agent(tmp_path)
    agents.update_agent(aid, dreaming_enabled=True)
    agent = agents.get_by_agent_id(aid)
    run = dreaming.create_dream_run(agent, local_date="2026-01-01", timezone_name="UTC",
                                    timezone_source="synthetic")
    first = dreaming.list_dream_runs(session="repro")[0]["rounds"][0]
    callbacks = {}
    monkeypatch.setattr(dreaming, "_dream_scratch_cwd", lambda *_: tmp_path)
    monkeypatch.setattr(dreaming.backends, "spawn_turn", lambda _backend, **kw: callbacks.update(kw))
    monkeypatch.setattr(dreaming.backends, "active_handles", lambda *_: [])
    clock = [db.now_ms()]
    monkeypatch.setattr(dreaming.db, "now_ms", lambda: clock[0])
    scheduler = dreaming.DreamingScheduler(send_dream=lambda _session, prompt:
        dreaming.dispatch_isolated_dream(agent, prompt), now=lambda: clock[0] / 1000)
    attempts = 0
    # More than a month of daily identical failures must not reserve a run
    # forever. No real wait, provider, or worker is involved.
    for _ in range(32):
        current = dreaming.active_run_for_agent(aid)
        if current is None:
            break
        attempts += scheduler._advance_run(current)
        callbacks["on_error"]("synthetic permanent backend rejection")
        clock[0] += 86_400_000
    assert dreaming.active_run_for_agent(aid) is None, (
        f"failed dream {run['run_id']} remains active after {attempts} failed daily attempts")
