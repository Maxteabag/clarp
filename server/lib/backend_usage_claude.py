"""Moved out of server/lib/backend_usage.py by tools/refactor/split_apply.py (split 'backend-usage', recipe tools/refactor/splits/s6-backends.toml).
backend_usage.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any
from .clock import now_ms as _now_ms
from .backend_usage_store import CLAUDE, CLAUDE_REFRESH_MS, FRESH_MS, _decode_raw, _normalize_time, _private_ref, _row


def _claude_response_row(now: int) -> dict[str, Any]:
    """Claude usage from Clarp's own per-turn accounting.

    No Claude endpoint reports quota, and the statusline that used to supply it
    never runs under `-p` — so it saw none of the turns Clarp dispatches.
    `used_percentage` is therefore always None: this is spend, not headroom.
    `windows` stays empty so a client written for the quota shape renders
    "unknown" instead of a wrong number; the real figures live in `totals`.
    """
    from . import turn_usage
    data = turn_usage.totals(CLAUDE, now=now)
    last = data["last_turn_at"]
    return {
        "backend": CLAUDE,
        "used_percentage": None,
        "resets_at": None,
        "source": "clarp-turn-accounting",
        "fetched_at": _normalize_time(last / 1000) if last else None,
        "freshness": "unknown" if not last else (
            "fresh" if now - last <= FRESH_MS else "stale"),
        "error": "" if last else "no turns recorded yet",
        "windows": {},
        "totals": data["windows"],
    }


def _claude_config_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def _read_claude_auth() -> tuple[str, int, str]:
    payload = json.loads((_claude_config_dir() / ".credentials.json").read_text())
    login = payload.get("claudeAiOauth")
    if not isinstance(login, dict):
        raise RuntimeError("Claude OAuth credentials unavailable")
    token = str(login.get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("Claude OAuth access token unavailable")
    expires = int(login.get("expiresAt") or 0)
    if expires and expires <= _now_ms():
        raise RuntimeError("Claude sign-in expired")
    tier = str(login.get("rateLimitTier") or "")
    plan = str(login.get("subscriptionType") or "")
    return token, expires, tier or plan


def _active_claude_auth_generation() -> str | None:
    try:
        token, _expires, _plan = _read_claude_auth()
        return _private_ref("claude-auth", token)
    except Exception:  # noqa: BLE001
        return None


def _claude_needs_refresh() -> bool:
    row = _row(CLAUDE)
    if not row:
        return True
    raw = _decode_raw(row)
    active = _active_claude_auth_generation()
    if active is not None and raw.get("auth_generation_id") and raw.get("auth_generation_id") != active:
        return True
    return _now_ms() - int(row.get("fetched_at") or 0) > CLAUDE_REFRESH_MS
