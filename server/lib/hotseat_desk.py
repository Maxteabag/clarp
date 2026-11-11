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
import re
import shutil
import subprocess
import threading
from typing import Callable

from . import agents as agents_db
from . import db
from .hotseat_desk_reading import _epoch_ms_from_seconds, _int_ms, _num, _ratio, no_extras
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
# After a live read that FAILED the next one is allowed this soon: a client can
# try a failed read again without waiting out the whole interval, and one in a
# loop still cannot hammer a Hotseat that is down.
FAILED_REFRESH_RETRY_MS = 10 * 1000
# The app waits 45 s for a switch. Reading who is active plus the switch itself
# can take longer than that (the CLI alone may use SWITCH_TIMEOUT), so the
# request answers "still running" after this long and the desk tells the rest.
SWITCH_ANSWER_BUDGET_S = 30
ACTIVE_READ_TIMEOUT_S = 45

_RESUME_OPENINGS = {
    "usage_limit": "Your work was interrupted by a usage limit.",
    "waiting_for_account": "Your work was parked, waiting for an account with quota.",
}


def resume_prompt(row: dict, *, observed_at: int | None, now: int) -> str:
    """What a continued agent is told. Only what the Host knows: why Hotseat
    listed the work as stopped, that Hotseat's last reading (of the stated age)
    said it could continue, with Hotseat's own note when it had one, and that
    nothing was checked again when this was sent. It does not say that quota
    is back: the Host did not ask."""
    opening = _RESUME_OPENINGS.get(row.get("cause"), "Your work stopped and has not continued since.")
    minutes = None if observed_at is None else max(0, int(now) - int(observed_at)) // 60_000
    age = ("of unknown age" if minutes is None else "less than a minute old" if minutes == 0
           else "1 minute old" if minutes == 1 else f"{minutes} minutes old")
    readiness = row.get("readiness") if isinstance(row.get("readiness"), dict) else {}
    note = " ".join(str(readiness.get("warning") or "").split())[:200].rstrip(".")
    reading = f"Hotseat's last reading, {age}, said this work could continue"
    if note:
        reading += f", with this note: {note}"
    return (
        f"{opening} You are asked from the Hotseat desk to continue it. {reading}. "
        "Nothing was checked again just now, so if the limit still holds, stop and say so rather than retrying. "
        "Re-check the results of the interrupted step before retrying it, and do not repeat work that already completed."
    )

_SAFE_ALIAS = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@-]{0,127}\Z")
_SAFE_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


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


ACCOUNT_WINDOW_KINDS = frozenset({"5h", "7d", "30d"})


def _reading(provider: str):
    """How the desk reads `provider` out of Hotseat's answers (a
    hotseat_desk_reading.DeskReading), or None for a provider Hotseat's table
    does not have (Muse): that one gets the generic treatment. The desk looks
    the provider up in the one table that lists Hotseat's providers and calls
    what it finds; it never asks which provider it is holding."""
    spec = janitor_hotseat.PROVIDERS.get(provider)
    return spec.desk if spec is not None else None


def _window_meta(provider: str, label: str) -> tuple[str, str]:
    """(kind, short label) of a quota window from Hotseat's label.

    An account window ("All models · 5-hour", "codex primary 30-day") gets one
    of ACCOUNT_WINDOW_KINDS. A window of one model ("Fable · weekly", a Codex
    limit not named after the provider) never does: it limits that model, not
    the account, and the kind is what keeps the two facts apart downstream."""
    raw = str(label or "").strip()
    reading = _reading(provider)
    meta = reading.window_meta(provider, raw) if reading is not None else None
    return meta or (f"window:{raw.casefold()[:24]}", raw[:24] or "Window")


def _account_limit_reached(windows: list[dict]) -> bool:
    """Whether a window of the account itself is used up. Model windows do not count."""
    return any(w.get("kind") in ACCOUNT_WINDOW_KINDS and (_num(w.get("used_ratio")) or 0) >= 1.0
               for w in windows if isinstance(w, dict))


