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

from . import hotseat_status, identity, janitor_hotseat, janitor_store, settings_store, turn_queue
from .clock import now_ms
from .log import log

SCHEMA = "clarp.hotseat-desk/1"
SNAPSHOT_KEY = "hotseat-desk.snapshot.v1"
ERROR_KEY = "hotseat-desk.error.v1"
STALE_MS = 10 * 60 * 1000
TUI_TIMEOUT_S = 45
LIST_TIMEOUT_S = 45
WORK_TIMEOUT_S = 20

RESUME_PROMPT = (
    "Continue the work that was interrupted by the usage limit. The account has "
    "quota again. Re-check the results of the interrupted step before retrying it, "
    "and do not repeat work that already completed."
)

_SAFE_ALIAS = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@-]{0,127}\Z")
_SAFE_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

_STATE_LOCK = threading.Lock()
_INFLIGHT: threading.Event | None = None


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


def refresh_desk(*, command: str | None = None, runner=subprocess.run) -> dict:
    """Single-flight live read from Hotseat CLI; updates both desk and status stores."""
    global _INFLIGHT
    cmd = (command or hotseat_command()).strip()

    with _STATE_LOCK:
        if _INFLIGHT is not None:
            wait_event = _INFLIGHT
            leader = False
        else:
            wait_event = threading.Event()
            _INFLIGHT = wait_event
            leader = True

    if not leader:
        wait_event.wait(timeout=TUI_TIMEOUT_S + 5)
        return payload(refresh=False, command=cmd)

    try:
        observed_at = now_ms()
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

        snapshot = _build_desk_snapshot(tui_data, list_data, work_data, observed_at=observed_at)
        settings_store.set_text(SNAPSHOT_KEY, json.dumps(snapshot, separators=(",", ":")))
        settings_store.set_text(ERROR_KEY, "")
    except Exception as exc:  # noqa: BLE001
        code = hotseat_status.error_code_for(exc)
        log("hotseatDeskRefreshFailed", f"code={code} error={type(exc).__name__}")
        settings_store.set_text(ERROR_KEY, code)
    finally:
        with _STATE_LOCK:
            _INFLIGHT = None
            wait_event.set()

    return payload(refresh=False, command=cmd)


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


def payload(
    *,
    refresh: bool = False,
    served_at: int | None = None,
    command: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner=subprocess.run,
) -> dict:
    """Serve the Hotseat desk payload. `refresh=False` never executes a subprocess."""
    cmd = (command or hotseat_command()).strip()
    if refresh:
        return refresh_desk(command=cmd, runner=runner)

    now = now_ms() if served_at is None else int(served_at)
    installed = cli_installed(cmd, which=which)
    status = hotseat_status.payload(served_at=now)
    persisted = _load_persisted()
    raw_err = str(settings_store.get(ERROR_KEY) or "").strip()
    last_error = raw_err if raw_err in hotseat_status.ERROR_CODES else None

    with _STATE_LOCK:
        refreshing = _INFLIGHT is not None

    if persisted is not None:
        observed_at = _int_ms(persisted.get("observed_at"))
        stale = observed_at is None or (now - observed_at > STALE_MS) or bool(last_error)
        accounts = _serve_accounts(persisted.get("accounts") or [], served_at=now, snapshot_stale=stale)
        sessions = int(persisted.get("sessions") or 0)
        errors = list(persisted.get("errors") or [])
        clarp_agents = dict(persisted.get("clarp_agents") or {
            "total": 0, "live": 0, "claude_backed": 0,
            "by_backend": {}, "live_by_account": {}, "would_use_by_account": {},
        })
        stopped_work = list(persisted.get("stopped_work") or [])
        recent_work = list(persisted.get("recent_work") or [])
    else:
        accounts, observed_at = _fallback_accounts_from_status(status)
        stale = True
        sessions = 0
        errors = []
        clarp_agents = {
            "total": 0, "live": 0, "claude_backed": 0,
            "by_backend": {}, "live_by_account": {}, "would_use_by_account": {},
        }
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
        "installed": installed,
        "error": last_error,
        "errors": errors,
        "sessions": sessions,
        "switcher": switcher_block,
        "clarp_agents": clarp_agents,
        "accounts": accounts,
        "stopped_work": stopped_work,
        "recent_work": recent_work,
    }


def switch_default(
    provider: str,
    alias: str,
    *,
    command: str | None = None,
    switch_fn: Callable[[str, str, str], bool] = janitor_hotseat.switch_account,
    runner=subprocess.run,
) -> tuple[int, dict]:
    """Switch the machine-wide default Claude or Codex account via Hotseat."""
    prov = str(provider or "").strip().lower()
    target = str(alias or "").strip()
    if prov not in janitor_hotseat.PROVIDERS:
        return 400, {"ok": False, "error": "invalid_provider"}
    if not _SAFE_ALIAS.fullmatch(target):
        return 400, {"ok": False, "error": "invalid_alias"}

    cmd = (command or hotseat_command()).strip()
    persisted = _load_persisted()
    previous_active = None
    if persisted:
        for acc in persisted.get("accounts") or []:
            if isinstance(acc, dict) and acc.get("provider") == prov and acc.get("active"):
                previous_active = str(acc.get("alias") or "") or None
                break

    try:
        switched = bool(switch_fn(cmd, prov, target))
    except Exception as exc:  # noqa: BLE001
        log("hotseatDeskSwitchError", f"provider={prov} error={type(exc).__name__}")
        switched = False

    if not switched:
        return 409, {"ok": False, "error": "switch_failed", "provider": prov, "alias": target}

    stamp = now_ms()
    settings_store.set_text(
        f"hotseat-switcher.{prov}.last-switch",
        json.dumps({"from": previous_active, "to": target, "at": stamp}),
    )
    log("hotseatDeskSwitched", f"provider={prov} to={hotseat_status.display_alias(target)}")
    desk = refresh_desk(command=cmd, runner=runner)
    return 200, {"ok": True, "switched": True, "provider": prov, "alias": target, "desk": desk}


def resume_work(
    item_id: str,
    *,
    kind: str = "clarp",
    backend: str = "",
    action: str = "continue",
    send_prompt: Callable[[str, str], dict] | None = None,
    unpause_fn: Callable[[str, str], dict] | None = None,
) -> tuple[int, dict]:
    """Continue a stopped Clarp agent, unpause its queue, or return a native resume command."""
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
    if not agent or agent.get("deleted_at") or agent.get("archived_at"):
        return 404, {"ok": False, "error": "agent_not_found"}
    if agent.get("is_janitor") or janitor_store.has_config(agent["agent_id"]):
        return 403, {"ok": False, "error": "janitor_inspection_only"}

    session = str(agent["session"])
    agent_id = str(agent["agent_id"])
    queue_state = turn_queue.state(agent_id)

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

    if queue_state.get("paused"):
        name = str(agent.get("persona") or session)
        return 409, {
            "ok": False,
            "error": "queue_paused",
            "message": f"{name}'s queue is paused. Unpause the queue so waiting work can run.",
        }

    if send_prompt is None:
        return 503, {"ok": False, "error": "dispatch_unavailable"}

    delivery = send_prompt(session, RESUME_PROMPT)
    if not delivery.get("ok"):
        return 502, {"ok": False, "error": "prompt_failed", "detail": str(delivery.get("error") or "")[:200]}

    return 200, {
        "ok": True,
        "id": session,
        "kind": "clarp",
        "action": "continue",
        "continued": True,
    }
