"""Encode per-agent MCP server choices.

The column holds ``{"configured": true, "servers": [...]}`` once the user has
made a choice; the default ``'[]'`` means the agent has never been configured.
"""
from __future__ import annotations

import json


def encode(servers: list[str]) -> str:
    return json.dumps({"configured": True, "servers": servers})


def decode(raw: str | None) -> tuple[bool, list[str]]:
    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return False, []
    if isinstance(value, dict) and value.get("configured") is True:
        servers = value.get("servers")
        return True, [str(item) for item in servers] if isinstance(servers, list) else []
    return False, []
