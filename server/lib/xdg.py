"""XDG base directories for Clarp.

One place decides where things live, so `paths.RuntimePaths` and
`deployment.DeploymentLayout` cannot drift apart.

Linux follows the XDG Base Directory spec. macOS deliberately uses native
Library directories even when XDG variables happen to be inherited; explicit
CLARP_* overrides remain available to the installer and service.

The three-way split is deliberate:

  config  ($XDG_CONFIG_HOME/clarp)  hand-edited, worth backing up:
                                    config.toml, user-values.md, vocabulary.txt
  data    ($XDG_DATA_HOME/clarp)    must never be lost: the sqlite database,
                                    installed releases, downloaded models, and
                                    user-supplied uploads and media
  cache   ($XDG_CACHE_HOME/clarp)   safe to delete at any time: synthesized
                                    audio clips, logs, session markers

Uploads and media are data, not cache: a phone upload is the user's only copy,
and `rm -rf ~/.cache/*` is something people do. The container layout has always
treated them that way (/data/media); this makes the native layout agree.

Logs stay in cache rather than $XDG_STATE_HOME. Strict XDG would separate them,
but that is a fourth directory for a couple of files and near-universal practice
puts logs in cache.
"""
from __future__ import annotations

import os
import pathlib
import sys

APP = "clarp"

MAC_APP = "Clarp"


def _base(env: str, default: str, home: pathlib.Path | None = None) -> pathlib.Path:
    """Resolve one XDG base dir. An empty or relative value is ignored, per spec."""
    home = (home or pathlib.Path.home()).expanduser()
    raw = (os.environ.get(env) or "").strip()
    if raw:
        candidate = pathlib.Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
    return home / default


def config_home(home: pathlib.Path | None = None) -> pathlib.Path:
    if sys.platform == "darwin":
        return (home or pathlib.Path.home()).expanduser() / "Library/Application Support"
    return _base("XDG_CONFIG_HOME", ".config", home)


def data_home(home: pathlib.Path | None = None) -> pathlib.Path:
    if sys.platform == "darwin":
        return (home or pathlib.Path.home()).expanduser() / "Library/Application Support"
    return _base("XDG_DATA_HOME", ".local/share", home)


def cache_home(home: pathlib.Path | None = None) -> pathlib.Path:
    if sys.platform == "darwin":
        return (home or pathlib.Path.home()).expanduser() / "Library/Caches"
    return _base("XDG_CACHE_HOME", ".cache", home)


def config_dir(home: pathlib.Path | None = None) -> pathlib.Path:
    return config_home(home) / (MAC_APP if sys.platform == "darwin" else APP)


def data_dir(home: pathlib.Path | None = None) -> pathlib.Path:
    return data_home(home) / (MAC_APP if sys.platform == "darwin" else APP)


def cache_dir(home: pathlib.Path | None = None) -> pathlib.Path:
    return cache_home(home) / (MAC_APP if sys.platform == "darwin" else APP)


def log_dir(home: pathlib.Path | None = None) -> pathlib.Path:
    home = (home or pathlib.Path.home()).expanduser()
    if sys.platform == "darwin":
        return home / "Library/Logs" / MAC_APP
    return cache_dir(home) / "logs"
