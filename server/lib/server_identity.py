"""Stable identity advertised to multi-server clients."""
from __future__ import annotations

import os
import pathlib
import re
import socket
import uuid
from . import xdg

from .settings_store import get_text, set_text

_ID_KEY = "server_instance_id"
_NAME_KEY = "server_display_name"

# Oldest Clarp iOS app this Host still speaks to. The app carries the mirror
# constant (HostCompatibilityPolicy.minimumHostVersion) and shows an update
# prompt when either side is behind.
MIN_APP_VERSION = "1.0"

# Product surfaces this Host implements, advertised on /server-info so an
# older app can hide an entry point the Host lacks instead of showing a dead
# toggle, and a newer app can hide one this Host has not grown yet. Names are
# stable ids, not endpoints; add one when a feature ships, never rename.
CAPABILITIES_VERSION = 1
FEATURES: tuple[str, ...] = (
    "teams",
    "oracle",
    "dreaming",
    "heartbeat",
    "location",
    "calendar",
    "media",
    "artifacts",
    "background_jobs",
    "herald",
    "personalities",
    "managed_skills",
    "agent_portraits",
    "remote_action",
    "orchestrator",
    "backend_auth",
    "transcription",
    "tts",
    "pairing",
    "diagnostics",
)


def capabilities() -> dict[str, object]:
    return {"version": CAPABILITIES_VERSION, "features": list(FEATURES)}

def _pyproject_candidates() -> list[pathlib.Path]:
    """pyproject.toml next to the code, in either layout.

    A source checkout keeps this module at server/lib/ (pyproject two levels
    up); an installed release flattens it to lib/ beside server.py (one level
    up). The release has no installed distribution, so the file is the source.
    """
    here = pathlib.Path(__file__).resolve()
    return [parent / "pyproject.toml" for parent in list(here.parents)[1:3]]


def clarp_version() -> str:
    """Release version of this Host (pyproject `version`), "" if unknown.

    Distinct from DEPLOYED_VERSION, which is a git SHA on native installs and
    only says *which build* is running, not whether it is compatible.
    """
    for candidate in _pyproject_candidates():
        try:
            text = candidate.read_text()
        except OSError:
            continue
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        if match:
            return match.group(1)
    try:
        from importlib.metadata import version
        return version("clarp-server")
    except Exception:  # noqa: BLE001 - no distribution installed
        pass
    for env_var in ("CLARP_IMAGE_VERSION", "CLARP_VERSION", "DEPLOYED_VERSION"):
        val = os.environ.get(env_var, "").strip().lstrip("v")
        match = re.match(r"^(\d+\.\d+(?:\.\d+)?)", val)
        if match:
            return match.group(1)
    return ""


def get_server_info() -> dict[str, object]:
    server_id = get_text(_ID_KEY).strip()
    if not server_id:
        server_id = str(uuid.uuid4())
        set_text(_ID_KEY, server_id)
    name = (get_text(_NAME_KEY).strip()
            or os.environ.get("CLARP_SERVER_NAME", "").strip()
            or socket.gethostname())
    mode = os.environ.get("CLARP_DEPLOYMENT_MODE", "native").strip().lower()
    image_version = os.environ.get("CLARP_IMAGE_VERSION", "").strip()
    deployed_version = ""
    version_file = pathlib.Path(os.environ.get(
        "CLARP_SHARE_DIR", xdg.data_dir()
    )) / "DEPLOYED_VERSION"
    try:
        deployed_version = version_file.read_text().strip()
    except OSError:
        pass
    version = image_version if mode == "container" and image_version else deployed_version
    default_cwd = (os.environ.get("CLARP_WORKSPACE_ROOT", "/data/workspace")
                   if mode == "container" else str(pathlib.Path.home()))
    return {
        "server_id": server_id,
        "name": name,
        "deployment_mode": "container" if mode == "container" else "native",
        "version": version,
        "image_version": image_version,
        "default_cwd": default_cwd,
        "clarp_version": clarp_version(),
        "min_app_version": MIN_APP_VERSION,
        "capabilities": capabilities(),
    }
