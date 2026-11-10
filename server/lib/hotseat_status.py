"""Hotseat status: the persisted, read-only account snapshot behind GET /hotseat/status.

The Hotseat switcher Janitor already reads `hotseat list --json` and
`hotseat codex --json` on its own cadence. Those reads are the only source:
`record_read` keeps an allowlisted copy of each one, `record_failure` and
`record_action` keep structured receipts of the switcher's runs, and
`payload` serves what was stored. Serving never runs the CLI, a provider read
or anything else that could refresh a number, so `served_at` never moves
`observed_at`.

Contract: /var/tmp/hotseat-ui/CONTRACT-v2-root.md, canonical sha256 0b8c0d2d...298ca5042b
(joint C/D draft 504d7a7e folded in), wire schema clarp.hotseat-status/1. Ratios are 0..1 or null; timestamps are
epoch milliseconds.

Privacy: nothing from the CLI is stored or served unless it passes an
allowlist. Aliases that look like emails are redacted; tokens, paths, URLs and
raw error text never leave this module, not even in logs. Each account is
bound to a salted identity key (provider, alias, email, org, account id) that
is stored but never served, so an old reading is never shown under an account
it does not belong to.

Record functions never raise into the Janitor: a failure is logged by site and
exception class only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import dataclass
from typing import Callable

from . import settings_store
from .clock import now_ms
from . import log as log_module
from .log import log

SCHEMA = "clarp.hotseat-status/1"
ROLE = "account-hotseat"
SNAPSHOT_PREFIX = "hotseat-status.snapshot.v1."
ACTION_PREFIX = "hotseat-status.action.v1."
LANE_PREFIX = "hotseat-status.lane.v1."
SALT_KEY = "hotseat-status.salt.v1"

STALE_FLOOR_S = 600
MAX_ACCOUNTS = 32
MAX_WINDOWS = 8
MAX_ALIAS = 32
MAX_LABEL = 40

ERROR_CODES = frozenset({"auth_error", "exhausted", "timeout", "cli_failed", "parse_failed",
                         "provider_error", "disabled"})
ACTION_KINDS = frozenset({"switched", "would_switch", "noop", "exhausted", "skipped", "failed"})
# Contract 0b8c0d2d section C: exactly these six, 1:1 with the
# switcher's branches. disabled/timeout/cli_failed/parse_failed/provider_error are provider
# error_code values only: a failed read or a disabled switcher writes no receipt.
REASON_CODES = frozenset({"no_active_account", "quota_unknown", "already_ok", "below_floor",
                          "switch_failed", "no_better_account"})
NO_RESET_REASONS = {"no credits": "no_credits", "spend cap": "spend_cap"}  # codex.NoResetReason
MODEL_FAMILIES = ("opus", "sonnet", "haiku", "fable", "gpt-5", "spark")
PLANS = {"free": "Free", "pro": "Pro", "max": "Max", "team": "Team", "plus": "Plus",
         "business": "Business", "enterprise": "Enterprise", "edu": "Edu"}
COST_NOTE = ("Numbers come from the Hotseat switcher's own scheduled reads, which may probe or "
             "refresh provider quota; opening this view adds no read.")

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
_TOKENISH = re.compile(r"[A-Za-z0-9+=_]{20,}")
_SAFE_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._\-]*")
_PLAN_LABEL = re.compile(r"(Free|Pro|Max|Team|Plus|Business|Enterprise|Edu)( [0-9]{1,2}×)?")
_UNSAFE_MARKERS = ("://", "/", "\\", "~")


# --- redaction -------------------------------------------------------------

def _salt() -> bytes:
    """A per-install secret, so stored identity keys cannot be matched to an email."""
    value = settings_store.get(SALT_KEY) or settings_store.set_text_if_blank(SALT_KEY, secrets.token_hex(16))
    return str(value).encode()


def _keyed(*parts) -> str:
    """A salted, one-way reference; stored internally, never served."""
    message = "\0".join("" if p is None else str(p) for p in parts).encode()
    return hmac.new(_salt(), message, hashlib.sha256).hexdigest()[:32]


def identity_key(provider: str, alias, *, email=None, org=None, account_id=None) -> str:
    return _keyed("identity", provider, alias, email, org, account_id)


def _alias_tag(provider: str, alias) -> str:
    return _keyed("alias", provider, alias)


def display_alias(alias) -> str:
    """What may be shown for an alias: itself when plainly safe, else a redaction."""
    text = str(alias or "").strip()
    match = _EMAIL.fullmatch(text)
    if match:
        local, _, domain = text.partition("@")
        return f"{local[:1]}…@{domain[:1]}…"
    if (not text or len(text) > MAX_ALIAS or _TOKENISH.search(text) or "@" in text
            or any(m in text for m in _UNSAFE_MARKERS) or not _SAFE_ALIAS.fullmatch(text)):
        return "account"
    return text


def _served_alias(value) -> str | None:
    """Re-check a stored alias at the door: our own redaction, optionally numbered."""
    if value is None:
        return None
    text = str(value)
    numbered = re.fullmatch(r"(.+) \(([0-9]{1,2})\)", text)
    safe = display_alias(numbered.group(1) if numbered else text)
    return f"{safe} ({numbered.group(2)})" if numbered else safe


def _plan(value) -> str | None:
    return PLANS.get(str(value).strip().lower()) if isinstance(value, str) else None


def _plan_label(value) -> str | None:
    if isinstance(value, str) and _PLAN_LABEL.fullmatch(value.strip()):
        return value.strip()[:MAX_LABEL]
    return None


def _ratio(value) -> float | None:
    """A used fraction 0..1, or None. A missing or malformed number is never 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0 or value > 1.5:
        return None
    return round(min(value, 1.0), 4)


