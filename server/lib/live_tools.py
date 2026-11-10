"""What a tool call is, for live tool items (docs/live-items.md §1.1).

``classify_tool`` maps any provider's tool name and input to a category
(exec read list search edit write fetch mcp todo agent other), a short label
and the raw command, so clients can render "Running npm test" and fold reads,
listings and searches into one Exploring/Explored row.
"""
from __future__ import annotations

from typing import Any

from .activity import path_tail, truncate
from .tool_output import _count_diff, diff_stats, output_tail

_BY_NAME = {
    "read": "read", "notebookread": "read", "view": "read", "read_file": "read",
    "grep": "search", "glob": "search", "search": "search", "codebase_search": "search",
    "ls": "list", "list": "list", "list_dir": "list",
    "edit": "edit", "multiedit": "edit", "notebookedit": "edit", "apply_patch": "edit",
    "patch": "edit", "str_replace": "edit",
    "write": "write", "create": "write", "write_file": "write",
    "webfetch": "fetch", "websearch": "fetch", "web_search": "fetch", "fetch": "fetch",
    "web_search_call": "fetch",
    "todowrite": "todo", "update_plan": "todo", "todoread": "todo",
    "task": "agent", "agent": "agent",
    "file_change": "edit", "patch_apply": "edit", "mcp_tool_call": "mcp",
    "image_view": "read", "image_generation": "other", "dynamic_tool_call": "other",
    "collab_tool_call": "agent", "collab_agent_tool_call": "agent",
}
_SHELL_NAMES = {"bash", "shell", "exec_command", "local_shell", "run_terminal_command",
                "run_command", "command_execution", "terminal"}
_LABEL_LIMIT = 120


def _shell_category(command: str) -> tuple[str, str]:
    from .codex_transcript import _classify_exploration, _strip_shell_wrapper
    try:
        line = _classify_exploration(command)
    except Exception:  # noqa: BLE001 - classification is presentation only
        line = None
    if line:
        kind = {"Read": "read", "List": "list", "Search": "search"}.get(line["label"], "exec")
        return kind, truncate(line["text"], _LABEL_LIMIT)
    try:
        shown = _strip_shell_wrapper(command)
    except Exception:  # noqa: BLE001
        shown = command
    first = shown.strip().splitlines()[0] if shown.strip() else command
    return "exec", truncate(first, _LABEL_LIMIT)


def classify_tool(name: str, tool_input: dict[str, Any] | None) -> tuple[str, str, str | None]:
    """(category, label, command) for one tool call."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    raw_name = str(name or "").strip()
    key = raw_name.lower()
    command = tool_input.get("command") or tool_input.get("cmd")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if key.startswith("mcp__") or key.startswith("mcp:"):
        return "mcp", truncate(raw_name.split("__")[-1], _LABEL_LIMIT), None
    if key in _SHELL_NAMES or (command and key not in _BY_NAME):
        text = str(command or raw_name)
        category, label = _shell_category(text)
        return category, label, truncate(text, 2000)
    category = _BY_NAME.get(key)
    if category is None and (" " in raw_name or "/" in raw_name):
        # Codex reports a command line as the tool's name.
        category, label = _shell_category(raw_name)
        return category, label, truncate(raw_name, 2000)
    path = tool_input.get("file_path") or tool_input.get("filePath") or tool_input.get("path") \
        or tool_input.get("notebook_path")
    if category in {"read", "edit", "write", "list"} and path:
        return category, path_tail(path), None
    if category == "search":
        pattern = tool_input.get("pattern") or tool_input.get("query") or path or ""
        return category, truncate(pattern, _LABEL_LIMIT), None
    if category == "fetch":
        target = tool_input.get("url") or tool_input.get("query") or ""
        return category, truncate(target, _LABEL_LIMIT), None
    if category == "agent":
        return category, truncate(tool_input.get("description") or raw_name, _LABEL_LIMIT), None
    if category == "todo":
        return category, "plan", None
    if category:
        return category, path_tail(path) if path else raw_name, None
    return "other", truncate(raw_name, _LABEL_LIMIT), None