def _classify_account_state(*, error: str, error_code: str | None, limited: bool, windows: list[dict],
                            extra_usage: bool = False) -> tuple[str, str | None]:
    """The state of the ACCOUNT. `exhausted` means the account cannot take a
    turn: Hotseat says it is limited, or one of its own windows (5-hour, weekly,
    30-day) is at its limit and no extra usage covers it. A model at its limit
    is not that: the account still serves the other models, and the model's
    window in `windows` (and `blocked_models`) says which one and until when.
    The same rule `/hotseat/status` follows."""
    lower_err = error.casefold()
    if error_code == "auth_error" or ("token" in lower_err and ("expired" in lower_err or "revoked" in lower_err or "sign-in" in lower_err)):
        return "auth_error", "auth_error"
    if error:
        return "error", error_code or "provider_error"
    if limited or (not extra_usage and _account_limit_reached(windows)):
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


def _resume_command(backend: str, item_id: str) -> str | None:
    """The command that resumes a native session, as that session's backend
    gives it. None when the backend is unknown or has none: a Claude command
    is not offered for a session of something else."""
    from . import backends
    known = backends.get(backend)
    return known.native_resume_command(item_id) if known is not None else None


def _carry_last_good(accounts: list[dict], previous) -> None:
    """`last_good_checked_at` of an account whose check failed this time is the
    one the stored desk had for it: when it was last known good."""
    known = {(acc.get("provider"), acc.get("alias")): acc.get("last_good_checked_at")
             for acc in previous or [] if isinstance(acc, dict)}
    for acc in accounts:
        if acc.get("last_good_checked_at") is None:
            acc["last_good_checked_at"] = _int_ms(known.get((acc.get("provider"), acc.get("alias"))))


def _rows_by_alias(rows) -> dict[str, dict]:
    return {row["alias"]: row for row in rows or []
            if isinstance(row, dict) and isinstance(row.get("alias"), str)}


def _clarp_block(list_data: dict | None) -> tuple[dict, list[dict]]:
    """(agents summary, stopped items) from the Clarp extension of `hotseat list --json`."""
    summary = _no_clarp_agents()
    stopped: list[dict] = []
    extensions = list_data.get("extensions") if isinstance(list_data, dict) else None
    ext_clarp = (extensions.get("clarp") or {}) if isinstance(extensions, dict) else {}
    if not isinstance(ext_clarp, dict):
        return summary, stopped
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
        summary = {
            "total": int(agents_block.get("total") or 0),
            "live": int(agents_block.get("live") or 0),
            "claude_backed": int(agents_block.get("claude_backed") or 0),
            "by_backend": {str(k): int(v) for k, v in by_backend.items() if isinstance(v, int)},
            "live_by_account": {str(k): int(v) for k, v in live_map.items() if isinstance(v, int)},
            "would_use_by_account": would_counts,
        }
    if isinstance(ext_clarp.get("stopped"), list):
        stopped = [item for item in ext_clarp["stopped"] if isinstance(item, dict)]
    return summary, stopped


