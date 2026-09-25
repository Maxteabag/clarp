"""Codex: the app-server runner, goal protocol and account failover."""
from __future__ import annotations

from .base import resolve
from .stream_json import StreamJsonBackend


class CodexBackend(StreamJsonBackend):
    """Runs through the long-lived Codex app-server (``codex_app_server``)."""

    def terminal_argv(self, session_id: str) -> list[str]:
        argv = [self.required_binary]
        if session_id:
            argv += ["resume", session_id]
        return argv

    def on_credential_change(self) -> None:
        """Drop leftover Codex app-servers after credentials change.

        ``codex login`` rewrites ``~/.codex/auth.json`` in another process.
        The per-agent app-server still holds thread writer locks and the
        previous token. The next ``thread/resume`` then fails with
        ``already has an active writer``. Closing stdin lets flock drop; the
        next turn starts a fresh app-server that re-reads auth.
        """
        from ..log import log_exception
        try:
            resolve("codex_app_server", "recycle_clients")()
        except Exception as exc:  # noqa: BLE001
            log_exception("codexAppServerRecycleFail", exc)

    def recover_usage_limit(self, message: str) -> bool:
        """Ask the app-server whether the limit was a stale connection it can
        reconnect past; True means the turn may be retried as is."""
        return bool(resolve("codex_app_server", "recover_usage_failure")(message))

    def account_pool(self) -> str:
        """The pool is named after the CLI: its ``codex-switch`` command and
        failover coordinator are keyed by this name in turn_dispatch."""
        return self.id

    def classify_usage_limit(self, message: str, *,
                             quota_confirmed: bool | None = None) -> dict | None:
        if quota_confirmed is False:
            return None
        return resolve("backend_usage", "record_classified_usage_limit")(self.id)