def _epoch_ms(seconds) -> int | None:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return int(round(seconds * 1000))


def _int_ms(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _family(model) -> str | None:
    text = str(model or "").lower()
    return next((f for f in MODEL_FAMILIES if f in text), None)


def diagnose(event: str, detail: str) -> str:
    """Best-effort, codes-only diagnostic for the observational hooks; it never raises.

    The status view only observes the switcher, so a broken stderr sink must not turn a good read,
    switch or run into a failure, and the read-only GET must not start writing state. When the log
    sink fails, the same codes plus the sink's exception class go to the structured eventlog: the
    second half of what log() itself writes. No settings/state write ever. That helper swallows its
    own sink errors, so the fallback is only an attempt: "eventlog_attempted" proves no delivery, and
    the event may still be lost. If even the attempt raises, it is "dropped". Nothing is ever
    reported as stored or accepted."""
    try:
        log(event, detail)
        return "logged"
    except Exception as exc:  # noqa: BLE001 - a closed or broken sink (OSError, BrokenPipeError, ValueError)
        sink_error = type(exc).__name__
    try:
        log_module._eventlog_emit(event, "warning", f"{detail} sink_error={sink_error}"[:256])
        return "eventlog_attempted"
    except Exception:  # noqa: BLE001 - the eventlog can share the fault; the observed operation outranks it
        return "dropped"


def _log_failure(site: str, exc: BaseException) -> None:
    # Codes only: the message of a parse or provider error can carry payload text.
    diagnose("hotseatStatusRecordFailed", f"site={site} error={type(exc).__name__}")


# --- parsing the CLI payloads ---------------------------------------------

def _window(kind: str, label: str, used, reset_ms, observed_at: int) -> dict:
    return {"kind": kind, "label": label, "used": _ratio(used), "resets_at": reset_ms,
            "observed_at": observed_at, "source": "hotseat-cli"}


def _unique(windows: list[dict]) -> tuple[list[dict], bool]:
    """One window per kind (contract D.4): the most constraining sample wins (highest used),
    null loses, then label, so the choice is deterministic. Reports whether any was dropped.
    Windows whose name maps to no kind never get here: unknown names are dropped, not invented."""
    best: dict[str, dict] = {}
    for window in windows:
        held = best.get(window["kind"])
        if held is None or _rank(window) < _rank(held):
            best[window["kind"]] = window
    return list(best.values()), len(best) < len(windows)


SOURCE_ORDER = ("hotseat-cli", "usage-worker")  # contract 0b8c0d2d D.4: cli first on a full tie


def _rank(window: dict) -> tuple:
    used = window["used"]
    source = window.get("source")
    return (used is None, -(used or 0.0),
            SOURCE_ORDER.index(source) if source in SOURCE_ORDER else len(SOURCE_ORDER), str(window["label"]))


def _numbers(windows) -> bool:
    return any(w["used"] is not None for w in windows)


def _structured_auth(row: dict) -> bool:
    """Authoritative auth evidence is a typed field, never free text."""
    return row.get("error_code") == "auth_error"


def _claude_account(provider: str, row: dict, observed_at: int) -> dict:
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    windows = []
    # A row whose read failed (free-text error or typed auth) contributes no samples: its numbers may be
    # cached or partial, and the same identity's last-good values stay. A missing number stays null, never 0.
    if usage and not row.get("error") and not _structured_auth(row):
        windows = [_window("5h", "5-hour", usage.get("used_5h"), _epoch_ms(usage.get("reset_5h")), observed_at),
                   _window("7d", "7-day", usage.get("used_7d"), _epoch_ms(usage.get("reset_7d")), observed_at)]
        for scoped in usage.get("scoped") or []:
            family = _family(scoped.get("model")) if isinstance(scoped, dict) else None
            if family and _ratio(scoped.get("used")) is not None:
                windows.append(_window(f"model:{family}", f"{family.capitalize()} weekly", scoped.get("used"),
                                       _epoch_ms(scoped.get("reset")), observed_at))
    windows, dropped = _unique(windows)
    if _structured_auth(row):
        state, code = "auth_error", "auth_error"
    elif row.get("error"):
        state, code = "unknown", "provider_error"
    elif usage.get("limited") is True or usage.get("status") == "rejected":
        state, code = "exhausted", "exhausted"
    elif _numbers(windows):
        state, code = "ok", None
    else:
        state, code = "unknown", None
    return {"identity": identity_key(provider, row.get("alias"), email=row.get("email"), org=row.get("org")),
            "tag": _alias_tag(provider, row.get("alias")), "alias": display_alias(row.get("alias")),
            "plan": _plan(row.get("plan")), "label": _plan_label(row.get("plan_label")),
            "active": row.get("is_active") is True, "state": state, "error_code": code, "windows": windows,
            "dropped": dropped}


def _codex_kind(provider: str, label: str) -> tuple[str, str] | None:
    """Hotseat labels a window "<limit> [tier] <span>"; the account-wide limit carries the provider's name."""
    words = label.lower().split()
    if not words:
        return None
    span = {"5-hour": "5h", "weekly": "7d"}.get(words[-1])
    if words[0] == provider:
        return (span, "5-hour" if span == "5h" else "weekly") if span else None  # Codex's own word for 7d
    family = _family(words[0])
    if not family:
        return None
    return f"model:{family}", f"{family.capitalize()} {'5-hour' if span == '5h' else 'weekly' if span else 'window'}"


def _no_reset(value) -> str | None:
    """Why a window reset will not unblock the account (Hotseat's closed reasons), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return NO_RESET_REASONS.get(value.strip(), "other")


def _codex_account(provider: str, row: dict, observed_at: int) -> dict:
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else None
    failed = _structured_auth(row) or bool(usage and (_structured_auth(usage) or usage.get("error")))
    windows = []
    # A failed row (usage.error or typed auth) contributes no samples, even when it still carries numeric
    # (cached) windows: the same identity's last-good values stay, with the current error.
    for raw in [] if failed else (usage or {}).get("windows") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("label"), str):
            continue
        kind = _codex_kind(provider, raw["label"])
        if kind:
            windows.append(_window(kind[0], kind[1], raw.get("used"), _epoch_ms(raw.get("reset")), observed_at))
    windows, dropped = _unique(windows)
    if _structured_auth(row) or (usage and _structured_auth(usage)):
        state, code = "auth_error", "auth_error"
    elif usage is None:
        state, code = "unknown", None
    elif usage.get("error"):
        state, code = "unknown", "provider_error"
    elif usage.get("blocked") is True:
        state, code = "exhausted", "exhausted"
    elif usage.get("usable") is False:  # not usable, yet no typed block: the reason is unknown
        state, code = "unknown", None
    elif _numbers(windows):
        state, code = "ok", None
    else:
        state, code = "unknown", None
    return {"identity": identity_key(provider, row.get("alias"), email=row.get("email"),
                                     account_id=row.get("account_id")),
            "tag": _alias_tag(provider, row.get("alias")), "alias": display_alias(row.get("alias")),
            "plan": _plan(row.get("plan")), "label": None, "active": row.get("is_active") is True,
            "state": state, "error_code": code, "windows": windows, "dropped": dropped, "failed": failed,
            "no_reset": _no_reset((usage or {}).get("no_reset_reason"))}


def parse(provider: str, payload, observed_at: int) -> dict:
    """The allowlisted reading of one CLI payload. Raises ValueError on a shape it cannot trust."""
    reader = READERS.get(provider)
    if reader is None or reader.build is None:
        raise ValueError("provider")
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list):
        raise ValueError("shape")
    rows = [r for r in payload["accounts"] if isinstance(r, dict) and r.get("alias")]
    accounts = [reader.build(provider, r, observed_at) for r in rows[:MAX_ACCOUNTS]]
    partial = (len(rows) > MAX_ACCOUNTS or any(a["state"] == "unknown" for a in accounts)
               or any(a.pop("dropped") for a in accounts))
    if reader.quota_read_key and payload.get(reader.quota_read_key) is False:
        partial = True
    for account in accounts:
        if len(account["windows"]) > MAX_WINDOWS:
            account["windows"] = account["windows"][:MAX_WINDOWS]
            partial = True
    seen: dict[str, int] = {}
    for account in accounts:  # two redactions may collide; rows stay distinguishable
        count = seen[account["alias"]] = seen.get(account["alias"], 0) + 1
        if count > 1:
            account["alias"] = f"{account['alias']} ({count})"
    return {"accounts": accounts, "partial": partial, "inventory": "ok" if accounts else "empty"}


@dataclass(frozen=True)
class Reader:
    """How one Hotseat provider's payload is read; looked up, never branched on."""
    build: Callable[[str, dict, int], dict] | None  # None: no admitted reader
    age_key: str | None = None         # payload field giving a cached reading's age in seconds
    quota_read_key: str | None = None  # payload field that is False when quota was not read


READERS: dict[str, Reader] = {
    "claude": Reader(_claude_account),
    "codex": Reader(_codex_account, age_key="quota_age_s", quota_read_key="quota_read"),
    "muse": Reader(None),  # missing Muse data is unavailable, never an empty inventory
}
PROVIDERS = tuple(READERS)
READ_PROVIDERS = tuple(name for name, reader in READERS.items() if reader.build is not None)


def _observed_at(provider: str, payload, received_at: int) -> int:
    """When the numbers were read: a cached reading reports its age."""
    reader = READERS.get(provider)
    if reader and reader.age_key and isinstance(payload, dict):
        age = payload.get(reader.age_key)
        if not isinstance(age, bool) and isinstance(age, (int, float)) and math.isfinite(age) and age > 0:
            return received_at - int(age * 1000)
    return received_at


# --- persistence ------------------------------------------------------------

def _load(prefix: str, provider: str) -> dict | None:
    raw = settings_store.get(prefix + provider)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        _log_failure(f"load.{prefix.split('.')[1]}", exc)
        return None
    return value if isinstance(value, dict) else None


def _store(prefix: str, provider: str, value: dict) -> None:
    settings_store.set_text(prefix + provider, json.dumps(value, sort_keys=True, separators=(",", ":")))


def _merge(previous: dict | None, reading: dict, observed_at: int) -> list[dict]:
    """Carry last-good windows only to the exact same identity, never by alias."""
    before = {a.get("identity"): a for a in (previous or {}).get("accounts") or [] if isinstance(a, dict)}
    merged = []
    for account in reading["accounts"]:
        prior = before.get(account["identity"]) or {}
        if account.pop("failed", False) and "no_reset" in account:
            # A failed row's reset metadata is no sample either: the no-reset hint stays the one that
            # goes with the retained last-good windows of this exact identity (none without a prior).
            account["no_reset"] = prior.get("no_reset")
        kept = {w.get("kind"): w for w in prior.get("windows") or []  # D2: no observed_at, not eligible
                if isinstance(w, dict) and w.get("used") is not None and _int_ms(w.get("observed_at"))}
        if _numbers(account["windows"]):  # per kind: a fresh value wins, a null keeps last-good
            account["windows"] = [w if w["used"] is not None else kept.get(w["kind"], w)
                                  for w in account["windows"]]
        else:  # the row carried no numbers at all: keep every last-good window
            account["windows"] = list(kept.values()) or account["windows"]
        good = [w["observed_at"] for w in account["windows"] if w.get("used") is not None]
        account["last_good_observed_at"] = max(good) if good else None
        merged.append(account)
    return merged


def record_read(provider: str, payload, observed_at: int | None = None) -> None:
    """Keep the allowlisted reading of one successful `hotseat ... --json` call."""
    try:
        received = int(observed_at) if observed_at is not None else now_ms()
        stamp = _observed_at(provider, payload, received)
        previous = _load(SNAPSHOT_PREFIX, provider)
        if previous and isinstance(previous.get("observed_at"), int) and previous["observed_at"] > stamp:
            return  # an older read finishing late never replaces a newer one
        try:
            reading = parse(provider, payload, stamp)
        except ValueError:
            diagnose("hotseatStatusReadRejected", f"provider={provider} code=parse_failed")
            _store_failure(provider, "parse_failed", received, previous)
            return
        _store(SNAPSHOT_PREFIX, provider, {
            "v": 1, "observed_at": stamp, "attempt_at": received, "read_state": "ok",
            "error_code": None, "inventory": reading["inventory"], "partial": reading["partial"],
            "accounts": _merge(previous, reading, stamp)})
    except Exception as exc:  # noqa: BLE001 - never break the Janitor's read
        _log_failure("record_read", exc)


def error_code_for(exc: BaseException | None) -> str:
    import subprocess
    if isinstance(exc, subprocess.TimeoutExpired) or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ValueError):  # json.JSONDecodeError included
        return "parse_failed"
    if isinstance(exc, (RuntimeError, OSError)):
        return "cli_failed"
    return "provider_error"


