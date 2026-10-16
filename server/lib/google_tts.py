"""Google Chirp 3 HD text-to-speech, the built-in `google` voice provider.

One batch Cloud Text-to-Speech `text:synthesize` request per clip, MP3 out. The input is plain
text, or, when the spoken text carries Clarp's own `<break time="…"/>` pauses, Host-built SSML
whose only element is that break: every other character is XML-escaped text, so no client or
agent markup reaches Google. Google marks SSML breaks for Chirp 3 HD as Preview, so a pause is
approximate. Text over Google's 5,000-byte input limit is refused (`text_too_long`), never cut.

It authenticates like [google_stt] (lib/google_stt.access_token: an authorized_user ADC file,
no scope request), from ONE source: an explicit [google_tts] section, or else [google_stt].
"Configured" says nothing about whether that account may call Text-to-Speech.

Every failure is a GoogleTTSError whose `code` is one of ERROR_CODES; no response body, header
or credential path is ever put in an error.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import pathlib
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from xml.sax.saxutils import escape

from . import google_stt
from .voice_markup import strip_emotion_tags

PROVIDER = "google"
ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"
# The 30 Chirp 3 HD voices; a voice id is "<language>-Chirp3-HD-<name>".
VOICE_NAMES = (
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede", "Autonoe", "Callirrhoe",
    "Charon", "Despina", "Enceladus", "Erinome", "Fenrir", "Gacrux", "Iapetus", "Kore",
    "Laomedeia", "Leda", "Orus", "Puck", "Pulcherrima", "Rasalgethi", "Sadachbia",
    "Sadaltager", "Schedar", "Sulafat", "Umbriel", "Vindemiatrix", "Zephyr", "Zubenelgenubi",
)
LANGUAGES = ("nb-NO",)
VOICES = tuple(f"{language}-Chirp3-HD-{name}" for language in LANGUAGES for name in VOICE_NAMES)
# The documented default for a NEW google choice only; a saved voice is never replaced by it.
DEFAULT_VOICE = "nb-NO-Chirp3-HD-Kore"

MAX_INPUT_BYTES = 5_000            # Google's limit on input.text / input.ssml, in UTF-8 bytes
MAX_AUDIO_BYTES = 8 * 1024 * 1024  # decoded MP3
MAX_RESPONSE_BYTES = MAX_AUDIO_BYTES * 4 // 3 + 64 * 1024   # base64 audio plus the JSON around it
TIME_BUDGET_SECONDS = 30.0         # token refresh and the request together
MAX_BREAK_MS = 3_000
CHUNK_BYTES = 64 * 1024

# Reported by the voice catalog for a provider that cannot speak.
CATALOG_ERROR_CODES = (
    "google_credentials_missing", "google_project_missing", "google_auth_failed", "google_quota")
ERROR_CODES = CATALOG_ERROR_CODES + (
    "voice_unavailable", "text_too_long", "google_timeout", "google_request_failed",
    "google_http_error", "google_bad_response", "google_audio_too_large", "google_empty_audio")

_MARKUP_RE = re.compile(r"<(/?)(break|speed|volume|emotion)\b([^>]*)>", re.IGNORECASE)
_BREAK_TIME_RE = re.compile(r"""\btime\s*=\s*["'](\d{1,6}(?:\.\d{1,3})?)\s*(ms|s)["']""",
                            re.IGNORECASE)


class GoogleTTSError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        assert code in ERROR_CODES, code
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Credentials:
    source: str            # "google_tts" or "google_stt"
    credentials_file: str  # "" unless readable
    project: str
    error: str             # "" or one of CATALOG_ERROR_CODES

    def status(self) -> dict:
        """What /tts/providers may say: the source and state, never the file or project."""
        return {"credential_source": self.source, "configured": not self.error,
                "error": self.error or None}


def _readable(path: str) -> str:
    if not path:
        return ""
    resolved = pathlib.Path(os.path.expanduser(path))
    return str(resolved) if os.access(resolved, os.R_OK) and resolved.is_file() else ""


def credentials(cfg) -> Credentials:
    """The one credential source synthesis uses. An explicit [google_tts] that is incomplete
    or unreadable is an error; it never falls back to [google_stt]."""
    if cfg.google_tts_section:
        source, credentials_file, project = (
            "google_tts", _readable(cfg.google_tts_credentials), cfg.google_tts_project)
    else:
        source, credentials_file, project = (
            "google_stt", cfg.google_stt_credentials_file(), cfg.google_stt_project)
    error = ("google_credentials_missing" if not credentials_file else
             "google_project_missing" if not project else "")
    return Credentials(source, credentials_file, project, error)


def language_code(voice: str) -> str:
    return voice.rsplit("-Chirp3-HD-", 1)[0]


def _break_ms(attributes: str) -> int | None:
    match = _BREAK_TIME_RE.search(attributes)
    if not match:
        return None
    value = float(match.group(1)) * (1 if match.group(2).lower() == "ms" else 1000)
    return min(MAX_BREAK_MS, round(value)) if value > 0 else None


