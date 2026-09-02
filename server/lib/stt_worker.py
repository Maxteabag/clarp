"""Whisper inference worker — runs in its own process.

Spawned by `lib.stt.SubprocessWhisperSTT`. Speaks newline-delimited JSON on
stdin/stdout:

  → {"op": "load", "model_source": ..., "compute_type": ..., "cpu_threads": N,
     "language": ...}
  ← {"event": "ready"} | {"event": "load_error", "error": "..."}
  → {"op": "transcribe", "id": "...", "path": "/tmp/x.wav", "prompt": "..."}
  ← {"id": "...", "segments": [{"text": "...", "no_speech_prob": 0.1}, ...]}
    | {"id": "...", "error": "..."}

Why a process: faster-whisper releases the GIL inside CTranslate2 kernels,
but its Python driver loop (feature extraction, per-segment decode,
tokenization) runs hot bytecode between them and convoys every HTTP handler
thread in the server (measured: a 243 s transcribe stalled /teams for 56 s).
A worker process isolates CPU, GIL, memory and crashes at once.

Set CLAUDE_PWA_STT_FAKE=1 to use a stub model (tests).
"""
from __future__ import annotations

import json
import os
import sys
import traceback


class _FakeModel:
    def transcribe(self, path, **kwargs):
        class Seg:
            text = " fake transcript."
            no_speech_prob = 0.0
        return iter([Seg()]), None


def _load(req: dict):
    if os.environ.get("CLAUDE_PWA_STT_FAKE") == "1":
        return _FakeModel()
    from faster_whisper import WhisperModel  # type: ignore
    return WhisperModel(
        req["model_source"], device="cpu",
        compute_type=req.get("compute_type") or "int8",
        cpu_threads=int(req.get("cpu_threads") or 0),
    )


def _transcribe(model, req: dict, language: str | None) -> list[dict]:
    segments_iter, _info = model.transcribe(
        req["path"],
        language=language,
        beam_size=1,
        condition_on_previous_text=False,
        initial_prompt=req.get("prompt") or "",
        no_speech_threshold=0.7,
        log_prob_threshold=-0.9,
        compression_ratio_threshold=2.4,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 150},
    )
    return [{"text": seg.text,
             "no_speech_prob": float(getattr(seg, "no_speech_prob", 0.0))}
            for seg in segments_iter]


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    model = None
    language: str | None = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = req.get("op")
        if op == "load":
            try:
                language = req.get("language") or None
                model = _load(req)
                _emit({"event": "ready"})
            except Exception as e:  # noqa: BLE001
                _emit({"event": "load_error", "error": f"{e}\n{traceback.format_exc()[-800:]}"})
                return 1
        elif op == "transcribe":
            rid = req.get("id")
            if model is None:
                _emit({"id": rid, "error": "model not loaded"})
                continue
            try:
                _emit({"id": rid, "segments": _transcribe(model, req, language)})
            except Exception as e:  # noqa: BLE001
                _emit({"id": rid, "error": str(e)})
        elif op == "quit":
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
