"""Dependency-injection container for the HTTP server.

Wraps every external collaborator (TTS, audio stream, STT) plus the
filesystem paths the handler reads. Production boots a ServerContext with
real implementations; tests boot one with `FakeTTSEngine` and a stub STT —
no API calls, no real subprocesses.

The HTTP handler reads its dependencies via `self.server.ctx`, which is the
standard Python pattern for sharing state without module-level globals.
"""
from __future__ import annotations

import os
import pathlib
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from .audio_stream import AudioStream
from .clip_stream import ClipStreamBroker
from .config import load as load_config
from .log import log_exception
from .paths import RuntimePaths
from .tts_engine import ElevenLabsEngine, TTSEngine
from .vocab import (
    build_initial_prompt,
    delegation_agent_names_enabled,
    read_technical_glossary,
)


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
    returns. Real STT replacement happens by setting `ctx.stt = ...`.
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


@dataclass
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

    # Where client-uploaded files (images/docs from the phone) are written,
    # one subdir per session. Defaulted/derived so existing construction sites
    # (tests) don't have to pass it; tests that exercise /upload inject a tmp
    # dir for isolation.
    uploads_dir: pathlib.Path | None = None
    # Managed storage for agent-published images/media. SQLite remains the
    # authoritative index; files here are opaque blob storage.
    media_dir: pathlib.Path | None = None

    def __post_init__(self):
        if self.clip_broker is None:
            object.__setattr__(self, "clip_broker", ClipStreamBroker())
        paths = RuntimePaths.from_home(pathlib.Path.home())
        if self.uploads_dir is None:
            derived = paths.uploads_dir
            object.__setattr__(self, "uploads_dir", derived)
        if self.media_dir is None:
            object.__setattr__(self, "media_dir", paths.media_dir)

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

    def active_agent_names(self) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()

        def add(name: object) -> None:
            text = str(name or "").strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                names.append(text)

        try:
            from . import agents as agents_db  # local: avoid startup cycles
            for info in agents_db.list_agents():
                add(info.get("persona"))
        except Exception as e:  # noqa: BLE001 - prompt bias must never break STT
            log_exception("vocabAgentsFail", e)
        return names

    def vocab_prompt(self, *, delegated: bool) -> str:
        """Legacy string form of the biasing prompt.

        Retained for callers that only want a prompt. Prefer
        `vocab_compile_result` when the caller can record the audit row -
        that is the path that makes a transcript traceable to its prompt.
        """
        return self.vocab_compile_result(delegated=delegated).payload

    def vocab_compile_result(
        self,
        *,
        delegated: bool,
        provider: str = "faster-whisper",
        model: str = "",
        recent_transcripts: tuple[str, ...] = (),
        corrections: tuple[tuple[str, str], ...] = (),
    ):
        """Compile this turn's biasing payload against the active model.

        The two long-standing settings still govern what may contribute:
        delegation turns are primed with agent names, ordinary turns with the
        user's technical glossary. Everything below that is new - the glossary
        becomes a static pack and the rest is generated, then all of it is
        fitted to whatever the provider actually accepts.
        """
        from .vocab_budget import Pack, Term
        from .vocab_compile import Sources, compile_for
        from .vocab_generators import estimate_rarity

        include_names = delegated and delegation_agent_names_enabled()
        static: list[Pack] = []
        if not delegated:
            glossary = read_technical_glossary()
            terms = tuple(
                Term(text=line, pack="glossary", rarity=estimate_rarity(line))
                for line in (l.strip() for l in glossary.splitlines())
                if line
            )
            if terms:
                # Curated by hand, so it earns a floor: a term the user typed
                # should not be crowded out by generated workspace noise.
                static.append(Pack(
                    name="glossary", terms=terms, priority=1.5, floor=3))

        return compile_for(
            provider=provider,
            model=model,
            static_packs=static,
            sources=Sources(
                agent_names=(
                    tuple(self.active_agent_names()) if include_names else ()),
                recent_transcripts=recent_transcripts,
                corrections=corrections,
            ),
        )

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
    def production(cls) -> "ServerContext":
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
        return cls(
            root=root,
            static=static,
            audio_dir=audio_dir,
            agents_path=AGENTS_FILE,
            default_session=cfg.default_session,
            auth_token=cfg.auth_token,
            uploads_dir=paths.uploads_dir,
            media_dir=paths.media_dir,
            tts=tts,
            stream=stream,
            stt=stt,
            roster_names=tuple(get_roster().keys()),
        )