def _store_failure(provider: str, code: str, at: int, previous: dict | None) -> None:
    kept = dict(previous or {"v": 1, "observed_at": None, "inventory": None, "partial": False, "accounts": []})
    kept.update({"attempt_at": at, "read_state": "failed", "error_code": code})
    _store(SNAPSHOT_PREFIX, provider, kept)


def _store_lane(provider: str, *, mode, floor_pct, cadence_s, at: int, next_due_at) -> None:
    _store(LANE_PREFIX, provider, {
        "mode": mode if mode in ("automatic", "notify") else None,
        "floor_pct": int(floor_pct) if isinstance(floor_pct, int) and not isinstance(floor_pct, bool) else None,
        "cadence_s": int(cadence_s) if isinstance(cadence_s, int) and not isinstance(cadence_s, bool) and cadence_s > 0 else None,
        "last_run_at": at, "next_due_at": _int_ms(next_due_at)})


def record_failure(provider: str, exc: BaseException | None, *, now: int) -> None:
    """A read that failed: only read_state and error_code change (contract C). The last
    action and the last-good samples stay as they were; serving marks the samples stale."""
    try:
        _store_failure(provider, error_code_for(exc), int(now), _load(SNAPSHOT_PREFIX, provider))
    except Exception as e:  # noqa: BLE001
        _log_failure("record_failure", e)


