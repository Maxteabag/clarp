"""Persona portraits keyed by the model an Agent actually runs on.

An Agent still showing its bundled persona portrait can instead show a
variant drawn for the model behind it, when one has been bundled for that
persona. Resolution is model-first, because the model is the claim being
made: an Antigravity Agent pinned to a Claude model is an Opus portrait,
not a Gemini one. Only when the model is unknown does the CLI stand in for
it, and only where the CLI names a single model family -- the Claude CLI
spans Opus, Fable, Sonnet and Haiku, so it names none and the Agent keeps
its ordinary portrait.

Variants are bundled files, so a persona with no art for its family simply
has no model portrait; nothing is generated and nothing is guessed.
"""
from __future__ import annotations

import pathlib
import re
import threading

from . import backends
from .avatar_urls import avatar_content_version

ROUTE = "/static/avatars/models"

# Checked in order; the first family whose token appears in the model id
# wins. Ordered so "claude-opus-4-6-thinking" is Opus rather than a prefix
# match on the provider name.
FAMILY_TOKENS: tuple[tuple[str, str], ...] = (
    ("fable", "fable"),
    ("opus", "opus"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),
    ("gemini", "gemini"),
    ("grok", "grok"),
    ("codex", "codex"),
)

# The family a CLI names when the Agent pins no model of its own. Claude and
# OpenCode are deliberately absent: both front several families, so neither
# is evidence of which model is answering.
BACKEND_FAMILIES: dict[str, str] = {
    backends.CODEX: "codex",
    backends.AGY: "gemini",
    backends.GROK: "grok",
}

_SLUG = re.compile(r"[^a-z0-9_-]")
_lock = threading.Lock()
_versions: dict[str, tuple[int, int, str]] = {}


def slug(name: str) -> str:
    """The bundled-avatar slug for a persona name, as the clients compute it."""
    return _SLUG.sub("", str(name or "").lower())


def family_for_model(model: str) -> str:
    """The model family a model id belongs to, or "" when it names none."""
    text = str(model or "").strip().lower()
    if not text:
        return ""
    for token, family in FAMILY_TOKENS:
        if token in text:
            return family
    return ""


def families_for(backend: str, model: str, *, default_model: str = "") -> list[str]:
    """Families to try for one Agent, best evidence first.

    ``default_model`` is what this Computer has configured for the backend;
    it speaks for an Agent that pins no model of its own.
    """
    resolved = backends.normalize(backend)
    candidates = [
        family_for_model(model),
        family_for_model(default_model),
        BACKEND_FAMILIES.get(resolved, ""),
    ]
    ordered: list[str] = []
    for family in candidates:
        if family and family not in ordered:
            ordered.append(family)
    return ordered


def url_for(persona: str, backend: str, model: str, *,
            root: pathlib.Path, default_model: str = "") -> str:
    """The bundled model portrait for one Agent, or "" when none is bundled."""
    name = slug(persona)
    if not name:
        return ""
    for family in families_for(backend, model, default_model=default_model):
        path = pathlib.Path(root) / f"{name}.{family}.png"
        version = _version(path)
        if version:
            return f"{ROUTE}/{name}.{family}.png?v={version}"
    return ""


def _version(path: pathlib.Path) -> str:
    """Content version for a bundled file, hashed once per file revision."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = str(path)
    signature = (int(stat.st_mtime_ns), int(stat.st_size))
    with _lock:
        cached = _versions.get(key)
        if cached and cached[:2] == signature:
            return cached[2]
    version = avatar_content_version(path)
    with _lock:
        _versions[key] = (signature[0], signature[1], version)
    return version