def synthesis_input(text: str) -> dict:
    """`{"text": …}`, or `{"ssml": …}` when a Clarp pause survives. Only <break> becomes an
    element; Clarp's other voice tags are dropped and everything else is escaped text."""
    text = strip_emotion_tags(text)
    parts: list[str] = []
    pauses = 0
    position = 0
    for match in _MARKUP_RE.finditer(text):
        parts.append(escape(text[position:match.start()]))
        position = match.end()
        milliseconds = (_break_ms(match.group(3))
                        if match.group(2).lower() == "break" and not match.group(1) else None)
        if milliseconds is None:
            parts.append(" ")
        else:
            parts.append(f'<break time="{milliseconds}ms"/>')
            pauses += 1
    parts.append(escape(text[position:]))
    if not pauses:
        return {"text": " ".join(_MARKUP_RE.sub(" ", text).split())}
    return {"ssml": "<speak>" + "".join(parts).strip() + "</speak>"}


def _urllib_transport(url: str, body: bytes, headers: dict, deadline: float,
                      limit: int) -> tuple[int, bytes]:
    """(status, at most limit + 1 body bytes); TimeoutError once `deadline` passes. An error
    status comes back without its body."""
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=deadline - time.monotonic()) as response:
            chunks, size = [], 0
            while size <= limit:
                if time.monotonic() > deadline:
                    raise TimeoutError("Google TTS time budget spent")
                chunk = response.read(min(CHUNK_BYTES, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            return response.status, b"".join(chunks)
    except urllib.error.HTTPError as error:
        return error.code, b""


def _http_error(status: int) -> GoogleTTSError:
    if status in (401, 403):
        return GoogleTTSError("google_auth_failed", f"HTTP {status}")
    if status == 429:
        return GoogleTTSError("google_quota", "HTTP 429")
    return GoogleTTSError("google_http_error", f"HTTP {status}")


def synthesize(*, text: str, voice: str, out_path: pathlib.Path, creds: Credentials,
               on_chunk=None, transport=_urllib_transport,
               token=google_stt.access_token) -> int:
    """Write the clip's MP3 to `out_path` and return its size. The file appears only when the
    whole clip arrived; `on_chunk(index, bytes)` then sees it in order."""
    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    if creds.error:
        raise GoogleTTSError(creds.error)
    if voice not in VOICES:
        raise GoogleTTSError("voice_unavailable")
    spoken = synthesis_input(text)
    if len(next(iter(spoken.values())).encode("utf-8")) > MAX_INPUT_BYTES:
        raise GoogleTTSError("text_too_long")
    try:
        access = token(creds.credentials_file, timeout=max(0.1, deadline - time.monotonic()))
    except google_stt.GoogleSTTError as error:
        # access_token words a transport failure "request failed"; anything else is the account.
        raise GoogleTTSError("google_request_failed" if "request failed" in str(error)
                             else "google_auth_failed") from error
    body = json.dumps({
        "input": spoken,
        "voice": {"languageCode": language_code(voice), "name": voice},
        "audioConfig": {"audioEncoding": "MP3"},
    }).encode()
    headers = {"Authorization": f"Bearer {access}", "x-goog-user-project": creds.project,
               "Content-Type": "application/json"}
    if time.monotonic() >= deadline:
        raise GoogleTTSError("google_timeout")
    try:
        status, payload = transport(ENDPOINT, body, headers, deadline, MAX_RESPONSE_BYTES)
    except TimeoutError as error:
        raise GoogleTTSError("google_timeout") from error
    except (urllib.error.URLError, OSError) as error:
        if isinstance(getattr(error, "reason", None), TimeoutError):
            raise GoogleTTSError("google_timeout") from error
        raise GoogleTTSError("google_request_failed") from error
    if status != 200:
        raise _http_error(status)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise GoogleTTSError("google_audio_too_large")
    try:
        encoded = json.loads(payload).get("audioContent")
    except (ValueError, AttributeError) as error:
        raise GoogleTTSError("google_bad_response") from error
    if not isinstance(encoded, str):
        raise GoogleTTSError("google_bad_response")
    if len(encoded) // 4 * 3 > MAX_AUDIO_BYTES:
        raise GoogleTTSError("google_audio_too_large")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise GoogleTTSError("google_bad_response") from error
    if not audio:
        raise GoogleTTSError("google_empty_audio")
    _publish(audio, out_path)
    if on_chunk is not None:
        for index, start in enumerate(range(0, len(audio), CHUNK_BYTES)):
            on_chunk(index, audio[start:start + CHUNK_BYTES])
    return len(audio)


def _publish(audio: bytes, out_path: pathlib.Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=out_path.parent, prefix=".google-tts-",
                                         suffix=".part")
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(audio)
        os.replace(temporary, out_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
