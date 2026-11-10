"""Pure diff statistics and bounded tool-output formatting."""
from __future__ import annotations

from typing import Any


def _count_diff(old: str, new: str) -> tuple[int, int, list[str]]:
    import difflib
    lines = list(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(), lineterm="", n=1))
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    body = [line for line in lines if not line.startswith(("---", "+++"))]
    return added, removed, body


def diff_stats(name: str, tool_input: dict[str, Any] | None,
               preview_lines: int = 40) -> dict[str, Any] | None:
    """Unified-diff counts (+A -R) and a bounded preview for an edit or write.

    Claude ``Edit``/``MultiEdit``/``Write`` inputs, and unified diff text
    (Codex ``fileChange``/``apply_patch``) under ``diff``/``patch``.
    """
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    key = str(name or "").lower()
    path = str(tool_input.get("file_path") or tool_input.get("filePath")
               or tool_input.get("path") or "")
    added = removed = 0
    body: list[str] = []
    if key in {"edit", "str_replace"}:
        added, removed, body = _count_diff(tool_input.get("old_string") or "",
                                           tool_input.get("new_string") or "")
    elif key == "multiedit":
        for edit in tool_input.get("edits") or []:
            if isinstance(edit, dict):
                a, r, b = _count_diff(edit.get("old_string") or "", edit.get("new_string") or "")
                added, removed, body = added + a, removed + r, body + b
    elif key in {"write", "create", "write_file"} and isinstance(tool_input.get("content"), str):
        content_lines = tool_input["content"].splitlines()
        added, body = len(content_lines), ["+" + line for line in content_lines]
    elif isinstance(tool_input.get("diff") or tool_input.get("patch"), str):
        text = tool_input.get("diff") or tool_input.get("patch")
        lines = text.splitlines()
        added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        body = [line for line in lines if not line.startswith(("---", "+++"))]
    else:
        return None
    preview = "\n".join(line[:400] for line in body[:preview_lines])
    return {"added": added, "removed": removed,
            "files": [{"path": path, "added": added, "removed": removed}] if path else [],
            "preview": preview}


def output_tail(text: Any, limit: int = 50) -> tuple[list[str], int]:
    """The last ``limit`` lines of a tool's output and its total line count."""
    if isinstance(text, list):
        text = "\n".join(str(part.get("text") or "") if isinstance(part, dict) else str(part)
                         for part in text)
    lines = str(text or "").splitlines()
    return [line[:400] for line in lines[-limit:]], len(lines)
