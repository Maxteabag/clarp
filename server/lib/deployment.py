"""Deployment-aware filesystem layout shared by native and container modes.

Directory choices and the config/data/cache split live in lib.xdg.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from . import xdg


def deployment_mode() -> str:
    value = os.environ.get("CLARP_DEPLOYMENT_MODE", "native").strip().lower()
    return "container" if value == "container" else "native"


@dataclass(frozen=True)
class DeploymentLayout:
    mode: str
    home: pathlib.Path
    data_root: pathlib.Path
    share: pathlib.Path
    config_dir: pathlib.Path
    cache_dir: pathlib.Path
    claude_home: pathlib.Path
    codex_home: pathlib.Path
    models_dir: pathlib.Path
    media_dir: pathlib.Path
    uploads_dir: pathlib.Path
    workspace_root: pathlib.Path

    @classmethod
    def from_environment(cls) -> "DeploymentLayout":
        mode = deployment_mode()
        home = pathlib.Path.home()
        if mode == "container":
            data = pathlib.Path(os.environ.get("CLARP_DATA_DIR", "/data"))
            share = pathlib.Path(os.environ.get("CLARP_SHARE_DIR", "/opt/clarp"))
            config = pathlib.Path(os.environ.get("CLARP_CONFIG_DIR", data / "clarp"))
            cache = pathlib.Path(os.environ.get("CLARP_CACHE_DIR", "/tmp/clarp-cache"))
            claude_home = pathlib.Path(os.environ.get("CLARP_CLAUDE_HOME", data / "claude"))
            codex_home = pathlib.Path(os.environ.get("CODEX_HOME", data / "codex"))
            models = pathlib.Path(os.environ.get("CLARP_TRANSCRIPTION_MODELS", data / "models"))
            media = pathlib.Path(os.environ.get("CLARP_MEDIA_DIR", data / "media"))
            uploads = pathlib.Path(os.environ.get("CLARP_UPLOADS_DIR", data / "uploads"))
            workspace = pathlib.Path(os.environ.get("CLARP_WORKSPACE_ROOT", data / "workspace"))
        else:
            data = pathlib.Path(os.environ.get(
                "CLARP_DATA_DIR", xdg.data_dir(home)))
            share = pathlib.Path(os.environ.get("CLARP_SHARE_DIR", data))
            config = pathlib.Path(os.environ.get(
                "CLARP_CONFIG_DIR", xdg.config_dir(home)))
            cache = pathlib.Path(os.environ.get(
                "CLARP_CACHE_DIR", xdg.cache_dir(home)))
            claude_home = pathlib.Path(os.environ.get(
                "CLARP_CLAUDE_HOME", home / ".claude"))
            codex_home = pathlib.Path(os.environ.get(
                "CODEX_HOME", home / ".codex"))
            models = pathlib.Path(os.environ.get(
                "CLARP_TRANSCRIPTION_MODELS", data / "models"))
            # Data, not cache: a phone upload is the user's only copy.
            media = pathlib.Path(os.environ.get("CLARP_MEDIA_DIR", data / "media"))
            uploads = pathlib.Path(os.environ.get("CLARP_UPLOADS_DIR", data / "uploads"))
            workspace = pathlib.Path(os.environ.get("CLARP_WORKSPACE_ROOT", home))
        return cls(mode, home, data, share, config, cache, claude_home,
                   codex_home, models, media, uploads, workspace)

    @property
    def state_database(self) -> pathlib.Path:
        return pathlib.Path(os.environ.get(
            "CLAUDE_PWA_DB", self.data_root / "clarp/state.sqlite"
            if self.mode == "container" else self.share / "state.sqlite"))

    @property
    def config_file(self) -> pathlib.Path:
        return pathlib.Path(os.environ.get(
            "CLAUDE_PWA_CONFIG", self.config_dir / "config.toml"))

    def create_container_directories(self) -> None:
        if self.mode != "container":
            return
        for path in (
            self.data_root, self.config_dir, self.cache_dir, self.claude_home,
            self.codex_home, self.models_dir, self.media_dir, self.uploads_dir,
            self.workspace_root, self.data_root / "skills/imported",
            self.data_root / "skills/git", self.data_root / "git/gh",
            self.data_root / "git/ssh", self.data_root / "clarp/backups",
        ):
            path.mkdir(parents=True, exist_ok=True)


LAYOUT = DeploymentLayout.from_environment()


def plugin_dir() -> pathlib.Path | None:
    """The directory handed to `claude --plugin-dir`.

    Registering Clarp's hooks through the CLI flag instead of
    ~/.claude/settings.json means an install writes nothing into the user's
    Claude Code configuration. The path resolves through the `current`
    release symlink, so a deploy or rollback takes effect on the next turn
    with no re-registration.

    Returns None when no plugin ships alongside this code, so a partial
    install degrades to "no state hooks" rather than a failed spawn.
    """
    for candidate in (LAYOUT.share / "plugin",
                      pathlib.Path(__file__).resolve().parents[2] / "plugin"):
        if (candidate / ".claude-plugin" / "plugin.json").is_file():
            return candidate
    return None
