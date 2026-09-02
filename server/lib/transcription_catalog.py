"""Curated transcription models supported by Clarp's provider adapters.

The catalog is deliberately small and versioned with Clarp. It describes
models Clarp knows how to install; runtime capability discovery remains the
authority for which models are actually installed on a host.
"""
from __future__ import annotations

import os
import sys

CATALOG = (
    {"id": "faster-whisper:base.en", "provider": "faster-whisper",
     "model": "base.en", "name": "Base English", "weight": "light",
     "download_bytes": 148_000_000, "languages": ["en"], "platforms": ["linux"]},
    {"id": "faster-whisper:small.en", "provider": "faster-whisper",
     "model": "small.en", "name": "Small English", "weight": "medium",
     "download_bytes": 488_000_000, "languages": ["en"], "recommended": True,
     "platforms": ["linux"]},
    {"id": "faster-whisper:small", "provider": "faster-whisper",
     "model": "small", "name": "Small Multilingual", "weight": "medium",
     "download_bytes": 488_000_000, "languages": ["multilingual"],
     "platforms": ["linux"]},
    {"id": "faster-whisper:medium", "provider": "faster-whisper",
     "model": "medium", "name": "Medium", "weight": "heavy",
     "download_bytes": 1_530_000_000, "languages": ["multilingual"],
     "platforms": ["linux"]},
    {"id": "faster-whisper:large-v3-turbo", "provider": "faster-whisper",
     "model": "large-v3-turbo", "name": "Large v3 Turbo", "weight": "very-heavy",
     "download_bytes": 1_620_000_000, "languages": ["multilingual"],
     "platforms": ["linux"]},
    {"id": "whisper.cpp:base.en", "provider": "whisper.cpp",
     "model": "base.en", "name": "whisper.cpp Base English", "weight": "light",
     "download_bytes": 148_000_000, "languages": ["en"], "platforms": ["macos"]},
    {"id": "whisper.cpp:small.en", "provider": "whisper.cpp",
     "model": "small.en", "name": "whisper.cpp Small English", "weight": "medium",
     "download_bytes": 488_000_000, "languages": ["en"], "platforms": ["macos"]},
)

RECOMMENDED_MODEL_ID = "faster-whisper:small.en"


def platform_kind() -> str:
    override = os.environ.get("CLARP_PLATFORM_OVERRIDE", "").strip().lower()
    if override in {"linux", "macos"}:
        return override
    return "macos" if sys.platform == "darwin" else "linux"


def recommended_model_id() -> str:
    return ("whisper.cpp:small.en" if platform_kind() == "macos"
            else RECOMMENDED_MODEL_ID)


def model_by_id(model_id: str) -> dict | None:
    return next((dict(item) for item in CATALOG if item["id"] == model_id), None)


def public_catalog(installed_ids: set[str] | None = None) -> list[dict]:
    installed_ids = installed_ids or set()
    host = platform_kind()
    result = []
    for item in CATALOG:
        supported = host in item.get("platforms", ["linux", "macos"])
        result.append(dict(
            item, installed=item["id"] in installed_ids,
            supported=supported, recommended=item["id"] == recommended_model_id()))
    return result
