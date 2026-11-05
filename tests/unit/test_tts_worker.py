"""Unit tests for the TTS worker.

The worker is the executor side of the Phase A split: hooks enqueue,
worker drains. These tests exercise the drain path directly with the
FakeTTSEngine pattern that test_server_di uses, so no ElevenLabs HTTP
goes out.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db                 # noqa: E402
from lib import tts_queue                            # noqa: E402
from lib.protocol import ClipStatus, TurnSource  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A registered agent + a HOME-aware audio dir."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    from lib import config
    monkeypatch.setattr(
        config, "_CACHED",
        config.Config(tts_provider="elevenlabs", eleven_api_key="test-key"))

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V_MIKE",
        cwd=str(tmp_path), session="claude",
    )
    return {
        "tmp_path": tmp_path,
        "audio_dir": audio_dir,
        "agent_id": agent_id,
    }


@pytest.fixture
def patch_eleven(monkeypatch):
    from lib import tts_worker
    # PWA-mode rows route through eleven_ws.synthesize_streaming; patch its
    # imported binding in tts_worker so no real WS opens.
    def _fake_ws(*, text, voice_id, out_path, api_key, model,
                 speed=1.2, stability=0.5, similarity_boost=0.75,
                 timeout=30.0, on_chunk=None, **_kw):
        import pathlib
        pathlib.Path(out_path).write_bytes(b"\xff\xfb\x90\x00")
        return 4
    monkeypatch.setattr(tts_worker, "synthesize_streaming", _fake_ws)


def test_enqueue_then_drain_lands_clip_on_disk(env, patch_eleven):
    qid = tts_queue.enqueue(
        agent_id=env["agent_id"],
        text="hello",
        voice_id="V_MIKE",
        session="claude",
        source=TurnSource.PWA,
        trace_id="trace-1",
    )
    assert qid > 0
    assert tts_queue.pending_count() == 1

    from lib.tts_worker import synth_one
    assert synth_one(audio_dir=env["audio_dir"]) is True

    # Queue row should be done now.
    assert tts_queue.pending_count() == 0
    assert tts_queue.in_flight_count() == 0
    recent = tts_queue.recent(limit=1)
    assert recent[0]["status"] == tts_queue.DONE
    assert recent[0]["error"] is None

    # And an mp3 should be on disk.
    clips = list(env["audio_dir"].glob("*__claude.mp3"))
    assert len(clips) == 1, [c.name for c in clips]

    # …with a sidecar carrying the trace_id we enqueued.
    import json
    sidecar = clips[0].with_suffix(clips[0].suffix + ".json")
    meta = json.loads(sidecar.read_text())
    assert meta["trace_id"] == "trace-1"
    assert meta["agent_id"] == env["agent_id"]
    assert meta["voice_id"] == "V_MIKE"


def test_drain_when_queue_empty_returns_false(env, patch_eleven):
    from lib.tts_worker import synth_one
    assert synth_one(audio_dir=env["audio_dir"]) is False


def test_claim_next_is_atomic_across_calls(env, patch_eleven):
    """Two consecutive claims must not return the same row."""
    qid1 = tts_queue.enqueue(
        agent_id=env["agent_id"], text="one", voice_id="V_MIKE",
        session="claude", source=TurnSource.PWA,
    )
    qid2 = tts_queue.enqueue(
        agent_id=env["agent_id"], text="two", voice_id="V_MIKE",
        session="claude", source=TurnSource.PWA,
    )
    r1 = tts_queue.claim_next()
    r2 = tts_queue.claim_next()
    assert r1 is not None and r2 is not None
    assert {r1["queue_id"], r2["queue_id"]} == {qid1, qid2}
    assert r1["queue_id"] != r2["queue_id"]
    # Third claim has nothing left.
    assert tts_queue.claim_next() is None


def test_reset_in_flight_recovers_stuck_rows(env, patch_eleven):
    """If the worker dies mid-claim, the row should re-queue on next start."""
    qid = tts_queue.enqueue(
        agent_id=env["agent_id"], text="hello", voice_id="V_MIKE",
        session="claude", source=TurnSource.PWA,
    )
    r = tts_queue.claim_next()
    assert r is not None and r["queue_id"] == qid
    assert tts_queue.in_flight_count() == 1

    # Simulate a worker crash by NOT marking done/failed. Now restart.
    n = tts_queue.reset_in_flight()
    assert n == 1
    assert tts_queue.in_flight_count() == 0
    assert tts_queue.pending_count() == 1
    # And it's claimable again.
    r2 = tts_queue.claim_next()
    assert r2 is not None and r2["queue_id"] == qid


def test_silent_turn_is_suppressed_before_it_reaches_worker(env):
    assert tts_queue.enqueue(
        agent_id=env["agent_id"], text="silent", voice_id="V_MIKE",
        session="claude", source=TurnSource.PWA,
        synthesize_audio=False,
    ) == 0
    assert tts_queue.pending_count() == 0


def test_cartesia_fallback_is_used_only_when_explicit(env, monkeypatch):
    from lib import config, tts_worker
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="cartesia", tts_fallback="elevenlabs",
        cartesia_api_key="cartesia-key", eleven_api_key="eleven-key",
        cartesia_voices={"Mike": "cartesia-voice"}))
    monkeypatch.setattr(
        tts_worker, "cartesia_synthesize",
        lambda **_kwargs: (_ for _ in ()).throw(
            tts_worker.CartesiaError("outage")))

    def eleven(**kwargs):
        pathlib.Path(kwargs["out_path"]).write_bytes(b"mp3")
        return 3

    monkeypatch.setattr(tts_worker, "synthesize_streaming", eleven)
    tts_queue.enqueue(
        agent_id=env["agent_id"], text="Fallback",
        voice_id="eleven-voice", session="claude", source=TurnSource.PWA)
    assert tts_worker.synth_one(audio_dir=env["audio_dir"]) is True
    assert tts_queue.recent(limit=1)[0]["status"] == tts_queue.DONE


def test_a_restart_fails_stale_in_flight_speech_instead_of_speaking_it_again(env, patch_eleven):
    """VP-05: a restart during synthesis re-spoke the reply, however old, and
    left its clip row streaming for ever."""
    from lib import db, tts_worker
    from lib.protocol import ClipProducerStatus

    stale = tts_queue.enqueue(agent_id=env["agent_id"], text="old answer", voice_id="V_MIKE",
                              session="claude", source=TurnSource.PWA)
    fresh = tts_queue.enqueue(agent_id=env["agent_id"], text="new answer", voice_id="V_MIKE",
                              session="claude", source=TurnSource.PWA)
    assert tts_queue.claim_next()["queue_id"] == stale
    assert tts_queue.claim_next()["queue_id"] == fresh
    now = db.now_ms()
    db.conn().execute("UPDATE tts_queue SET claimed_at = ? WHERE queue_id = ?",
                      (now - 2 * 60 * 60 * 1000, stale))
    clip = agents_db.record_clip(agent_id=env["agent_id"], path=str(env["audio_dir"] / "dead.pcm"),
                       voice_id="V_MIKE", trace_id="t-dead",
                       producer_status=ClipProducerStatus.STREAMING)

    tts_worker.recover_in_flight(now_ms=now)

    rows = {r["queue_id"]: r["status"] for r in db.conn().execute(
        "SELECT queue_id, status FROM tts_queue")}
    assert rows == {stale: tts_queue.FAILED, fresh: tts_queue.QUEUED}
    status = db.conn().execute(
        "SELECT producer_status FROM clips WHERE clip_id = ?", (clip,)).fetchone()[0]
    assert status == ClipProducerStatus.FAILED


def _fallback_clip(env, monkeypatch, *, primary_reads):
    """One Cartesia->ElevenLabs clip through _synthesize, with the real
    Cartesia adapter reading `primary_reads` from an in-memory response
    (an exception in it is the upstream drop) and providers calling the
    live sink as on_chunk(index, bytes)."""
    from lib import cartesia_tts, config, tts_worker
    cfg = config.Config(
        tts_provider="cartesia", tts_fallback="elevenlabs",
        cartesia_api_key="cartesia-key", eleven_api_key="eleven-key",
        cartesia_voices={"Mike": "cartesia-voice"})

    class Response:
        reads = iter(primary_reads)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, _size):
            step = next(self.reads, b"")
            if isinstance(step, Exception):
                raise step
            return step

    monkeypatch.setattr(cartesia_tts.urllib.request, "urlopen",
                        lambda *_a, **_kw: Response())
    fallback_ran = []

    def eleven(*, on_chunk, out_path, **_kw):
        fallback_ran.append(True)
        for index, chunk in enumerate((b"ELEVEN-FULL-1", b"ELEVEN-FULL-2")):
            try:  # eleven_ws isolates callback errors the same way
                on_chunk(index, chunk)
            except Exception:  # noqa: BLE001
                pass
        pathlib.Path(out_path).write_bytes(b"ELEVEN-FULL-1ELEVEN-FULL-2")
        return 26

    monkeypatch.setattr(tts_worker, "synthesize_streaming", eleven)
    live: list[tuple[int, bytes]] = []
    row = {"voice_id": "eleven-voice", "text": "Sure, I'll check the logs",
           "agent_id": env["agent_id"], "session": "claude"}
    run = lambda: tts_worker._synthesize(  # noqa: E731
        cfg=cfg, row=row, agent={"persona": "Mike"},
        out_path=env["audio_dir"] / "clip.mp3",
        on_chunk=lambda index, chunk: live.append((index, chunk)),
        trace_id=None)
    return run, live, fallback_ran


def test_fallback_after_audio_reached_the_stream_fails_the_clip(env, monkeypatch):
    """VP-01: once the primary fed the live stream, a fallback would play a
    second full clip after the partial one; the clip fails instead."""
    from lib import tts_worker
    run, live, fallback_ran = _fallback_clip(
        env, monkeypatch, primary_reads=[b"CARTESIA-PART-1", OSError("dropped")])
    emitted = []
    monkeypatch.setattr(tts_worker, "_emit",
                        lambda _src, name, **kw: emitted.append((name, kw)))
    with pytest.raises(tts_worker.CartesiaError):
        run()
    assert live == [(0, b"CARTESIA-PART-1")]
    assert not fallback_ran
    skipped = [kw for name, kw in emitted if name == "providerFallbackSkipped"]
    assert len(skipped) == 1 and skipped[0]["level"] == "warn"
    assert skipped[0]["detail"]["fallback"] == "elevenlabs"


def test_fallback_before_any_audio_still_speaks(env, monkeypatch):
    run, live, fallback_ran = _fallback_clip(
        env, monkeypatch, primary_reads=[OSError("refused")])
    assert run() == 26
    assert live == [(0, b"ELEVEN-FULL-1"), (1, b"ELEVEN-FULL-2")]
    assert fallback_ran


def test_primary_streams_its_indexed_chunks_to_the_live_sink(env, monkeypatch):
    run, live, fallback_ran = _fallback_clip(
        env, monkeypatch, primary_reads=[b"CARTESIA-1", b"CARTESIA-2"])
    assert run() == 20
    assert live == [(0, b"CARTESIA-1"), (1, b"CARTESIA-2")]
    assert not fallback_ran



def _fallback_wav(seconds=.1):
    import io
    import math
    import struct
    import wave
    output = io.BytesIO()
    samples = b''.join(struct.pack('<hh', value, value) for value in (
        int(10000 * math.sin(2 * math.pi * 1000 * frame / 22050))
        for frame in range(int(22050 * seconds))))
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(samples)
    return output.getvalue()


@pytest.mark.parametrize('mode,fallback,outcome', [
    pytest.param('before', 'elevenlabs', 'ok', id='before-f32'),
    ('before', 'elevenlabs', 'provider-error'),
    ('before', 'none', 'ok'),
    ('after', 'elevenlabs', 'ok'),
    ('success', 'elevenlabs', 'ok'),
    ('stall', 'elevenlabs', 'ok'),
    ('late', 'elevenlabs', 'ok'),
    ('near-budget', 'elevenlabs', 'ok'),
    ('before', 'elevenlabs', 'stop-resume'),
    ('before', 'elevenlabs', 'barge'),
    ('before', 'elevenlabs', 'primary-stop'),
    ('success', 'elevenlabs', 'primary-stop'),
    ('before', 'elevenlabs', 'feed-stop'),
    ('before', 'elevenlabs', 's16'),
    ('before', 'elevenlabs', 'decode-error'),
    ('before', 'elevenlabs', 'decode-timeout'),
    ('before', 'elevenlabs', 'decode-stop'),
])
def test_raw_pcm_outage_restarts_only_before_audio(env, monkeypatch, mode, fallback, outcome):
    """641: one early raw event/session; fallback is decoded to its exact PCM."""
    import base64
    import json
    import math
    import struct
    import subprocess
    import types
    from lib import cartesia_ws, config, db, tts_worker, turn_queue
    from lib.clip_delivery.raw_pcm import RawPcmDelivery
    from lib.clip_stream import ClipStreamBroker

    encoding, rate = ('pcm_s16le', 24000) if outcome == 's16' else ('pcm_f32le', 44100)
    monkeypatch.setattr(config, '_CACHED', config.Config(
        tts_provider='cartesia', tts_fallback=fallback,
        cartesia_api_key='cartesia-key', eleven_api_key='eleven-key',
        cartesia_voices={'Mike': 'cartesia-voice'},
        raw_pcm_encoding=encoding, raw_pcm_sample_rate=rate))
    clock = [0.0]
    if mode in ('stall', 'late', 'near-budget'):
        monkeypatch.setattr(cartesia_ws, 'time', types.SimpleNamespace(perf_counter=lambda: clock[0]))
    primary_audio = b'PCM-FIRST'
    frames = []
    if mode in ('after', 'success', 'late', 'near-budget'):
        frames.append(json.dumps({'type': 'chunk',
                                  'data': base64.b64encode(primary_audio).decode()}))
    frames.append(json.dumps({'type': 'done'}) if mode in ('success', 'near-budget', 'late')
                  else OSError('Cartesia outage'))
    sent, backup_calls, recorded, emitted, timeouts = [], [], [], [], []

    def interrupt():
        if outcome == 'barge':
            agents_db.set_trace(env['agent_id'], 'new-user-turn')
        else:
            turn_queue.set_paused(env['agent_id'], True, user_stop=True)
            turn_queue.set_paused(env['agent_id'], False)

    monkeypatch.setattr(tts_worker, '_emit',
                        lambda _src, name, **kw: emitted.append((name, kw)))

    class Socket:
        closed = False
        timeout = 30.0
        def settimeout(self, timeout):
            self.timeout = timeout
        def send(self, raw):
            # Announcement stays BEFORE Cartesia warms up, including success.
            assert len([e for e in recorded if e['type'] == 'audio']) == 1
            sent.append(json.loads(raw))
        def recv(self):
            if mode in ('stall', 'late', 'near-budget') and clock[0] == 0:
                clock[0] = self.timeout if mode == 'stall' else (12.0 if mode == 'late' else 9.999)
                if mode == 'stall':
                    raise cartesia_ws.websocket.WebSocketTimeoutException('first byte stalled')
            if outcome == 'primary-stop' and len(frames) == (2 if mode == 'success' else 1):
                interrupt()
            frame = frames.pop(0)
            if isinstance(frame, Exception):
                raise frame
            return frame
        def close(self):
            self.closed = True
        def shutdown(self):
            self.closed = True

    socket = Socket()
    monkeypatch.setattr(cartesia_ws, '_open_ws', lambda *_, **__: socket)
    monkeypatch.setattr(tts_worker, 'cartesia_synthesize',
                        lambda **_: pytest.fail('failed Cartesia must not be retried as MP3'))

    def backup(*, text, voice_id, out_path, on_chunk, **_):
        backup_calls.append((text, voice_id))
        assert on_chunk is None, 'compressed fallback bytes must never enter the raw broker'
        if outcome == 'provider-error':
            raise tts_worker.ElevenWSError('backup outage')
        data = b'not audio' if outcome == 'decode-error' else _fallback_wav(2 if outcome == 'feed-stop' else .1)
        pathlib.Path(out_path).write_bytes(data)
        if outcome in ('stop-resume', 'barge'):
            interrupt()
        return len(data)

    monkeypatch.setattr(tts_worker, 'synthesize_streaming', backup)
    if outcome in ('decode-timeout', 'decode-stop'):
        run = subprocess.run
        def bounded_timeout(command, *args, **kwargs):
            if pathlib.Path(command[0]).name == 'ffmpeg':
                timeouts.append(kwargs.get('timeout'))
                assert 0 < kwargs['timeout'] <= 20
                if outcome == 'decode-timeout':
                    raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                result = run(command, *args, **kwargs)
                interrupt()
                return result
            return run(command, *args, **kwargs)
        monkeypatch.setattr(subprocess, 'run', bounded_timeout)

    class Stream:
        def broadcast(self, event):
            recorded.append(event)

    broker = ClipStreamBroker()
    append = broker.append
    def append_then_stop(clip_id, chunk):
        if mode in ('stall', 'late', 'near-budget'):
            if clock[0] >= 30:
                # Advance the actual writer from its initial empty wait to
                # the virtual deadline. The broker then discards late PCM.
                import io
                from lib import clip_stream
                live = broker.get(clip_id)
                times = iter((0.0, clock[0]))
                with monkeypatch.context() as patch:
                    patch.setattr(clip_stream, 'time', types.SimpleNamespace(time=lambda: next(times)))
                    clip_stream._write_live_chunks(types.SimpleNamespace(wfile=io.BytesIO()), live)
                assert live.failed and live.error == 'stream wait timeout'
        append(clip_id, chunk)
        if outcome == 'feed-stop':
            interrupt()
    monkeypatch.setattr(broker, 'append', append_then_stop)
    qid = tts_queue.enqueue(agent_id=env['agent_id'], text='Fallback speech.',
                            voice_id='eleven-voice', session='claude',
                            source=TurnSource.PWA, trace_id='vp02b-trace')
    assert tts_worker.synth_one(audio_dir=env['audio_dir'], broker=broker,
                                delivery=RawPcmDelivery(broker=broker, encoding=encoding, sample_rate=rate),
                                stream=Stream())
    queue = tts_queue.recent(limit=1)[0]
    clips = [dict(r) for r in db.conn().execute(
        'SELECT clip_id,path,producer_status FROM clips ORDER BY clip_id')]
    should_fallback = mode in ('before', 'stall', 'late') and fallback == 'elevenlabs' and outcome != 'primary-stop'
    assert len(backup_calls) == int(should_fallback)
    assert socket.closed and len(sent) == 1
    assert sent[0]['voice']['id'] == 'cartesia-voice'
    assert sent[0]['output_format'] == {'container': 'raw', 'encoding': encoding, 'sample_rate': rate}
    assert len(clips) == 1, 'fallback must retain the original clip identity'
    raw = clips[0]
    raw_stream = broker.get(raw['clip_id'])
    audio = [e for e in recorded if e['type'] == 'audio']
    assert len(audio) == 1 and audio[0]['delivery'] == 'raw-pcm'
    assert audio[0]['clip_id'] == raw['clip_id'] and audio[0]['trace_id'] == 'vp02b-trace'
    assert audio[0]['audio_format'] == {
        'container': 'raw', 'encoding': encoding, 'sample_rate': rate, 'channels': 1}
    cancelled = outcome in ('stop-resume', 'barge', 'primary-stop', 'feed-stop', 'decode-stop')
    succeeded = (mode in ('success', 'near-budget') or should_fallback and outcome in ('ok', 's16')) and not cancelled
    if succeeded:
        assert queue['status'] == tts_queue.DONE and queue['clip_id'] == raw['clip_id']
        assert raw['producer_status'] == 'complete' and raw_stream.finished
        data = pathlib.Path(raw['path']).read_bytes()
        assert b''.join(raw_stream.chunks) == data
        if mode in ('success', 'near-budget'):
            if mode == 'near-budget':
                assert clock[0] == 9.999
            assert data == primary_audio
        else:
            assert backup_calls == [('Fallback speech.', 'eleven-voice')]
            sample_bytes, code = (2, '<h') if encoding == 'pcm_s16le' else (4, '<f')
            assert len(data) == rate // 10 * sample_bytes, 'decoded rate/channel/sample width differs from audio_format'
            values = [sample[0] for sample in struct.iter_unpack(code, data)]
            assert all(math.isfinite(value) for value in values)
            peak = max(abs(value) for value in values)
            assert (9000 < peak < 11000) if sample_bytes == 2 else (.25 < peak < .5), peak
            crossings = sum((a < 0) != (b < 0) for a, b in zip(values, values[1:]))
            assert 195 <= crossings <= 205, 'decoded tone rate differs from audio_format'
        assert not [e for e in recorded if e['type'] == 'tts-error']
    else:
        assert queue['status'] == tts_queue.FAILED
        assert raw['producer_status'] == 'failed' and raw_stream.failed
        assert not pathlib.Path(raw['path']).exists()
        assert not pathlib.Path(raw['path'] + '.json').exists()
        expected = [primary_audio] if mode == 'after' else []
        if outcome == 'feed-stop':
            assert len(raw_stream.chunks) == 1, 'bytes continued after Stop during PCM feeding'
        else:
            assert raw_stream.chunks == expected
        errors = [e for e in recorded if e['type'] == 'tts-error']
        if cancelled:
            assert queue['error'].startswith(tts_queue.CANCELLED_PREFIX)
            assert errors == []
        else:
            assert len(errors) == 1
            if outcome.startswith('decode-'):
                assert 'decode' in queue['error'].lower()
    if outcome in ('decode-timeout', 'decode-stop'):
        assert len(timeouts) == 1
    transitions = [kw['detail'] for name, kw in emitted if name == 'providerFallback']
    assert len(transitions) == int(should_fallback)
    if transitions:
        assert transitions[0]['provider'] == 'cartesia' and transitions[0]['fallback'] == 'elevenlabs'
        assert transitions[0]['error_code'] == 'cartesia_raw_before_audio'
        assert 'Fallback speech.' not in json.dumps(transitions[0])
    assert qid == queue['queue_id']

def test_a_queued_utterance_cannot_overtake_its_raw_fallback(env, monkeypatch):
    import threading
    from lib import config, tts_worker
    from lib.clip_delivery.raw_pcm import RawPcmDelivery
    from lib.clip_stream import ClipStreamBroker
    entered, release, blocked, done = (threading.Event() for _ in range(4))
    calls = []
    monkeypatch.setattr(config, '_CACHED', config.Config(
        tts_provider='cartesia', tts_fallback='elevenlabs', cartesia_api_key='cartesia-key',
        eleven_api_key='eleven-key', cartesia_voices={'Mike': 'cartesia-voice'}))
    def raw(**_):
        raise tts_worker.CartesiaError('outage')
    monkeypatch.setattr(tts_worker, 'cartesia_synthesize_raw_pcm', raw)
    def backup(*, text, out_path, on_chunk, **_):
        calls.append(text)
        if text == 'first':
            blocked.clear()
            entered.set()
            assert release.wait(10), 'fallback release timed out'
        data = _fallback_wav()
        pathlib.Path(out_path).write_bytes(data)
        assert on_chunk is None
        return len(data)
    monkeypatch.setattr(tts_worker, 'synthesize_streaming', backup)
    ids = [tts_queue.enqueue(agent_id=env['agent_id'], text=text, voice_id='eleven-voice',
                              session='claude', source=TurnSource.PWA) for text in ('first', 'second')]
    claim, mark_done = tts_queue.claim_next, tts_queue.mark_done
    def observe_claim(skip=frozenset()):
        row = claim(skip)
        if row is None and env['agent_id'] in skip:
            blocked.set()
        return row
    def observe_done(qid, **kwargs):
        mark_done(qid, **kwargs)
        if qid == ids[1]:
            done.set()
    monkeypatch.setattr(tts_queue, 'claim_next', observe_claim)
    monkeypatch.setattr(tts_queue, 'mark_done', observe_done)
    broker = ClipStreamBroker()
    worker = tts_worker.TTSWorker(audio_dir=env['audio_dir'], interval_sec=.01,
                                  broker=broker, delivery=RawPcmDelivery(broker=broker))
    try:
        worker.start()
        assert entered.wait(5), 'first fallback never started'
        assert blocked.wait(5), 'another pool lane never checked the busy agent'
        assert calls == ['first']
        assert tts_queue.recent(limit=2)[0]['status'] == tts_queue.QUEUED
        release.set()
        assert done.wait(10), 'second utterance never completed after fallback'
        assert calls == ['first', 'second']
        assert all(r['status'] == tts_queue.DONE for r in tts_queue.recent(limit=2))
    finally:
        release.set()
        worker.stop(timeout=5)
        assert all(not thread.is_alive() for thread in worker._threads)


@pytest.mark.parametrize('phase', ['before-feed', 'during-feed', 'finalize'])
def test_a_fallback_after_the_empty_stream_deadline_is_not_marked_done(env, monkeypatch, phase):
    import io
    from pathlib import Path
    from types import SimpleNamespace
    from lib import config, db, tts_worker, tts_queue, clip_stream
    from lib.clip_delivery.raw_pcm import RawPcmDelivery
    monkeypatch.setattr(config, '_CACHED', config.Config(
        tts_provider='cartesia', tts_fallback='elevenlabs',
        cartesia_api_key='primary-key', eleven_api_key='backup-key',
        cartesia_voices={'Mike':'primary-voice'}))
    broker = clip_stream.ClipStreamBroker()
    records, emitted = [], []
    monkeypatch.setattr(tts_worker, '_emit', lambda source, event, **kwargs: emitted.append((event, kwargs)))
    class Stream:
        def broadcast(self, event):records.append(event)
    def primary(**kwargs):raise tts_worker.CartesiaError('before-byte outage')
    def expire():
        cid = db.conn().execute('SELECT clip_id FROM clips ORDER BY clip_id DESC LIMIT 1').fetchone()[0]
        live = broker.get(cid)
        ticks = iter((0.0, 31.0))
        with monkeypatch.context() as patch:
            patch.setattr(clip_stream, 'time', SimpleNamespace(time=lambda: next(ticks)))
            clip_stream._write_live_chunks(SimpleNamespace(wfile=io.BytesIO()), live)
        assert live.failed and live.error == 'stream wait timeout'
    def backup(*, out_path, on_chunk, **kwargs):
        assert on_chunk is None
        Path(out_path).write_bytes(_fallback_wav(2 if phase == 'during-feed' else .1))
        if phase == 'before-feed':
            expire()
        return Path(out_path).stat().st_size
    if phase == 'during-feed':
        append = broker.append
        def append_then_expire(cid, data):
            append(cid, data)
            if not broker.get(cid).failed:
                expire()
        monkeypatch.setattr(broker, 'append', append_then_expire)
    if phase == 'finalize':
        from lib.clip_delivery.raw_pcm import RawPcmSession
        finalize = RawPcmSession.finalize
        def finalize_after_expiry(self, **kwargs):
            expire()
            return finalize(self, **kwargs)
        monkeypatch.setattr(RawPcmSession, 'finalize', finalize_after_expiry)
    monkeypatch.setattr(tts_worker, 'cartesia_synthesize_raw_pcm', primary)
    monkeypatch.setattr(tts_worker, 'synthesize_streaming', backup)
    qid = tts_queue.enqueue(agent_id=env['agent_id'], text='Synthetic fallback', voice_id='backup-voice',
                            session='claude', source='pwa', trace_id='trace')
    assert tts_worker.synth_one(audio_dir=env['audio_dir'], broker=broker,
                               delivery=RawPcmDelivery(broker=broker), stream=Stream())
    row = next(r for r in tts_queue.recent() if r['queue_id'] == qid)
    assert row['status'] == tts_queue.FAILED, 'expired live stream discarded fallback bytes but queue reported successful speech'
    assert len([r for r in records if r['type'] == 'tts-error']) == 1
    clip = db.conn().execute('SELECT producer_status,path FROM clips ORDER BY clip_id DESC LIMIT 1').fetchone()
    assert clip['producer_status'] == 'failed'
    assert not Path(clip['path']).exists()

    failures = [kw['detail'] for name, kw in emitted if name == 'rawStreamFailed']
    assert len(failures) == 1
    assert failures[0]['code'] == 'raw_stream_wait_timeout'
    assert 'Synthetic fallback' not in str(failures[0])
