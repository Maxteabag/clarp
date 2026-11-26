"""Car-mode instructions: a user-editable Markdown doc folded into the
app-turn preamble whenever the client says the user is driving.

The iOS app sends ``car_mode: true`` on ``/send`` while its Car Mode surface
is presented. That flag travels the same road as ``synthesize_audio``
(turn_dispatch → backend runner → preamble / additionalContext / Claude hook)
and, when set, the text of this doc is appended after the voice guidance.

Resolution order mirrors ``leader_memory``'s user-values doc so the two
hand-edited files live side by side:

1. ``$CLARP_CAR_MODE_DOC`` (explicit override, tests)
2. ``<config_dir>/car-mode.md`` (``~/.config/clarp/car-mode.md`` on Linux)
3. ``<root>/docs/car-mode.example.md`` shipped with the release

The doc is bounded so a runaway edit can't blow the turn's context budget.
"""
from __future__ import annotations

import os
import pathlib
import threading

from . import xdg

CAR_MODE_DOC = xdg.config_dir() / "car-mode.md"
CAR_MODE_DOC_ENV = "CLARP_CAR_MODE_DOC"
EXAMPLE_DOC_RELATIVE = pathlib.Path("docs") / "car-mode.example.md"

# Prompt-cache friendly: the block is stable across turns, so it's prefixed
# with a fixed header the model can anchor on and the strippers can spot.
CAR_MODE_HEAD = "[car-mode]"
DEFAULT_MAX_CHARS = 2400

# Last-resort text when neither the user doc nor the shipped example exists
# (a stripped install). Short on purpose: the example doc is the real thing.
_FALLBACK = (
    "The user is driving and hands-free. Keep every spoken reply short and "
    "conversational, never read code, paths, URLs, or long lists aloud, and "
    "confirm before doing anything irreversible."
)

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}


def _root_candidates() -> list[pathlib.Path]:
    here = pathlib.Path(__file__).resolve()
    candidates = [
        pathlib.Path.cwd(),
        here.parents[1],  # installed root: ~/.local/share/clarp/lib/...
        here.parents[2],  # source root: repo/server/lib/...
    ]
    seen: set[pathlib.Path] = set()
    out: list[pathlib.Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def doc_candidates() -> list[pathlib.Path]:
    candidates: list[pathlib.Path] = []
    env_path = os.environ.get(CAR_MODE_DOC_ENV, "").strip()
    if env_path:
        candidates.append(pathlib.Path(env_path))
    candidates.append(CAR_MODE_DOC)
    for root in _root_candidates():
        candidates.append(root / EXAMPLE_DOC_RELATIVE)
    seen: set[pathlib.Path] = set()
    out: list[pathlib.Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def active_doc_path() -> pathlib.Path | None:
    """The file that would be injected right now, or None when none exists."""
    for candidate in doc_candidates():
        if candidate.is_file():
            return candidate
    return None


def read_doc() -> str:
    """Raw text of the active doc (mtime-cached), "" when none exists."""
    path = active_doc_path()
    if path is None:
        return ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    key = str(path)
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    with _lock:
        _cache[key] = (mtime, text)
    return text


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def car_mode_instruction(max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The block appended to the app-turn preamble on car-mode turns.

    Always non-empty: a missing doc falls back to a one-line safety rule so
    the flag is never silently ignored."""
    body = read_doc() or _FALLBACK
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n\n[Car-mode instructions truncated.]"
    return f"{CAR_MODE_HEAD} {body}"
