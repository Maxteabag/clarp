"""Dependency-injection container for the HTTP server.

Wraps every external collaborator (TTS, audio stream, STT) plus the
filesystem paths the handler reads. Production boots a ServerContext with
real implementations; tests boot one with `FakeTTSEngine` and a stub STT —
no API calls, no real subprocesses.

The HTTP handler reads its dependencies via `self.server.ctx`, which is the
standard Python pattern for sharing state without module-level globals.

The context is frozen. Configuration changes by replacement (`with_`). Four
services are installed or swapped after construction - the STT (model
switch), tool explanations and the herald (both built after the context), and
the runtime client - and only through the locked `replace_service` path, so
every holder of the context sees the same current service.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from .audio_stream import AudioStream
from .clip_stream import ClipStreamBroker
from .config import load as load_config
from .log import log_exception
from .paths import RuntimePaths
from .tts_engine import ElevenLabsEngine, TTSEngine
from .vocab_service import TranscriptionVocab, VocabService

__all__ = ["ServerContext", "StubSTT", "STTLike", "TranscriptionVocab", "resolve_root"]


def resolve_root(self_file: pathlib.Path, env) -> pathlib.Path:
    """Locate the project root by looking for static/index.html.

    Two layouts:
      - repo:    <repo>/server/lib/context.py  → root = self_file.parent.parent.parent
      - install: <share>/lib/context.py        → root = self_file.parent.parent
    Honour $CLAUDE_PWA_ROOT first, then probe.
    """
    override = env.get("CLAUDE_PWA_ROOT")
    if override:
        return pathlib.Path(override)
    install_root = self_file.parent.parent          # context.py is at <root>/lib/
    repo_root = install_root.parent                 # repo layout has another level up
    for candidate in (install_root, repo_root):
        if (candidate / "static" / "index.html").is_file():
            return candidate
    return repo_root  # fall back loudly


class STTLike(Protocol):
    """Minimal interface the /transcribe handler needs.

    `WhisperSTT` implements this; tests pass a stub that resolves instantly
    without loading a Whisper model.
    """
    ready: Any  # threading.Event in prod; .is_set() returns bool in any case

    def transcribe_bytes(
        self, audio_bytes: bytes, content_type: str, vocab_prompt: str,
        *, wait: float = 0.0
    ) -> tuple[str, bool, float]: ...


class StubSTT:
    """Default STT for tests — always-ready, returns a canned transcription.

    Construct with `StubSTT(text="hello world")` to control what /transcribe
    returns. Real STT replacement goes through `ctx.replace_stt(...)`.
    """

    def __init__(self, text: str = "", ends_terminal: bool = False):
        self.text = text
        self.ends_terminal = ends_terminal
        self.ready = threading.Event()
        self.ready.set()
        self.calls: list[tuple[bytes, str, str]] = []

    def transcribe_bytes(self, audio_bytes, content_type, vocab_prompt, *, wait: float = 0.0):
        self.calls.append((audio_bytes, content_type, vocab_prompt))
        return self.text, self.ends_terminal, 0.0


class HeraldLike(Protocol):
    """The part of lib.herald.HeraldManager the handler and workers call."""

    def set_focus(self, session: str | None, *args: Any, **kwargs: Any) -> Any: ...

    def ingest_clip(self, *args: Any, **kwargs: Any) -> Any: ...


class ToolExplanationsLike(Protocol):
    """lib.tool_explanations.ToolExplanations as the handler uses it."""

    def request(self, level: Any, items: Any, **kwargs: Any) -> Any: ...


class RuntimeClientLike(Protocol):
    """lib.runtime_bridge.RuntimeClient: the split runtime's RPC surface."""

    def status(self) -> dict: ...


# Services installed or swapped after construction; see replace_service.
REPLACEABLE_SERVICES = frozenset({"stt", "tool_explanations", "herald", "runtime_client"})


