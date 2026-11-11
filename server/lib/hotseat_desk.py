"""Hotseat Desk: full TUI-grade snapshot and interactive controls for iOS/desktop.

`GET /hotseat/status` remains the strict, redacted, zero-CLI contract snapshot.
`hotseat_desk` serves authenticated full-device clients with the complete Hotseat
TUI model (`claude`, `codex`, `muse` accounts, inline windows, reset credits,
token/sign-in clocks, Clarp live/routed session counts, stopped & recent work)
plus interactive operations:

  * `GET /hotseat/desk` (`refresh=0`, default): instant persisted read (no CLI).
  * `GET /hotseat/desk?refresh=1`: single-flight live `hotseat` CLI refresh.
  * `POST /hotseat/switch`: switch machine-wide default `claude` or `codex` seat.
  * `POST /hotseat/resume`: continue a stopped Clarp agent or unpause its queue.

The job state of the desk (the one live read in flight and when it started, the
switch lock of each provider, the latest switch asked for here) belongs to a
`HotseatDesk` object that `build_server` builds and the handler reaches through
the context. Everything that must survive a restart is in the settings store.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import re
import shutil
import subprocess
import threading
from typing import Callable

from . import agents as agents_db
from . import db
from . import (hotseat_status, identity, janitor_hotseat, janitor_store, message_store,
               settings_store, turn_queue)
from .clock import now_ms
from .log import log

SCHEMA = "clarp.hotseat-desk/1"
SNAPSHOT_KEY = "hotseat-desk.snapshot.v1"
ERROR_KEY = "hotseat-desk.error.v1"
# How the latest switch asked for on the desk ended, per provider. The status
# route's action receipt is not used for it: its reason codes are a closed list
# of the automatic switcher's own branches, and record_action also rewrites the
# switcher's lane (mode, floor, cadence), neither of which fits a manual switch.
SWITCH_KEY_PREFIX = "hotseat-desk.switch.v1."
SWITCH_STATES = frozenset({"running", "switched", "already_active", "failed", "unavailable"})
STALE_MS = 10 * 60 * 1000
TUI_TIMEOUT_S = 45
LIST_TIMEOUT_S = 45
WORK_TIMEOUT_S = 20
# The shortest interval the Hotseat switcher itself may be set to check at
# (janitors.py, account-hotseat `interval_seconds` min): a client cannot make
# the Host read the providers more often than its own switcher may.
MIN_REFRESH_INTERVAL_MS = 60 * 1000
# The app waits 45 s for a switch. Reading who is active plus the switch itself
# can take longer than that (the CLI alone may use SWITCH_TIMEOUT), so the
# request answers "still running" after this long and the desk tells the rest.
SWITCH_ANSWER_BUDGET_S = 30
ACTIVE_READ_TIMEOUT_S = 45

RESUME_PROMPT = (
    "Continue the work that was interrupted by the usage limit. The account has "
    "quota again. Re-check the results of the interrupted step before retrying it, "
    "and do not repeat work that already completed."
)

_SAFE_ALIAS = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@-]{0,127}\Z")
_SAFE_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

# Who is active, asked of Hotseat itself. Codex without the quota probe: the
# probe is the slow part and says nothing about which profile is in use.
_ACTIVE_READS: dict[str, tuple[tuple[str, ...], Callable[[object], list[dict]]]] = {
    "claude": (("list", "--json"), janitor_hotseat.parse_claude),
    "codex": (("codex", "--no-quota", "--json"), janitor_hotseat.parse_codex),
}


def _no_clarp_agents() -> dict:
    return {"total": 0, "live": 0, "claude_backed": 0,
            "by_backend": {}, "live_by_account": {}, "would_use_by_account": {}}


def hotseat_command() -> str:
    """Configured Hotseat binary from the switcher Janitor options, or `hotseat`."""
    try:
        agent_id = janitor_store.builtin_agent_id(janitor_hotseat.ROLE)
        row = janitor_store.config_row(agent_id) if agent_id else None
        options = janitor_store.decode(row["options_json"], {}) if row else {}
        cmd = str(options.get("hotseat_command") or "").strip()
        if cmd:
            return cmd
    except Exception as exc:  # noqa: BLE001
        log("hotseatDeskCommandFallback", f"error={type(exc).__name__}")
    return "hotseat"


def cli_installed(command: str | None = None, *, which: Callable[[str], str | None] = shutil.which) -> bool:
    cmd = (command or hotseat_command()).strip()
    return bool(cmd and which(cmd))


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


def _window_meta(provider: str, label: str) -> tuple[str, str]:
    raw = str(label or "").strip()
    lower = raw.casefold()
    if provider == "claude":
        if "5-hour" in lower or "5h" in lower:
            return "5h", "5h"
        if lower.startswith("all models") and "weekly" in lower:
            return "7d", "Weekly"
        for family, title in (("fable", "Fable"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")):
            if family in lower:
                return f"model:{family}", title
        return f"window:{lower[:24]}", raw[:24] or "Window"
    if provider == "codex":
        if "spark" in lower:
            if "5-hour" in lower or "5h" in lower:
                return "model:spark:5h", "Spark 5h"
            return "model:spark", "Spark wk"
        if "5-hour" in lower or "5h" in lower:
            return "5h", "5h"
        if "30-day" in lower:
            return "30d", "30-day"
        if "weekly" in lower:
            return "7d", "Weekly"
    return f"window:{lower[:24]}", raw[:24] or "Window"


def _classify_account_state(*, error: str, error_code: str | None, limited: bool, windows: list[dict]) -> tuple[str, str | None]:
    lower_err = error.casefold()
    if error_code == "auth_error" or ("token" in lower_err and ("expired" in lower_err or "revoked" in lower_err or "sign-in" in lower_err)):
        return "auth_error", "auth_error"
    if error:
        return "error", error_code or "provider_error"
    if limited:
        return "exhausted", "exhausted"
    if any(w.get("used_ratio") is not None and w["used_ratio"] >= 1.0 for w in windows):
        return "exhausted", "exhausted"
    if any(w.get("used_ratio") is not None for w in windows):
        return "ok", None
    return "unknown", None


def _run_json(command: str, args: list[str], timeout: int, *, runner=subprocess.run) -> dict:
    proc = runner(
        [command, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        detail = stderr[-1][:160] if stderr else f"exit {proc.returncode}"
        raise RuntimeError(f"hotseat {' '.join(args[:2])} failed: {detail}")
    data = json.loads(proc.stdout or "{}")
    if not isinstance(data, dict):
        raise ValueError("hotseat JSON root must be an object")
    return data


def _build_desk_snapshot(
    tui_data: dict,
    list_data: dict | None,
    work_data: dict | None,
    *,
    observed_at: int,
) -> dict:
    claude_by_alias: dict[str, dict] = {}
    codex_by_alias: dict[str, dict] = {}
    clarp_agents_summary = {
        "total": 0,
        "live": 0,
        "claude_backed": 0,
        "by_backend": {},
        "live_by_account": {},
        "would_use_by_account": {},
    }
    stopped_raw: list[dict] = []

    if isinstance(list_data, dict):
        for row in list_data.get("accounts") or []:
            if isinstance(row, dict) and isinstance(row.get("alias"), str):
                claude_by_alias[row["alias"]] = row
        codex_block = list_data.get("codex")
        if isinstance(codex_block, dict):
            for row in codex_block.get("accounts") or []:
                if isinstance(row, dict) and isinstance(row.get("alias"), str):
                    codex_by_alias[row["alias"]] = row
        ext_clarp = ((list_data.get("extensions") or {}).get("clarp") or {}) if isinstance(list_data.get("extensions"), dict) else {}
        if isinstance(ext_clarp, dict):
            agents_block = ext_clarp.get("agents")
            if isinstance(agents_block, dict):
                would_counts: dict[str, int] = {}
                for agent_row in agents_block.get("agents") or []:
                    if isinstance(agent_row, dict):
                        target = agent_row.get("would_use")
                        if isinstance(target, str) and target:
                            would_counts[target] = would_counts.get(target, 0) + 1
                live_map = agents_block.get("live_by_account") if isinstance(agents_block.get("live_by_account"), dict) else {}
                by_backend = agents_block.get("by_backend") if isinstance(agents_block.get("by_backend"), dict) else {}
                clarp_agents_summary = {
                    "total": int(agents_block.get("total") or 0),
                    "live": int(agents_block.get("live") or 0),
                    "claude_backed": int(agents_block.get("claude_backed") or 0),
                    "by_backend": {str(k): int(v) for k, v in by_backend.items() if isinstance(v, int)},
                    "live_by_account": {str(k): int(v) for k, v in live_map.items() if isinstance(v, int)},
                    "would_use_by_account": would_counts,
                }
            if isinstance(ext_clarp.get("stopped"), list):
                stopped_raw = [item for item in ext_clarp["stopped"] if isinstance(item, dict)]

    accounts: list[dict] = []
    for raw in tui_data.get("accounts") or []:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider") or "").strip().lower()
        alias = str(raw.get("alias") or "").strip()
        if not provider or not alias:
            continue
        checked_ms = _epoch_ms_from_seconds(raw.get("checked_at")) or observed_at
        error = str(raw.get("error") or "").strip()
        no_reset_reason = str(raw.get("no_reset_reason") or "").strip()

        windows: list[dict] = []
        for win in raw.get("windows") or []:
            if not isinstance(win, dict):
                continue
            label = str(win.get("label") or "").strip()
            kind, short_label = _window_meta(provider, label)
            used = None if error else _ratio(win.get("used"))
            resets_at = _epoch_ms_from_seconds(win.get("reset"))
            windows.append({
                "kind": kind,
                "label": label,
                "short_label": short_label,
                "used_ratio": used,
                "remaining_ratio": None if used is None else round(1.0 - used, 4),
                "resets_at": resets_at,
                "observed_at": checked_ms,
                "stale": bool(raw.get("cached")),
            })

        claude_extra = claude_by_alias.get(alias, {}) if provider == "claude" else {}
        codex_extra = codex_by_alias.get(alias, {}) if provider == "codex" else {}
        claude_usage = claude_extra.get("usage") if isinstance(claude_extra.get("usage"), dict) else {}
        codex_usage = codex_extra.get("usage") if isinstance(codex_extra.get("usage"), dict) else {}

        error_code = claude_extra.get("error_code") if isinstance(claude_extra.get("error_code"), str) else None
        limited = bool(raw.get("limited")) or bool(codex_usage.get("blocked"))
        state, code = _classify_account_state(
            error=error,
            error_code=error_code,
            limited=limited,
            windows=windows,
        )

        reset_credits = raw.get("reset_credits")
        if isinstance(reset_credits, bool) or not isinstance(reset_credits, int):
            reset_credits = None

        blocked_models = [
            str(m) for m in (claude_usage.get("blocked_models") or [])
            if isinstance(m, str) and m.strip()
        ]
        access_hours = _num(claude_extra.get("access_hours_left"))
        signin_days = _num(claude_extra.get("signin_days_left"))
        token_hours = _num(codex_extra.get("token_hours_left"))

        accounts.append({
            "provider": provider,
            "alias": alias,
            "display_alias": str(raw.get("display_alias") or alias),
            "aliases": [str(a) for a in (raw.get("aliases") or [alias]) if isinstance(a, str)],
            "email": str(raw.get("email") or claude_extra.get("email") or codex_extra.get("email") or ""),
            "workspace": str(raw.get("workspace") or claude_extra.get("org") or codex_extra.get("account_id") or ""),
            "plan": str(raw.get("plan") or claude_extra.get("plan_label") or codex_extra.get("plan") or ""),
            "plan_code": str(claude_extra.get("plan") or codex_extra.get("plan") or "").lower() or None,
            "rate_limit_tier": str(raw.get("rate_limit_tier") or claude_extra.get("rate_limit_tier") or ""),
            "active": bool(raw.get("active")),
            "saved": bool(raw.get("saved", True)),
            "can_switch": bool(raw.get("can_switch")),
            "can_launch": bool(raw.get("can_launch")),
            "limited": limited,
            "cached": bool(raw.get("cached")),
            "state": state,
            "error_code": code,
            "error": error,
            "warning": str(raw.get("warning") or ""),
            "profile_notes": [str(n) for n in (raw.get("profile_notes") or []) if isinstance(n, str) and n.strip()],
            "no_reset_reason": no_reset_reason,
            "checked_at": checked_ms,
            "reset_credits": reset_credits,
            "reset_credits_error": str(raw.get("reset_credits_error") or ""),
            "access_expires_at": _int_ms(claude_extra.get("access_expires_at")),
            "access_hours_left": round(access_hours, 2) if access_hours is not None else None,
            "refresh_expires_at": _int_ms(claude_extra.get("refresh_expires_at")),
            "signin_days_left": round(signin_days, 1) if signin_days is not None else None,
            "signin_due_soon": bool(claude_extra.get("signin_due_soon")) if provider == "claude" else None,
            "token_expires_at": _epoch_ms_from_seconds(codex_extra.get("token_expires_at")),
            "token_hours_left": round(token_hours, 2) if token_hours is not None else None,
            "api_key_present": bool(codex_extra["api_key_present"]) if "api_key_present" in codex_extra else None,
            "blocked_models": blocked_models,
            "extra_usage_enabled": bool(claude_usage["extra_usage_enabled"]) if "extra_usage_enabled" in claude_usage else None,
            "extra_usage_reason": str(claude_usage.get("extra_usage_reason") or "") if claude_usage else "",
            "live_sessions": int(clarp_agents_summary["live_by_account"].get(alias, 0)),
            "would_use_sessions": int(clarp_agents_summary["would_use_by_account"].get(alias, 0)),
            "windows": windows,
        })

    stopped_work: list[dict] = []
    for item in stopped_raw:
        kind = str(item.get("kind") or "clarp")
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        backend = str(item.get("backend") or "")
        readiness_raw = item.get("readiness") if isinstance(item.get("readiness"), dict) else None
        readiness = None
        if readiness_raw is not None:
            readiness = {
                "ready": bool(readiness_raw.get("ready")),
                "hard": str(readiness_raw["hard"]) if isinstance(readiness_raw.get("hard"), str) and readiness_raw["hard"] else None,
                "warning": str(readiness_raw["warning"]) if isinstance(readiness_raw.get("warning"), str) and readiness_raw["warning"] else None,
            }
        resume_cmd = None
        if kind != "clarp":
            resume_cmd = f"codex resume {item_id}" if backend == "codex" else f"claude --resume {item_id}"
        stopped_work.append({
            "kind": kind,
            "cause": str(item.get("cause") or "usage_limit"),
            "pending": int(item.get("pending") or 0),
            "id": item_id,
            "name": str(item.get("name") or item_id),
            "backend": backend,
            "model": str(item.get("model") or ""),
            "account": str(item.get("account") or ""),
            "cwd": str(item.get("cwd") or ""),
            "last_message": str(item.get("last_message") or ""),
            "stopped_at": _epoch_ms_from_seconds(item.get("stopped_at")),
            "readiness": readiness,
            "resume_command": resume_cmd,
        })

    recent_work: list[dict] = []
    if isinstance(work_data, dict):
        for item in (work_data.get("items") or [])[:40]:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            provider = str(item.get("provider") or "")
            can_resume = bool(item.get("can_resume"))
            resume_cmd = None
            if can_resume and not item.get("live"):
                resume_cmd = f"codex resume {item_id}" if provider == "codex" else f"claude --resume {item_id}"
            recent_work.append({
                "id": item_id,
                "provider": provider,
                "title": str(item.get("title") or ""),
                "state": str(item.get("state") or "unknown"),
                "updated_at": _epoch_ms_from_seconds(item.get("updated")),
                "cwd": str(item.get("cwd") or ""),
                "live": bool(item.get("live")),
                "can_resume": can_resume,
                "can_reboot": bool(item.get("can_reboot")),
                "reason": str(item.get("reason") or ""),
                "resume_command": resume_cmd,
            })

    errors = [str(e) for e in (tui_data.get("errors") or []) if isinstance(e, str) and e.strip()]
    sessions_val = tui_data.get("sessions")
    sessions = int(sessions_val) if isinstance(sessions_val, int) and not isinstance(sessions_val, bool) else 0

    return {
        "v": 1,
        "observed_at": observed_at,
        "sessions": sessions,
        "errors": errors,
        "clarp_agents": clarp_agents_summary,
        "accounts": accounts,
        "stopped_work": stopped_work,
        "recent_work": recent_work,
    }


def _load_persisted() -> dict | None:
    raw = settings_store.get(SNAPSHOT_KEY)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) and parsed.get("v") == 1 else None


def _fallback_accounts_from_status(status_payload: dict) -> tuple[list[dict], int | None]:
    accounts: list[dict] = []
    latest_observed: int | None = None
    for prov in status_payload.get("providers") or []:
        if not isinstance(prov, dict):
            continue
        provider = str(prov.get("provider") or "")
        prov_obs = _int_ms(prov.get("observed_at"))
        if prov_obs and (latest_observed is None or prov_obs > latest_observed):
            latest_observed = prov_obs
        for acc in prov.get("accounts") or []:
            if not isinstance(acc, dict):
                continue
            alias = str(acc.get("alias") or "account")
            windows = []
            for w in acc.get("windows") or []:
                if not isinstance(w, dict):
                    continue
                kind = str(w.get("kind") or "")
                label = str(w.get("label") or kind)
                _, short_label = _window_meta(provider, label)
                windows.append({
                    "kind": kind,
                    "label": label,
                    "short_label": short_label,
                    "used_ratio": _ratio(w.get("used_ratio")),
                    "remaining_ratio": _ratio(w.get("remaining_ratio")),
                    "resets_at": _int_ms(w.get("resets_at")),
                    "observed_at": _int_ms(w.get("observed_at")),
                    "stale": bool(w.get("stale", True)),
                })
            accounts.append({
                "provider": provider,
                "alias": alias,
                "display_alias": alias,
                "aliases": [alias],
                "email": "",
                "workspace": "",
                "plan": str(acc.get("label") or acc.get("plan") or ""),
                "plan_code": str(acc.get("plan") or "").lower() or None,
                "rate_limit_tier": "",
                "active": bool(acc.get("active_default")),
                "saved": True,
                "can_switch": False,
                "can_launch": False,
                "limited": acc.get("state") == "exhausted",
                "cached": True,
                "state": str(acc.get("state") or "unknown"),
                "error_code": acc.get("error_code"),
                "error": "",
                "warning": "",
                "profile_notes": [],
                "no_reset_reason": "",
                "checked_at": _int_ms(acc.get("last_good_observed_at")),
                "reset_credits": None,
                "reset_credits_error": "",
                "access_expires_at": None,
                "access_hours_left": None,
                "refresh_expires_at": None,
                "signin_days_left": None,
                "signin_due_soon": None,
                "token_expires_at": None,
                "token_hours_left": None,
                "api_key_present": None,
                "blocked_models": [],
                "extra_usage_enabled": None,
                "extra_usage_reason": "",
                "live_sessions": 0,
                "would_use_sessions": 0,
                "windows": windows,
            })
    return accounts, latest_observed


def _serve_accounts(raw_accounts: list[dict], *, served_at: int, snapshot_stale: bool) -> list[dict]:
    served: list[dict] = []
    for acc in raw_accounts:
        if not isinstance(acc, dict):
            continue
        item = dict(acc)
        no_reset = bool(item.get("no_reset_reason"))
        out_windows: list[dict] = []
        reset_passed = False
        for win in item.get("windows") or []:
            if not isinstance(win, dict):
                continue
            w = dict(win)
            used = _ratio(w.get("used_ratio"))
            resets_at = _int_ms(w.get("resets_at"))
            obs = _int_ms(w.get("observed_at"))
            stale = bool(w.get("stale")) or snapshot_stale
            if not no_reset and resets_at is not None and resets_at <= served_at and (obs is None or obs < resets_at):
                used = None
                stale = True
                reset_passed = True
            w["used_ratio"] = used
            w["remaining_ratio"] = None if used is None else round(1.0 - used, 4)
            w["resets_at"] = resets_at
            w["stale"] = stale
            out_windows.append(w)
        if item.get("state") == "exhausted" and reset_passed and not any(
            (w.get("used_ratio") or 0) >= 1.0 for w in out_windows if w.get("used_ratio") is not None
        ):
            item["state"] = "unknown"
            item["error_code"] = None
            item["limited"] = False
        item["windows"] = out_windows
        served.append(item)
    return served


def _read_live(cmd: str, runner, observed_at: int) -> dict:
    """One live read of Hotseat: three CLI calls, merged into a desk snapshot.
    Also hands the account list to the status store, as the switcher's read does."""
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="hotseat-desk") as pool:
        fut_tui = pool.submit(_run_json, cmd, ["tui", "--snapshot"], TUI_TIMEOUT_S, runner=runner)
        fut_list = pool.submit(_run_json, cmd, ["list", "--json"], LIST_TIMEOUT_S, runner=runner)
        fut_work = pool.submit(_run_json, cmd, ["bridge", "work"], WORK_TIMEOUT_S, runner=runner)

        tui_data = fut_tui.result()
        try:
            list_data = fut_list.result()
        except Exception as exc:  # noqa: BLE001
            log("hotseatDeskListPartial", f"error={type(exc).__name__}")
            list_data = None
        try:
            work_data = fut_work.result()
        except Exception as exc:  # noqa: BLE001
            log("hotseatDeskWorkPartial", f"error={type(exc).__name__}")
            work_data = None

    if isinstance(list_data, dict):
        hotseat_status.record_read("claude", list_data, observed_at=observed_at)
        if isinstance(list_data.get("codex"), dict):
            hotseat_status.record_read("codex", list_data["codex"], observed_at=observed_at)
    return _build_desk_snapshot(tui_data, list_data, work_data, observed_at=observed_at)


