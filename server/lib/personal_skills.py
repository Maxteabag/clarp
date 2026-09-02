"""Container-owned personal skill imports and Git-backed sources."""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

from .deployment import LAYOUT

ROOT = LAYOUT.data_root / "skills"
IMPORTED = ROOT / "imported"
GIT = ROOT / "git"
REGISTRY = ROOT / "sources.json"
CLAUDE_SKILLS = pathlib.Path(os.environ.get(
    "CLARP_CLAUDE_SKILLS", LAYOUT.claude_home / "skills"))
CODEX_SKILLS = pathlib.Path(os.environ.get(
    "CLARP_CODEX_SKILLS", LAYOUT.codex_home / "skills"))


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.")
    if not safe:
        raise ValueError("skill/source name is empty")
    return safe


def _registry() -> dict:
    try:
        value = json.loads(REGISTRY.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_registry(value: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    temporary = REGISTRY.with_suffix(".next")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(REGISTRY)


def _skill_dirs(root: pathlib.Path) -> list[pathlib.Path]:
    if (root / "SKILL.md").is_file():
        return [root]
    return sorted(path for path in root.iterdir()
                  if path.is_dir() and (path / "SKILL.md").is_file())


def _validate_skill_tree(skill: pathlib.Path) -> None:
    root = skill.resolve()
    for path in skill.rglob("*"):
        if not path.is_symlink():
            continue
        raw = pathlib.Path(os.readlink(path))
        if raw.is_absolute():
            raise ValueError(f"skill contains absolute symlink: {path}")
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(f"skill symlink escapes its source: {path}") from exc


def _link(skill: pathlib.Path) -> None:
    for root in (CLAUDE_SKILLS, CODEX_SKILLS):
        root.mkdir(parents=True, exist_ok=True)
        destination = root / skill.name
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() and destination.resolve() == skill.resolve():
                continue
            raise ValueError(f"preserving existing skill at {destination}")
        destination.symlink_to(skill, target_is_directory=True)


def import_path(source: pathlib.Path, *, replace: bool = False) -> list[str]:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"skill directory does not exist: {source}")
    skills = _skill_dirs(source)
    if not skills:
        raise ValueError("no SKILL.md found")
    IMPORTED.mkdir(parents=True, exist_ok=True)
    specs: list[tuple[pathlib.Path, str, pathlib.Path, list[pathlib.Path]]] = []
    seen: set[str] = set()
    for skill in skills:
        _validate_skill_tree(skill)
        skill_id = _safe_id(skill.name)
        if skill_id in seen:
            raise ValueError(f"duplicate skill id in import: {skill_id}")
        seen.add(skill_id)
        destination = IMPORTED / skill_id
        link_paths = [root / skill_id for root in (CLAUDE_SKILLS, CODEX_SKILLS)]
        if destination.exists():
            if not replace:
                raise ValueError(f"skill already imported: {skill_id}")
        for link in link_paths:
            if link.exists() or link.is_symlink():
                owned = (destination.exists() and link.is_symlink()
                         and link.resolve() == destination.resolve())
                if not owned:
                    raise ValueError(f"preserving existing skill at {link}")
        staging = IMPORTED / f".{skill_id}.next"
        previous = IMPORTED / f".{skill_id}.previous"
        if staging.exists(): shutil.rmtree(staging)
        if previous.exists(): shutil.rmtree(previous)
        shutil.copytree(skill, staging, symlinks=True)
        specs.append((staging, skill_id, destination, link_paths))

    committed: list[tuple[pathlib.Path, pathlib.Path]] = []
    created_links: list[pathlib.Path] = []
    try:
        for staging, skill_id, destination, link_paths in specs:
            previous = IMPORTED / f".{skill_id}.previous"
            if destination.exists(): destination.rename(previous)
            staging.rename(destination)
            committed.append((destination, previous))
            for link in link_paths:
                if not link.exists() and not link.is_symlink():
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(destination, target_is_directory=True)
                    created_links.append(link)
    except BaseException:
        for link in created_links:
            link.unlink(missing_ok=True)
        for destination, previous in reversed(committed):
            if destination.exists(): shutil.rmtree(destination)
            if previous.exists(): previous.rename(destination)
        for staging, _skill_id, _destination, _links in specs:
            if staging.exists(): shutil.rmtree(staging)
        raise
    for _destination, previous in committed:
        if previous.exists(): shutil.rmtree(previous)
    return [skill_id for _staging, skill_id, _destination, _links in specs]


def add_git(url: str, name: str = "", ref: str = "") -> dict:
    source_id = _safe_id(name or pathlib.PurePosixPath(url.rstrip("/")).stem)
    destination = GIT / source_id
    if destination.exists():
        raise ValueError(f"skill source already exists: {source_id}")
    GIT.mkdir(parents=True, exist_ok=True)
    staging = GIT / f".{source_id}.next"
    if staging.exists(): shutil.rmtree(staging)
    created_links: list[pathlib.Path] = []
    try:
        subprocess.run(["git", "clone", "--filter=blob:none", url, str(staging)],
                       check=True, text=True)
        if ref:
            subprocess.run(["git", "checkout", "--detach", ref], cwd=staging,
                           check=True, text=True)
        staged_skills = _skill_dirs(staging)
        if not staged_skills:
            raise ValueError("Git source contains no SKILL.md")
        for skill in staged_skills: _validate_skill_tree(skill)
        published = [destination / skill.relative_to(staging) for skill in staged_skills]
        for skill in published:
            for root in (CLAUDE_SKILLS, CODEX_SKILLS):
                link = root / skill.name
                if link.exists() or link.is_symlink():
                    raise ValueError(f"preserving existing skill at {link}")
        staging.rename(destination)
        for skill in published:
            for root in (CLAUDE_SKILLS, CODEX_SKILLS):
                link = root / skill.name
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(skill, target_is_directory=True)
                created_links.append(link)
        records = _registry()
        records[source_id] = {
            "url": url, "ref": ref, "skills": [p.name for p in published]}
        _write_registry(records)
    except BaseException:
        for link in created_links:
            link.unlink(missing_ok=True)
        if staging.exists(): shutil.rmtree(staging)
        if destination.exists(): shutil.rmtree(destination)
        raise
    return {"id": source_id, **records[source_id]}


def update_git(source_id: str) -> dict:
    source_id = _safe_id(source_id)
    records = _registry()
    record = records.get(source_id)
    if not isinstance(record, dict):
        raise ValueError(f"unknown skill source: {source_id}")
    destination = GIT / source_id
    staging = GIT / f".{source_id}.next"
    previous = GIT / f".{source_id}.previous"
    if staging.exists(): shutil.rmtree(staging)
    if previous.exists(): shutil.rmtree(previous)
    ref = str(record.get("ref") or "")
    original_links: dict[pathlib.Path, str] = {}
    created_links: list[pathlib.Path] = []
    records = _registry()
    try:
        subprocess.run([
            "git", "clone", "--filter=blob:none", str(record["url"]), str(staging)],
            check=True, text=True)
        if ref:
            subprocess.run(["git", "checkout", "--detach", ref], cwd=staging,
                           check=True, text=True)
        staged_skills = _skill_dirs(staging)
        if not staged_skills:
            raise ValueError("Git source contains no SKILL.md")
        for skill in staged_skills: _validate_skill_tree(skill)
        published = [destination / skill.relative_to(staging) for skill in staged_skills]
        old_skills = _skill_dirs(destination)
        for skill in old_skills + published:
            for root in (CLAUDE_SKILLS, CODEX_SKILLS):
                link = root / skill.name
                if link.exists() or link.is_symlink():
                    if not link.is_symlink():
                        raise ValueError(f"preserving existing skill at {link}")
                    try:
                        owned = link.resolve().is_relative_to(destination.resolve())
                    except OSError:
                        owned = False
                    if not owned:
                        raise ValueError(f"preserving existing skill at {link}")
                    original_links[link] = os.readlink(link)
        destination.rename(previous)
        staging.rename(destination)
        for link in original_links:
            link.unlink(missing_ok=True)
        for skill in published:
            for root in (CLAUDE_SKILLS, CODEX_SKILLS):
                link = root / skill.name
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(skill, target_is_directory=True)
                created_links.append(link)
        updated = {**record, "skills": [skill.name for skill in published]}
        records[source_id] = updated
        _write_registry(records)
    except BaseException:
        for link in created_links:
            link.unlink(missing_ok=True)
        for link, target in original_links.items():
            if not link.exists() and not link.is_symlink():
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
        if destination.exists() and previous.exists():
            shutil.rmtree(destination)
        if previous.exists(): previous.rename(destination)
        if staging.exists(): shutil.rmtree(staging)
        _write_registry(_registry() if source_id not in records else {
            **records, source_id: record})
        raise
    if previous.exists(): shutil.rmtree(previous)
    return {"id": source_id, **updated}


def _description(path: pathlib.Path) -> str:
    text = (path / "SKILL.md").read_text(errors="replace")[:4000]
    match = re.search(r"(?m)^description:\s*[\"']?(.+?)[\"']?\s*$", text)
    return match.group(1).strip() if match else "Personal skill"


def status() -> list[dict]:
    rows: list[dict] = []
    roots = [IMPORTED]
    if GIT.is_dir():
        roots.extend(path for path in GIT.iterdir() if path.is_dir())
    for root in roots:
        if not root.is_dir():
            continue
        for skill in _skill_dirs(root):
            text = (skill / "SKILL.md").read_text(errors="replace")
            host_path = bool(re.search(
                r"(?<![A-Za-z0-9_])/(?:home|Users)/[^/\s\"'`]+/", text))
            links = {
                "claude": "healthy" if (CLAUDE_SKILLS / skill.name).is_symlink() else "missing",
                "codex": "healthy" if (CODEX_SKILLS / skill.name).is_symlink() else "missing",
            }
            health = ("host-path-dependency" if host_path else
                      "healthy" if all(v == "healthy" for v in links.values()) else "missing")
            rows.append({
                "id": skill.name, "pack": "personal",
                "description": _description(skill), "enabled": True,
                "optional": False, "health": health, "links": links,
                "requirements_ok": not host_path,
                "missing_requirements": ["host-specific path"] if host_path else [],
                "source": str(skill),
            })
    return sorted(rows, key=lambda row: row["id"])


def repair_links() -> None:
    for row in status():
        source = pathlib.Path(str(row.get("source") or ""))
        if source.is_dir():
            for root in (CLAUDE_SKILLS, CODEX_SKILLS):
                destination = root / source.name
                if destination.is_symlink() and destination.resolve() == source.resolve():
                    continue
                if not destination.exists() and not destination.is_symlink():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.symlink_to(source, target_is_directory=True)
