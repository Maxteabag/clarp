"""DeepSeek: OpenCode pinned to the DeepSeek model catalogue.

A model family, not a CLI. The card runs through the OpenCode binary with
its own catalogue and config fields, so the chooser reads "DeepSeek ->
model" instead of "OpenCode -> provider -> model". Composition over an
existing strategy, not a copy.
"""
from __future__ import annotations

from .opencode import OpenCodeBackend


class DeepSeekBackend(OpenCodeBackend):
    """OpenCode with the DeepSeek catalogue and config fields."""
