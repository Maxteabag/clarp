"""Server-side agent personality prompt settings."""
from __future__ import annotations

from dataclasses import dataclass

from . import settings_store


KEY_ENABLED = "personalities.enabled"


@dataclass(frozen=True)
class PersonalitySettings:
    enabled: bool = True

    def as_dict(self) -> dict:
        return {"enabled": self.enabled}


def get_settings() -> PersonalitySettings:
    return PersonalitySettings(
        enabled=settings_store.get_bool(KEY_ENABLED, default=True),
    )


def update_settings(data: dict) -> PersonalitySettings:
    if "enabled" in data:
        settings_store.set_bool(KEY_ENABLED, bool(data.get("enabled")))
    return get_settings()
