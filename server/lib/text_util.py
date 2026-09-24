"""Small text helpers shared by the transcript parsers.

Leaf module: no lib imports, so anything may depend on it without cycles.
"""
from __future__ import annotations

from typing import Any


def truncate(s: Any, n: int = 600) -> str:
    """Clip a string to `n` characters with an ellipsis; non-strings become ""."""
    if not isinstance(s, str):
        return ""
    return s if len(s) <= n else s[:n] + "…"
