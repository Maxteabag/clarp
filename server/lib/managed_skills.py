"""Inspect and manage Clarp-owned skill links for this server."""
from __future__ import annotations

import json
import os
import fcntl
from pathlib import Path
import shutil
import threading
from contextlib import contextmanager
from . import xdg

HOME = Path.home()
SHARE = Path(os.environ.get("CLARP_SHARE_DIR", xdg.data_dir(HOME)))
INSTALL_STATE = Path(os.environ.get(
    "CLARP_INSTALL_STATE", xdg.config_dir(HOME) / "install.json"))
CLAUDE_SKILLS = Path(os.environ.get("CLARP_CLAUDE_SKILLS", HOME / ".claude/skills"))
CODEX_SKILLS = Path(os.environ.get(
    "CLARP_CODEX_SKILLS", Path(os.environ.get("CODEX_HOME", HOME / ".codex")) / "skills"))
_ADJACENT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_ROOT = (_ADJACENT_ROOT if (_ADJACENT_ROOT / "skills").is_dir()
                else _ADJACENT_ROOT.parent)
_lock = threading.RLock()


@contextmanager
def _state_file_lock(*, exclusive: bool):
    path = INSTALL_STATE.with_suffix(INSTALL_STATE.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write_state(state: dict) -> None:
    INSTALL_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = INSTALL_STATE.with_name(f".{INSTALL_STATE.name}.{os.getpid()}.next")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(INSTALL_STATE)


def _manifest() -> list[dict]:
    path = RELEASE_ROOT / "skills/manifest.json"
    value = _read_json(path, {})
    skills = value.get("skills") if isinstance(value, dict) else None
    if not isinstance(skills, list):
        raise RuntimeError("Clarp skill manifest is unavailable")
    return skills


def _source(skill_id: str) -> Path:
    return RELEASE_ROOT / "skills" / skill_id


def _link_status(destination: Path, source: Path) -> str:
    if not destination.exists() and not destination.is_symlink():
        return "missing"
    if not destination.is_symlink():
        return "modified"
    raw_target = Path(os.readlink(destination))
    target = raw_target if raw_target.is_absolute() else destination.parent / raw_target
    resolved = target.resolve(strict=False)
    if resolved == source.resolve():
        return "healthy"
    try:
        relative = resolved.relative_to((SHARE / "releases").resolve())
        if (len(relative.parts) == 3 and relative.parts[1] == "skills"
                and relative.parts[2] == source.name):
            return "outdated"
    except (OSError, ValueError):
        pass
    return "modified"


def _requirements(item: dict) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for expression in item.get("requires") or []:
        choices = [part.strip() for part in str(expression).split(" or ") if part.strip()]
        if choices and not any(shutil.which(choice) for choice in choices):
            missing.append(str(expression))
    return not missing, missing


def status() -> list[dict]:
    with _lock:
        with _state_file_lock(exclusive=False):
            active = set(_read_json(INSTALL_STATE, {}).get("skills") or [])
            rows = []
            for item in _manifest():
                skill_id = str(item["id"])
                source = _source(skill_id)
                links = {
                    "claude": _link_status(CLAUDE_SKILLS / skill_id, source),
                    "codex": _link_status(CODEX_SKILLS / skill_id, source),
                }
                enabled = skill_id in active
                requirements_ok, missing_requirements = _requirements(item)
                if not (source / "SKILL.md").is_file():
                    health = "missing"
                elif "modified" in links.values():
                    health = "modified"
                elif "outdated" in links.values():
                    health = "outdated"
                elif not enabled and "healthy" in links.values():
                    health = "modified"
                elif not enabled:
                    health = "inactive"
                elif "missing" in links.values():
                    health = "missing"
                elif not requirements_ok:
                    health = "dependency-missing"
                else:
                    health = "healthy"
                rows.append({
                    **item, "enabled": enabled, "optional": item.get("pack") != "core",
                    "health": health, "links": links,
                    "requirements_ok": requirements_ok,
                    "missing_requirements": missing_requirements,
                })
            return rows


def set_enabled(skill_id: str, enabled: bool) -> dict:
    with _lock:
        with _state_file_lock(exclusive=True):
            return _set_enabled_locked(skill_id, enabled)


def _set_enabled_locked(skill_id: str, enabled: bool) -> dict:
    entries = {str(item["id"]): item for item in _manifest()}
    item = entries.get(skill_id)
    if item is None:
        raise ValueError(f"unknown Clarp skill: {skill_id}")
    if not enabled and item.get("pack") == "core":
        raise ValueError("core Clarp skills cannot be disabled")
    source = _source(skill_id)
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"skill source is missing: {skill_id}")
    state = _read_json(INSTALL_STATE, {})
    active = set(state.get("skills") or [])
    destinations = [root / skill_id for root in (CLAUDE_SKILLS, CODEX_SKILLS)]
    link_states = {destination: _link_status(destination, source)
                   for destination in destinations}
    if enabled:
        conflicts = [destination for destination, link_state in link_states.items()
                     if link_state == "modified"]
        if conflicts:
            raise ValueError(f"preserving non-Clarp skill at {conflicts[0]}")
    originals = {
        destination: os.readlink(destination) if destination.is_symlink() else None
        for destination in destinations
    }

    def replace_with_target(destination: Path, target: Path | str) -> None:
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{threading.get_ident()}.next")
        if temporary.is_symlink():
            temporary.unlink()
        temporary.symlink_to(target, target_is_directory=True)
        temporary.replace(destination)

    try:
        for destination in destinations:
            link_state = link_states[destination]
            if enabled:
                destination.parent.mkdir(parents=True, exist_ok=True)
                replace_with_target(destination, source)
            elif (destination.is_symlink()
                  and link_state in {"healthy", "outdated", "missing"}):
                destination.unlink()
        if enabled:
            active.add(skill_id)
        else:
            active.discard(skill_id)
        state["skills"] = sorted(active)
        _write_state(state)
    except BaseException:
        for destination, original in originals.items():
            try:
                if original is None:
                    if destination.is_symlink():
                        destination.unlink()
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    replace_with_target(destination, original)
            except OSError:
                pass
        raise
    # Avoid reacquiring the inter-process lock through status().
    return {**item, "enabled": enabled}
