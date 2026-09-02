"""User preference for special display/notification treatment of automation."""
from . import settings_store

KEY = "automation_special_treatment"


def get() -> dict[str, bool]:
    return {"special_treatment": settings_store.get_bool(KEY, default=False)}


def update(value: bool) -> dict[str, bool]:
    settings_store.set_text(KEY, "true" if value else "false")
    return get()
