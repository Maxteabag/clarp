"""Speech-to-text providers: the catalogue, the two switches, and dispatch.

Two things are chosen independently:

  * the **engine** that turns a clip into text - the local Whisper family or a
    cloud model such as Deepgram Nova-3 - stored as `transcription.engine`;
  * the **turn-taking strategy** - `native` (Silero VAD + Smart Turn on the
    phone, with eager commit and retraction) or `provider` (a streaming
    recogniser that owns end-of-turn itself) - stored as
    `transcription.turn_taking`.

A cloud engine that only offers batch transcription cannot own turn-taking,
so `provider` is only accepted when the selected engine advertises it. Each
engine also declares its biasing budget, which is what lets the vocabulary
compiler fit one profile to every model and lets the app show how much fits.

Mirrors `tts_providers`: the catalogue is data, credentials come from config,
and adapters are thin HTTP clients that never see anything but bytes.
"""
from __future__ import annotations

from . import settings_store
from .vocab_compile import budget_for

ENGINE_KEY = "transcription.engine"
TURN_TAKING_KEY = "transcription.turn_taking"

LOCAL_ENGINE = "local"
TURN_NATIVE = "native"
TURN_PROVIDER = "provider"
VALID_TURN_TAKING = frozenset({TURN_NATIVE, TURN_PROVIDER})

# One row per provider. `models` are the ids the /transcribe X-Transcription-
# Model header may carry ("deepgram:nova-3"). `turn_detection` says whether
# the provider can own end-of-turn on a live stream; batch-only providers
# transcribe what they are given and leave turn-taking to the phone.
CATALOG: tuple[dict, ...] = (
    {"id": "deepgram", "name": "Deepgram", "kind": "cloud",
     "credential": "DEEPGRAM_API_KEY", "streaming": True,
     "turn_detection": "own", "turn_detection_model": "flux-general-en",
     "models": (
         {"id": "deepgram:nova-3", "model": "nova-3", "name": "Deepgram Nova-3",
          "biasing": "keyterm"},
     )},
    {"id": "elevenlabs", "name": "ElevenLabs Scribe", "kind": "cloud",
     "credential": "ELEVEN_API_KEY", "streaming": True,
     "turn_detection": "native", "turn_detection_model": "",
     "models": (
         {"id": "elevenlabs:scribe_v2", "model": "scribe_v2",
          "name": "ElevenLabs Scribe v2", "biasing": "keyterms"},
     )},
    {"id": "cartesia", "name": "Cartesia Ink", "kind": "cloud",
     "credential": "CARTESIA_API_KEY", "streaming": True,
     "turn_detection": "own", "turn_detection_model": "ink-2",
     "models": (
         {"id": "cartesia:ink-whisper", "model": "ink-whisper",
          "name": "Cartesia Ink-Whisper", "biasing": "none"},
     )},
)
VALID_IDS = frozenset(item["id"] for item in CATALOG)


def provider_of(model_id: str) -> str:
    return model_id.split(":", 1)[0].strip().lower() if ":" in model_id else ""


def is_cloud_model(model_id: str) -> bool:
    return provider_of(model_id) in VALID_IDS


def _definition(provider: str) -> dict | None:
    for item in CATALOG:
        if item["id"] == provider:
            return item
    return None


def _model_row(model_id: str) -> dict | None:
    definition = _definition(provider_of(model_id))
    if definition is None:
        return None
    for row in definition["models"]:
        if row["id"] == model_id:
            return row
    return None


def _key_for(cfg, provider: str) -> str:
    if provider == "deepgram":
        return cfg.deepgram_key()
    if provider == "elevenlabs":
        return cfg.eleven_key()
    if provider == "cartesia":
        return cfg.cartesia_key()
    return ""


def selected_engine() -> str:
    return settings_store.get_text(ENGINE_KEY, default=LOCAL_ENGINE) or LOCAL_ENGINE


def selected_turn_taking() -> str:
    value = settings_store.get_text(TURN_TAKING_KEY, default=TURN_NATIVE)
    return value if value in VALID_TURN_TAKING else TURN_NATIVE


def cloud_models(*, available_only: bool = False) -> list[dict]:
    """Catalogue rows in the shape `capabilities()["models"]` uses."""
    from .config import load
    cfg = load()
    rows: list[dict] = []
    for definition in CATALOG:
        available = bool(_key_for(cfg, definition["id"]))
        if available_only and not available:
            continue
        for model in definition["models"]:
            budget = budget_for(definition["id"], model["model"])
            rows.append({
                "id": model["id"], "name": model["name"],
                "provider": definition["id"], "model": model["model"],
                "weight": "cloud", "available": available,
                "credential": definition["credential"],
                "turn_detection": definition["turn_detection"],
                "biasing": model["biasing"],
                "budget": {"unit": budget.unit, "capacity": budget.capacity,
                           "max_term_chars": budget.max_term_chars},
            })
    return rows