def _kind(decision: dict, mode: str, switched) -> tuple[str, str | None]:
    """Contract C: the switcher's branch as (kind, reason_code)."""
    action = decision.get("action")
    if action == "switch":
        if mode != "automatic":
            return "would_switch", "below_floor"
        return ("switched", "below_floor") if switched else ("failed", "switch_failed")
    if action == "noop":
        return "noop", "already_ok"
    if action == "exhausted":
        return "exhausted", "no_better_account"
    # decide() writes one of two fixed sentences for a skip; nothing provider-written is read here.
    reason = str(decision.get("reason") or "")
    if reason == "Hotseat reports no account in use":
        return "skipped", "no_active_account"
    if reason.endswith(": quota unknown"):
        return "skipped", "quota_unknown"
    return "skipped", None  # not a branch decide() has: no receipt is written


def _receipt_alias(provider: str, alias, snapshot: dict | None) -> str | None:
    if not alias:
        return None
    tag = _alias_tag(provider, alias)
    for account in (snapshot or {}).get("accounts") or []:
        if isinstance(account, dict) and account.get("tag") == tag:
            return account.get("alias")
    return display_alias(alias)


def record_action(provider: str, decision: dict, *, mode: str, switched, now: int, floor_pct=None,
                  cadence_s=None, next_due_at=None) -> None:
    """The switcher's structured receipt for one run: kind, reason code, redacted aliases."""
    try:
        decision = dict(decision or {})
        kind, reason = _kind(decision, mode, switched)
        if reason is None:  # every emitted reason_code is in the closed list, so an unknown branch writes nothing
            diagnose("hotseatStatusReceiptSkipped", f"provider={provider} code=unknown_branch")
            return
        snapshot = _load(SNAPSHOT_PREFIX, provider)
        target = decision.get("target") if decision.get("action") == "switch" else None
        _store(ACTION_PREFIX, provider, {
            "kind": kind, "at": int(now), "reason_code": reason,
            "from_alias": _receipt_alias(provider, decision.get("active"), snapshot),
            "to_alias": _receipt_alias(provider, target, snapshot)})
        _store_lane(provider, mode=mode, floor_pct=floor_pct, cadence_s=cadence_s, at=int(now),
                    next_due_at=next_due_at)
    except Exception as exc:  # noqa: BLE001
        _log_failure("record_action", exc)


