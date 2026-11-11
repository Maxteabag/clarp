"""How the Hotseat desk reads each provider out of Hotseat's answers.

Hotseat names its windows, lays out its `list --json` rows and reports quota
differently per provider. Everything the desk needs to know about one provider
is a `DeskReading` here. The readings are attached to the one table that lists
Hotseat's providers (`janitor_hotseat.PROVIDERS`); `hotseat_desk` looks a
provider up there and calls what it finds, and never asks which provider it is
holding (docs/architecture/backend-strategy.md). A provider without a reading
(Muse) gets the desk's generic treatment.

A leaf: it imports nothing from the desk or the switcher.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    val = float(value)
    return val if math.isfinite(val) else None


def _ratio(value) -> float | None:
    val = _num(value)
    if val is None or val < 0 or val > 1.5:
        return None
    return round(min(val, 1.0), 4)


def _epoch_ms_from_seconds(value) -> int | None:
    val = _num(value)
    if val is None or val <= 0:
        return None
    return int(round(val * 1000))


def _int_ms(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    val = int(round(float(value)))
    return val if val > 0 else None


_CLAUDE_FAMILIES = (("fable", "Fable"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"))


def _claude_window_meta(provider: str, raw: str) -> tuple[str, str] | None:
    """"All models · 5-hour" and "All models · weekly" are the account's own
    windows; "<Model> · weekly" is that model's."""
    lower = raw.casefold()
    five_hour = "5-hour" in lower or "5h" in lower
    account = lower.startswith("all models") or "·" not in raw
    if account and five_hour:
        return "5h", "5h"
    if lower.startswith("all models") and "weekly" in lower:
        return "7d", "Weekly"
    for family, title in _CLAUDE_FAMILIES:
        if family in lower:
            return (f"model:{family}:5h", f"{title} 5h") if five_hour else (f"model:{family}", title)
    return None


def _codex_window_meta(provider: str, raw: str) -> tuple[str, str] | None:
    """Hotseat labels a Codex window "<limit> [tier] <span>"; the account's own
    limit carries the provider's name (hotseat_status._codex_kind)."""
    lower = raw.casefold()
    words = lower.replace("·", " ").split()
    five_hour = "5-hour" in lower or "5h" in lower
    if "spark" in lower:
        return ("model:spark:5h", "Spark 5h") if five_hour else ("model:spark", "Spark wk")
    if words and (words[0] == provider or len(words) == 1):
        if five_hour:
            return "5h", "5h"
        if "30-day" in lower:
            return "30d", "30-day"
        if "weekly" in lower:
            return "7d", "Weekly"
    return None


def _usage_of(row: dict) -> dict | None:
    return row.get("usage") if isinstance(row.get("usage"), dict) else None


def no_extras() -> dict:
    """What `hotseat list --json` adds to an account when it says nothing."""
    return {"email": "", "workspace": "", "plan": "", "plan_code": None, "rate_limit_tier": "",
            "error_code": None, "blocked": False, "extra_usage": False, "blocked_models": [],
            "access_expires_at": None, "access_hours_left": None, "refresh_expires_at": None,
            "signin_days_left": None, "signin_due_soon": None,
            "token_expires_at": None, "token_hours_left": None, "api_key_present": None,
            "extra_usage_enabled": None, "extra_usage_reason": ""}


def _claude_extras(row: dict) -> dict:
    usage = _usage_of(row) or {}
    access_hours = _num(row.get("access_hours_left"))
    signin_days = _num(row.get("signin_days_left"))
    return {
        "email": row.get("email"), "workspace": row.get("org"), "plan": row.get("plan_label"),
        "plan_code": row.get("plan"), "rate_limit_tier": row.get("rate_limit_tier"),
        "error_code": row.get("error_code") if isinstance(row.get("error_code"), str) else None,
        "extra_usage": usage.get("extra_usage_enabled") is True,
        "blocked_models": [str(m) for m in (usage.get("blocked_models") or []) if isinstance(m, str) and m.strip()],
        "access_expires_at": _int_ms(row.get("access_expires_at")),
        "access_hours_left": round(access_hours, 2) if access_hours is not None else None,
        "refresh_expires_at": _int_ms(row.get("refresh_expires_at")),
        "signin_days_left": round(signin_days, 1) if signin_days is not None else None,
        "signin_due_soon": bool(row.get("signin_due_soon")),
        "extra_usage_enabled": bool(usage["extra_usage_enabled"]) if "extra_usage_enabled" in usage else None,
        "extra_usage_reason": str(usage.get("extra_usage_reason") or "") if usage else "",
    }


