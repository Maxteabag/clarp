"""Read-only release checks plus the supported native update trigger.

Docker application files are immutable. A container therefore reports the
exact replacement command instead of mutating itself or mounting the host's
Docker socket into the application container.
"""
from __future__ import annotations

import os
import json
import pathlib
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
from functools import cmp_to_key

from .server_identity import get_server_info
from . import xdg

_SEMVER = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_lock = threading.Lock()
_update_launch_lock = threading.Lock()
_cache: tuple[float, dict] | None = None
UPDATE_JOB_ID = "managed-server-update"


def _credential_free_remote(value: str) -> str:
    value = str(value or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return value
    host = parsed.netloc
    if parsed.username is not None:
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, host, parsed.path, "", ""))


def _version_tuple(value: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None] | None:
    match = _SEMVER.match(value.strip())
    if not match:
        return None
    base = tuple(map(int, match.groups()[:3]))
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else None
    return base, prerelease


def _compare_versions(left: str, right: str) -> int:
    parsed_left = _version_tuple(left)
    parsed_right = _version_tuple(right)
    if parsed_left is None or parsed_right is None:
        return (left > right) - (left < right)
    left_base, left_pre = parsed_left
    right_base, right_pre = parsed_right
    if left_base != right_base:
        return (left_base > right_base) - (left_base < right_base)
    if left_pre is None or right_pre is None:
        if left_pre is None and right_pre is None:
            return 0
        return 1 if left_pre is None else -1
    for left_part, right_part in zip(left_pre, right_pre):
        if left_part == right_part:
            continue
        left_numeric, right_numeric = left_part.isdigit(), right_part.isdigit()
        if left_numeric and right_numeric:
            return (int(left_part) > int(right_part)) - (int(left_part) < int(right_part))
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return (left_part > right_part) - (left_part < right_part)
    return (len(left_pre) > len(right_pre)) - (len(left_pre) < len(right_pre))


def _remote_refs(remote: str) -> tuple[str, str, str]:
    result = subprocess.run(
        ["git", "ls-remote", remote, "refs/heads/main", "refs/tags/v*"],
        text=True, capture_output=True, timeout=8, check=True,
    )
    main = ""
    tags: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == "refs/heads/main":
            main = sha
        elif ref.startswith("refs/tags/"):
            tag = ref.removeprefix("refs/tags/")
            if tag.endswith("^{}"):
                peeled[tag[:-3]] = sha
                continue
            parsed = _version_tuple(tag)
            # The supported Docker tag and native default channel are stable.
            # A prerelease tag must never make a `stable` pull look outdated.
            if parsed is not None and parsed[1] is None:
                tags[tag] = sha
    ordered = sorted(tags, key=cmp_to_key(_compare_versions), reverse=True)
    latest = ordered[0] if ordered else ""
    return main, latest, peeled.get(latest, tags.get(latest, ""))


def _install_channel() -> str:
    config_dir = pathlib.Path(os.environ.get(
        "CLARP_CONFIG_DIR", xdg.config_dir()))
    path = pathlib.Path(os.environ.get("CLARP_INSTALL_STATE", config_dir / "install.json"))
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return "stable"
    return "development" if data.get("channel") == "development" else "stable"


def _update_remote() -> str:
    configured = _credential_free_remote(
        os.environ.get("CLARP_UPDATE_REMOTE", ""))
    if configured:
        return configured
    config_dir = pathlib.Path(os.environ.get(
        "CLARP_CONFIG_DIR", xdg.config_dir()))
    path = pathlib.Path(os.environ.get(
        "CLARP_INSTALL_STATE", config_dir / "install.json"))
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        state = {}
    recorded = _credential_free_remote(state.get("source_remote") or "")
    if recorded:
        return recorded
    share = pathlib.Path(os.environ.get(
        "CLARP_SHARE_DIR", xdg.data_dir()))
    for candidate in (share / "current/SOURCE_REMOTE", share / "SOURCE_REMOTE"):
        try:
            recorded = _credential_free_remote(candidate.read_text())
        except OSError:
            continue
        if recorded:
            return recorded
    return ""


