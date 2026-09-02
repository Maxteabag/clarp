"""Pluggable strategy for delivering a synthesized clip to the client.

The Phase B audio pipeline has two responsibilities that come together at
this seam:
  1. Producer: ElevenLabs WebSocket → mp3 byte stream (lib.eleven_ws).
  2. Consumer: client `<audio>` element → playback.

Between those, the server has to decide HOW the bytes get from "in the
worker process" to "playing in the browser." Today that's
`/clips/<id>/stream` (chunked Transfer-Encoding fed by `ClipStreamBroker`)
with the disk mp3 as fallback/replay. Tomorrow we want HLS:
`/clips/<id>/playlist.m3u8` referencing AAC segments produced by ffmpeg.
The client picks the right URL from the SSE `audio` event.

This module is the seam. A delivery is selected at server-startup time
(via `cfg.delivery`) and the worker hands bytes to a per-clip session.
The session owns:
  - what URL(s) the SSE event advertises (stream_url, playlist_url, ...)
  - where bytes physically land (broker + file, ffmpeg pipe, etc.)
  - the clip-row state transitions (producer_status: STREAMING → COMPLETE)
  - the sidecar lifecycle (pre-synth write, finalize, failure cleanup)

Adding a new delivery is one new module that implements `ClipDelivery`
plus one branch in `build_from_config`. The worker doesn't change.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class FinalizeResult:
    """Plain-data result of `ClipDeliverySession.finalize`. The worker uses
    this for the synthOk eventlog + queue mark_done."""
    path: pathlib.Path                 # representative path for logs / fallback
    clip_id: int                       # allocated in begin()
    sse_url: str                       # legacy /audio/<name> url for eventlog
    ok: bool = True


@runtime_checkable
class ClipDeliverySession(Protocol):
    """A live, per-clip delivery session. Created by `ClipDelivery.begin`,
    used by the worker for the lifetime of one synthesis.

    The session owns the entire "synthesis is happening" state machine.
    The worker is responsible for one thing: pumping bytes via
    `feed(idx, chunk)` and signaling completion via `finalize()` / `fail()`."""

    @property
    def publish_after_finalize(self) -> bool:
        """True when the SSE `audio` event should be broadcast AFTER
        synthesis completes (the playlist/file is fully written), False
        when it should be broadcast at begin() time (the client tracks
        a live stream as it grows). HLS uses True — iOS Safari can't
        reliably play live-updating playlists. ChunkedFile uses False —
        the broker fans bytes to clients in real time."""
        ...

    @property
    def clip_id(self) -> int:
        """Allocated by `begin`. Used in URL paths and DB lookups."""
        ...

    @property
    def target_path(self) -> pathlib.Path | None:
        """Filesystem path that `lib.eleven_ws.synthesize_streaming` should
        write to, or None if the delivery handles bytes purely via `feed`.

        ChunkedFileDelivery returns a real path (the mp3 lives there as a
        replay/fallback artifact). HlsDelivery returns None (ffmpeg
        consumes bytes piped via `feed` — no need for an mp3 on disk)."""
        ...

    @property
    def sse_fields(self) -> dict:
        """Extra fields the worker should include in the SSE `audio` event
        broadcast. At minimum: `streamable: true` plus the URL the client
        will play (e.g. `stream_url` or `playlist_url`). The worker adds
        the universal fields (url, name, session, clip_id, agent_id,
        persona, trace_id)."""
        ...

    def feed(self, chunk_idx: int, chunk: bytes) -> None:
        """Called once per ElevenLabs audio chunk, AFTER synthesize_streaming
        has decoded base64 but BEFORE it writes the chunk to disk (if
        target_path is set). ChunkedFileDelivery uses this to fan bytes
        into the live broker; HlsDelivery pipes them into ffmpeg."""
        ...

    def finalize(self, *, total_bytes: int) -> FinalizeResult:
        """Called once EOS is reached. Updates the clip row to
        producer_status=COMPLETE, writes the final sidecar, closes the
        broker / ffmpeg pipe / segment writer."""
        ...

    def fail(self, error: str) -> None:
        """Called when synthesis fails mid-stream. Cleanup partial
        artifacts so a half-written clip doesn't leak to clients."""
        ...


@runtime_checkable
class ClipDelivery(Protocol):
    """Stateless factory that produces a per-clip session."""

    name: str   # short identifier for logs / config ("chunked-file", "hls")

    def begin(self, *,
              audio_dir: pathlib.Path,
              agent: dict,
              voice_id: str,
              session: str,
              source: str,
              text_len: int,
              trace_id: str | None) -> ClipDeliverySession:
        """Open a new delivery session. Responsibilities:

          1. Allocate a clip_id (`agents_db.record_clip` with
             producer_status=STREAMING).
          2. Write the pre-synth sidecar so the watcher / herald can't see
             the mp3 (if any) without metadata.
          3. Compute the SSE-event fields that announce the clip's URL(s).

        The worker then publishes the SSE event using `session.sse_fields`
        and calls `synthesize_streaming(out_path=session.target_path,
        on_chunk=session.feed, ...)`.
        """
        ...


# ---- factory -------------------------------------------------------------


@dataclass
class DeliveryDeps:
    """Things the delivery needs from the server context. Threaded in via
    the factory so the delivery implementations don't pull in the kitchen
    sink. Each implementation only uses the deps it actually needs (e.g.
    HlsDelivery ignores `broker`)."""
    broker: object = None              # ClipStreamBroker | None
    # room for future deps without changing the factory signature.
    extras: dict = field(default_factory=dict)


def build_from_config(cfg, *, deps: DeliveryDeps | None = None) -> ClipDelivery:
    """Factory called by `build_server`. Picks an implementation based on
    `cfg.delivery`. New deliveries get one branch here."""
    deps = deps or DeliveryDeps()
    name = (getattr(cfg, "delivery", None) or "chunked-file").strip().lower()
    if name in ("chunked-file", "chunked_file", "file", ""):
        from .chunked_file import ChunkedFileDelivery
        return ChunkedFileDelivery(broker=deps.broker)
    if name == "hls":
        from .hls import HlsDelivery     # imported lazily — ffmpeg dep
        return HlsDelivery()
    if name in ("raw-pcm", "raw_pcm", "native-low-latency", "native"):
        from .raw_pcm import RawPcmDelivery
        return RawPcmDelivery(
            broker=deps.broker,
            encoding=getattr(cfg, "raw_pcm_encoding", None),
            sample_rate=getattr(cfg, "raw_pcm_sample_rate", None),
        )
    raise ValueError(
        f"unknown delivery '{name}' — supported: chunked-file, hls, raw-pcm"
    )
