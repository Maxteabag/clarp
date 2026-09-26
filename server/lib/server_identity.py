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

# Client contract. Marketing versions ("1.0", a git SHA, a container tag) say
# which build is running, not whether two builds speak the same protocol, so
# compatibility is negotiated with two monotonic integers on each side:
#
#   HOST_CONTRACT     bump whenever this Host adds or changes anything a
#                     client may depend on (an endpoint, a field, an event, a
#                     feature). Never decrease.
#   MIN_IOS_CONTRACT  the oldest iOS client contract this Host still serves.
#                     Bump only when something older clients rely on is
#                     removed or changed incompatibly.
#
# The iOS app carries the mirror image (ClientContract.current and
# .minimumHost). A pair is compatible when both floors are met. Every bump
# adds a row to docs/compatibility.md; tests/unit/test_client_contract.py
# fails when the table and these constants disagree. Clients identify
# themselves with the X-Clarp-Client header ("ios/2620 contract=1").
HOST_CONTRACT = 16
MIN_IOS_CONTRACT = 1
CLIENT_HEADER = "X-Clarp-Client"
FEATURES: tuple[str, ...] = (
    "teams",
    "oracle",
    "dreaming",
    "heartbeat",
    "location",
    "calendar",
    "media",
    "artifacts",
    "attention_index",
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
    "message_audio_replay",
    "pairing",
    "diagnostics",
    "tool_explanations",
    "agent_creation_details",
    "launch_directories",
    "backend_quota",
    "agent_goals",
    "oracle_contact",
    "oracle_direct_contact",
    "oracle_context_memory",
    "turn_queue_resume",
    "controller_narration",
    "tool_explanation_sources",
    "oracle_agent_voices",
    "helper_agents",
    "claude_subagent_cells",
    "oracle_earcons",
    "background_process_detail",
)


# The Host contract at which each feature became available, so a client can
# say "Oracle needs Host contract N" instead of a generic failure. Every
# feature in FEATURES has an entry; a new feature starts at the contract that
# introduces it.
FEATURE_CONTRACTS: dict[str, int] = {feature: 1 for feature in FEATURES}
FEATURE_CONTRACTS["agent_goals"] = 5
FEATURE_CONTRACTS["attention_index"] = 2
FEATURE_CONTRACTS["backend_quota"] = 3
FEATURE_CONTRACTS["oracle_contact"] = 7
FEATURE_CONTRACTS["oracle_direct_contact"] = 8
FEATURE_CONTRACTS["oracle_context_memory"] = 9
FEATURE_CONTRACTS["turn_queue_resume"] = 7
FEATURE_CONTRACTS["controller_narration"] = 8
FEATURE_CONTRACTS["tool_explanation_sources"] = 10
FEATURE_CONTRACTS["oracle_agent_voices"] = 12
FEATURE_CONTRACTS["helper_agents"] = 13
FEATURE_CONTRACTS["claude_subagent_cells"] = 14
FEATURE_CONTRACTS["oracle_earcons"] = 15
FEATURE_CONTRACTS["background_process_detail"] = 16


def capabilities() -> dict[str, object]:
    return {"version": CAPABILITIES_VERSION, "features": list(FEATURES)}


def contract() -> dict[str, object]:
    return {
        "host": HOST_CONTRACT,
        "min_ios": MIN_IOS_CONTRACT,
        "features": dict(FEATURE_CONTRACTS),
    }


def parse_client_header(value: str | None) -> dict[str, object] | None:
    """`X-Clarp-Client: ios/2620 contract=1` -> platform, build, contract.

    Returns None when the header is absent or unreadable; an app older than
    the handshake sends nothing and is treated as contract 0.
    """
    text = (value or "").strip()
    if not text:
        return None
    platform, _, build = text.split(None, 1)[0].partition("/")
    contract_value = 0
    for token in text.split()[1:]:
        key, _, raw = token.partition("=")
        if key == "contract":
            try:
                contract_value = max(0, int(raw))
            except ValueError:
                contract_value = 0
    if not platform:
        return None
    return {"platform": platform.lower(), "build": build, "contract": contract_value}


def evaluate_client(header_value: str | None) -> dict[str, object]:
    """Compatibility verdict for the client that sent `header_value`.

    Only iOS carries a contract today; other or unidentified clients are
    reported as unknown rather than judged.
    """
    client = parse_client_header(header_value)
    if not client or client["platform"] != "ios":
        return {"platform": client["platform"] if client else "", "build": "",
                "contract": 0, "status": "unknown", "reason": ""}
    verdict = dict(client)
    if client["contract"] < MIN_IOS_CONTRACT:
        verdict["status"] = "client_outdated"
        verdict["reason"] = (
            f"this Host needs iOS client contract {MIN_IOS_CONTRACT} or newer; "
            f"the app sent {client['contract']}")
    else:
        verdict["status"] = "compatible"
        verdict["reason"] = ""
    return verdict

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
        "contract": contract(),
    }
