"""HTTP Live Streaming delivery: ffmpeg → playlist.m3u8 + AAC segments.

Implementation strategy:

  * One ffmpeg subprocess per clip. Bytes from ElevenLabs (mp3) are
    piped to ffmpeg's stdin as they arrive. ffmpeg writes:
        - playlist.m3u8 (manifest, grows as segments land,
          finalised with #EXT-X-ENDLIST when input closes)
        - segment-N.m4s (one per ~2s slice) plus init.mp4
    into the clip's directory.

  * The SSE `audio` event includes `playlist_url:
    /clips/<id>/playlist.m3u8`. iOS Safari plays this natively via plain
    `<audio src=playlist_url>` — no MSE, no Range quirks, no chunked
    Transfer-Encoding. The whole iOS streaming-quirk treadmill goes away.

  * The fallback for older clients (no native HLS, no MediaSource for
    audio/mpeg) is unchanged — they still use `/clips/<id>/stream`. The
    SSE event carries BOTH urls so the client picks. But this delivery
    doesn't write an mp3 file or feed the broker, so non-HLS clients
    would get a 404 — that's intentional. If a deployment can't trust
    HLS support across its clients it should stay on ChunkedFileDelivery.

  * On failure (mid-stream WS drop, ffmpeg crash) we kill the
    subprocess and tear down the partial playlist + segments so a
    half-finished playlist can't be played.

ffmpeg invocation: see _ffmpeg_argv. The key flags are
`-hls_playlist_type event` (manifest grows, never rolls), `-hls_list_size 0`
(unbounded segment list), and `-hls_time 2` (target segment duration).
With these, iOS sees a live playlist while ffmpeg writes, and once the
EOS frame arrives ffmpeg appends `#EXT-X-ENDLIST` so iOS knows the
stream is complete.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import threading

from .. import agents as agents_db
from .. import clips as clips_lib
from ..log import log_exception
from ..protocol import ClipProducerStatus
from . import FinalizeResult


# How long /clips/<id>/playlist.m3u8 will block waiting for ffmpeg to
# write the file. The race is SSE-event → ffmpeg-receives-first-chunk =
# ~200-500ms in production. 3 seconds is generous; if we hit the timeout
# something else is wrong (no ElevenLabs response, ffmpeg crashed).
_PLAYLIST_WAIT_SEC = 3.0


# Where per-clip HLS output goes — sibling of audio_dir. Layout:
#   <hls_root>/<clip_id>/playlist.m3u8
#   <hls_root>/<clip_id>/init.mp4
#   <hls_root>/<clip_id>/segment-0.m4s
# Served via /clips/<id>/playlist.m3u8 and /clips/<id>/segment-N.m4s.
def _hls_dir(audio_dir: pathlib.Path, clip_id: int) -> pathlib.Path:
    return audio_dir / "hls" / str(clip_id)


def _ffmpeg_argv(clip_dir: pathlib.Path) -> list[str]:
    """The ffmpeg invocation. Pulled out for testability — sim-e2e
    swaps this for a stub that writes a fake playlist + segments.

    Uses fMP4 (CMAF) segments instead of raw AAC ADTS. Why: iOS Safari's
    HLS decoder is hit-or-miss with ADTS — it fires `ended` after the
    first segment, leaves audio.duration=NaN, and audio playback stops.
    fMP4 carries proper timestamps via the moof/mdat box structure and
    is the format Apple actually optimises for. Same player API on the
    client, dramatically more reliable on iOS.
    """
    return [
        "ffmpeg",
        "-loglevel", "warning",
        "-f", "mp3",                  # input format (over stdin)
        "-i", "pipe:0",
        "-c:a", "aac",                # iOS prefers AAC over MP3 in HLS
        "-b:a", "128k",
        "-vn",                        # no video (defensive)
        "-hls_time", "2",             # ~2s segments
        "-hls_playlist_type", "event",
        "-hls_list_size", "0",        # unbounded — VOD-like once finalized
        "-hls_segment_type", "fmp4",  # fMP4/CMAF segments; iOS-friendly
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", str(clip_dir / "segment-%d.m4s"),
        str(clip_dir / "playlist.m3u8"),
    ]


class HlsDelivery:
    """Selected when `cfg.delivery = "hls"`. Spawns ffmpeg per clip and
    pipes bytes to it. iOS-native progressive playback."""

    name = "hls"

    def __init__(self, *, ffmpeg_bin: str | None = None,
                 argv_builder=None):
        # `ffmpeg_bin` is mostly a manual-test escape hatch (e.g. point
        # at a build with libfdk_aac). Default: whatever `ffmpeg` resolves
        # to on PATH at server-start time.
        self._ffmpeg_bin = ffmpeg_bin or "ffmpeg"
        # `argv_builder(clip_dir) -> list[str]` lets tests substitute a
        # fake "ffmpeg" without monkeypatching subprocess globals.
        self._argv_builder = argv_builder or _ffmpeg_argv

    def begin(self, *,
              audio_dir: pathlib.Path,
              agent: dict,
              voice_id: str,
              session: str,
              source: str,
              text_len: int,
              trace_id: str | None) -> "HlsSession":
        # 1. Allocate clip identity. Same shape as ChunkedFileDelivery —
        #    the delivery owns this so its sse_fields can reference clip_id.
        #    `path` is set to the playlist file even before it exists, so
        #    later DB lookups can find the artifact directory.
        clip_dir = _hls_dir(audio_dir, 0)  # tmp; we don't have clip_id yet
        # Allocate the row to get clip_id, then derive the real clip_dir.
        clip_id = agents_db.record_clip(
            agent_id=agent["agent_id"],
            path="",   # placeholder until we know clip_dir
            voice_id=voice_id,
            trace_id=trace_id,
            producer_status=ClipProducerStatus.STREAMING,
        )
        if not clip_id:
            raise RuntimeError("record_clip returned no clip_id")
        clip_dir = _hls_dir(audio_dir, clip_id)
        clip_dir.mkdir(parents=True, exist_ok=True)
        playlist_path = clip_dir / "playlist.m3u8"
        # Update the clip row's path to the playlist now that we know it.
        try:
            from ..db import conn as _conn
            _conn().execute(
                "UPDATE clips SET path = ? WHERE clip_id = ?",
                (str(playlist_path), clip_id),
            )
        except Exception as e:  # noqa: BLE001
            log_exception("hlsUpdatePathFail", e, detail=str(clip_id))

        # 2. Pre-synth sidecar — sits beside the playlist file in clip_dir.
        clips_lib.write_sidecar(
            playlist_path,
            clip_id=clip_id,
            agent_id=agent["agent_id"],
            persona=agent.get("persona"),
            voice_id=voice_id,
            session=session,
            source=source,
            text_len=text_len,
            trace_id=trace_id,
            extra={"streamable": True,
                   "delivery": "hls",
                   "playlist_url": f"/clips/{clip_id}/playlist.m3u8"},
        )

        # 3. Spawn ffmpeg. If it fails to start, raise so the worker
        #    marks the queue row failed and the SSE event never goes out.
        argv = [self._ffmpeg_bin, *self._argv_builder(clip_dir)[1:]]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as e:
            log_exception("hlsSpawnFail", e, detail=str(clip_dir))
            raise RuntimeError(f"ffmpeg spawn failed: {e}") from e

        return HlsSession(
            clip_id=clip_id, clip_dir=clip_dir,
            playlist_path=playlist_path,
            proc=proc, agent=agent, voice_id=voice_id,
            session=session, source=source,
            text_len=text_len, trace_id=trace_id,
        )


class HlsSession:
    """Per-clip session for HlsDelivery. Bytes go to ffmpeg's stdin via
    `feed`. There is no `target_path` (no mp3 file)."""

    def __init__(self, *,
                 clip_id: int,
                 clip_dir: pathlib.Path,
                 playlist_path: pathlib.Path,
                 proc: subprocess.Popen,
                 agent: dict,
                 voice_id: str,
                 session: str,
                 source: str,
                 text_len: int,
                 trace_id: str | None):
        self._clip_id = int(clip_id)
        self._clip_dir = clip_dir
        self._playlist_path = playlist_path
        self._proc = proc
        self._agent = agent
        self._voice_id = voice_id
        self._session = session
        self._source = source
        self._text_len = text_len
        self._trace_id = trace_id
        self._stderr_buf: list[bytes] = []
        # Drain stderr in the background so a chatty ffmpeg doesn't
        # block the pipe. We don't act on stderr unless ffmpeg exits
        # non-zero — then it's diagnostic.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    # iOS Safari can't reliably play live-updating HLS playlists — it
    # stalls after the first segment. Broadcast only after ENDLIST is
    # in place so the client gets a complete VOD playlist.
    publish_after_finalize = True

    @property
    def clip_id(self) -> int:
        return self._clip_id

    @property
    def target_path(self) -> None:
        return None    # ffmpeg owns the bytes — no mp3 on disk

    @property
    def sse_fields(self) -> dict:
        return {
            "streamable": True,
            "delivery": "hls",
            "playlist_url": f"/clips/{self._clip_id}/playlist.m3u8",
        }

    def feed(self, chunk_idx: int, chunk: bytes) -> None:
        if not self._proc.stdin or self._proc.stdin.closed:
            return
        try:
            self._proc.stdin.write(chunk)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            # ffmpeg died — log and let synthesize_streaming continue;
            # fail() will tear things down when EOS arrives.
            log_exception("hlsFeedFail", e, detail=str(self._clip_id))

    def finalize(self, *, total_bytes: int) -> FinalizeResult:
        # 1. Close ffmpeg's stdin so it flushes the last segment + appends
        #    #EXT-X-ENDLIST to the playlist. Then wait for it to exit.
        rc = self._close_ffmpeg(timeout=10.0)
        if rc not in (0, None):
            stderr = b"".join(self._stderr_buf)[:1000].decode("utf-8", "replace")
            log_exception("hlsFfmpegNonZero",
                          RuntimeError(f"ffmpeg rc={rc}"),
                          detail=stderr)

        # 2. Producer state.
        playlist_size = (self._playlist_path.stat().st_size
                         if self._playlist_path.is_file() else 0)
        try:
            agents_db.mark_clip_producer_status(
                clip_id=self._clip_id,
                producer_status=ClipProducerStatus.COMPLETE,
                byte_count=total_bytes,
            )
        except Exception as e:  # noqa: BLE001
            log_exception("hlsMarkCompleteFail", e, detail=str(self._clip_id))

        # 3. Final sidecar — same shape as begin() plus bytes.
        clips_lib.write_sidecar(
            self._playlist_path,
            clip_id=self._clip_id,
            agent_id=self._agent["agent_id"],
            persona=self._agent.get("persona"),
            voice_id=self._voice_id,
            session=self._session,
            source=self._source,
            bytes_=total_bytes,
            text_len=self._text_len,
            trace_id=self._trace_id,
            extra={"streamable": True,
                   "delivery": "hls",
                   "playlist_url": f"/clips/{self._clip_id}/playlist.m3u8",
                   "playlist_bytes": playlist_size},
        )

        return FinalizeResult(
            path=self._playlist_path,
            clip_id=self._clip_id,
            sse_url=f"/clips/{self._clip_id}/playlist.m3u8",
            ok=True,
        )

    def fail(self, error: str) -> None:
        # Kill ffmpeg if still running, nuke the artifact dir so a half-
        # finished playlist can't be served. Mark producer FAILED.
        if self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass
        try:
            shutil.rmtree(self._clip_dir, ignore_errors=True)
        except OSError:
            pass
        try:
            agents_db.mark_clip_producer_status(
                clip_id=self._clip_id,
                producer_status=ClipProducerStatus.FAILED,
                error=error,
            )
        except Exception as e:  # noqa: BLE001
            log_exception("hlsMarkFailedFail", e, detail=str(self._clip_id))

    # ---- internals ------------------------------------------------------

    def _close_ffmpeg(self, *, timeout: float) -> int | None:
        if self._proc.stdin and not self._proc.stdin.closed:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try: self._proc.kill()
            except OSError: pass
            return self._proc.poll()

    def _drain_stderr(self) -> None:
        if not self._proc.stderr:
            return
        try:
            for line in iter(self._proc.stderr.readline, b""):
                self._stderr_buf.append(line)
                # Bound the buffer so a runaway ffmpeg doesn't eat RAM.
                if len(self._stderr_buf) > 200:
                    self._stderr_buf = self._stderr_buf[-200:]
        except (OSError, ValueError):
            return


# ---- HTTP routing helpers ------------------------------------------------


def serve_hls_artifact(handler, audio_dir: pathlib.Path,
                       clip_id: int, filename: str) -> None:
    """Serve `/clips/<id>/playlist.m3u8` or `/clips/<id>/segment-N.m4s`.

    Both are plain static files in the clip's HLS dir. No Range games —
    iOS handles HLS via its own HTTP fetches per segment, which are small
    enough to send in one shot. Content-Type matters: m3u8 must be
    `application/vnd.apple.mpegurl` for iOS to recognize the playlist.

    `playlist.m3u8` has a short BLOCKING WAIT: the worker broadcasts the
    SSE audio event right after HlsDelivery.begin(), but ffmpeg doesn't
    write the playlist file until it has consumed the first chunk from
    ElevenLabs (a ~200-500ms gap). iOS Safari hits the URL within a few
    ms of receiving the SSE event; if it gets 404 four times in a row it
    gives up on the source with NotSupportedError. So when the request
    races ffmpeg, wait up to PLAYLIST_WAIT_SEC for the file to appear.
    """
    import time as _t
    # Allowed artifacts: playlist.m3u8, the fMP4 init segment, and
    # numbered fMP4 segments.
    is_segment = filename.startswith("segment-") and filename.endswith(".m4s")
    if filename not in ("playlist.m3u8", "init.mp4") and not is_segment:
        return _send_http_error(handler, 404, "no such artifact")

    target = (_hls_dir(audio_dir, clip_id) / filename).resolve()
    if _hls_dir(audio_dir, clip_id).resolve() not in target.parents:
        return _send_http_error(handler, 403, "forbidden")

    # Race-window wait: the playlist always gets written within ~500ms
    # of the SSE broadcast that announced this URL. Block until the file
    # exists or PLAYLIST_WAIT_SEC has elapsed — segments don't need this
    # because by the time the client requests segment-N, the playlist
    # has already listed it (so the file is already on disk).
    if filename == "playlist.m3u8":
        deadline = _t.monotonic() + _PLAYLIST_WAIT_SEC
        while not target.is_file() and _t.monotonic() < deadline:
            _t.sleep(0.025)
    if not target.is_file():
        return _send_http_error(handler, 404, "not found")

    if filename == "playlist.m3u8":
        ctype = "application/vnd.apple.mpegurl"
    elif filename == "init.mp4" or filename.endswith(".m4s"):
        # fMP4 segments + their init.mp4 use the iso-bmff video container,
        # which is what iOS expects for HLS fMP4 streams. Yes, it's "mp4"
        # even though we're only carrying audio.
        ctype = "video/mp4"
    else:
        ctype = "audio/aac"

    try:
        total = target.stat().st_size
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(total))
        # Playlists must NEVER be cached aggressively — they grow during
        # live synthesis. Segments are immutable once written.
        if filename == "playlist.m3u8":
            handler.send_header("Cache-Control", "no-store")
        else:
            handler.send_header("Cache-Control", "max-age=3600")
        handler.end_headers()
        with target.open("rb") as f:
            while True:
                buf = f.read(64 * 1024)
                if not buf: break
                handler.wfile.write(buf)
    except (BrokenPipeError, ConnectionResetError):
        return
    except OSError as e:
        log_exception("hlsServeFail", e, detail=str(target))


def _send_http_error(handler, code: int, message: str) -> None:
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(message)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(message.encode("utf-8"))
    except OSError as e:
        log_exception("hlsErrorReplyFail", e, detail=str(code))