def get_update_status(force: bool = False) -> dict:
    global _cache
    now = time.monotonic()
    with _lock:
        if not force and _cache and now - _cache[0] < 300:
            return dict(_cache[1])

    info = get_server_info()
    current = info.get("version", "")
    mode = info.get("deployment_mode", "native")
    response = {
        **info,
        "checked": False,
        "update_available": None,
        "latest_version": "",
        "update_method": "docker-compose" if mode == "container" else "managed",
        "can_update_in_app": mode != "container",
        "update_command": "clarp-admin update",
    }
    if mode == "container":
        project = os.environ.get("CLARP_COMPOSE_PROJECT", "").strip()
        directory = os.environ.get("CLARP_COMPOSE_DIRECTORY", "").strip()
        files = os.environ.get("CLARP_COMPOSE_FILES", "compose.yaml").strip().split(":")
        if project and directory:
            file_args = " ".join(f"-f {shlex.quote(item)}" for item in files if item)
            compose = f"docker compose -p {shlex.quote(project)} {file_args}".strip()
            response["update_command"] = (
                f"cd {shlex.quote(directory)} && {compose} pull && {compose} up -d"
            )
        else:
            # Compose's inferred project name and host working directory are
            # not visible from inside a container. Never fabricate them: a
            # plausible but wrong command can create a second data volume.
            response["update_command"] = ""
    try:
        remote = _update_remote()
        main_sha, latest_tag, latest_tag_sha = _remote_refs(remote)
        current_semver = _version_tuple(current)
        latest_semver = _version_tuple(latest_tag)
        if current_semver is not None and latest_semver is not None:
            response["latest_version"] = latest_tag
            response["update_available"] = _compare_versions(current, latest_tag) < 0
        elif current and current not in {"dev", "unknown"}:
            stable_native = mode != "container" and _install_channel() == "stable"
            target_sha = latest_tag_sha if stable_native else main_sha
            target_label = latest_tag if stable_native else main_sha[:7]
            if target_sha:
                response["latest_version"] = target_label
                response["update_available"] = not target_sha.startswith(current.split("-", 1)[0])
        response["checked"] = True
    except (OSError, subprocess.SubprocessError):
        # Version display must remain useful offline. The client renders an
        # unknown update state rather than guessing that a build is current.
        pass
    with _lock:
        _cache = (now, dict(response))
    return response


def _worker_script() -> pathlib.Path:
    configured = os.environ.get("CLARP_SERVER_UPDATE_WORKER", "").strip()
    if configured:
        return pathlib.Path(configured)
    share = pathlib.Path(os.environ.get("CLARP_SHARE_DIR", xdg.data_dir()))
    return share / "current/scripts/server_update_job.py"


def _launch_update(session: str, status: dict) -> tuple[int, dict]:
    from . import agents, background_jobs, service_manager
    existing = background_jobs.get(UPDATE_JOB_ID)
    if service_manager.platform_kind() == "linux":
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "clarp-update.service"],
            check=False,
        )
    else:
        active = subprocess.CompletedProcess([], 1)
    if active.returncode == 0 or (
        existing and existing["status"] in background_jobs.ACTIVE_STATUSES
    ):
        return 202, {
            "ok": True, "status": "running",
            "job": existing,
        }
    if existing and not agents.get_by_session(existing["session"]):
        existing = background_jobs.reassign_terminal_owner(
            UPDATE_JOB_ID, session=session)
    owner_session = existing["session"] if existing else session
    job = background_jobs.upsert(
        session=owner_session, job_id=UPDATE_JOB_ID, kind="server-update",
        title="Update Clarp", detail="Preparing the managed server update",
        status="queued", restart_cancelled=True, heartbeat_timeout_ms=120_000,
        metadata={
            "latest_version": status.get("latest_version") or "",
            "expire_queued": True,
        },
    )
    handle = background_jobs.job_handle(job)
    command = [sys.executable, str(_worker_script()),
               "--session", owner_session, "--handle", handle]
    if service_manager.platform_kind() == "linux":
        launched = subprocess.run([
            "systemd-run", "--user", "--collect", "--unit=clarp-update",
            "--property=Type=exec", *command,
        ], text=True, capture_output=True, check=False)
        launch_error = launched.stderr.strip()
        launch_ok = launched.returncode == 0
    else:
        try:
            logs = pathlib.Path(os.environ.get(
                "CLAUDE_PWA_LOG_DIR", xdg.log_dir()))
            logs.mkdir(parents=True, exist_ok=True)
            output = (logs / "server-update.log").open("ab")
            subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True,
                close_fds=True)
            output.close()
            launch_ok, launch_error = True, ""
        except OSError as exc:
            launch_ok, launch_error = False, str(exc)
    if not launch_ok:
        background_jobs.finish(
            UPDATE_JOB_ID, generation=job["generation"], status="failed",
            reason="update_launch_failed",
        )
        return 500, {
            "ok": False,
            "message": launch_error or "Could not start the update service.",
        }
    return 202, {
        "ok": True, "status": "queued", "job": job, "job_handle": handle,
    }


def request_update(session: str = "") -> tuple[int, dict]:
    status = get_update_status()
    if status["update_method"] == "docker-compose":
        return 409, {
            "ok": False,
            "requires_host": True,
            "message": "Docker images are replaced by the Docker host, not from inside the container.",
            "update_command": status["update_command"],
        }
    if status.get("update_available") is not True:
        return 409, {
            "ok": False,
            "message": (
                "No newer release is available."
                if status.get("update_available") is False
                else "The latest release could not be verified."
            ),
        }
    if not session:
        return 400, {"ok": False, "message": "Update owner session is required."}
    with _update_launch_lock:
        return _launch_update(session, status)
