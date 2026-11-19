#!/usr/bin/env python3
"""The Rust Host's own engine that listens: a worker that keeps a Whisper model loaded.

Started by the Host (`crates/clarp-speech/src/whisper.rs`) with a Python that has
`faster-whisper`, and kept for as long as the Host runs. It reads a line of JSON at a time
on standard input and answers each with one line on standard output:

    -> {"op":"load","model_source":"small.en","compute_type":"int8","cpu_threads":0,"language":"en"}
    <- {"event":"ready"} | {"event":"load_error","error":"..."}
    -> {"op":"transcribe","id":"c1","path":"/.../clip.m4a","prompt":"...","beam_size":1,"language":"nb"}
    <- {"id":"c1","segments":[{"text":"...","no_speech_prob":0.1}]} | {"id":"c1","error":"..."}
    -> {"op":"probe","id":"p1","path":"/.../clip.m4a"}
    <- {"id":"p1","seconds":30.0}  # zero when unknown
    -> {"op":"quit"}

The lines are the Python Host's own (`server/lib/stt_worker.py`), and so is how the model is
asked: which pieces it holds back as silence, how it cuts a clip at pauses. What is done with
the pieces (which are kept, what is thrown away as invented) is the Host's, the same for every
engine. A clip may say its language; one that does not is heard in the language the model was
loaded for.

It is a process of its own because hearing a long clip is minutes of processor time, and
because the model's library is not the Host's code: when this falls over, the Host starts
another.

With CLARP_WHISPER_FAKE=1 (or the Python Host's CLAUDE_PWA_STT_FAKE=1) no model is loaded and
every clip is heard as the same words: for tests, and for holding the two Hosts to each other.
CLARP_WHISPER_FAKE_SAYS names other words for it to hear, and is the same switch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback


class _FakeModel:
    """Hears the same thing in every clip, as the Python Host's own stand-in does."""

    def transcribe(self, path, **asked):
        class Piece:
            text = ' ' + (os.environ.get('CLARP_WHISPER_FAKE_SAYS') or 'fake transcript.')
            no_speech_prob = 0.0
        return iter([Piece()]), None


def fake() -> bool:
    return (os.environ.get('CLARP_WHISPER_FAKE') == '1' or os.environ.get('CLAUDE_PWA_STT_FAKE') == '1'
            or bool(os.environ.get('CLARP_WHISPER_FAKE_SAYS')))


def load(asked: dict):
    if fake():
        return _FakeModel()
    from faster_whisper import WhisperModel  # the heavy import, only where a model is wanted
    return WhisperModel(
        asked['model_source'], device='cpu',
        compute_type=asked.get('compute_type') or 'int8',
        cpu_threads=int(asked.get('cpu_threads') or 0),
    )


def beam_size(asked: dict) -> int:
    given = asked.get('beam_size', 1)
    if isinstance(given, bool) or not isinstance(given, int) or given < 1:
        raise ValueError(f'beam_size must be a positive int, got {given!r}')
    return min(given, 8)


def hear(model, asked: dict, language: str | None) -> list[dict]:
    pieces, _about = model.transcribe(
        asked['path'],
        language=asked.get('language') or language,
        beam_size=beam_size(asked),
        condition_on_previous_text=False,
        initial_prompt=asked.get('prompt') or '',
        no_speech_threshold=0.7,
        log_prob_threshold=-0.9,
        compression_ratio_threshold=2.4,
        vad_filter=True,
        vad_parameters={'min_silence_duration_ms': 400, 'speech_pad_ms': 150},
    )
    return [{'text': piece.text, 'no_speech_prob': float(getattr(piece, 'no_speech_prob', 0.0))}
            for piece in pieces]


def say(answer: dict) -> None:
    sys.stdout.write(json.dumps(answer, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def container_seconds(path: str) -> float:
    import av
    # A file-like input and restricted protocols keep a container within the uploaded clip.
    with open(path, 'rb') as audio, av.open(audio, mode='r', options={'protocol_whitelist': 'pipe'}) as container:
        stream = next((s for s in container.streams if s.type == 'audio'), None)
        if stream is None:
            return 0.0
        if container.duration:
            return float(container.duration) / av.time_base
        if stream.duration and stream.time_base:
            return float(stream.duration * stream.time_base)
        rate = int(stream.rate or 0)
        samples = sum(frame.samples for frame in container.decode(stream))
        return samples / rate if rate > 0 else 0.0


def probe(path: str) -> float:
    # A stuck decoder must not cost the loaded Whisper model or consume CPU beyond the deadline.
    try:
        result = subprocess.run([sys.executable, __file__, '--probe-file', path], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=5, check=True)
        return float(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def main() -> int:
    model = None
    language: str | None = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            asked = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = asked.get('op')
        if op == 'load':
            try:
                language = asked.get('language') or None
                model = load(asked)
                say({'event': 'ready'})
            except Exception as error:  # noqa: BLE001 - whatever the library fails with is the answer
                say({'event': 'load_error', 'error': f'{error}\n{traceback.format_exc()[-800:]}'})
                return 1
        elif op == 'probe':
            say({'id': asked.get('id'), 'seconds': probe(asked['path'])})
        elif op == 'transcribe':
            ident = asked.get('id')
            if model is None:
                say({'id': ident, 'error': 'model not loaded'})
                continue
            try:
                say({'id': ident, 'segments': hear(model, asked, language)})
            except Exception as error:  # noqa: BLE001 - one clip that fails is not the worker's end
                say({'id': ident, 'error': str(error)})
        elif op == 'quit':
            break
    return 0


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe-file':
        print(container_seconds(sys.argv[2]))
    else:
        sys.exit(main())
