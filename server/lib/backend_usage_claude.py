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


def _claude_account_marker() -> str | None:
    """Stable account marker across OAuth access-token refreshes, or None when
    only a bare accessToken is present. Never hashes accessToken/refreshToken."""
    cfg_dir = _claude_config_dir()
    candidates: list[pathlib.Path] = []
    explicit = (os.environ.get("CLAUDE_CONFIG_FILE") or "").strip()
    if explicit:
        candidates.append(pathlib.Path(explicit).expanduser())
    candidates.append(cfg_dir / ".claude.json")
    candidates.append(cfg_dir.parent / ".claude.json")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            continue
        oauth = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(oauth, dict):
            acct_uuid = str(oauth.get("accountUuid") or "").strip()
            org_uuid = str(oauth.get("organizationUuid") or "").strip()
            email = str(oauth.get("emailAddress") or "").strip().casefold()
            if acct_uuid or org_uuid or email:
                return f"oauth:{acct_uuid}\0{org_uuid}\0{email}"
    marker_path = cfg_dir / "profiles" / ".current_profile"
    try:
        if marker_path.is_file():
            alias = marker_path.read_text().strip()
            if alias:
                return f"profile:{alias}"
    except (OSError, UnicodeDecodeError):
        pass
    try:
        cred_data = json.loads((cfg_dir / ".credentials.json").read_text())
        login = cred_data.get("claudeAiOauth") if isinstance(cred_data, dict) else None
        if isinstance(login, dict):
            acct_uuid = str(login.get("accountUuid") or "").strip()
            org_uuid = str(login.get("organizationUuid") or "").strip()
            email = str(login.get("emailAddress") or "").strip().casefold()
            if acct_uuid or org_uuid or email:
                return f"cred:{acct_uuid}\0{org_uuid}\0{email}"
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        pass
    return None


def _active_claude_account_ref(*, create: bool = False) -> str | None:
    try:
        _read_claude_auth()
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError, RuntimeError):
        return None
    marker = _claude_account_marker()
    if not marker:
        return None
    return _private_ref("claude-account", marker, create=create)


def _claude_needs_refresh() -> bool:
    row = _row(CLAUDE)
    if not row:
        return True
    raw = _decode_raw(row)
    stored_ref = raw.get("account_scope_ref")
    if stored_ref:
        active_ref = _active_claude_account_ref(create=False)
        if active_ref is not None and stored_ref != active_ref:
            return True
    return _now_ms() - int(row.get("fetched_at") or 0) > CLAUDE_REFRESH_MS
