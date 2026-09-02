"""Discover resumable CLI sessions with user-facing metadata."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime
from typing import Any

from .log import log_exception


def _user_text(event: dict[str, Any]) -> str:
    if event.get("type") != "user":
        return ""
    content = (event.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return str(item.get("text") or "").strip()
    return ""


def _claude_metadata(path: pathlib.Path) -> dict[str, Any] | None:
    """Read Claude's persisted cwd, preview, and latest generated/custom title."""
    cwd = preview = generated_title = custom_title = ""
    def ingest(event: Any) -> None:
        nonlocal cwd, preview, generated_title, custom_title
        if not isinstance(event, dict):
            return
        cwd = str(event.get("cwd") or cwd)
        preview = preview or _user_text(event)
        if event.get("type") == "custom-title":
            custom_title = str(event.get("customTitle") or custom_title)
        generated_title = str(event.get("aiTitle") or generated_title)

    try:
        with path.open(encoding="utf-8") as fp:
            for index, line in enumerate(fp):
                if index >= 120:
                    break
                try:
                    ingest(json.loads(line))
                except json.JSONDecodeError:
                    continue
        # `/rename` is normally near the end. Read a bounded tail rather than
        # scanning multi-megabyte transcripts for every picker refresh.
        with path.open("rb") as fp:
            fp.seek(0, 2)
            size = fp.tell()
            fp.seek(max(0, size - 64 * 1024))
            tail = fp.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()
        if size > 64 * 1024 and lines:
            lines = lines[1:]  # discard a partial first JSONL record
        for line in lines:
            try:
                ingest(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        log_exception("pastSessionsReadFail", exc, detail=str(path))
        return None
    try:
        mtime = int(path.stat().st_mtime)
    except OSError:
        mtime = 0
    return {
        "id": path.stem,
        "mtime": mtime,
        "preview": preview[:240],
        "title": (custom_title or generated_title)[:160],
        "cwd": cwd,
    }


def list_claude_sessions(
    cwd: str,
    *,
    all_projects: bool = False,
    limit: int = 100,
    projects_root: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """List Claude sessions for one cwd or across every local project.

    Claude Code can resume a renamed session by name, but Clarp deliberately
    launches by the stable UUID while presenting the latest ``/rename`` title.
    """
    root = projects_root or (pathlib.Path.home() / ".claude" / "projects")
    if not root.is_dir():
        return []
    index_paths = list(root.glob("*/sessions-index.json")) if all_projects else []
    if not all_projects:
        encoded = "-" + str(pathlib.Path(cwd)).strip("/").replace("/", "-")
        index = root / encoded / "sessions-index.json"
        if index.is_file():
            index_paths = [index]
    indexed: dict[str, dict[str, Any]] = {}
    indexed_transcript_ids: set[str] = set()
    for index in index_paths:
        try:
            payload = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log_exception("pastSessionsIndexReadFail", exc, detail=str(index))
            continue
        for entry in payload.get("entries", []) if isinstance(payload, dict) else []:
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            sid = str(entry.get("sessionId") or "")
            if not sid:
                continue
            declared_path = pathlib.Path(str(entry.get("fullPath") or ""))
            transcript_path = (
                declared_path if str(declared_path) and declared_path.is_file()
                else index.parent / f"{sid}.jsonl"
            )
            if not transcript_path.is_file():
                continue
            indexed_transcript_ids.add(sid)
            file_mtime = int(entry.get("fileMtime") or 0)
            if file_mtime > 10_000_000_000:
                file_mtime //= 1000
            if not file_mtime:
                try:
                    file_mtime = int(datetime.fromisoformat(
                        str(entry.get("modified") or "").replace("Z", "+00:00")
                    ).timestamp())
                except (TypeError, ValueError):
                    file_mtime = 0
            item = {
                "id": sid,
                "mtime": file_mtime,
                "preview": str(entry.get("firstPrompt") or "")[:240],
                "title": str(
                    entry.get("customTitle") or entry.get("summary") or "")[:160],
                "cwd": str(entry.get("projectPath") or ""),
            }
            if not all_projects and cwd and item["cwd"] \
                    and str(pathlib.Path(item["cwd"])) != str(pathlib.Path(cwd)):
                continue
            prior = indexed.get(sid)
            if prior is None or item["mtime"] > prior["mtime"]:
                indexed[sid] = item
    # Compatibility fallback for missing, partial, or stale indexes. UUIDs
    # already represented by an index never need their transcript opened.
    if all_projects:
        candidates = [
            path
            for project in root.iterdir()
            if project.is_dir()
            for path in project.glob("*.jsonl")
            if path.is_file() and path.stem not in indexed_transcript_ids
        ]
    else:
        encoded = "-" + str(pathlib.Path(cwd)).strip("/").replace("/", "-")
        project = root / encoded
        candidates = [
            path for path in project.glob("*.jsonl")
            if path.is_file() and path.stem not in indexed_transcript_ids
        ] if project.is_dir() else []
    try:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as exc:
        log_exception("pastSessionsListFail", exc, detail=str(root))
        return []
    out: dict[str, dict[str, Any]] = dict(indexed)
    wanted = str(pathlib.Path(cwd)) if cwd else ""
    for path in candidates[:limit]:
        item = _claude_metadata(path)
        if not item:
            continue
        if not all_projects and wanted and item["cwd"] and item["cwd"] != wanted:
            continue
        prior = out.get(item["id"])
        if prior is None or item["mtime"] > prior["mtime"]:
            out[item["id"]] = item
    return sorted(out.values(), key=lambda item: item["mtime"], reverse=True)[:limit]