# --- serving ------------------------------------------------------------------

def janitor_enabled() -> bool:
    """Whether an enabled Hotseat switcher exists. One database read; no admission work."""
    try:
        from . import janitor_store
        return bool(janitor_store.janitor_agent_ids(template_id=ROLE, enabled_only=True))
    except Exception as exc:  # noqa: BLE001 - unknown is served as disabled (stale)
        _log_failure("janitor_enabled", exc)
        return False


def stale_after_ms(cadence_s) -> int:
    cadence = cadence_s if isinstance(cadence_s, int) and cadence_s > 0 else 0
    return max(2 * cadence, STALE_FLOOR_S) * 1000


def _served_window(window: dict, *, stale: bool, served_at: int, no_reset: bool = False) -> dict:
    used = _ratio(window.get("used"))
    # A reset that will not unblock the account (Hotseat's no_reset_reason) is never advertised:
    # null means unknown. The closed reason stays internal; v2 has no field for it.
    resets_at = None if no_reset else _int_ms(window.get("resets_at"))
    observed = _int_ms(window.get("observed_at"))
    if resets_at is not None and resets_at <= served_at and (observed is None or observed < resets_at):
        used, stale = None, True  # unknown since the reset, not a full quota
    return {"kind": window.get("kind"), "label": window.get("label"), "used_ratio": used,
            "remaining_ratio": None if used is None else round(1 - used, 4),
            "resets_at": resets_at, "observed_at": observed, "stale": stale,
            "source": "hotseat-cli"}


