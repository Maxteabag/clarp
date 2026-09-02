"""Filesystem layout for the installed server, hooks, and local state.

Directory choices and the config/data/cache split live in lib.xdg.
"""
from __future__ import annotations

import pathlib
import os
from dataclasses import dataclass

from . import xdg


@dataclass(frozen=True)
class RuntimePaths:
    home: pathlib.Path
    cache_dir: pathlib.Path
    config_dir: pathlib.Path
    data_dir: pathlib.Path
    app_session: pathlib.Path
    last_source: pathlib.Path
    source_markers_dir: pathlib.Path
    audio_dir: pathlib.Path
    positions_dir: pathlib.Path
    hook_log: pathlib.Path
    uploads_dir: pathlib.Path
    media_dir: pathlib.Path

    @classmethod
    def from_home(cls, home: pathlib.Path | str) -> "RuntimePaths":
        home = pathlib.Path(home)
        cache = pathlib.Path(os.environ.get(
            "CLARP_CACHE_DIR", xdg.cache_dir(home)))
        config = pathlib.Path(os.environ.get(
            "CLARP_CONFIG_DIR", xdg.config_dir(home)))
        data = pathlib.Path(os.environ.get(
            "CLARP_DATA_DIR", os.environ.get(
                "CLARP_SHARE_DIR", xdg.data_dir(home))))
        return cls(
            home=home,
            cache_dir=cache,
            config_dir=config,
            data_dir=data,
            app_session=cache / "current-session",
            last_source=cache / "last-source",
            source_markers_dir=cache / "source-markers",
            audio_dir=cache / "audio",
            positions_dir=cache / "positions",
            hook_log=cache / "hook.log",
            # Uploads and media are the user's only copy of a phone upload, so
            # they live in data where a cache sweep cannot reach them.
            uploads_dir=pathlib.Path(os.environ.get(
                "CLARP_UPLOADS_DIR", data / "uploads")),
            media_dir=pathlib.Path(os.environ.get(
                "CLARP_MEDIA_DIR", data / "media")),
        )

    def source_marker(self, session: str) -> pathlib.Path:
        return self.source_markers_dir / _safe_session(session)

    def upload_dir(self, session: str) -> pathlib.Path:
        """Per-session directory for files the user uploads from a client.
        Lives outside any agent's working tree so uploads never pollute a
        repo / git status; agents read them by absolute path."""
        return self.uploads_dir / _safe_session(session)


def _safe_session(session: str) -> str:
    safe = "".join(c for c in session if c.isalnum() or c in "._-")
    return safe or "unknown"
