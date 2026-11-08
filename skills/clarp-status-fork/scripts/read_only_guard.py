#!/usr/bin/env python3
"""PreToolUse hook for clarp-status-fork: a fork may only read.

Every tool call outside READ_ONLY_TOOLS is denied, whatever the inherited
permission rules allow. A hook decides at call time, so the fork keeps the
agent's tool list and therefore its prompt cache.
"""
import json
import sys

READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})


def decision(tool_name: str) -> dict | None:
    """None leaves a read to the normal permission rules (e.g. a Read(./.env) deny)."""
    if tool_name in READ_ONLY_TOOLS:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"{tool_name} is not allowed: this is a read-only status fork. "
            "Answer from the conversation and file reads only."),
    }}


def main() -> int:
    try:
        tool_name = json.load(sys.stdin).get("tool_name", "")
    except (json.JSONDecodeError, AttributeError) as exc:
        # Unreadable input is not a reason to let an unknown call through.
        print(f"read_only_guard: unreadable hook input ({exc}); denying", file=sys.stderr)
        tool_name = ""
    verdict = decision(tool_name)
    if verdict is not None:
        print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