def _served_account(account: dict, *, source_failed: bool, stale_after: int, served_at: int) -> dict:
    state = account.get("state") if account.get("state") in ("ok", "exhausted", "auth_error", "unknown") else "unknown"
    code = account.get("error_code") if account.get("error_code") in ERROR_CODES else None
    # Contract D.6: each window by its own age; a failed source (or this row's own error) is stale.
    failed = source_failed or code in ("provider_error", "auth_error")
    windows = []
    for window in (account.get("windows") or [])[:MAX_WINDOWS]:
        if not isinstance(window, dict):
            continue
        observed = _int_ms(window.get("observed_at"))
        stale = failed or observed is None or served_at - observed > stale_after
        windows.append(_served_window(window, stale=stale, served_at=served_at,
                                      no_reset=bool(account.get("no_reset"))))
    if state == "exhausted" and not account.get("no_reset") and any(w["used_ratio"] is None and w["resets_at"] is not None
                                    and w["resets_at"] <= served_at for w in windows):
        state, code = "unknown", None  # a reset passed since the exhaustion was seen (and it unblocks)
    if state == "ok" and windows and all(w["used_ratio"] is None for w in windows):
        state = "unknown"
    return {"alias": _served_alias(account.get("alias")) or "account",
            "label": _plan_label(account.get("label")), "plan": _plan(account.get("plan")),
            "active_default": account.get("active") is True, "state": state, "error_code": code,
            "last_good_observed_at": _int_ms(account.get("last_good_observed_at")), "windows": windows}


