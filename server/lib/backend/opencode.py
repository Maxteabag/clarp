"""OpenCode: a JSON-lines CLI fronting several model providers."""
from __future__ import annotations

from .stream_json import StreamJsonBackend


class OpenCodeBackend(StreamJsonBackend):
    """Runs through ``opencode_runner``; no compaction or terminal yet."""