def status() -> dict:
    """Everything the settings screen needs to draw both switches."""
    engine = selected_engine()
    strategy = selected_turn_taking()
    models = cloud_models()
    selected_row = next((m for m in models if m["id"] == engine), None)
    from .heard_audio import enabled as retain_audio
    return {
        "engine": engine,
        "turn_taking": strategy,
        "retain_audio": retain_audio(),
        "turn_taking_options": [
            {"id": TURN_NATIVE, "name": "Native (on-device VAD + Smart Turn)",
             "available": True},
            {"id": TURN_PROVIDER, "name": "Provider-owned end of turn",
             "available": bool(selected_row and selected_row["turn_detection"] == "own")},
        ],
        "providers": [
            {"id": d["id"], "name": d["name"], "kind": d["kind"],
             "credential": d["credential"],
             "turn_detection": d["turn_detection"],
             "available": any(m["available"] for m in models if m["provider"] == d["id"])}
            for d in CATALOG
        ],
        "models": models,
    }


def update_settings(data: dict) -> dict:
    """Validate and store the two switches. Raises ValueError on bad input."""
    engine = data.get("engine", selected_engine())
    strategy = data.get("turn_taking", selected_turn_taking())
    if not isinstance(engine, str) or not isinstance(strategy, str):
        raise ValueError("engine and turn_taking must be strings")
    engine = engine.strip() or LOCAL_ENGINE
    strategy = strategy.strip() or TURN_NATIVE
    if engine != LOCAL_ENGINE and _model_row(engine) is None:
        # Local model ids ("faster-whisper:small.en") are governed by the
        # installed-model registry, not this catalogue; only reject cloud ids
        # we do not know.
        if is_cloud_model(engine) or ":" not in engine:
            raise ValueError(f"unknown transcription engine: {engine}")
    if strategy not in VALID_TURN_TAKING:
        raise ValueError(f"unknown turn-taking strategy: {strategy}")
    row = _model_row(engine)
    if strategy == TURN_PROVIDER and not (row and _definition(row["id"].split(":")[0])["turn_detection"] == "own"):
        raise ValueError("provider-owned turn taking needs an engine that detects turns")
    retain = data.get("retain_audio")
    if retain is not None and not isinstance(retain, bool):
        raise ValueError("retain_audio must be boolean")
    settings_store.set_text(ENGINE_KEY, engine)
    settings_store.set_text(TURN_TAKING_KEY, strategy)
    if retain is not None:
        from .heard_audio import set_enabled
        set_enabled(retain)
    return status()


def split_terms(vocab_payload: str) -> list[str]:
    """The compiler renders TERMS-unit payloads as 'a, b, c'; undo that."""
    return [t.strip() for t in (vocab_payload or "").split(",") if t.strip()]


def ends_terminal(text: str) -> bool:
    return bool(text and text.rstrip()[-1:] in ".!?")


def transcribe(model_id: str, audio_bytes: bytes, content_type: str,
               vocab_payload: str, *, timeout: float = 30.0
               ) -> tuple[str, bool, float]:
    """Send one clip to the cloud engine named by `model_id`.

    Returns `(text, ends_terminal, duration_seconds)` like the local path.
    Raises RuntimeError subclasses from the adapters; the handler maps them.
    """
    from .config import load
    row = _model_row(model_id)
    if row is None:
        raise ValueError(f"unknown transcription engine: {model_id}")
    provider = provider_of(model_id)
    api_key = _key_for(load(), provider)
    if not api_key:
        raise RuntimeError(f"{provider} API key is not configured")
    terms = split_terms(vocab_payload)
    if provider == "deepgram":
        from .deepgram_stt import transcribe as run
    elif provider == "elevenlabs":
        from .eleven_stt import transcribe as run
    else:
        from .cartesia_stt import transcribe as run
    text, duration = run(
        audio_bytes=audio_bytes, content_type=content_type, api_key=api_key,
        model=row["model"], keyterms=terms, timeout=timeout)
    from .hallucinations import is_pure_hallucination
    text = (text or "").strip()
    if is_pure_hallucination(text):
        text = ""
    return text, ends_terminal(text), float(duration or 0.0)
