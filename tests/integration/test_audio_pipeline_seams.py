"""Cross-seam tests for the streaming audio pipeline.

Each component (worker, herald, AudioStream, /audio handler) passes its own
unit tests, but the bugs that have bitten us in production all live in the
*seam between* them — at points where one component's output needs to be
readable by the next at a specific moment in time:

  Bug A: herald's _broadcast_audio stripped streamable + stream_url from
        meta. Caught only after a live trace.
  Bug B: worker wrote the mp3 BEFORE the sidecar, so a reader could see the
        file without its metadata. Same symptom (no streamable on the wire),
        different root cause.
  Bug C: /audio/<file> fallback path serves whatever bytes are on disk RIGHT
        NOW. If the worker is still writing, iOS plays the first 4 words then
        thinks the file ended.

The tests below run the REAL worker write path (with a controllable-timing
fake ElevenLabs) through the REAL herald and AudioStream and subscribe to
the resulting SSE bus. They assert on the wire — the actual broadcast
payload — which is the contract the client depends on.
"""
from __future__ import annotations

import pathlib
import queue
import socket
import sys
import threading
import time
import urllib.request

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db                  # noqa: E402
from lib import clips as clips_lib                    # noqa: E402
from lib import tts_queue                             # noqa: E402
from lib.audio_stream import AudioStream              # noqa: E402
from lib.context import ServerContext, StubSTT       # noqa: E402
from lib.herald import HeraldManager                  # noqa: E402
from lib.protocol import TurnSource          # noqa: E402
from lib.tts_engine import FakeTTSEngine              # noqa: E402

import importlib.util as _ilu                         # noqa: E402
_spec = _ilu.spec_from_file_location(
    "claude_pwa_server_for_seams", _SERVER_DIR / "server.py")
assert _spec and _spec.loader
_srv_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_srv_mod)
build_server = _srv_mod.build_server


# ---- harness ------------------------------------------------------------


