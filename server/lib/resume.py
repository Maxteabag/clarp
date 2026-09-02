"""Resolve each agent's backend_session_id UUID at server boot.

Pure DB/disk operation: for every agent in the DB we look up its
saved Claude-session UUID (from the runtimes table) and verify the
JSONL still exists on disk. If it doesn't, the agent stays "fresh"
and the next /send mints a new UUID via bind_backend_session.

Deliberately no "newest jsonl in cwd" fallback: agents can share the same home
directory, so picking the newest file would bind one agent to another's
transcript.
"""
from __future__ import annotations

import pathlib

from .log import log, log_exception
from . import backends
from .protocol import AgentBackend


def _encoded_project_dir(cwd: str, projects_root: pathlib.Path) -> pathlib.Path:
    encoded = "-" + str(pathlib.Path(cwd)).strip("/").replace("/", "-")
    return projects_root / encoded


def find_session_jsonl(backend_session_id: str, cwd: str,
                       projects_root: pathlib.Path) -> pathlib.Path | None:
    """Locate the JSONL for a Claude session id.

    Preferred: <projects_root>/<encoded-cwd>/<id>.jsonl (matches the saved cwd).
    Fallback: scan every project dir — claude-code lets a session cd between
    directories, and the JSONL lives in whichever dir was current when the
    first turn ran.
    """
    if not backend_session_id:
        return None
    target = _encoded_project_dir(cwd, projects_root) / f"{backend_session_id}.jsonl"
    if target.is_file():
        return target
    for hit in projects_root.glob(f"*/{backend_session_id}.jsonl"):
        return hit
    return None


def _cwd_from_project_dir(proj_dir: pathlib.Path) -> str:
    """Convert `~/.claude/projects/-foo-bar-baz` → `/foo/bar/baz`.

    Claude Code encodes the conversation's working directory into the
    project directory name by replacing '/' with '-'. We reverse that.
    """
    name = proj_dir.name
    if not name.startswith("-"):
        return ""
    return "/" + name[1:].replace("-", "/")


def resume_missing_sessions(
    agents: dict,
    home: pathlib.Path,
    projects_root: pathlib.Path | None = None,
    backend_sessions_by_session: dict[str, str] | None = None,
) -> list[dict]:
    """Resolve each agent's backend_session_id UUID at boot.

    For every agent we:
      1. look up the saved Claude-session UUID in the DB
      2. verify the JSONL still exists on disk
      3. realign cwd if the JSONL lives in a different project dir
      4. leave the agent "fresh" (no UUID) if nothing's on disk —
         the next /send pre-mints one via bind_backend_session

    Returns: list[{sid, persona, action, ok, detail, backend_session_id}]
      action ∈ {"resumed", "fresh"}.
    """
    projects_root = projects_root or (pathlib.Path.home() / ".claude" / "projects")
    results: list[dict] = []
    for sid, info in agents.items():
        info = info or {}
        persona = info.get("name") or sid
        cwd = (info.get("cwd") or "").strip() or str(home)
        if not pathlib.Path(cwd).is_dir():
            log("resumeCwdMissing", f"{sid} cwd={cwd}")
            cwd = str(home)

        action = "fresh"
        claude_id = ""
        backend = (info.get("backend") or AgentBackend.CLAUDE).strip().lower()
        mapped = (backend_sessions_by_session or {}).get(sid, "")
        if mapped:
            try:
                jsonl = backends.find_resume_transcript(
                    backend, mapped, cwd=cwd, projects_root=projects_root,
                )
            except OSError as e:
                log_exception("resumeFindJsonlFail", e, detail=sid)
                jsonl = None
            if jsonl is not None:
                claude_id = mapped
                action = "resumed"
                # cwd realignment only applies to Claude's project-dir naming;
                # Codex rollout filenames encode the date, not the cwd.
                if backend == AgentBackend.CLAUDE:
                    derived = _cwd_from_project_dir(jsonl.parent)
                    if derived and derived != cwd:
                        log("resumeCwdRealign", f"{sid} {cwd} → {derived}")
                        cwd = derived
            else:
                log("resumeMappedMissing", f"{sid} id={mapped} not on disk")

        log("resumeOk", f"{sid} action={action} cwd={cwd} id={claude_id or '-'}")
        results.append({"sid": sid, "persona": persona, "action": action,
                        "ok": True, "detail": cwd,
                        "backend_session_id": claude_id})
    return results