@dataclass(frozen=True)
class ServerContext:
    # Filesystem layout
    root: pathlib.Path
    static: pathlib.Path
    audio_dir: pathlib.Path
    agents_path: pathlib.Path

    # Behaviour
    default_session: str

    # Injected services
    tts: TTSEngine
    stream: AudioStream
    stt: STTLike

    # Roster — used for announcement defaults. Transcription guidance only
    # includes live sessions from SQLite, never this static product roster.
    roster_names: tuple[str, ...]

    clip_broker: ClipStreamBroker | None = None

    # Optional bearer-token guard. Empty = no auth check.
    auth_token: str = ""
    local_tls_port: int = 0
    local_tls_directory: pathlib.Path | None = None
    relay_settings: Any | None = None

    # Where client-uploaded files (images/docs from the phone) are written,
    # one subdir per session. Defaulted/derived so existing construction sites
    # (tests) don't have to pass it; tests that exercise /upload inject a tmp
    # dir for isolation.
    uploads_dir: pathlib.Path | None = None
    # Managed storage for agent-published images/media. SQLite remains the
    # authoritative index; files here are opaque blob storage.
    media_dir: pathlib.Path | None = None
    # Production HTTP processes submit agent execution to a separately managed
    # runtime. Tests and the runtime process itself leave this unset and run the
    # injected/local dispatch implementation.
    runtime_client: RuntimeClientLike | None = None
    tool_explanations: ToolExplanationsLike | None = None
    # HeraldManager (lib.herald) deciding which off-focus agent's clip plays.
    # Production passes it to build_server, which installs it before any
    # worker or handler runs. None = no arbitration.
    herald: HeraldLike | None = None

    _service_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.clip_broker is None:
            object.__setattr__(self, "clip_broker", ClipStreamBroker())
        paths = RuntimePaths.from_home(pathlib.Path.home())
        if self.uploads_dir is None:
            derived = paths.uploads_dir
            object.__setattr__(self, "uploads_dir", derived)
        if self.media_dir is None:
            object.__setattr__(self, "media_dir", paths.media_dir)

    # ---- Replacement

    def with_(self, **changes: Any) -> "ServerContext":
        """A copy with `changes` applied; services are shared, not copied."""
        return dataclasses.replace(self, **changes)

    def replace_service(self, name: str, value: Any) -> Any:
        """Swap one of REPLACEABLE_SERVICES in place and return the old one.

        In place because the handler, the dispatch service and the workers
        all hold this same context; a copy would leave them on the old one.
        """
        if name not in REPLACEABLE_SERVICES:
            raise dataclasses.FrozenInstanceError(f"cannot replace {name!r}; use with_()")
        with self._service_lock:
            previous = getattr(self, name)
            object.__setattr__(self, name, value)
        return previous

    def replace_stt(self, stt: STTLike) -> STTLike:
        return self.replace_service("stt", stt)

    def install_tool_explanations(self, service: ToolExplanationsLike) -> None:
        self.replace_service("tool_explanations", service)

    def install_herald(self, herald: HeraldLike | None) -> None:
        self.replace_service("herald", herald)

    # ---- Helpers consumed by handlers (keep them on the ctx so tests can
    # override behaviour without monkey-patching the server module).

    def sw_version(self) -> str:
        """Newest mtime of any static file — drives auto-reload."""
        try:
            newest = max(
                (p.stat().st_mtime for p in self.static.rglob("*") if p.is_file()),
                default=0,
            )
        except OSError as e:
            log_exception("swVersionScanFail", e)
            newest = 0
        return str(int(newest))

    # ---- Vocabulary: the logic lives in vocab_service; these keep the
    # handler's `ctx.vocab_*` calls working.

    @property
    def vocab(self) -> VocabService:
        return VocabService(stt=lambda: self.stt)

    def active_agent_names(self) -> list[str]:
        return self.vocab.active_agent_names()

    def vocab_prompt(self, *, delegated: bool) -> str:
        return self.vocab.prompt(delegated=delegated)

    def vocab_for_transcription(self, *, delegated: bool, session: str = "", trace_id: str = "",
                                requested_model: str = "") -> TranscriptionVocab:
        return self.vocab.for_transcription(delegated=delegated, session=session, trace_id=trace_id,
                                            requested_model=requested_model)

    def vocab_preview(self, *, session: str = "", requested_model: str = "",
                      delegated: bool = False) -> dict:
        return self.vocab.preview(session=session, requested_model=requested_model, delegated=delegated)

    def vocab_compile_result(self, *, delegated: bool, provider: str = "faster-whisper", model: str = "",
                             recent_transcripts: tuple[str, ...] = (),
                             corrections: tuple[tuple[str, str], ...] = ()):
        return self.vocab.compile_result(delegated=delegated, provider=provider, model=model,
                                         recent_transcripts=recent_transcripts, corrections=corrections)

    def deployed_version(self) -> str:
        try:
            return (self.root / "DEPLOYED_VERSION").read_text().strip()
        except OSError:
            return "development"

    def deployed_release_id(self) -> str:
        try:
            return (self.root / "DEPLOYED_RELEASE_ID").read_text().strip()
        except OSError:
            return "development"

    def speak_announcement(
        self, text: str, voice_id: str | None, session: str | None = None
    ) -> None:
        """Synthesise a one-off clip. No-op when no api key is configured."""
        if not voice_id:
            return
        try:
            self.tts.synthesize(text, voice_id, session=session)
        except Exception as e:
            log_exception("speakAnnounceFail", e, detail=text[:60])

    @classmethod
    def production(cls, *, connect_runtime: bool = True) -> "ServerContext":
        """Build the ctx used by the live server. Reads config.toml."""
        from .agent_store import AGENTS_FILE, get_roster  # local: avoid cycle

        cfg = load_config()
        root = resolve_root(pathlib.Path(__file__).resolve(), os.environ)
        paths = RuntimePaths.from_home(pathlib.Path.home())
        static = root / "static"
        audio_dir = pathlib.Path(os.environ.get(
            "CLAUDE_PWA_AUDIO_DIR",
            str(paths.audio_dir),
        ))
        tts = ElevenLabsEngine(
            audio_dir,
            api_key=cfg.eleven_key(),
            model=cfg.eleven_model,
            speed=cfg.eleven_speed,
        )
        stream = AudioStream(audio_dir)
        # Caller is responsible for starting the stream + STT loading once
        # they want background threads running.
        from .stt import (CustomAdapterSTT, DisabledSTT, SubprocessWhisperSTT, UnavailableSTT,
                          WhisperCppSTT, WhisperSTT, _installed_model_records)
        if not cfg.whisper_enabled:
            stt = DisabledSTT()
        else:
            provider = getattr(cfg, "whisper_provider", "faster-whisper")
            default_id = f"{provider}:{cfg.whisper_model}"
            from .custom_stt_adapters import get as custom_stt_adapter
            custom_manifest = custom_stt_adapter(provider)
            record = next((item for item in _installed_model_records()
                           if item["id"] == default_id), None)
            if custom_manifest is not None:
                stt = CustomAdapterSTT(custom_manifest, cfg.whisper_model)
            elif record and provider == "whisper.cpp":
                stt = WhisperCppSTT(
                    cfg.whisper_model, model_source=record["_local_path"],
                    runtime_source=record["_runtime_path"])
            elif record:
                stt_cls = (SubprocessWhisperSTT
                           if getattr(cfg, "whisper_isolate", True) else WhisperSTT)
                stt = stt_cls(
                    cfg.whisper_model, cfg.whisper_compute,
                    model_source=record["_local_path"])
            else:
                stt = UnavailableSTT(
                    cfg.whisper_model, cfg.whisper_compute,
                    f"configured transcription model is not installed: {default_id}",
                    provider=provider)
        runtime_client = None
        if connect_runtime:
            from .runtime_bridge import RuntimeClient
            runtime_client = RuntimeClient(paths.runtime_socket)
        from .relay_settings import load as load_relay_settings
        return cls(
            root=root,
            static=static,
            audio_dir=audio_dir,
            agents_path=AGENTS_FILE,
            default_session=cfg.default_session,
            auth_token=cfg.auth_token,
            local_tls_port=cfg.local_tls_port if cfg.local_enabled else 0,
            local_tls_directory=pathlib.Path(cfg._config_path).parent / "local-tls",
            relay_settings=(load_relay_settings(
                pathlib.Path(cfg.relay_config_path)) if cfg.relay_enabled else None),
            uploads_dir=paths.uploads_dir,
            media_dir=paths.media_dir,
            tts=tts,
            stream=stream,
            stt=stt,
            roster_names=tuple(get_roster().keys()),
            runtime_client=runtime_client,
        )


def _frozen_setattr(self: ServerContext, name: str, value: Any) -> None:
    """Refuse assignment except the replaceable services.

    TODO(integration): server.py still assigns `ctx.stt = replacement`
    (STT switch), `ctx.tool_explanations = ToolExplanations()` and
    `ctx.herald = herald` (build_server). Those three lines become
    `ctx.replace_stt(replacement)`, `ctx.install_tool_explanations(...)` and
    `ctx.install_herald(herald)`; this shim then only raises.
    """
    if name in REPLACEABLE_SERVICES:
        self.replace_service(name, value)
        return
    raise dataclasses.FrozenInstanceError(f"cannot assign to field {name!r}; use with_()")


ServerContext.__setattr__ = _frozen_setattr  # type: ignore[method-assign]
