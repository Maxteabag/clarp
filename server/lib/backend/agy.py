"""Antigravity: model ids carry their effort; owner-gated spawn."""
from __future__ import annotations

from .base import adapter_terminal_argv
from .stream_json import StreamJsonBackend


class AgyBackend(StreamJsonBackend):
    """Runs through ``agy_runner``; validates model ids against its catalogue."""

    def terminal_argv(self, session_id: str) -> list[str]:
        return adapter_terminal_argv(self, session_id)
