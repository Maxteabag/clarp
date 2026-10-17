"""Voice-output provider catalog and synthesis through custom adapters."""
from __future__ import annotations

from pathlib import Path


CATALOG = (
    {"id": "cartesia", "name": "Cartesia Sonic", "kind": "cloud",
     "streaming": True, "credential": "CARTESIA_API_KEY", "can_fallback": True,
     "supports_preview": True, "allows_custom_voice": False, "custom": False},
    {"id": "elevenlabs", "name": "ElevenLabs", "kind": "cloud",
     "streaming": True, "credential": "ELEVEN_API_KEY", "can_fallback": True,
     "supports_preview": True, "allows_custom_voice": False, "custom": False},
    {"id": "gemini", "name": "Gemini TTS", "kind": "cloud",
     "streaming": True, "credential": "GEMINI_API_KEY", "can_fallback": True,
     "supports_preview": True, "allows_custom_voice": False, "custom": False},
    {"id": "deepgram", "name": "Deepgram", "kind": "cloud",
     "streaming": True, "credential": "DEEPGRAM_API_KEY", "can_fallback": True,
     "supports_preview": True, "allows_custom_voice": False, "custom": False},
    # Built-in Google Chirp 3 HD (lib/google_tts). The installed custom.google-chirp adapter,
    # if any, is a separate provider with its own voices and stored maps.
    {"id": "google", "name": "Google Chirp 3 HD", "kind": "cloud",
     "streaming": False, "credential": "google", "can_fallback": True,
     "supports_preview": True, "allows_custom_voice": False, "custom": False,
     "capabilities": {"batch_ssml_breaks": True, "streaming": False}},
    {"id": "none", "name": "No voice output", "kind": "disabled",
     "streaming": False, "can_fallback": False, "supports_preview": False,
     "allows_custom_voice": False, "custom": False},
)
VALID_IDS = frozenset(item["id"] for item in CATALOG)
# The fixed error a legacy custom package with id "google" shows (no path, no name).
LEGACY_GOOGLE_ERROR = ("legacy custom adapter id 'google' conflicts with built-in Google; "
                       "operator resolution required (clarp-admin tts google-collision --report)")


def definitions() -> tuple[dict, ...]:
    """Built-ins plus every installed custom package, including damaged ones."""
    from .custom_tts_adapters import AdapterError, discover, inventory, voices
    manifests = {
        item.id: item for item in discover(reserved_ids=VALID_IDS)}
    rows = inventory(reserved_ids=VALID_IDS)
    for row in rows:
        item = manifests.get(row["id"])
        if item is None:
            continue
        try:
            voices(item)
        except AdapterError as exc:
            row["available"] = False
            row["error"] = str(exc)[:300]
    return CATALOG + tuple(rows)


def valid_ids() -> frozenset[str]:
    from .custom_tts_adapters import discover
    return frozenset(item["id"] for item in CATALOG) | frozenset(
        item.id for item in discover(reserved_ids=VALID_IDS))


def status() -> dict:
    from .config import load
    from . import google_tts
    cfg = load()
    google = google_tts.credentials(cfg).status()
    blocked = google_tts.collision_code(cfg)
    rows = []
    for definition in definitions():
        provider = definition["id"]
        available = (
            bool(cfg.cartesia_key()) if provider == "cartesia" else
            bool(cfg.eleven_key()) if provider == "elevenlabs" else
            bool(cfg.deepgram_key()) if provider == "deepgram" else
            bool(cfg.gemini_key()) if provider == "gemini" else
            google["configured"] if provider == "google" else
            bool(definition.get("installed", True))
        )
        if provider == "google":
            # Which source would sign in and why it cannot, never the file or project. A collision
            # with a legacy custom adapter blocks it with a fixed code before any credential matters.
            definition = dict(definition, credential_source=google["credential_source"],
                              error=blocked or google["error"])
            available = available and not blocked
        elif (definition.get("custom") and str(definition.get("id", "")).startswith("invalid.")
                and str(definition.get("error", "")).endswith("reserved adapter id: google")):
            definition = dict(definition, error=LEGACY_GOOGLE_ERROR)
        rows.append(dict(
            definition, available=(
                bool(definition.get("available", True))
                if definition.get("custom") else available),
            installed=bool(definition.get("installed", True)),
            selected=provider == cfg.tts_provider,
            fallback=provider == cfg.tts_fallback,
        ))
    return {
        "provider": cfg.tts_provider, "fallback": cfg.tts_fallback,
        "voice": cfg.local_tts_voice, "providers": rows,
    }


def synthesize(
    provider: str, *, text: str, voice: str, out_path: Path, on_chunk=None,
) -> int:
    """Synthesize through an installed custom adapter.

    The built-in Kokoro and Piper runtimes were removed: shipping and updating
    two local model stacks earned its keep for nobody, and a custom adapter
    covers the same ground without the install machinery.
    """
    provider = provider.strip().lower()
    from .custom_tts_adapters import get, synthesize as custom_synthesize
    manifest = get(provider, reserved_ids=VALID_IDS)
    if manifest is None:
        raise RuntimeError(f"no TTS adapter installed for provider: {provider}")
    return custom_synthesize(
        manifest, text=text, voice=voice, out_path=out_path, on_chunk=on_chunk)


def _wav_to_mp3(source: Path, destination: Path) -> None:
    import av
    from av.audio.resampler import AudioResampler
    destination.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(source)) as input_container:
        with av.open(str(destination), mode="w", format="mp3") as output_container:
            input_stream = input_container.streams.audio[0]
            output_stream = output_container.add_stream("mp3", rate=24_000)
            output_stream.layout = "mono"
            resampler = AudioResampler(
                format="fltp", layout="mono", rate=24_000)
            for frame in input_container.decode(input_stream):
                for converted in resampler.resample(frame):
                    converted.pts = None
                    for packet in output_stream.encode(converted):
                        output_container.mux(packet)
            for converted in resampler.resample(None):
                converted.pts = None
                for packet in output_stream.encode(converted):
                    output_container.mux(packet)
            for packet in output_stream.encode(None):
                output_container.mux(packet)
