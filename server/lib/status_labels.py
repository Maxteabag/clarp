"""Shared rules for short agent-visible status labels."""
from __future__ import annotations

MAX_STATUS_LABEL_CHARS = 20
MAX_STATUS_LABEL_WORDS = 3


def shorten_status_label(label: str | None) -> tuple[str, bool]:
    """Return a header-safe status label and whether it changed.

    Agents are instructed to write very short labels themselves. This helper is
    a guardrail for stray long labels at the source.
    """
    original = " ".join(str(label or "").split())
    if not original:
        return "", False
    words = original.split()
    if len(original) <= MAX_STATUS_LABEL_CHARS and len(words) <= MAX_STATUS_LABEL_WORDS:
        return original, False
    for count in range(min(MAX_STATUS_LABEL_WORDS, len(words)), 0, -1):
        candidate = " ".join(words[:count])
        if len(candidate) <= MAX_STATUS_LABEL_CHARS:
            return candidate, True
    return words[0][:MAX_STATUS_LABEL_CHARS].rstrip(), True
