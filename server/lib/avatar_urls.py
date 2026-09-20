"""Content-versioned avatar routes for mutable portrait files."""
from __future__ import annotations

import hashlib
import hmac
import pathlib
import re
import threading
from urllib.parse import quote


_JANITOR_PORTRAITS = {
    "task-labels": "rivet-task-label-keeper.jpg",
    "message-delegator": "delegator-message-dispatcher.jpg",
    "tool-explainer": "explainer-tool-guide.jpg",
}


def janitor_avatar_url(is_janitor: bool, template_id: str | None = None,
                       *, static_root: pathlib.Path | None = None) -> str:
    """Project a role portrait without replacing the agent's original photo."""
    if not is_janitor:
        return ""
    if static_root is None:
        # Source checkout: server/lib; installed release: lib.
        code = pathlib.Path(__file__).resolve().parent.parent
        static_root = code / "static"
        if not static_root.is_dir():
            static_root = code.parent / "static"
    root = pathlib.Path(static_root) / "janitor-avatars"
    name = _JANITOR_PORTRAITS.get(template_id, "janitor-default.jpg")
    if not (root / name).is_file():
        name = "janitor-default.jpg"
    if not (root / name).is_file():
        return ""
    return versioned_avatar_url("/static/janitor-avatars", name, str(root / name))

_version_lock = threading.Lock()
_versions: dict[str, tuple[int, int, str]] = {}


def avatar_content_version(path: pathlib.Path) -> str:
    """Content hash of a portrait file, hashed once per (mtime, size) revision.

    Snapshots project every agent's portrait on every build; re-reading and
    hashing ~400 KB per agent each time was pure waste, and a stale hash is
    impossible because any rewrite moves mtime or size.
    """
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    signature = (int(stat.st_mtime_ns), int(stat.st_size))
    key = str(path)
    with _version_lock:
        cached = _versions.get(key)
        if cached and cached[:2] == signature:
            return cached[2]
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"
    with _version_lock:
        _versions[key] = (*signature, digest)
    return digest


_PERSONA_SLUG = re.compile(r"[^a-z0-9_-]")

# Backend ids as the static defaults are named; Antigravity ships as "gemini".
_DEFAULT_PORTRAIT_FAMILY = {"agy": "gemini"}


def persona_avatar_slug(name: str) -> str:
    """The static-avatar slug for a persona name, as the clients compute it."""
    return _PERSONA_SLUG.sub("", str(name or "").lower())


def static_persona_avatar_url(persona: str, backend: str, *,
                              static_root: pathlib.Path | None) -> str:
    """The Host's own portrait for an agent that has no chosen one.

    Clients used to guess `/static/avatars/<slug>.png` themselves, so a persona
    without a shipped PNG (Codex-8194, Astra, custom names) was a 404 retried
    every few seconds, and a persona with one was re-downloaded on every
    reconnect because the guessed URL carried no content version. The Host
    knows which file exists; it now says so, versioned, and falls back to the
    backend's default portrait so every agent has a face.
    """
    if static_root is None:
        return ""
    root = pathlib.Path(static_root) / "avatars"
    slug = persona_avatar_slug(persona)
    candidates = []
    if slug:
        candidates.append(f"{slug}.png")
    family = (backend or "").strip().lower()
    family = _DEFAULT_PORTRAIT_FAMILY.get(family, family)
    if family:
        candidates.append(f"default-{family}.png")
    for name in candidates:
        path = root / name
        if path.is_file():
            return versioned_avatar_url("/static/avatars", name, str(path))
    return ""


def versioned_avatar_url(prefix: str, identity: str, avatar_path: str) -> str:
    if not avatar_path:
        return ""
    digest = avatar_content_version(pathlib.Path(avatar_path))
    route = prefix.rstrip("/") + "/" + quote(str(identity), safe="")
    return f"{route}?v={digest}"


def notification_avatar_signature(
    secret: str,
    agent_id: str,
    content_version: str,
    expires_at: int,
) -> str:
    message = f"{agent_id}\n{content_version}\n{int(expires_at)}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def notification_avatar_authorized(
    *,
    secret: str,
    agent_id: str,
    content_version: str,
    expires_at: int,
    signature: str,
    now: int,
) -> bool:
    if not secret or expires_at < now or expires_at > now + 25 * 60 * 60:
        return False
    expected = notification_avatar_signature(
        secret, agent_id, content_version, expires_at)
    return hmac.compare_digest(expected, signature)