def read_active_alias(command: str, provider: str, *, runner=subprocess.run) -> str | None:
    """The alias Hotseat reports as the machine-wide default of `provider`, or
    None when it reports none. Raises when Hotseat cannot be read."""
    args, parse = _ACTIVE_READS[provider]
    payload = _run_json(command, list(args), ACTIVE_READ_TIMEOUT_S, runner=runner)
    for account in parse(payload):
        if account.get("active"):
            return str(account["alias"])
    return None


def _credentials_changed(provider: str) -> None:
    """A runner that read credentials once must restart to see the new account;
    the same call the automatic switcher makes after its switch."""
    from . import backends
    backends.by_id(provider).on_credential_change()


def _mark_active(provider: str, alias: str) -> None:
    """Hotseat confirmed the switch: the stored desk names the new account at
    once, before the live read that follows has finished (or if it fails)."""
    snapshot = _load_persisted()
    if snapshot is None:
        return
    accounts = [acc for acc in snapshot.get("accounts") or []
                if isinstance(acc, dict) and acc.get("provider") == provider]
    if not any(alias == acc.get("alias") or alias in (acc.get("aliases") or []) for acc in accounts):
        return
    for acc in accounts:
        acc["active"] = alias == acc.get("alias") or alias in (acc.get("aliases") or [])
    settings_store.set_text(SNAPSHOT_KEY, json.dumps(snapshot, separators=(",", ":")))


