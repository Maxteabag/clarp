"""Google Chirp 3 batch speech-to-text (Cloud Speech-to-Text v2).

Chirp 3 was the engine that got short Norwegian clips right where Cartesia
Ink-Whisper produced Icelandic or invented subtitles on silence, and it
returns nothing for a silent clip. It is file-based: the phone owns turn
taking and the Host sends each finished clip to the synchronous `recognize`
method.

The Speech API rejects API keys, so this authenticates as the Host user's
Application Default Credentials (an `authorized_user` refresh token written by
`gcloud auth application-default login`) and caches the access token until it
expires. Synchronous recognition takes at most 60 s of audio, so a clip is
decoded once to 16 kHz mono PCM and longer ones are sent as <=55 s pieces in
parallel, their text joined in order.
"""
from __future__ import annotations

import base64
import concurrent.futures
import io
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

TOKEN_URL = "https://oauth2.googleapis.com/token"
SAMPLE_RATE = 16_000
PIECE_SECONDS = 55
_BYTES_PER_SECOND = SAMPLE_RATE * 2  # s16le mono
# Clarp's ISO 639-1 codes to the BCP-47 tags Chirp expects. Anything not
# listed is passed through, so "en-GB" or "nb-NO" can be stored directly.
BCP47 = {
    "no": "nb-NO", "nb": "nb-NO", "nn": "nn-NO", "en": "en-US",
    "sv": "sv-SE", "da": "da-DK", "de": "de-DE", "fi": "fi-FI",
    "fr": "fr-FR", "es": "es-ES", "it": "it-IT", "nl": "nl-NL",
    "pl": "pl-PL", "pt": "pt-BR", "is": "is-IS", "ja": "ja-JP",
    "ko": "ko-KR", "zh": "cmn-Hans-CN", "uk": "uk-UA", "ru": "ru-RU",
}

_TOKEN_LOCK = threading.Lock()
_TOKENS: dict[str, tuple[str, float]] = {}  # credentials path -> (token, expiry)


class GoogleSTTError(RuntimeError):
    pass


def language_tag(code: str) -> str:
    code = (code or "").strip()
    if "-" in code:
        return code
    return BCP47.get(code.lower(), code.lower() or "en-US")


def _post(request: urllib.request.Request, timeout: float, what: str) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise GoogleSTTError(
            f"{what} HTTP {error.code}: {detail or error.reason}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise GoogleSTTError(f"{what} request failed: {error}") from error


def access_token(credentials_file: str, *, timeout: float = 15.0) -> str:
    """A cached OAuth access token for the ADC file, refreshed near expiry."""
    with _TOKEN_LOCK:
        cached = _TOKENS.get(credentials_file)
        if cached and cached[1] - 60 > time.time():
            return cached[0]
    try:
        with open(credentials_file, encoding="utf-8") as f:
            creds = json.load(f)
    except (OSError, ValueError) as error:
        raise GoogleSTTError(f"cannot read Google credentials: {error}") from error
    if creds.get("type") != "authorized_user":
        raise GoogleSTTError(
            "Google credentials must be an authorized_user ADC file "
            "(gcloud auth application-default login)")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": creds.get("refresh_token", ""),
        "client_id": creds.get("client_id", ""),
        "client_secret": creds.get("client_secret", ""),
    }).encode()
    payload = _post(urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"}),
        timeout, "Google token")
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise GoogleSTTError("Google token response had no access_token")
    expiry = time.time() + float(payload.get("expires_in") or 3600)
    with _TOKEN_LOCK:
        _TOKENS[credentials_file] = (token, expiry)
    return token


def _forget_token(credentials_file: str) -> None:
    with _TOKEN_LOCK:
        _TOKENS.pop(credentials_file, None)


def decode_pcm(audio_bytes: bytes, *, timeout: float = 30.0) -> bytes:
    """16 kHz mono s16le PCM of any clip ffmpeg can read."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise GoogleSTTError("ffmpeg is not installed")
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"],
            input=audio_bytes, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GoogleSTTError(f"could not decode audio: {error}") from error
    if result.returncode != 0:
        raise GoogleSTTError(
            "could not decode audio: "
            + result.stderr.decode(errors="replace").strip()[:200])
    return result.stdout


def pieces(pcm: bytes, seconds: int = PIECE_SECONDS) -> list[bytes]:
    """PCM split into WAV files of at most `seconds` each, in order."""
    step = seconds * _BYTES_PER_SECOND
    out = []
    for start in range(0, len(pcm), step):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm[start:start + step])
        out.append(buffer.getvalue())
    return out


def _recognize(wav: bytes, *, credentials_file: str, project: str,
               location: str, model: str, language: str, timeout: float) -> str:
    host = ("speech.googleapis.com" if location == "global"
            else f"{location}-speech.googleapis.com")
    url = (f"https://{host}/v2/projects/{project}/locations/{location}"
           "/recognizers/_:recognize")
    body = json.dumps({
        "config": {"autoDecodingConfig": {}, "model": model,
                   "languageCodes": list(dict.fromkeys(
                       language_tag(code) for code in language.split(","))),
                   "features": {"enableAutomaticPunctuation": True}},
        "content": base64.b64encode(wav).decode("ascii"),
    }).encode()
    # Chirp answers a 55 s piece in ~1-2 s, so a long wait means a stuck
    # request (seen 2026-10-02: four 30 s timeouts in a row). Give up early
    # and try once more instead of making the user wait half a minute.
    seconds = len(wav) / _BYTES_PER_SECOND
    attempt_timeout = min(timeout, 8.0 + seconds / 5.0)
    for attempt in (0, 1):
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {access_token(credentials_file)}",
                     "x-goog-user-project": project,
                     "Content-Type": "application/json"})
        try:
            payload = _post(request, attempt_timeout, "Google STT")
            break
        except GoogleSTTError as error:
            # A revoked or early-expired token: refresh once and retry.
            if attempt == 0 and "HTTP 401" in str(error):
                _forget_token(credentials_file)
                continue
            # A stuck or overloaded request: one retry.
            message = str(error)
            if attempt == 0 and ("timed out" in message or "HTTP 5" in message
                                 or "HTTP 429" in message):
                continue
            raise
    texts = []
    for result in payload.get("results") or ():
        alternatives = result.get("alternatives") or ()
        if alternatives and isinstance(alternatives[0].get("transcript"), str):
            texts.append(alternatives[0]["transcript"].strip())
    return " ".join(t for t in texts if t)


def transcribe(*, audio_bytes: bytes, content_type: str, api_key: str,
               model: str = "chirp_3", keyterms: list[str] | None = None,
               language: str = "en", timeout: float = 60.0,
               project: str = "", location: str = "eu"
               ) -> tuple[str, float]:
    """`api_key` is the ADC credentials file (the provider's credential)."""
    del content_type, keyterms  # ffmpeg sniffs the container; no biasing
    if not api_key:
        raise GoogleSTTError("Google credentials are not configured")
    if not project:
        raise GoogleSTTError("[google_stt] project is not configured")
    pcm = decode_pcm(audio_bytes)
    duration = len(pcm) / _BYTES_PER_SECOND
    if not pcm:
        return "", 0.0
    wavs = pieces(pcm)
    tag = language_tag(language)

    def run(wav: bytes) -> str:
        return _recognize(wav, credentials_file=api_key, project=project,
                          location=location, model=model, language=tag,
                          timeout=timeout)

    if len(wavs) == 1:
        texts = [run(wavs[0])]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(wavs), 4)) as pool:
            texts = list(pool.map(run, wavs))
    return " ".join(t for t in texts if t), duration
