"""User preference for showing model-keyed persona portraits."""
from . import settings_store

KEY = "avatars.model_variants"


def get() -> dict[str, bool]:
    return {"model_avatars": settings_store.get_bool(KEY, default=False)}


def update(value: bool) -> dict[str, bool]:
    settings_store.set_bool(KEY, bool(value))
    return get()
