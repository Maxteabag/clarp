"""Grok Build: a plain JSON-lines CLI with no interactive terminal yet."""
from __future__ import annotations

from .stream_json import StreamJsonBackend


class GrokBackend(StreamJsonBackend):
    """Runs through ``grok_runner``."""
