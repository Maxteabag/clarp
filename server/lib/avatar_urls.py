"""Content-versioned avatar routes for mutable portrait files."""
from __future__ import annotations

import hashlib
import hmac
import pathlib
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

def avatar_content_version(path: pathlib.Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


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
