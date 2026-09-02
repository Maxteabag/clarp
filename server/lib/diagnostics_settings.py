"""Computer-owned opt-in controls for detailed developer diagnostics."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

from . import settings_store


KEY = "diagnostics.capture.v1"
CATEGORIES = (
    "requests",
    "database",
    "network",
    "interactions",
    "transcript",
    "resources",
    "feedback",
    "voice",
    "agents",
    "client",
)
_CATEGORY_SET = frozenset(CATEGORIES)
_LOCK = threading.Lock()
_CACHED: "Settings | None" = None
_CACHED_AT = 0.0
CACHE_TTL_SEC = 2.0


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    categories: frozenset[str] = frozenset()

    def public(self) -> dict:
        return {
            "enabled": self.enabled,
            "categories": [name for name in CATEGORIES if name in self.categories],
            "retention_hours": 24,
            "rollup_retention_days": 30,
        }


def _decode(raw: str) -> Settings:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    categories = value.get("categories")
    if not isinstance(categories, list):
        categories = []
    accepted = frozenset(
        str(name).strip().lower() for name in categories
        if str(name).strip().lower() in _CATEGORY_SET
    )
    return Settings(enabled=value.get("enabled") is True, categories=accepted)


def get() -> Settings:
    global _CACHED, _CACHED_AT
    now = time.monotonic()
    with _LOCK:
        if _CACHED is None or now - _CACHED_AT >= CACHE_TTL_SEC:
            _CACHED = _decode(settings_store.get_text(KEY, default=""))
            _CACHED_AT = now
        return _CACHED


def update(data: dict) -> Settings:
    global _CACHED, _CACHED_AT
    enabled = data.get("enabled") is True
    raw_categories = data.get("categories")
    if not isinstance(raw_categories, list):
        raise ValueError("categories must be a list")
    requested = {str(name).strip().lower() for name in raw_categories}
    unknown = sorted(requested - _CATEGORY_SET)
    if unknown:
        raise ValueError(f"unknown diagnostic categories: {', '.join(unknown)}")
    value = Settings(enabled=enabled, categories=frozenset(requested))
    settings_store.set_text(KEY, json.dumps({
        "enabled": value.enabled,
        "categories": [name for name in CATEGORIES if name in value.categories],
    }, separators=(",", ":")))
    with _LOCK:
        _CACHED = value
        _CACHED_AT = time.monotonic()
    return value


def allows(category: str) -> bool:
    value = get()
    return value.enabled and category in value.categories


def allows_event(*, source: str, event: str, path: str | None = None) -> bool:
    value = get()
    if not value.enabled:
        return False
    category = category_for(source=source, event=event, path=path)
    if event.strip().lower() == "httprequest":
        return bool(value.categories & {"requests", "database"})
    return category in value.categories


def category_for(*, source: str, event: str, path: str | None = None) -> str:
    source_key = source.strip().lower()
    event_key = event.strip().lower()
    if "user-slow-report" in event_key:
        return "feedback"
    if "device-resources" in event_key:
        return "resources"
    if "conversation-open" in event_key:
        return "interactions"
    if source_key == "client" or event_key.startswith("ios."):
        return "client"
    if source_key in {"tts", "stt", "audio", "audio_central"}:
        return "voice"
    if source_key in {"sse", "transcript", "transcript_streamer", "state_watcher"}:
        return "transcript" if source_key == "transcript" else "network"
    if source_key in {"resource", "resources"}:
        return "resources"
    if source_key in {"feedback", "interaction"}:
        return source_key if source_key == "feedback" else "interactions"
    if source_key in {"database", "sqlite", "db"}:
        return "database"
    if any(word in event_key for word in ("sqlite", "database", "lock")):
        return "database"
    if any(word in event_key for word in (
            "sse", "transcript", "connection", "network", "stream")):
        return "transcript" if "transcript" in event_key else "network"
    if any(word in event_key for word in (
            "tts", "synth", "transcrib", "voice", "audio", "clip")):
        return "voice"
    if event_key == "httprequest" or path:
        return "requests"
    return "agents"


def reset_for_tests() -> None:
    global _CACHED, _CACHED_AT
    with _LOCK:
        _CACHED = None
        _CACHED_AT = 0.0


def accepts_client_uploads() -> bool:
    value = get()
    return value.enabled and bool(value.categories & {
        "client", "interactions", "resources", "feedback", "network",
    })