def _stored_switch(provider: str) -> dict | None:
    raw = settings_store.get(SWITCH_KEY_PREFIX + provider)
    try:
        record = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("state") not in SWITCH_STATES - {"running"}:
        return None
    return record


def _refusal(error: str, message: str, **fields) -> tuple[int, dict]:
    """409 with a code for the client's logic and a sentence it can show as is."""
    return 409, {"ok": False, "error": error, "message": message, **fields}


def resume_message_id(agent_id: str, stopped_at: int) -> str:
    """The id of the one resume prompt a stop can get. Derived from the agent
    and the stop, so every request about that stop is the same send to the
    dispatcher, whose own retry protection then starts at most one turn."""
    return f"hotseat-resume-{agent_id}-{int(stopped_at)}"


class HotseatDesk:
    """The desk of one Host process.

    Holds what cannot outlive the process and must not be shared by accident:
    the live read in flight, when the last one started, one switch lock per
    provider (the automatic switcher takes the same lock for its own switch),
    the latest switch asked for here and the lock that orders resumes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: threading.Event | None = None
        self._last_read_at = 0
        self._switch_locks = {provider: threading.Lock() for provider in janitor_hotseat.PROVIDERS}
        self._switches: dict[str, dict] = {}
        self._resume_lock = threading.Lock()

    def switch_lock(self, provider: str) -> threading.Lock:
        """Held around every `hotseat switch` of `provider` and the write of its
        flip-back record, whoever asks: this desk or the automatic switcher."""
        return self._switch_locks[provider]

    # --- reading ----------------------------------------------------------

    def _next_refresh_at(self, persisted: dict | None) -> int:
        observed = _int_ms((persisted or {}).get("observed_at")) or 0
        with self._lock:
            last = self._last_read_at
        return max(last, observed) + MIN_REFRESH_INTERVAL_MS

    def refresh(self, *, command: str | None = None, runner=subprocess.run, force: bool = False) -> bool:
        """One live read of Hotseat, single-flight. False when the read was not
        allowed yet (inside MIN_REFRESH_INTERVAL_MS of the last one) and nothing
        ran. A request that finds a read in flight waits for it and counts as
        read. `force` is for the desk's own read after a switch: it never joins
        a read that started before the switch and is not held back by the interval."""
        cmd = (command or hotseat_command()).strip()
        while True:
            observed = _int_ms((_load_persisted() or {}).get("observed_at")) or 0
            with self._lock:
                waiting_for = self._inflight
                if waiting_for is None:
                    now = now_ms()
                    if not force and now < max(self._last_read_at, observed) + MIN_REFRESH_INTERVAL_MS:
                        return False
                    done = self._inflight = threading.Event()
                    self._last_read_at = now
                    break
            waiting_for.wait(timeout=TUI_TIMEOUT_S + 5)
            if not force:
                return True
        try:
            snapshot = _read_live(cmd, runner, now)
            settings_store.set_text(SNAPSHOT_KEY, json.dumps(snapshot, separators=(",", ":")))
            settings_store.set_text(ERROR_KEY, "")
        except Exception as exc:  # noqa: BLE001
            code = hotseat_status.error_code_for(exc)
            log("hotseatDeskRefreshFailed", f"code={code} error={type(exc).__name__}")
            settings_store.set_text(ERROR_KEY, code)
        finally:
            with self._lock:
                self._inflight = None
            done.set()
        return True

    def payload(
        self,
        *,
        refresh: bool = False,
        served_at: int | None = None,
        command: str | None = None,
        which: Callable[[str], str | None] = shutil.which,
        runner=subprocess.run,
    ) -> dict:
        """The desk as stored. `refresh=False` never executes a subprocess;
        `refresh=True` runs a live read first when one is allowed, and says so
        in `refresh_throttled` when it was not."""
        cmd = (command or hotseat_command()).strip()
        throttled = bool(refresh) and not self.refresh(command=cmd, runner=runner)

        now = now_ms() if served_at is None else int(served_at)
        installed = cli_installed(cmd, which=which)
        status = hotseat_status.payload(served_at=now)
        persisted = _load_persisted()
        raw_err = str(settings_store.get(ERROR_KEY) or "").strip()
        last_error = raw_err if raw_err in hotseat_status.ERROR_CODES else None

        with self._lock:
            refreshing = self._inflight is not None
            running = {p: dict(record) for p, record in self._switches.items()}
        switches = [record for p in janitor_hotseat.PROVIDERS
                    if (record := running.get(p) or _stored_switch(p))]

        if persisted is not None:
            observed_at = _int_ms(persisted.get("observed_at"))
            stale = observed_at is None or (now - observed_at > STALE_MS) or bool(last_error)
            accounts = _serve_accounts(persisted.get("accounts") or [], served_at=now, snapshot_stale=stale)
            sessions = int(persisted.get("sessions") or 0)
            errors = list(persisted.get("errors") or [])
            clarp_agents = dict(persisted.get("clarp_agents") or _no_clarp_agents())
            stopped_work = list(persisted.get("stopped_work") or [])
            recent_work = list(persisted.get("recent_work") or [])
        else:
            accounts, observed_at = _fallback_accounts_from_status(status)
            stale = True
            sessions = 0
            errors = []
            clarp_agents = _no_clarp_agents()
            stopped_work = []
            recent_work = []

        switcher_block = dict(status.get("switcher") or {})
        switcher_block["actions"] = list(status.get("actions") or [])

        return {
            "schema": SCHEMA,
            "served_at": now,
            "observed_at": observed_at,
            "stale": stale,
            "refreshing": refreshing,
            "refresh_throttled": throttled,
            "next_refresh_at": self._next_refresh_at(persisted),
            "installed": installed,
            "error": last_error,
            "errors": errors,
            "sessions": sessions,
            "switcher": switcher_block,
            "switches": switches,
            "clarp_agents": clarp_agents,
            "accounts": accounts,
            "stopped_work": stopped_work,
            "recent_work": recent_work,
        }

    # --- switching --------------------------------------------------------

    def _note_switch(self, provider: str, **fields) -> None:
        """Update the record of the switch that holds `provider`'s lock."""
        with self._lock:
            self._switches[provider] = {**self._switches.get(provider, {}), **fields}

    def _finish_switch(self, provider: str, state: str) -> None:
        """The switch that holds the lock ended: its record becomes the stored
        one, so the desk still shows it after a restart of the Host."""
        with self._lock:
            record = {**self._switches.pop(provider, {}), "state": state, "finished_at": now_ms()}
        try:
            settings_store.set_text(SWITCH_KEY_PREFIX + provider, json.dumps(record, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001
            log("hotseatDeskSwitchRecordFailed", f"provider={provider} error={type(exc).__name__}")

    def switch_default(
        self,
        provider: str,
        alias: str,
        *,
        command: str | None = None,
        switch_fn: Callable[[str, str, str], bool] = janitor_hotseat.switch_account,
        read_active: Callable[[str, str], str | None] | None = None,
        credentials_changed: Callable[[str], None] = _credentials_changed,
        runner=subprocess.run,
        budget_s: float = SWITCH_ANSWER_BUDGET_S,
        refresh_after: bool = True,
    ) -> tuple[int, dict]:
        """Make `alias` the machine-wide default of `provider` through Hotseat.

        The work runs on its own thread and the request waits `budget_s` for it.
        Inside the budget the answer is the outcome; past it the answer is 202
        `pending` and the switch goes on: `switches` in the desk says how it
        ended, and `accounts[].active` names the account once Hotseat confirmed.
        The live read that follows a switch never holds the answer back."""
        prov = str(provider or "").strip().lower()
        target = str(alias or "").strip()
        if prov not in janitor_hotseat.PROVIDERS:
            return 400, {"ok": False, "error": "invalid_provider"}
        if not _SAFE_ALIAS.fullmatch(target):
            return 400, {"ok": False, "error": "invalid_alias"}

        cmd = (command or hotseat_command()).strip()
        if read_active is None:
            def read_active(command: str, provider: str) -> str | None:
                return read_active_alias(command, provider, runner=runner)
        outcome: dict = {}
        answered = threading.Event()

        def work() -> None:
            try:
                try:
                    outcome["answer"] = self._switch(prov, target, cmd, switch_fn, read_active, credentials_changed)
                except Exception as exc:  # noqa: BLE001
                    log("hotseatDeskSwitchError", f"provider={prov} error={type(exc).__name__}")
                    outcome["answer"] = (500, {"ok": False, "error": "switch_error", "provider": prov, "alias": target})
                finally:
                    answered.set()
                if refresh_after and outcome["answer"][1].get("switched"):
                    self._read_after_switch(prov, target, cmd, runner)
            except Exception as exc:  # noqa: BLE001
                log("hotseatDeskAfterSwitchFailed", f"provider={prov} error={type(exc).__name__}")
            finally:
                db.close_local()  # a short-lived thread gives its connection back

        threading.Thread(target=work, name=f"hotseat-desk-switch-{prov}", daemon=True).start()
        if answered.wait(timeout=budget_s):
            status, body = outcome["answer"]
        else:
            status, body = 202, {
                "ok": True, "switched": False, "pending": True, "reason": "switch_in_progress",
                "provider": prov, "alias": target,
                "message": "The switch is still running. The desk shows how it ended.",
            }
        return status, {**body, "desk": self.payload(command=cmd)}

    def _read_after_switch(self, prov: str, target: str, cmd: str, runner) -> None:
        """The live read that follows a switch. If it fails, a read that began
        before the switch may have been the last to write the stored desk; the
        account Hotseat confirmed is then marked again, unless a later switch
        of this provider has ended since."""
        self.refresh(command=cmd, runner=runner, force=True)
        if str(settings_store.get(ERROR_KEY) or "").strip():
            latest = _stored_switch(prov) or {}
            if latest.get("state") == "switched" and latest.get("alias") == target:
                _mark_active(prov, target)

    def _switch(self, prov: str, target: str, cmd: str, switch_fn, read_active, credentials_changed) -> tuple[int, dict]:
        base = {"provider": prov, "alias": target}
        requested_at = now_ms()
        with self.switch_lock(prov):
            # Only now, with every other switch of this provider finished, is
            # "who is active" a fact this request can act on. The record below
            # is the one of the switch that holds the lock, never of one waiting.
            with self._lock:
                self._switches[prov] = {**base, "state": "running", "from": None,
                                        "requested_at": requested_at, "finished_at": None}
            state = "failed"
            try:
                try:
                    active = read_active(cmd, prov)
                except Exception as exc:  # noqa: BLE001
                    log("hotseatDeskActiveUnknown", f"provider={prov} error={type(exc).__name__}")
                    state = "unavailable"
                    return 503, {
                        "ok": False, "error": "hotseat_unavailable", **base,
                        "message": "Hotseat could not say which account is in use. Nothing was switched.",
                    }
                self._note_switch(prov, **{"from": active})
                if active == target:
                    state = "already_active"
                    return 200, {
                        "ok": True, "switched": False, "reason": "already_active", "from": active, **base,
                        "message": f"{hotseat_status.display_alias(target)} is already the account in use.",
                    }
                try:
                    switched = bool(switch_fn(cmd, prov, target))
                except Exception as exc:  # noqa: BLE001
                    log("hotseatDeskSwitchError", f"provider={prov} error={type(exc).__name__}")
                    switched = False
                stamp = now_ms()
                if switched:
                    settings_store.set_text(
                        f"hotseat-switcher.{prov}.last-switch",
                        json.dumps({"from": active, "to": target, "at": stamp}),
                    )
                    _mark_active(prov, target)
                    state = "switched"
            finally:
                self._finish_switch(prov, state)
        if not switched:
            return 409, {
                "ok": False, "error": "switch_failed", "from": active, **base,
                "message": "Hotseat did not confirm the switch.",
            }
        log("hotseatDeskSwitched", f"provider={prov} to={hotseat_status.display_alias(target)}")
        try:
            credentials_changed(prov)
        except Exception as exc:  # noqa: BLE001
            log("hotseatDeskCredentialHookFailed", f"provider={prov} error={type(exc).__name__}")
        return 200, {"ok": True, "switched": True, "from": active, **base}

    # --- continuing stopped work -------------------------------------------

    def resume_work(
        self,
        item_id: str,
        *,
        kind: str = "clarp",
        backend: str = "",
        action: str = "continue",
        send_prompt: Callable[[str, str, str], dict] | None = None,
        unpause_fn: Callable[[str, str], dict] | None = None,
    ) -> tuple[int, dict]:
        """Continue a stopped Clarp agent, unpause its queue, or return a native resume command.

        Continuing is conditional: the agent must be in the desk's stopped-work
        list with a readiness that says it can continue, must not have finished
        a turn since that stop and must not be working now. It is idempotent
        per stop: `send_prompt(session, text, client_msg_id)` gets the id of
        that stop every time, so the dispatcher starts at most one turn."""
        target_id = str(item_id or "").strip()
        item_kind = str(kind or "clarp").strip().lower()
        mode = str(action or "continue").strip().lower()

        if not _SAFE_SESSION_ID.fullmatch(target_id):
            return 400, {"ok": False, "error": "invalid_id"}
        if item_kind not in ("clarp", "native"):
            return 400, {"ok": False, "error": "invalid_kind"}
        if mode not in ("continue", "unpause"):
            return 400, {"ok": False, "error": "invalid_action"}

        if item_kind == "native":
            prov = str(backend or "").strip().lower()
            cmd = f"codex resume {target_id}" if prov == "codex" else f"claude --resume {target_id}"
            return 200, {
                "ok": True,
                "id": target_id,
                "kind": "native",
                "action": "command",
                "continued": False,
                "resume_command": cmd,
            }

        agent = identity.lookup(target_id)
        if not agent or agent.get("deleted_at"):
            return 404, {"ok": False, "error": "agent_not_found"}
        session = str(agent["session"])
        agent_id = str(agent["agent_id"])
        name = str(agent.get("persona") or session)
        if agent.get("archived_at"):
            return _refusal("agent_archived", f"{name} is archived. Restore it before continuing its work.")
        if agent.get("is_janitor") or janitor_store.has_config(agent_id):
            return _refusal("janitor_inspection_only",
                            f"{name} is a Janitor. Its runs are started by its own triggers, not from here.")

        if mode == "unpause":
            if unpause_fn is None:
                turn_queue.set_paused(agent_id, False)
                updated = turn_queue.state(agent_id)
                result = {"paused": updated["paused"], "count": updated["count"]}
            else:
                result = unpause_fn(agent_id, session)
            return 200, {
                "ok": True,
                "id": session,
                "kind": "clarp",
                "action": "unpause",
                "continued": True,
                "queue_paused": bool(result.get("paused", False)),
                "queued_count": int(result.get("count", 0)),
            }

        if turn_queue.state(agent_id).get("paused"):
            return _refusal("queue_paused", f"{name}'s queue is paused. Unpause the queue so waiting work can run.")

        row = next((item for item in (_load_persisted() or {}).get("stopped_work") or []
                    if isinstance(item, dict) and item.get("kind") == "clarp"
                    and item.get("id") in (session, agent_id)), None)
        stopped_at = _int_ms(row.get("stopped_at")) if row else None
        if stopped_at is None:
            return _refusal("not_stopped", f"{name} is not in the list of stopped work. Refresh the desk and look again.")
        if send_prompt is None:
            return 503, {"ok": False, "error": "dispatch_unavailable"}

        message_id = resume_message_id(agent_id, stopped_at)
        with self._resume_lock:
            # A request about a stop that already got its prompt goes to the
            # dispatcher again under the same id: it starts nothing, unless
            # that first prompt was admitted and never launched.
            already_sent = (message_store.has_client_message(message_id)
                            or bool(turn_queue.status(message_id)))
            if not already_sent:
                readiness = row.get("readiness") if isinstance(row.get("readiness"), dict) else {}
                if not readiness.get("ready"):
                    reason = str(readiness.get("hard") or readiness.get("warning") or "").strip().rstrip(".")
                    return _refusal("not_ready", f"{name} cannot continue yet: {reason}." if reason
                                    else f"{name} cannot continue yet.")
                if agents_db.completed_turn_since(agent_id, stopped_at):
                    return _refusal("not_stopped", f"{name} has finished a turn since it stopped. Refresh the desk.")
                if agents_db.is_busy(agent_id):
                    return _refusal("agent_busy", f"{name} is working now.")
            delivery = send_prompt(session, RESUME_PROMPT, message_id)
        if not delivery.get("ok"):
            return 502, {"ok": False, "error": "prompt_failed", "detail": str(delivery.get("error") or "")[:200],
                         "message": f"The prompt to {name} was not accepted. Nothing was started."}

        return 200, {
            "ok": True,
            "id": session,
            "kind": "clarp",
            "action": "continue",
            "continued": True,
            "already_sent": already_sent,
            "stopped_at": stopped_at,
        }