def _account_row(raw: dict, list_row: dict, clarp_agents: dict) -> dict | None:
    """One account of the desk: `raw` as `hotseat tui --snapshot` reports it,
    `list_row` the row `hotseat list --json` has for the same provider and alias."""
    provider = str(raw.get("provider") or "").strip().lower()
    alias = str(raw.get("alias") or "").strip()
    if not provider or not alias:
        return None
    # When Hotseat checked this account, or None when it gave no time (a failed
    # check, an account it never verified). Never the time of the read: that
    # would date a number nobody dated.
    checked_ms = _epoch_ms_from_seconds(raw.get("checked_at"))
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

    reading = _reading(provider)
    extras = {**no_extras(), **(reading.extras(list_row) if reading is not None and isinstance(list_row, dict) else {})}
    limited = bool(raw.get("limited")) or bool(extras["blocked"])
    state, code = _classify_account_state(
        error=error,
        error_code=extras["error_code"],
        limited=limited,
        windows=windows,
        extra_usage=bool(extras["extra_usage"]),
    )

    reset_credits = raw.get("reset_credits")
    if isinstance(reset_credits, bool) or not isinstance(reset_credits, int):
        reset_credits = None

    return {
        "provider": provider,
        "alias": alias,
        "display_alias": str(raw.get("display_alias") or alias),
        "aliases": [str(a) for a in (raw.get("aliases") or [alias]) if isinstance(a, str)],
        "email": str(raw.get("email") or extras["email"] or ""),
        "workspace": str(raw.get("workspace") or extras["workspace"] or ""),
        "plan": str(raw.get("plan") or extras["plan"] or ""),
        "plan_code": str(extras["plan_code"] or "").lower() or None,
        "rate_limit_tier": str(raw.get("rate_limit_tier") or extras["rate_limit_tier"] or ""),
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
        "last_good_checked_at": None if error else checked_ms,
        "reset_credits": reset_credits,
        "reset_credits_error": str(raw.get("reset_credits_error") or ""),
        "access_expires_at": extras["access_expires_at"],
        "access_hours_left": extras["access_hours_left"],
        "refresh_expires_at": extras["refresh_expires_at"],
        "signin_days_left": extras["signin_days_left"],
        "signin_due_soon": extras["signin_due_soon"],
        "token_expires_at": extras["token_expires_at"],
        "token_hours_left": extras["token_hours_left"],
        "api_key_present": extras["api_key_present"],
        "blocked_models": extras["blocked_models"],
        "extra_usage_enabled": extras["extra_usage_enabled"],
        "extra_usage_reason": extras["extra_usage_reason"],
        "live_sessions": int(clarp_agents["live_by_account"].get(alias, 0)),
        "would_use_sessions": int(clarp_agents["would_use_by_account"].get(alias, 0)),
        "windows": windows,
    }


