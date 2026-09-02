#!/usr/bin/env python3
"""Headless end-to-end verifier for native raw PCM playback.

The harness uses the production Cartesia WebSocket client and clip broker,
consumes the broker through the same chunked HTTP response used by iOS, then
checks byte identity, PCM integrity, playback-buffer timing, and speech
content through Cartesia batch STT.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import pathlib
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "server"))

from lib.cartesia_ws import synthesize_raw_pcm  # noqa: E402
from lib.clip_stream import ClipStreamBroker, _serve_live_chunked  # noqa: E402
from lib.config import load as load_config  # noqa: E402

SAMPLE_RATE = 44_100
BYTES_PER_FRAME = 4
READ_SIZE = 4096


def normalized_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.lower())


def word_error_rate(expected: str, actual: str) -> float:
    left = normalized_words(expected)
    right = normalized_words(actual)
    if not left:
        return 0.0 if not right else 1.0
    previous = list(range(len(right) + 1))
    for i, expected_word in enumerate(left, 1):
        current = [i]
        for j, actual_word in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (expected_word != actual_word),
            ))
        previous = current
    return previous[-1] / len(left)


def pcm_metrics(raw: bytes) -> dict:
    if len(raw) % BYTES_PER_FRAME:
        return {"frame_aligned": False, "trailing_bytes": len(raw) % 4}
    samples = array("f")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    finite = [sample for sample in samples if math.isfinite(sample)]
    if not finite:
        return {
            "frame_aligned": True,
            "samples": len(samples),
            "finite": False,
        }
    peak = max(abs(sample) for sample in finite)
    rms = math.sqrt(sum(sample * sample for sample in finite) / len(finite))
    max_jump = max(
        (abs(finite[index] - finite[index - 1])
         for index in range(1, len(finite))),
        default=0.0,
    )
    return {
        "frame_aligned": True,
        "samples": len(samples),
        "duration_ms": round(len(samples) * 1000 / SAMPLE_RATE),
        "finite": len(finite) == len(samples),
        "peak": round(peak, 6),
        "rms": round(rms, 6),
        "clipped_samples": sum(abs(sample) >= 0.999 for sample in finite),
        "max_adjacent_jump": round(max_jump, 6),
    }


def playback_buffer_metrics(arrivals: list[tuple[float, int]]) -> dict:
    """Model the native player's immediate scheduling from 4096-byte reads."""
    if not arrivals:
        return {"started_ms": None, "underruns": 1, "minimum_buffer_ms": 0}
    started_at = arrivals[0][0]
    buffered_seconds = 0.0
    underruns = 0
    minimum_before_refill = math.inf
    for arrived_at, byte_count in arrivals:
        elapsed = arrived_at - started_at
        before_refill = buffered_seconds - elapsed
        minimum_before_refill = min(minimum_before_refill, before_refill)
        if buffered_seconds > 0 and before_refill < 0:
            underruns += 1
        buffered_seconds += byte_count / (SAMPLE_RATE * BYTES_PER_FRAME)
    return {
        "started_ms": round(started_at * 1000),
        "reads": len(arrivals),
        "underruns": underruns,
        "minimum_buffer_ms": round(minimum_before_refill * 1000),
        "buffered_audio_ms": round(buffered_seconds * 1000),
    }