def _served_provider(provider: str, *, enabled: bool, served_at: int) -> dict:
    lane = _load(LANE_PREFIX, provider) or {}
    cadence = lane.get("cadence_s") if isinstance(lane.get("cadence_s"), int) else None
    if provider not in READ_PROVIDERS:
        return {"provider": provider, "inventory": "unavailable", "observed_at": None, "stale": True,
                "partial": False, "read_state": "never", "error_code": None, "cadence_s": None, "accounts": []}
    snapshot = _load(SNAPSHOT_PREFIX, provider)
    if snapshot is None:  # no data yet, which is not an empty inventory
        return {"provider": provider, "inventory": "unavailable", "observed_at": None, "stale": True,
                "partial": False, "read_state": "never", "error_code": None if enabled else "disabled",
                "cadence_s": cadence, "accounts": []}
    observed_at = _int_ms(snapshot.get("observed_at"))
    read_state = "failed" if snapshot.get("read_state") == "failed" else "ok" if observed_at else "never"
    code = snapshot.get("error_code") if snapshot.get("error_code") in ERROR_CODES else None
    stale = (not enabled or read_state != "ok" or observed_at is None
             or served_at - observed_at > stale_after_ms(cadence))
    if not enabled:
        code = "disabled"
    accounts = [_served_account(a, source_failed=not enabled or read_state != "ok",
                                stale_after=stale_after_ms(cadence), served_at=served_at)
                for a in (snapshot.get("accounts") or [])[:MAX_ACCOUNTS] if isinstance(a, dict)]
    inventory = snapshot.get("inventory") if snapshot.get("inventory") in ("ok", "empty") else "unavailable"
    return {"provider": provider, "inventory": inventory, "observed_at": observed_at, "stale": stale,
            "partial": bool(snapshot.get("partial")) or any(a["state"] == "unknown" for a in accounts),
            "read_state": read_state, "error_code": code, "cadence_s": cadence, "accounts": accounts}


def _served_lane(provider: str, *, enabled: bool) -> dict | None:
    lane = _load(LANE_PREFIX, provider)
    if lane is None:
        return None
    return {"provider": provider, "mode": lane.get("mode") if lane.get("mode") in ("automatic", "notify") else None,
            "floor_pct": lane.get("floor_pct") if isinstance(lane.get("floor_pct"), int) else None,
            "cadence_s": lane.get("cadence_s") if isinstance(lane.get("cadence_s"), int) else None,
            "last_run_at": _int_ms(lane.get("last_run_at")),
            "next_due_at": _int_ms(lane.get("next_due_at")) if enabled else None}


def _served_action(provider: str) -> dict | None:
    action = _load(ACTION_PREFIX, provider)
    if action is None or action.get("kind") not in ACTION_KINDS or action.get("reason_code") not in REASON_CODES:
        return None
    return {"provider": provider, "kind": action["kind"], "at": _int_ms(action.get("at")),
            "reason_code": action["reason_code"],
            "from_alias": _served_alias(action.get("from_alias")), "to_alias": _served_alias(action.get("to_alias"))}


def payload(*, served_at: int | None = None) -> dict:
    """The wire payload, built from persisted state only (no CLI, no provider read)."""
    served_at = now_ms() if served_at is None else int(served_at)
    enabled = janitor_enabled()
    return {
        "schema": SCHEMA,
        "served_at": served_at,
        "source": {"cli_version": None, "cost_note": COST_NOTE},
        "switcher": {"kind": "deterministic", "model": None, "enabled": enabled,
                     "lanes": [lane for p in READ_PROVIDERS if (lane := _served_lane(p, enabled=enabled))]},
        "actions": [action for p in READ_PROVIDERS if (action := _served_action(p))],
        "providers": [_served_provider(p, enabled=enabled, served_at=served_at) for p in PROVIDERS],
    }
