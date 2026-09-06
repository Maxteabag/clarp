"""Initialize persistent internal workers once as part of Host startup."""
from __future__ import annotations

from . import backends, janitor_builtins, settings_store


def initialize(*, cwd: str) -> None:
    from .orchestrator import get_legacy_settings

    legacy = get_legacy_settings()
    router = {"enabled": legacy.enabled, "options": {
        "hands_free_only": legacy.hands_free_only, "fallback_only": legacy.fallback_only,
        "confidence_threshold": legacy.confidence_threshold, "timeout_ms": legacy.timeout_ms,
        "voice_id": legacy.voice_id,
    }}
    # Import configured deployments; untouched Hosts receive the new Spark
    # default while retaining routing's original opt-in behavior.
    if (settings_store.get_text("orchestrator.provider", default="") or
            settings_store.get_text("orchestrator.model", default="")):
        router.update(backend=backends.CODEX if legacy.provider == "openai" else legacy.provider,
                      provider=legacy.provider, model=legacy.model, effort=legacy.effort)
    janitor_builtins.ensure_builtins(cwd=cwd, initial={"message-delegator": router})
    janitor_builtins.recover_expired_runs()
