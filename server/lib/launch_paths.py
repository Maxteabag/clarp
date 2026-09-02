from __future__ import annotations

import pathlib
import os


def workspace_root() -> pathlib.Path | None:
    if os.environ.get("CLARP_DEPLOYMENT_MODE") != "container":
        return None
    return pathlib.Path(os.environ.get("CLARP_WORKSPACE_ROOT", "/data/workspace")).resolve()


def validate_workspace_path(path: pathlib.Path) -> pathlib.Path:
    """Reject container paths outside its explicitly writable workspace."""
    root = workspace_root()
    resolved = path.expanduser().resolve()
    if root is not None and not resolved.is_relative_to(root):
        raise ValueError(f"path is outside the Clarp workspace root: {root}")
    return resolved


def existing_workspace_path(raw: object) -> pathlib.Path:
    """Resolve a stored cwd, confining legacy/restored rows in containers."""
    root = workspace_root()
    fallback = root or pathlib.Path.home()
    candidate = pathlib.Path(str(raw or "").strip() or str(fallback)).expanduser()
    try:
        candidate = validate_workspace_path(candidate)
    except ValueError:
        return fallback
    return candidate if candidate.is_dir() else fallback


def recover_user_path(raw: str, *, home: pathlib.Path | None = None) -> pathlib.Path:
    """Resolve launch paths and repair the common missing-username form.

    Mobile users sometimes omit the account component from a Linux home path,
    for example `/home/Projects/repo`. Preserve valid absolute paths verbatim;
    only repair a nonexistent `/home/...` path when the same suffix exists
    below the current user's home.
    """
    home = (home or pathlib.Path.home()).expanduser()
    root = workspace_root()
    if root is not None:
        value = str(raw or "").strip()
        candidate = pathlib.Path(value).expanduser() if value else root
        try:
            return validate_workspace_path(candidate)
        except ValueError:
            return root
    value = str(raw or "").strip()
    path = pathlib.Path(value).expanduser() if value else home
    if path.exists() or not path.is_absolute():
        return path
    try:
        suffix = path.relative_to("/home")
    except ValueError:
        return path
    candidate = home / suffix
    return candidate if candidate.exists() else path