def transcribe_raw(raw: bytes, *, api_key: str) -> str:
    boundary = f"----claude-pwa-{uuid.uuid4().hex}"
    parts = [
        _multipart_field(boundary, "model", b"ink-whisper"),
        _multipart_field(boundary, "language", b"en"),
        _multipart_field(
            boundary,
            "file",
            raw,
            filename="stream.pcm",
            content_type="application/octet-stream",
        ),
        f"--{boundary}--\r\n".encode(),
    ]
    query = urllib.parse.urlencode({
        "encoding": "pcm_f32le",
        "sample_rate": SAMPLE_RATE,
    })
    request = urllib.request.Request(
        f"https://api.cartesia.ai/stt?{query}",
        data=b"".join(parts),
        method="POST",
        headers={
            "X-API-Key": api_key,
            "Cartesia-Version": "2026-03-01",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.load(response)
    return str(payload.get("text") or "")


def _multipart_field(boundary: str, name: str, value: bytes, *,
                     filename: str | None = None,
                     content_type: str | None = None) -> bytes:
    disposition = f'Content-Disposition: form-data; name="{name}"'
    if filename:
        disposition += f'; filename="{filename}"'
    headers = [f"--{boundary}", disposition]
    if content_type:
        headers.append(f"Content-Type: {content_type}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode() + value + b"\r\n"


class StreamHarness:
    def __init__(self):
        self.broker = ClipStreamBroker()
        self.clip_id = 1
        self.broker.open(self.clip_id)
        stream = self.broker.get(self.clip_id)
        assert stream is not None
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                _serve_live_chunked(
                    self,
                    stream,
                    harness.clip_id,
                    content_type="application/octet-stream",
                )

            def log_message(self, _format, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/clips/1/stream"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def consume(url: str, ready: threading.Event,
            result: dict, started_at: float) -> None:
    try:
        with urllib.request.urlopen(url, timeout=90) as response:
            result["content_type"] = response.headers.get("Content-Type")
            result["transfer_encoding"] = response.headers.get("Transfer-Encoding")
            ready.set()
            chunks: list[bytes] = []
            arrivals: list[tuple[float, int]] = []
            while True:
                chunk = response.read(READ_SIZE)
                if not chunk:
                    break
                arrivals.append((time.perf_counter() - started_at, len(chunk)))
                chunks.append(chunk)
            result["raw"] = b"".join(chunks)
            result["arrivals"] = arrivals
    except Exception as error:  # noqa: BLE001
        result["error"] = str(error)
        ready.set()


def verify(text: str, output_dir: pathlib.Path) -> tuple[dict, bool]:
    config = load_config()
    api_key = config.cartesia_key()
    voice_id = config.cartesia_voice_for("Rachel")
    if not api_key or not voice_id:
        raise RuntimeError("Cartesia API key and Rachel voice are required")
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = output_dir / "producer.pcm"
    received_path = output_dir / "ios-consumer.pcm"

    started_at = time.perf_counter()
    result: dict = {}
    ready = threading.Event()
    with StreamHarness() as harness:
        consumer = threading.Thread(
            target=consume,
            args=(harness.url, ready, result, started_at),
            daemon=True,
        )
        consumer.start()
        if not ready.wait(timeout=5):
            raise RuntimeError("HTTP consumer did not connect")
        synthesize_raw_pcm(
            text=text,
            voice_id=voice_id,
            out_path=source_path,
            api_key=api_key,
            model=config.cartesia_model,
            on_chunk=lambda _index, chunk: harness.broker.append(1, chunk),
        )
        harness.broker.finish(1)
        consumer.join(timeout=90)
        if consumer.is_alive():
            raise RuntimeError("HTTP consumer did not finish")

    if result.get("error"):
        raise RuntimeError(f"HTTP consumer failed: {result['error']}")
    source = source_path.read_bytes()
    received = result.get("raw", b"")
    received_path.write_bytes(received)
    transcript = transcribe_raw(received, api_key=api_key)
    wer = word_error_rate(text, transcript)
    buffer = playback_buffer_metrics(result.get("arrivals", []))
    metrics = pcm_metrics(received)
    report = {
        "expected_text": text,
        "transcript": transcript,
        "word_error_rate": round(wer, 4),
        "transport": {
            "producer_bytes": len(source),
            "consumer_bytes": len(received),
            "sha256_match": hashlib.sha256(source).digest()
            == hashlib.sha256(received).digest(),
            "content_type": result.get("content_type"),
            "transfer_encoding": result.get("transfer_encoding"),
        },
        "pcm": metrics,
        "playback_buffer": buffer,
        "artifacts": {
            "producer": str(source_path),
            "consumer": str(received_path),
        },
    }
    passed = bool(
        report["transport"]["sha256_match"]
        and metrics.get("frame_aligned")
        and metrics.get("finite")
        and metrics.get("rms", 0) > 0.001
        and metrics.get("peak", 2) <= 1.0
        and wer <= 0.25
    )
    return report, passed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--text",
        default=(
            "The streamed audio verifier checks every byte in order, then "
            "confirms that the complete sentence can still be understood."
        ),
    )
    parser.add_argument("--output-dir", type=pathlib.Path,
                        default=pathlib.Path("/tmp/claude-pwa-audio-verify"))
    args = parser.parse_args()
    report, passed = verify(args.text, args.output_dir)
    report["passed"] = passed
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
