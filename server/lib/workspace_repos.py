"""Repository operations confined to the configured Clarp workspace."""
from __future__ import annotations

import pathlib
import re
import subprocess

from .deployment import LAYOUT
from .launch_paths import validate_workspace_path


def _name(value: str) -> str:
    name = re.sub(r"\.git$", "", pathlib.PurePosixPath(value.rstrip("/")).name)
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.")
    if not safe:
        raise ValueError("repository name is empty")
    return safe


def clone(url: str, name: str = "", ref: str = "") -> dict:
    root = LAYOUT.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    destination = validate_workspace_path(root / _name(name or url))
    if destination.exists():
        raise ValueError(f"workspace destination already exists: {destination.name}")
    command = ["git", "clone", "--filter=blob:none"]
    if ref:
        command += ["--branch", ref]
    command += [url, str(destination)]
    subprocess.run(command, check=True, text=True)
    return health(destination.name)


def list_repositories() -> list[dict]:
    root = LAYOUT.workspace_root
    if not root.is_dir():
        return []
    return [health(path.name) for path in sorted(root.iterdir())
            if path.is_dir() and (path / ".git").exists()]


def health(name: str) -> dict:
    path = validate_workspace_path(LAYOUT.workspace_root / _name(name))
    if not (path / ".git").exists():
        raise ValueError(f"not a Git repository: {name}")

    def output(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=path, text=True, capture_output=True,
            check=False).stdout.strip()

    return {
        "name": path.name,
        "path": str(path),
        "branch": output("branch", "--show-current"),
        "remote": output("remote", "get-url", "origin"),
        "dirty": bool(output("status", "--porcelain")),
    }