def _stopped_rows(stopped_raw: list[dict]) -> list[dict]:
    stopped_work: list[dict] = []
    for item in stopped_raw:
        # What Hotseat did not say stays unknown: a row of unknown kind is not
        # continuable, and an unknown cause is not shown as a usage limit.
        kind = item.get("kind") if item.get("kind") in ("clarp", "native") else None
        cause = item.get("cause").strip() if isinstance(item.get("cause"), str) else ""
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
        resume_cmd = _resume_command(backend, item_id) if kind == "native" else None
        stopped_work.append({
            "kind": kind,
            "cause": cause or None,
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
    return stopped_work


def _build_desk_snapshot(
    tui_data: dict,
    list_data: dict | None,
    work_data: dict | None,
    *,
    observed_at: int,
) -> dict:
    list_rows: dict[str, dict[str, dict]] = {}
    if isinstance(list_data, dict):
        list_rows = {provider: _rows_by_alias(spec.desk.list_rows(list_data))
                     for provider, spec in janitor_hotseat.PROVIDERS.items() if spec.desk is not None}
    clarp_agents_summary, stopped_raw = _clarp_block(list_data)

    accounts: list[dict] = []
    for raw in tui_data.get("accounts") or []:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider") or "").strip().lower()
        alias = str(raw.get("alias") or "").strip()
        row = _account_row(raw, list_rows.get(provider, {}).get(alias, {}), clarp_agents_summary)
        if row is not None:
            accounts.append(row)

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
            resume_cmd = _resume_command(provider, item_id) if can_resume and not item.get("live") else None
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
        "stopped_work": _stopped_rows(stopped_raw),
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
                "last_good_checked_at": _int_ms(acc.get("last_good_observed_at")),
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
            # A window also ages on its own: the switcher's scheduled read
            # refreshes one provider's rows, the others keep their own time.
            # One nobody can date is not served as current either.
            stale = (bool(w.get("stale")) or snapshot_stale
                     or obs is None or served_at - obs > STALE_MS)
            if not no_reset and resets_at is not None and resets_at <= served_at and (obs is None or obs < resets_at):
                used = None
                stale = True
                reset_passed = True
            w["used_ratio"] = used
            w["remaining_ratio"] = None if used is None else round(1.0 - used, 4)
            w["resets_at"] = resets_at
            w["stale"] = stale
            out_windows.append(w)
        if item.get("state") == "exhausted" and reset_passed and not _account_limit_reached(out_windows):
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
        sources = {"tui": "ok", "list": "ok", "work": "ok"}
        try:
            list_data = fut_list.result()
        except Exception as exc:  # noqa: BLE001
            log("hotseatDeskListPartial", f"error={type(exc).__name__}")
            list_data = None
            sources["list"] = "failed"
        try:
            work_data = fut_work.result()
        except Exception as exc:  # noqa: BLE001
            log("hotseatDeskWorkPartial", f"error={type(exc).__name__}")
            work_data = None
            sources["work"] = "failed"

    if isinstance(list_data, dict):
        hotseat_status.record_read("claude", list_data, observed_at=observed_at)
        if isinstance(list_data.get("codex"), dict):
            hotseat_status.record_read("codex", list_data["codex"], observed_at=observed_at)
    snapshot = _build_desk_snapshot(tui_data, list_data, work_data, observed_at=observed_at)
    # Which of the three reads this desk is made of answered. Without `list`
    # the accounts lack sign-in clocks, blocked models and typed errors and
    # there is no stopped work; without `work` there is no recent work.
    snapshot["sources"] = sources
    return snapshot


def read_active_alias(command: str, provider: str, *, runner=subprocess.run) -> str | None:
    """The alias Hotseat reports as the machine-wide default of `provider`, or
    None when it reports none. Raises when Hotseat cannot be read."""
    spec = janitor_hotseat.PROVIDERS[provider]
    # The switcher's own read of this provider, with the flags that leave out
    # what "who is in use" does not need (Codex's quota probe, the slow part).
    args = [spec.read_args[0], *spec.active_read_flags, *spec.read_args[1:]]
    payload = _run_json(command, args, ACTIVE_READ_TIMEOUT_S, runner=runner)
    for account in spec.parse(payload):
        if account.get("active"):
            return str(account["alias"])
    return None


def _credentials_changed(provider: str) -> None:
    """A runner that read credentials once must restart to see the new account;
    the same call the automatic switcher makes after its switch."""
    from . import backends
    backends.by_id(provider).on_credential_change()


def _raw_from_switcher_row(provider: str, stored: dict, row: dict, observed_at: int) -> dict | None:
    """One account as `hotseat tui --snapshot` would report it now, put together
    from the row the switcher's own read has for it (`list --json` for Claude,
    `codex --json` for Codex) the way Hotseat's bridge builds that row from the
    same numbers. What only the TUI knows (grouping, reset credits, whether the
    account can be switched to) is kept from the stored row. None when the row
    says nothing about the account's quota."""
    reading = _reading(provider)
    quota = reading.switcher_row(row) if reading is not None else None
    if quota is None:
        return None
    raw = {key: stored.get(key) for key in (
        "provider", "alias", "display_alias", "aliases", "email", "plan", "rate_limit_tier", "workspace",
        "saved", "can_switch", "can_launch", "warning", "profile_notes", "reset_credits", "reset_credits_error")}
    error = quota["error"]
    raw.update(active=row.get("is_active") is True, cached=False, error=error, limited=quota["limited"],
               no_reset_reason=quota["no_reset_reason"], windows=[] if error else quota["windows"],
               checked_at=0 if error else observed_at / 1000)
    return raw


def _set_active(snapshot: dict, provider: str, alias: str) -> bool:
    """Name `alias` as the account in use among `provider`'s accounts of `snapshot`.
    False, and nothing changed, when the snapshot does not list that alias."""
    accounts = [acc for acc in snapshot.get("accounts") or []
                if isinstance(acc, dict) and acc.get("provider") == provider]
    if not any(alias == acc.get("alias") or alias in (acc.get("aliases") or []) for acc in accounts):
        return False
    for acc in accounts:
        acc["active"] = alias == acc.get("alias") or alias in (acc.get("aliases") or [])
    return True


def _mark_active(provider: str, alias: str) -> None:
    """Hotseat confirmed the switch: the stored desk names the new account at
    once, before the live read that follows has finished (or if it fails)."""
    snapshot = _load_persisted()
    if snapshot is not None and _set_active(snapshot, provider, alias):
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


def _failed_start(name: str, delivery: dict, *, already_sent: bool, saved: bool) -> tuple[int, dict]:
    """The answer when the prompt did not start a turn, saying what state that
    leaves: whether the prompt is in the agent's chat (`saved`) and what the
    owner can do. The dispatcher keeps proof of a launch that never happened
    only for a message the user typed, so a prompt it saved and could not
    start is never started again from here: one stop gets one prompt."""
    detail = str(delivery.get("error") or "")[:200]
    if delivery.get("unconfirmed"):
        return 409, {"ok": False, "error": "delivery_unconfirmed", "saved": True, "message": (
            f"The prompt for this stop is saved in {name}'s chat, but {name} did not start and the Host "
            f"cannot send it again. Open {name}'s chat and send a message to continue.")}
    if not saved:
        message = f"{name} was not started and nothing was saved. Try again."
    elif already_sent:
        message = (f"An earlier Continue for this stop is saved in {name}'s chat, and this request did not "
                   f"get through. Look at {name}'s chat before trying again.")
    else:
        message = (f"{name} did not start. The prompt is saved in {name}'s chat but was not delivered. "
                   f"Open {name}'s chat and send a message to continue.")
    return 502, {"ok": False, "error": "prompt_failed", "detail": detail, "saved": saved, "message": message}


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
        # provider -> (when, alias) of the last switch this process saw end,
        # the desk's or the automatic switcher's. A read that began before
        # that moment knows the account in use less well than the switch does.
        self._switched: dict[str, tuple[int, str]] = {}
        self._last_read_ok = False
        self._resume_lock = threading.Lock()
        # Orders the read-change-write of the stored snapshot between a live
        # read, a switch marking the new account and the switcher's read.
        self._store_lock = threading.Lock()

    def switch_lock(self, provider: str) -> threading.Lock:
        """Held around every `hotseat switch` of `provider` and the write of its
        flip-back record, whoever asks: this desk or the automatic switcher."""
        return self._switch_locks[provider]

    # --- reading ----------------------------------------------------------

    def _next_allowed(self, observed: int) -> int:
        """When the next live read may start. Call with self._lock held.
        A read that worked moved the stored desk's time, and the full interval
        counts from there, also across a restart. Any read, a failed one
        included, holds the next one off for the short retry wait only."""
        return max(self._last_read_at + FAILED_REFRESH_RETRY_MS if self._last_read_at else 0,
                   observed + MIN_REFRESH_INTERVAL_MS if observed else 0)

    def _next_refresh_at(self, persisted: dict | None) -> int:
        observed = _int_ms((persisted or {}).get("observed_at")) or 0
        with self._lock:
            return self._next_allowed(observed)

    def _keep_switched_accounts(self, snapshot: dict, read_began: int, providers) -> None:
        """A read that began before a switch ended reports the account that was
        in use before it. The switch knows better: keep the account it made the
        default. A read that began after the switch is taken as it is."""
        with self._lock:
            switched = dict(self._switched)
        for provider in providers:
            at, alias = switched.get(provider, (0, ""))
            if alias and at >= read_began:
                _set_active(snapshot, provider, alias)

    def _saw_switch(self, provider: str, alias: str) -> None:
        with self._lock:
            self._switched[provider] = (now_ms(), alias)

    def refresh(self, *, command: str | None = None, runner=subprocess.run, force: bool = False) -> str:
        """One live read of Hotseat, single-flight. Says how it went for the caller:
        "ok" when a read ran (or one in flight was waited for) and succeeded,
        "failed" when it did not succeed, and "throttled" when no read was
        allowed yet (inside MIN_REFRESH_INTERVAL_MS of the last one) and nothing
        ran. `force` is for the desk's own read after a switch: it never joins a
        read that started before the switch and is not held back by the interval."""
        cmd = (command or hotseat_command()).strip()
        while True:
            observed = _int_ms((_load_persisted() or {}).get("observed_at")) or 0
            with self._lock:
                waiting_for = self._inflight
                if waiting_for is None:
                    now = now_ms()
                    if not force and now < self._next_allowed(observed):
                        return "throttled"
                    done = self._inflight = threading.Event()
                    self._last_read_at = now
                    break
            waiting_for.wait(timeout=TUI_TIMEOUT_S + 5)
            if not force:
                with self._lock:
                    return "ok" if self._inflight is None and self._last_read_ok else "failed"
        succeeded = False
        try:
            snapshot = _read_live(cmd, runner, now)
            with self._store_lock:
                _carry_last_good(snapshot["accounts"], (_load_persisted() or {}).get("accounts"))
                self._keep_switched_accounts(snapshot, now, janitor_hotseat.PROVIDERS)
                settings_store.set_text(SNAPSHOT_KEY, json.dumps(snapshot, separators=(",", ":")))
                settings_store.set_text(ERROR_KEY, "")
            succeeded = True
        except Exception as exc:  # noqa: BLE001
            code = hotseat_status.error_code_for(exc)
            log("hotseatDeskRefreshFailed", f"code={code} error={type(exc).__name__}")
            settings_store.set_text(ERROR_KEY, code)
        finally:
            with self._lock:
                self._inflight = None
                self._last_read_ok = succeeded
            done.set()
        return "ok" if succeeded else "failed"

    def absorb_switcher_read(self, provider: str, payload, *, started_at: int | None = None) -> bool:
        """The Hotseat switcher just read `provider` on its own schedule
        (`hotseat list --json` for Claude, `hotseat codex --json` for Codex).
        The stored desk takes from that read what it says: quota windows,
        state, the account in use, sign-in and token clocks of the accounts it
        already lists, and from the Claude read the stopped work and the
        agents' accounts. So a desk nobody refreshes stays as fresh as the
        switcher's cadence, at no extra read of the providers.

        It never adds or removes an account and creates no desk where no live
        read has stored one: that takes `tui --snapshot`. Only the Claude read,
        which carries the whole list, moves the desk's `observed_at`; rows the
        read did not cover keep their own `checked_at` and age by it.
        `started_at` is when the read began: a switch that ended since then
        knows the account in use better than this read does.

        True when the stored desk was updated. Never raises: the switcher's
        read must not fail because of the desk."""
        try:
            return self._absorb(provider, payload, started_at)
        except Exception as exc:  # noqa: BLE001
            log("hotseatDeskAbsorbFailed", f"provider={provider} error={type(exc).__name__}")
            return False

    def _absorb(self, provider: str, payload, started_at: int | None) -> bool:
        from . import server_identity
        reading = _reading(provider)
        if reading is None or not isinstance(payload, dict):
            return False
        if not server_identity.hotseat_desk_enabled():
            return False
        rows = _rows_by_alias(payload.get("accounts") if isinstance(payload.get("accounts"), list) else [])
        now = now_ms()
        with self._store_lock:
            snapshot = _load_persisted()
            if snapshot is None:
                return False
            extensions = payload.get("extensions")
            has_clarp = (reading.whole_list and isinstance(extensions, dict)
                         and isinstance(extensions.get("clarp"), dict))
            if has_clarp:
                snapshot["clarp_agents"], stopped_raw = _clarp_block(payload)
                snapshot["stopped_work"] = _stopped_rows(stopped_raw)
            clarp_agents = {**_no_clarp_agents(), **(snapshot.get("clarp_agents") or {})}
            accounts = []
            for stored in snapshot.get("accounts") or []:
                if not isinstance(stored, dict):
                    continue
                alias = stored.get("alias")
                row = rows.get(alias) if stored.get("provider") == provider else None
                raw = _raw_from_switcher_row(provider, stored, row, now) if row is not None else None
                if raw is None:
                    if has_clarp:
                        stored["live_sessions"] = int(clarp_agents["live_by_account"].get(alias, 0))
                        stored["would_use_sessions"] = int(clarp_agents["would_use_by_account"].get(alias, 0))
                    accounts.append(stored)
                    continue
                fresh = _account_row(raw, row, clarp_agents)
                if fresh is not None and fresh["last_good_checked_at"] is None:
                    fresh["last_good_checked_at"] = _int_ms(stored.get("last_good_checked_at"))
                accounts.append(fresh or stored)
            snapshot["accounts"] = accounts
            self._keep_switched_accounts(snapshot, now if started_at is None else int(started_at), (provider,))
            if reading.whole_list:
                snapshot["observed_at"] = now
                if isinstance(snapshot.get("sources"), dict):
                    snapshot["sources"]["list"] = "ok"  # this read is the one `list` stands for
                settings_store.set_text(ERROR_KEY, "")
            settings_store.set_text(SNAPSHOT_KEY, json.dumps(snapshot, separators=(",", ":")))
        return True

    def mark_switched(self, provider: str, alias: str) -> None:
        """The automatic switcher made `alias` the default: the stored desk
        names it at once, not at the switcher's next read. Never raises."""
        try:
            self._saw_switch(provider, alias)
            with self._store_lock:
                _mark_active(provider, alias)
        except Exception as exc:  # noqa: BLE001
            log("hotseatDeskMarkFailed", f"provider={provider} error={type(exc).__name__}")

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
        outcome = self.refresh(command=cmd, runner=runner) if refresh else None

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

        sources = persisted.get("sources") if persisted is not None and isinstance(persisted.get("sources"), dict) else None
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
            # Whether the live read THIS request asked for ran and succeeded;
            # otherwise the desk below is the stored one, of `observed_at`.
            "refreshed": outcome == "ok",
            "refresh_throttled": outcome == "throttled",
            "next_refresh_at": self._next_refresh_at(persisted),
            # Which of Hotseat's three reads the stored desk is made of answered
            # ("ok" or "failed" for tui, list, work); null when no desk read is
            # stored or it predates this field. `partial`: not all three did.
            "sources": sources,
            "partial": sources is None or any(sources.get(key) != "ok" for key in ("tui", "list", "work")),
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
        # No desk in the answer: none has been read since the switch, and a
        # desk from before it would say the opposite of `switched`. The client
        # reads GET /hotseat/desk, whose `switches` and accounts tell the rest.
        return status, body

    def _read_after_switch(self, prov: str, target: str, cmd: str, runner) -> None:
        """The live read that follows a switch. If it fails, a read that began
        before the switch may have been the last to write the stored desk; the
        account Hotseat confirmed is then marked again, unless a later switch
        of this provider has ended since."""
        self.refresh(command=cmd, runner=runner, force=True)
        if str(settings_store.get(ERROR_KEY) or "").strip():
            latest = _stored_switch(prov) or {}
            if latest.get("state") == "switched" and latest.get("alias") == target:
                with self._store_lock:
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
                    self._saw_switch(prov, target)
                    with self._store_lock:
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
            cmd = _resume_command(str(backend or "").strip().lower(), target_id)
            if cmd is None:
                return _refusal("no_resume_command", "No resume command is known for this session's backend.")
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

        snapshot = _load_persisted() or {}
        row = next((item for item in snapshot.get("stopped_work") or []
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
            text = resume_prompt(row, observed_at=_int_ms(snapshot.get("observed_at")), now=now_ms())
            delivery = send_prompt(session, text, message_id)
            saved = not delivery.get("ok") and message_store.has_client_message(message_id)
        if not delivery.get("ok"):
            return _failed_start(name, delivery, already_sent=already_sent, saved=saved)

        return 200, {
            "ok": True,
            "id": session,
            "kind": "clarp",
            "action": "continue",
            "continued": True,
            "already_sent": already_sent,
            # The prompt waits in the agent's queue behind a turn that was
            # running when it arrived; it runs when that turn ends.
            "queued": bool(delivery.get("queued")),
            "stopped_at": stopped_at,
        }
