"""Transcription language: a fixed code, or `auto` detection.

`transcription.language` is `en` (the default), any other ISO 639-1 code, or
`auto`. Providers that detect language themselves (ElevenLabs, Deepgram) are
simply told to. Cartesia has no automatic mode: with `language=en` it
translates Norwegian speech into English, and with `language=no` it can
translate English into Norwegian. So in `auto` the clip is sent once per
candidate language (`transcription.auto_languages`, default `en,no`) at the
same time, while a small local Whisper model listens to the first seconds and
picks which answer to keep. The user waits for the slower of those, not for
both in turn; each short clip costs one Cartesia request per candidate.
Large clips (over ~1 MB) are detected first and sent once, because two big
parallel uploads slow each other down more than detection costs.
"""
from __future__ import annotations

import io
import re
import threading

from . import settings_store
from .log import log_exception

LANGUAGE_KEY = "transcription.language"
AUTO_LANGUAGES_KEY = "transcription.auto_languages"
AUTO = "auto"
DEFAULT_LANGUAGE = "en"
DEFAULT_AUTO_LANGUAGES = ("en", "no")
_CODE = re.compile(r"^[a-z]{2,3}$")
# Whisper often hears Norwegian as Danish, Nynorsk or Swedish; count them as
# Norwegian unless they are candidates of their own.
_RELATED = {"no": ("no", "nn", "da", "sv")}
_DETECT_SECONDS = 8
_DETECT_MODEL = "tiny"
# Above this size two parallel uploads slow each other down more than the
# ~0.25 s detection costs, so large clips detect first and upload once.
_PARALLEL_MAX_BYTES = 1_000_000

_model = None
_model_lock = threading.Lock()


def selected() -> str:
    value = (settings_store.get_text(LANGUAGE_KEY) or "").strip().lower()
    return value if value == AUTO or _CODE.match(value) else DEFAULT_LANGUAGE


def auto_languages() -> tuple[str, ...]:
    raw = settings_store.get_text(AUTO_LANGUAGES_KEY) or ""
    codes = tuple(dict.fromkeys(
        c.strip().lower() for c in raw.split(",") if _CODE.match(c.strip().lower())))
    return codes if len(codes) >= 2 else DEFAULT_AUTO_LANGUAGES


def validate(value) -> str:
    if not isinstance(value, str):
        raise ValueError("language must be a string")
    value = value.strip().lower()
    if value != AUTO and not _CODE.match(value):
        raise ValueError("language must be 'auto' or an ISO 639-1 code such as 'en' or 'no'")
    return value


def validate_auto_languages(value) -> str:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("auto_languages must be a list of language codes")
    codes = list(dict.fromkeys(v.strip().lower() for v in value if v.strip()))
    if len(codes) < 2 or not all(_CODE.match(c) for c in codes):
        raise ValueError("auto_languages needs at least two ISO 639-1 codes")
    return ",".join(codes)


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(_DETECT_MODEL, device="cpu", compute_type="int8")
        return _model


_warming = threading.Event()


def warm() -> None:
    """Load the detector in the background so the first clip is not slow."""
    if _model is not None or _warming.is_set():
        return
    _warming.set()

    def run():
        try:
            _load_model()
        except Exception as e:  # noqa: BLE001
            log_exception("sttLanguageWarmFail", e)
    threading.Thread(target=run, daemon=True, name="stt-language-warm").start()


def _first_seconds(audio_bytes: bytes, seconds: int):
    """Mono 16 kHz float32 samples of the clip's opening, decoding no further."""
    import av
    import numpy as np
    chunks, total, limit = [], 0, 16000 * seconds
    with av.open(io.BytesIO(audio_bytes)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                samples = out.to_ndarray().reshape(-1)
                chunks.append(samples)
                total += len(samples)
            if total >= limit:
                break
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return (np.concatenate(chunks)[:limit].astype(np.float32) / 32768.0)


def detect(audio_bytes: bytes, candidates: tuple[str, ...]) -> str | None:
    """Most likely of `candidates` for the clip's first seconds, or None."""
    audio = _first_seconds(audio_bytes, _DETECT_SECONDS)
    if not len(audio):
        return None
    _, _, probabilities = _load_model().detect_language(audio)
    probabilities = dict(probabilities)
    scores = {}
    for code in candidates:
        related = [c for c in _RELATED.get(code, (code,))
                   if c == code or c not in candidates]
        scores[code] = sum(probabilities.get(c, 0.0) for c in related)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def run_auto(transcribe_with, audio_bytes: bytes, candidates: tuple[str, ...]):
    """Transcribe once per candidate in parallel and keep the detected one.

    `transcribe_with(language)` returns the provider's result. If detection
    fails, the first candidate's result is used. Large clips are detected
    first and sent once.
    """
    if len(audio_bytes) > _PARALLEL_MAX_BYTES:
        try:
            chosen = detect(audio_bytes, candidates) or candidates[0]
        except Exception as e:  # noqa: BLE001 — detection is best effort
            log_exception("sttLanguageDetectFail", e)
            chosen = candidates[0]
        return chosen, transcribe_with(chosen)
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=len(candidates) + 1,
                              thread_name_prefix="stt-language")
    try:
        detection = pool.submit(detect, audio_bytes, candidates)
        results = {code: pool.submit(transcribe_with, code) for code in candidates}
        try:
            chosen = detection.result() or candidates[0]
        except Exception as e:  # noqa: BLE001 — detection is best effort
            log_exception("sttLanguageDetectFail", e)
            chosen = candidates[0]
        return chosen, results[chosen].result()
    finally:
        # Return as soon as the kept answer is in; the discarded request
        # finishes in the background instead of delaying the user.
        pool.shutdown(wait=False)
