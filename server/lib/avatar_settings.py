"""User preference for showing model-keyed persona portraits."""
from . import settings_store

KEY = "avatars.model_variants"


def get(*, stored: dict[str, str] | None = None) -> dict[str, bool]:
    if stored is None:
        return {"model_avatars": settings_store.get_bool(KEY, default=False)}
    return {"model_avatars": stored.get(KEY, "false").strip().lower() in {"1", "true", "yes", "on"}}


def update(value: bool) -> dict[str, bool]:
    settings_store.set_bool(KEY, bool(value))
    return get()
