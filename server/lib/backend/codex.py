"""Codex: the app-server runner, goal protocol and account failover."""
from __future__ import annotations

from .base import adapter_terminal_argv, resolve
from .stream_json import StreamJsonBackend


class CodexBackend(StreamJsonBackend):
    """Runs through the long-lived Codex app-server (``codex_app_server``)."""

    def terminal_argv(self, session_id: str) -> list[str]:
        return adapter_terminal_argv(self, session_id)

    def on_credential_change(self) -> None:
        """Drop leftover app-servers so the next turn re-reads auth.json."""
        resolve("backend_auth", "_recycle_codex_writers")()

    def recover_usage_limit(self, message: str) -> bool:
        return bool(self.adapter.usage_limit_recovery(message))

    def account_pool(self) -> str:
        return self.adapter.account_pool

    def classify_usage_limit(self, message: str, *,
                             quota_confirmed: bool | None = None) -> dict | None:
        if quota_confirmed is False:
            return None
        return resolve("backend_usage", "record_classified_usage_limit")(self.id)