class _Subscriber:
    """Captures every event the AudioStream broadcasts (via the same
    queue.Queue path real SSE handlers use)."""
    def __init__(self, stream: AudioStream):
        self.q: queue.Queue = stream.subscribe()
        self._stream = stream

    def drain(self, timeout: float = 1.0) -> list[dict]:
        import json
        events = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                raw = self.q.get(timeout=max(0.0, end - time.time()))
            except queue.Empty:
                break
            events.append(json.loads(raw))
        return events

    def close(self) -> None:
        try: self._stream.unsubscribe(self.q)
        except Exception: pass


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Real AudioStream + real HeraldManager + real worker write path,
    wired the same way build_server wires them. Synthesis is faked via
    a controllable-timing stub so the test can drive the race window."""
    audio_dir = tmp_path / "audio"; audio_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    from lib import config
    monkeypatch.setattr(
        config, "_CACHED",
        config.Config(tts_provider="elevenlabs", eleven_api_key="simulated"))

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V_MIKE",
        cwd=str(tmp_path), session="claude",
    )
    agents_db.set_focus(agent_id)         # so herald takes the direct-broadcast path

    stream = AudioStream(audio_dir)
    herald = HeraldManager(stream=stream, tts=_FakeTTS(),
                           agents=lambda: agents_db.session_dict())
    herald.set_focus("claude")
    stream.start()
    sub = _Subscriber(stream)
    try:
        yield {
            "tmp_path": tmp_path,
            "audio_dir": audio_dir,
            "agent_id": agent_id,
            "subscriber": sub,
            "stream": stream,
            "herald": herald,
        }
    finally:
        sub.close()
        stream.stop()


class _FakeTTS:
    """Stub for HeraldManager's tts param. Heralds aren't exercised by
    these tests (focus matches), but the manager wants something."""
    def synthesize(self, text, voice_id, session=None):
        return ""


def _audio_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "audio"]


# ---- the actual cross-seam tests ----------------------------------------


def test_streamable_flag_survives_worker_through_herald_to_wire(harness, monkeypatch):
    """The producer-side invariant the client depends on: when a clip
    is synthesized with streamable=true in its sidecar, the SSE event
    that lands on the wire MUST carry streamable=true and stream_url.

    This single property catches bug A (herald stripping) and verifies
    the direct worker-published path announces the clip exactly once."""
    # Drive a real synth_one with a controllable fake synthesize_streaming
    # that pauses mid-file, so the event is published while the mp3 is
    # still growing.
    write_lock = threading.Event()

    def slow_streaming(*, text, voice_id, out_path, **kw):
        # Write the first byte, then PAUSE while the file is still growing.
        out = pathlib.Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as f:
            f.write(b"\xff\xfb")  # first 2 bytes
            f.flush()
            write_lock.wait(timeout=2.0)   # block until test releases
            f.write(b"\x90\x00\x44\x55")   # rest of the bytes
        return 6

    from lib import tts_worker as _tw
    monkeypatch.setattr(_tw, "synthesize_streaming", slow_streaming)

    # Enqueue + start the worker in a thread (so we can release write_lock
    # while it's mid-stream).
    tts_queue.enqueue(
        agent_id=harness["agent_id"], text="hello",
        voice_id="V_MIKE", session="claude",
        source=TurnSource.PWA,
        trace_id="trace-seam",
    )

    def run_worker():
        _tw.synth_one(audio_dir=harness["audio_dir"],
                      stream=harness["stream"],
                      herald=harness["herald"])
    t = threading.Thread(target=run_worker, daemon=True)
    t.start()

    # While the worker is paused mid-synth the event is already on the wire.
    time.sleep(0.20)
    write_lock.set()                       # release the synth
    t.join(timeout=3.0)

    events = harness["subscriber"].drain(timeout=1.0)
    audio = _audio_events(events)
    assert audio, f"no audio event broadcast; events = {events}"
    assert len(audio) == 1, f"worker clip should be announced once, got {audio}"
    ev = audio[0]
    assert ev.get("streamable") is True, (
        f"streamable=true did not survive the worker → herald → wire path. "
        f"event payload: {ev}. The herald stripped the flag (bug A)."
    )
    assert ev.get("stream_url") == f"/clips/{ev['clip_id']}/stream"
    assert ev.get("trace_id") == "trace-seam"
    assert ev.get("agent_id") == harness["agent_id"]


def test_sidecar_is_readable_at_every_moment_the_mp3_is_visible(harness, monkeypatch):
    """Stronger property: at no point during synthesis should the mp3
    file exist on disk without a readable sidecar carrying streamable.
    Pins bug B directly (sidecar must be written BEFORE the mp3 is
    visible, not after)."""
    from lib import clips as _clips
    audio_dir = harness["audio_dir"]
    observations: list[tuple[float, bool]] = []  # (t_offset, sidecar_present)

    def streaming_with_probe(*, text, voice_id, out_path, **kw):
        out = pathlib.Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        with out.open("wb") as f:
            for chunk in (b"AAA", b"BBB", b"CCC"):
                f.write(chunk); f.flush()
                # Inspect what a reader (/audio, audio_growing) sees RIGHT NOW.
                side = _clips.sidecar_path(out)
                observations.append((time.time() - t0, side.is_file()))
                time.sleep(0.02)
        return 9

    from lib import tts_worker as _tw
    monkeypatch.setattr(_tw, "synthesize_streaming", streaming_with_probe)

    tts_queue.enqueue(
        agent_id=harness["agent_id"], text="probe",
        voice_id="V_MIKE", session="claude",
        source=TurnSource.PWA,
    )
    _tw.synth_one(audio_dir=audio_dir)

    assert observations, "the probe should have fired during synthesis"
    not_yet = [t for t, present in observations if not present]
    assert not not_yet, (
        f"sidecar was MISSING at these moments during synth: {not_yet}. "
        f"/audio/<file> would have served the growing mp3 as if complete. "
        f"Fix: write the sidecar BEFORE opening the mp3 file."
    )


# ---- Bug C-2: /audio/<file> serves partial bytes during synthesis ------
#
# Live failure observed 2026-05-25 on iOS Safari, trace 57b5c09aec571ce7:
#
#   21:53:59.815  eleven_ws  firstChunk          bytes=6732
#   21:54:00.446  client     streamingFallback   reason=mse-unsupported
#   21:54:00.446  client     playOk              ← played the 6732 partial bytes
#   21:54:02.731  eleven_ws  eos                 bytes=922898 ← rest arrived after
#
# Because iOS doesn't support MSE for audio/mpeg, streaming-player.js falls
# back to <audio src="/audio/<file>">. The /audio endpoint serves whatever
# bytes are on disk RIGHT NOW with Content-Length set to the partial size,
# so iOS plays the first ~4 words and considers the file complete.
#
# The producer-side signal that synthesis is still in flight is the
# sidecar: during synthesis it carries streamable=true but NO `bytes`
# field; only the post-EOS finalize call writes `bytes=<final_size>`.


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(tmp_path):
    """Boot a real ThreadingHTTPServer wired the same way build_server does,
    so the /audio/<file> handler runs through the production dispatch table.
    Yields (base_url, audio_dir)."""
    static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio = tmp_path / "audio"; audio.mkdir()
    agents_path = tmp_path / "agents.json"
    agents_db.create_agent(persona="Mike", voice_id="V_MIKE",
                           cwd=str(tmp_path), session="claude")
    ctx = ServerContext(
        root=tmp_path, static=static, audio_dir=audio,
        agents_path=agents_path,
        default_session="claude",
        tts=FakeTTSEngine(audio),
        stream=AudioStream(audio),
        stt=StubSTT(text="", ends_terminal=False),
        roster_names=("Mike",),
    )
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/agents/snapshot", timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    try:
        yield (base, audio)
    finally:
        srv.shutdown()
        srv.server_close()


def test_audio_endpoint_returns_full_bytes_for_in_progress_clip(http_server):
    """Bug C-2: GET /audio/<file> while the worker is still streaming bytes
    must return the COMPLETE clip, not whatever's on disk at request time.

    Reproduces the iOS-fallback path: the worker has written the first
    ElevenLabs chunk and the pre-synth sidecar (streamable=true, no
    `bytes` field). The client requests /audio/<file>. The handler must
    detect the in-progress state and either block until completion or
    stream with Transfer-Encoding: chunked — either way, the response
    body equals the final file contents.

    Before the fix, the static-file handler reads the partial file off
    disk, sends Content-Length = <partial size>, and iOS's <audio>
    treats Content-Length as authoritative — playback stops after the
    first few hundred ms.
    """
    base, audio_dir = http_server
    filename = f"{int(time.time() * 1000)}__claude.mp3"
    target = audio_dir / filename

    # Stage 1 of the worker: pre-synth sidecar (no `bytes` field) + first
    # chunk of audio bytes on disk. Mimics _synth_pwa_streaming after the
    # ElevenLabs WS has delivered its firstChunk but before EOS.
    clips_lib.write_sidecar(
        target,
        agent_id="a-mike", persona="Mike", voice_id="V_MIKE",
        session="claude", source="pwa", text_len=42,
        trace_id="trace-c2", extra={"streamable": True},
    )
    first_chunk = b"\xff\xfb\x90\x00" + (b"A" * 6700)   # ~the 6732 from live
    target.write_bytes(first_chunk)

    # The completing step: ~150ms later, append the rest of the bytes and
    # write the final sidecar with bytes_=<final_size>. This is what the
    # worker does at EOS.
    rest = (b"B" * 30000) + (b"C" * 30000)
    final_size = len(first_chunk) + len(rest)

    def finish_synthesis():
        time.sleep(0.15)
        with target.open("ab") as f:
            f.write(rest); f.flush()
        clips_lib.write_sidecar(
            target,
            clip_id=1, agent_id="a-mike", persona="Mike", voice_id="V_MIKE",
            session="claude", source="pwa",
            bytes_=final_size, text_len=42,
            trace_id="trace-c2", extra={"streamable": True},
        )

    finisher = threading.Thread(target=finish_synthesis, daemon=True)
    finisher.start()

    # Request the clip RIGHT NOW (while only first_chunk is on disk).
    started = time.time()
    with urllib.request.urlopen(f"{base}/audio/{filename}", timeout=10) as r:
        headers = dict(r.headers.items())
        body = r.read()
    elapsed = time.time() - started

    finisher.join(timeout=5)

    assert len(body) == final_size, (
        f"GET /audio/{filename} returned {len(body)} bytes; final file size "
        f"is {final_size}. The static-file handler served the partial file "
        f"(headers: Content-Length={headers.get('Content-Length')}, "
        f"Transfer-Encoding={headers.get('Transfer-Encoding')}). "
        f"iOS Safari treats this Content-Length as authoritative and stops "
        f"playback after the first few words. Fix: detect in-progress state "
        f"via sidecar (streamable=true + no `bytes` field) and switch to "
        f"chunked transfer that waits for completion."
    )
    # And the wait actually happened — we couldn't have completed in <100ms
    # since the finisher sleeps 150ms before appending.
    assert elapsed >= 0.10, (
        f"response returned in {elapsed*1000:.0f}ms — too fast to have "
        f"waited for the finisher thread. Handler must not have blocked."
    )


def test_audio_endpoint_static_clip_still_works(http_server):
    """Inverse: a fully-synthesized clip (sidecar carries final `bytes`) or
    no sidecar at all (legacy/local clips) must serve as a normal static
    file — the in-progress detection must not regress the happy path."""
    base, audio_dir = http_server

    # Case A: complete sidecar (post-synth state).
    name_a = "complete.mp3"
    payload_a = b"\xff\xfb" + (b"X" * 1024)
    (audio_dir / name_a).write_bytes(payload_a)
    clips_lib.write_sidecar(
        audio_dir / name_a, clip_id=1, agent_id="a-mike", voice_id="V_MIKE",
        bytes_=len(payload_a), extra={"streamable": True},
    )
    with urllib.request.urlopen(f"{base}/audio/{name_a}", timeout=5) as r:
        assert r.read() == payload_a

    # Case B: no sidecar at all (legacy / herald / local-mode clips).
    name_b = "no_sidecar.mp3"
    payload_b = b"\xff\xfb" + (b"Y" * 512)
    (audio_dir / name_b).write_bytes(payload_b)
    with urllib.request.urlopen(f"{base}/audio/{name_b}", timeout=5) as r:
        assert r.read() == payload_b


def test_failure_path_leaves_no_partial_mp3(harness, monkeypatch):
    """If synthesis fails mid-stream, the worker must NOT leave a half-
    written mp3 (and orphan sidecar) on disk for /audio to serve."""
    from lib import tts_worker as _tw, eleven_ws

    def boom_mid_stream(*, text, voice_id, out_path, **kw):
        # Write a few bytes then explode — mimics the ElevenLabs WS
        # dropping mid-stream.
        out = pathlib.Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as f:
            f.write(b"PARTIAL"); f.flush()
        raise eleven_ws.ElevenWSError("connection reset mid-stream")
    monkeypatch.setattr(_tw, "synthesize_streaming", boom_mid_stream)

    tts_queue.enqueue(
        agent_id=harness["agent_id"], text="x",
        voice_id="V_MIKE", session="claude",
        source=TurnSource.PWA,
    )
    _tw.synth_one(audio_dir=harness["audio_dir"])

    leftover_mp3 = list(harness["audio_dir"].glob("*.mp3"))
    leftover_json = list(harness["audio_dir"].glob("*.json"))
    assert leftover_mp3 == [], f"partial mp3 left on disk: {leftover_mp3}"
    assert leftover_json == [], f"orphan sidecar left on disk: {leftover_json}"