def _codex_extras(row: dict) -> dict:
    usage = _usage_of(row) or {}
    token_hours = _num(row.get("token_hours_left"))
    return {
        "email": row.get("email"), "workspace": row.get("account_id"), "plan": row.get("plan"),
        "plan_code": row.get("plan"), "blocked": bool(usage.get("blocked")),
        "token_expires_at": _epoch_ms_from_seconds(row.get("token_expires_at")),
        "token_hours_left": round(token_hours, 2) if token_hours is not None else None,
        "api_key_present": bool(row["api_key_present"]) if "api_key_present" in row else None,
    }


def _claude_switcher_row(row: dict) -> dict | None:
    """The quota part of a Claude row of `list --json`, as the TUI's windows:
    Hotseat's bridge builds the TUI row from these same numbers."""
    usage = _usage_of(row)
    error = str(row.get("error") or "").strip()
    if usage is None and not error:
        return None
    usage = usage or {}
    windows: list[dict] = []
    for label, used_key, reset_key in (("All models · 5-hour", "used_5h", "reset_5h"),
                                       ("All models · weekly", "used_7d", "reset_7d")):
        if _num(usage.get(used_key)) is not None:
            windows.append({"label": label, "used": usage[used_key], "reset": usage.get(reset_key) or 0})
    for scoped in usage.get("scoped") or []:
        if isinstance(scoped, dict) and isinstance(scoped.get("model"), str) and scoped["model"].strip():
            windows.append({"label": f"{scoped['model']} · weekly", "used": scoped.get("used"),
                            "reset": scoped.get("reset") or 0})
    return {"error": error, "windows": windows, "no_reset_reason": "",
            "limited": usage.get("limited") is True or usage.get("status") == "rejected"}


def _codex_switcher_row(row: dict) -> dict | None:
    """The quota part of a Codex row of `codex --json`, as the TUI's windows."""
    usage = _usage_of(row)
    if usage is None:
        return None
    windows = [{"label": w.get("label"), "used": w.get("used"), "reset": w.get("reset") or 0}
               for w in usage.get("windows") or [] if isinstance(w, dict)]
    # Codex's block is `usage.blocked`, which the row carries into the extras itself.
    return {"error": str(usage.get("error") or "").strip(), "windows": windows, "limited": False,
            "no_reset_reason": str(usage.get("no_reset_reason") or "")}


def _codex_list_rows(list_data: dict):
    block = list_data.get("codex")
    return block.get("accounts") if isinstance(block, dict) else None


@dataclass(frozen=True)
class DeskReading:
    """How the desk reads one provider out of Hotseat's answers; looked up, never branched on."""
    # (kind, short label) of a quota window from Hotseat's label, None when it
    # is not a window this provider names. Called with the provider's own name.
    window_meta: Callable[[str, str], tuple[str, str] | None]
    # This provider's account rows inside `hotseat list --json`.
    list_rows: Callable[[dict], object]
    # What such a row adds to an account of the desk (the keys of no_extras).
    extras: Callable[[dict], dict]
    # The quota part of the row the switcher's scheduled read has for an account,
    # as the TUI's windows: {error, windows, limited, no_reset_reason}.
    switcher_row: Callable[[dict], dict | None]
    # The switcher's scheduled read of this provider is `list --json`: the read
    # that carries every account, the stopped work and the agents' accounts.
    whole_list: bool


def _claude_list_rows(list_data: dict):
    return list_data.get("accounts")


CLAUDE_READING = DeskReading(_claude_window_meta, _claude_list_rows, _claude_extras, _claude_switcher_row,
                             whole_list=True)
CODEX_READING = DeskReading(_codex_window_meta, _codex_list_rows, _codex_extras, _codex_switcher_row,
                            whole_list=False)
