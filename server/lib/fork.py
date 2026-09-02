"""Fork an existing Claude Code session into a new one.

Strategy: copy the JSONL line-by-line, rewriting every `sessionId` field to a
fresh uuid. The result is a self-contained transcript that `claude --resume
<new-uuid>` loads as its own conversation. The original is untouched.
"""
from __future__ import annotations

import json
import pathlib
import uuid

from .log import log_exception


def encoded_project_dir(cwd: str, projects_root: pathlib.Path) -> pathlib.Path:
    return projects_root / ("-" + str(pathlib.Path(cwd)).strip("/").replace("/", "-"))


def fork_session(source_id: str, cwd: str,
                 projects_root: pathlib.Path | None = None,
                 new_id: str | None = None) -> str:
    """Copy `<cwd-project>/<source_id>.jsonl` to a new uuid, rewriting all
    `sessionId` fields. Returns the new uuid.

    Raises FileNotFoundError if the source jsonl doesn't exist anywhere
    (saved cwd first, then global scan).
    """
    projects_root = projects_root or (pathlib.Path.home() / ".claude" / "projects")
    src = encoded_project_dir(cwd, projects_root) / f"{source_id}.jsonl"
    if not src.is_file():
        # Try global scan — the saved cwd may not match the actual project dir.
        candidates = list(projects_root.glob(f"*/{source_id}.jsonl"))
        if not candidates:
            raise FileNotFoundError(f"no jsonl found for session {source_id}")
        src = candidates[0]

    new_id = new_id or str(uuid.uuid4())
    dst = src.parent / f"{new_id}.jsonl"

    with src.open(encoding="utf-8") as f_in, dst.open("w", encoding="utf-8") as f_out:
        for line in f_in:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as e:
                log_exception("forkLineSkip", e, detail=src.name)
                continue
            if isinstance(d, dict) and "sessionId" in d:
                d["sessionId"] = new_id
            f_out.write(json.dumps(d) + "\n")
    return new_id
