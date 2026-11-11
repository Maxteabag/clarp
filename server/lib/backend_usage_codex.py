"""Moved out of server/lib/backend_usage.py by tools/refactor/split_apply.py (split 'backend-usage', recipe tools/refactor/splits/s6-backends.toml).
backend_usage.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import select
import time
from typing import Any
from urllib.parse import urlsplit
from . import db
from .clock import now_ms as _now_ms
from .log import log, log_exception
from .backend_usage_store import CODEX, CODEX_FRESH_MS, CODEX_REFRESH_MS, CODEX_SOURCE_REF, _codex_identity, _decode_raw, _event_payload, _insert_limit_event, _invalidate_rotated_auth, _iso_from_epoch_seconds, _iso_to_ms, _latest_completed_turn_ms, _observe_auth_generation, _open_episode, _provider_instance_id, _quota_windows, _record_codex_five_hour, _record_codex_weekly, _resolve_unknown_terminals, _retire_omitted_windows, _row, _to_float, _upsert, _window_by_duration, _window_from_payload, _window_id


def _active_codex_identity() -> tuple[str, str] | None:
    try:
        token, account_id = _read_codex_auth()
        return _codex_identity(token, account_id)
    except Exception:  # noqa: BLE001
        return None


def _codex_needs_refresh() -> bool:
    row = _row(CODEX)
    if not row:
        return True
    raw = _decode_raw(row)
    active = _active_codex_identity()
    if active is not None and raw.get("auth_generation_id") != active[0]:
        return True
    now = _now_ms()
    auth_gen = active[0] if active is not None else raw.get("auth_generation_id")
    if auth_gen:
        structured = db.conn().execute(
            """SELECT observed_at, used_percentage FROM provider_usage_windows
                WHERE provider_instance_id=? AND auth_generation_id=?
                  AND window_kind IN ('five_hour','seven_day')
                ORDER BY observed_at DESC LIMIT 1""",
            (_provider_instance_id(CODEX), auth_gen),).fetchone()
    else:
        structured = db.conn().execute(
            """SELECT observed_at, used_percentage FROM provider_usage_windows
                WHERE provider_instance_id=?
                  AND window_kind IN ('five_hour','seven_day')
                ORDER BY observed_at DESC LIMIT 1""",
            (_provider_instance_id(CODEX),),).fetchone()
    if (not structured
            or now - int(structured["observed_at"]) > CODEX_REFRESH_MS):
        return True
    fetched_at = int(row.get("fetched_at") or 0)
    if now - fetched_at > CODEX_REFRESH_MS:
        return True
    if raw.get("blocked") or (_to_float(row.get("used_percentage")) or 0.0) >= 100.0:
        done_ms = _latest_completed_turn_ms(CODEX, up_to_ms=now)
        if done_ms is not None and done_ms > fetched_at:
            return True
    return False


def _codex_home() -> pathlib.Path:
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return pathlib.Path(raw).expanduser()
    return pathlib.Path.home() / ".codex"


def _read_codex_auth(codex_home: pathlib.Path | None = None) -> tuple[str, str]:
    auth_path = (codex_home or _codex_home()) / "auth.json"
    data = json.loads(auth_path.read_text())
    if data.get("auth_mode") != "chatgpt":
        raise RuntimeError("codex auth is not ChatGPT OAuth")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise RuntimeError("codex auth tokens missing")
    access_token = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    if not access_token:
        raise RuntimeError("codex access token missing")
    if not account_id:
        raise RuntimeError("codex account id missing")
    return access_token, account_id


def _read_codex_chatgpt_base_url(codex_home: pathlib.Path | None = None) -> str:
    if os.environ.get("CODEX_CHATGPT_BASE_URL"):
        return os.environ["CODEX_CHATGPT_BASE_URL"].strip()
    config_path = (codex_home or _codex_home()) / "config.toml"
    if config_path.is_file():
        try:
            import tomllib

            data = tomllib.loads(config_path.read_text())
            value = data.get("chatgpt_base_url")
            if isinstance(value, str) and value.strip():
                return value.strip()
        except Exception as exc:  # noqa: BLE001
            log_exception("codexConfigReadFail", exc, detail=str(config_path))
    return "https://chatgpt.com/backend-api/"


def _codex_usage_url(base_url: str) -> str:
    raw = (base_url or "https://chatgpt.com/backend-api/").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("unapproved Codex usage origin") from exc
    if parsed.scheme.lower() != "https":
        raise RuntimeError("Codex usage origin must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("Codex usage origin must not contain userinfo")
    host = (parsed.hostname or "").lower()
    if host not in {"chatgpt.com", "chat.openai.com"}:
        raise RuntimeError("unapproved Codex usage host")
    if port not in {None, 443}:
        raise RuntimeError("unapproved Codex usage port")
    if parsed.query or parsed.fragment:
        raise RuntimeError("unapproved Codex usage URL suffix")
    path = parsed.path.rstrip("/")
    if path not in {"", "/backend-api"}:
        raise RuntimeError("unapproved Codex usage base path")
    return f"https://{host}/backend-api/wham/usage"


def _codex_rpc(proc: subprocess.Popen, request_id: int, method: str,
               params: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps({"id": request_id, "method": method,
                                 "params": params or {}}) + "\n")
    proc.stdin.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        line = proc.stdout.readline()
        if not line:
            break
        message = json.loads(line)
        if message.get("id") == request_id:
            return message
    raise RuntimeError(f"Codex app-server timed out during {method}")


def fetch_codex_usage_app_server(*, timeout: float = 8.0) -> dict[str, Any]:
    """CLI-native fallback, probing current approval-policy spellings safely."""
    last_error = "Codex app-server unavailable"
    for approval in ("never", "on-request", ""):
        command = ["codex", "-s", "read-only"]
        if approval:
            command += ["-a", approval]
        command.append("app-server")
        try:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            _codex_rpc(proc, 1, "initialize", {
                "clientInfo": {"name": "clarp-provider-usage", "version": "1"}}, timeout)
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
            proc.stdin.flush()
            limits = (_codex_rpc(proc, 2, "account/rateLimits/read", timeout=timeout)
                      .get("result") or {}).get("rateLimits") or {}
            if not limits:
                raise RuntimeError("Codex app-server returned no rate limits")
            return capture_codex_rate_limits(limits, source_detail="account/rateLimits/read")
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        if "invalid value" not in last_error.lower() and "approval" not in last_error.lower():
            break
    raise RuntimeError(last_error)


def capture_codex_rate_limits(
    payload: dict[str, Any], *,
    source_detail: str = "account/rateLimits/updated",
    identity: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Normalize a full endpoint response or sparse app-server observation."""
    if identity is None:
        token, account_id = _read_codex_auth()
        identity = _codex_identity(token, account_id)
    auth_generation_id, account_scope_ref = identity
    authoritative_refresh = source_detail in {
        "/wham/usage", "/api/codex/usage", "account/rateLimits/read",
    }
    block = _codex_block_from_payload(payload) if authoritative_refresh else None
    try:
        snapshot = _codex_snapshot_from_payload(payload)
    except RuntimeError:
        # A workspace without credits can report no window at all. The
        # provider still says turns are refused, which is what matters.
        if block is None:
            raise
        snapshot = {"used_percentage": None,
                    "resets_at": block["resets_at"] or "", "windows": {}}
    five_hour_observed = _window_by_duration(snapshot, 300) is not None
    weekly_observed = _window_by_duration(snapshot, 10_080) is not None
    rotation_events = _observe_auth_generation(
        provider_instance_id=_provider_instance_id(CODEX),
        auth_generation_id=auth_generation_id,
        observed_at=_now_ms())
    observed_kinds: set[str] = set()
    if five_hour_observed:
        observed_kinds.add("five_hour")
    if weekly_observed:
        observed_kinds.add("seven_day")
    retired_events = (
        _retire_omitted_windows(
            provider_instance_id=_provider_instance_id(CODEX),
            auth_generation_id=auth_generation_id,
            observed_kinds=observed_kinds, observed_at=_now_ms())
        if authoritative_refresh else [])
    cleared_unknown_events = (
        _resolve_unknown_terminals(
            provider_instance_id=_provider_instance_id(CODEX),
            auth_generation_id=auth_generation_id,
            observed_at=_now_ms())
        if (authoritative_refresh and block is None and snapshot["windows"]
            and all((_to_float(w.get("used_percentage")) or 0.0) < 100.0
                    for w in snapshot["windows"].values()
                    if isinstance(w, dict)))
        else [])
    existing_raw = _decode_raw(_row(CODEX))
    same_identity = (
        existing_raw.get("auth_generation_id") == auth_generation_id
        and existing_raw.get("account_scope_ref") == account_scope_ref
    )
    existing_windows = (
        _quota_windows(existing_raw)
        if same_identity and not authoritative_refresh else {})
    merged_windows = {**existing_windows, **snapshot["windows"]}
    snapshot["windows"] = merged_windows
    preferred = (
        merged_windows.get("primary")
        or merged_windows.get("secondary")
        or merged_windows.get("individual_limit")
        or {})
    snapshot["used_percentage"] = preferred.get("used_percentage")
    snapshot["resets_at"] = preferred.get("resets_at") or ""
    current_legacy = _row(CODEX)
    # Only an authoritative read can set or clear the block; a sparse
    # notification says nothing about it either way.
    blocked = block if authoritative_refresh else (
        existing_raw.get("blocked") if same_identity else None)
    legacy_fetched_at = (
        None if authoritative_refresh
        else int((current_legacy or {}).get("fetched_at") or 1)
    )
    _upsert(
        CODEX,
        used_percentage=snapshot["used_percentage"],
        resets_at=snapshot["resets_at"] or "",
        source="codex-usage-endpoint",
        raw={
            "windows": snapshot["windows"],
            "plan_type": payload.get("plan_type", payload.get("planType")),
            "source_ref": CODEX_SOURCE_REF,
            "url_path": source_detail,
            "auth_generation_id": auth_generation_id,
            "account_scope_ref": account_scope_ref,
            "blocked": blocked,
        },
        fetched_at=legacy_fetched_at,
    )
    log(
        "codexUsageFetched",
        f"used={snapshot['used_percentage']} resets={snapshot['resets_at'] or '-'}",
    )
    snapshot["limit_events"] = rotation_events + retired_events + cleared_unknown_events + (
        _record_codex_five_hour(
            snapshot, auth_generation_id=auth_generation_id,
            account_scope_ref=account_scope_ref,
            source_detail=source_detail)
        if five_hour_observed else []
    )
    if weekly_observed:
        _record_codex_weekly(
            snapshot, auth_generation_id=auth_generation_id,
            account_scope_ref=account_scope_ref,
            source_detail=source_detail)
    return snapshot


def _codex_block_from_payload(payload: Any) -> dict[str, Any] | None:
    """The provider's own statement that new turns are refused, if it makes one.

    `/wham/usage` answers `rate_limit.allowed: false` together with
    `rate_limit_reached_type.type` (e.g. workspace_member_credits_depleted)
    and `rate_limit_upsell.reset_at`. Percentages alone cannot tell spent
    credits from a spent window, and the two need different fixes.
    """
    if not isinstance(payload, dict):
        return None
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    if rate_limit.get("allowed") is not False and rate_limit.get("limit_reached") is not True:
        return None
    reached = payload.get("rate_limit_reached_type")
    kind = str(reached.get("type") or "") if isinstance(reached, dict) else ""
    upsell = payload.get("rate_limit_upsell")
    reset = upsell.get("reset_at") if isinstance(upsell, dict) else None
    return {
        "reason": "credits_depleted" if "credit" in kind.lower() else "usage_limit",
        "provider_reason": kind,
        "resets_at": _iso_from_epoch_seconds(reset) or None,
    }


def _fresh_codex_block(now_ms: int) -> dict[str, Any] | None:
    row = _row(CODEX)
    raw = _decode_raw(row)
    block = raw.get("blocked")
    if not row or not isinstance(block, dict):
        return None
    active = _active_codex_identity()
    if active is not None and raw.get("auth_generation_id") and raw.get("auth_generation_id") != active[0]:
        return None
    if now_ms - int(row.get("fetched_at") or 0) > CODEX_FRESH_MS:
        return None
    reset_ms = _iso_to_ms(block.get("resets_at"))
    if reset_ms is not None and now_ms >= reset_ms:
        return None
    return block


def _codex_snapshot_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("codex usage response was not an object")
    rate_limit = _select_rate_limit_payload(payload)
    if not isinstance(rate_limit, dict):
        raise RuntimeError("codex usage response missing rate_limit")
    primary = _window_from_payload(
        rate_limit.get("primary_window", rate_limit.get("primary")))
    secondary = _window_from_payload(
        rate_limit.get("secondary_window", rate_limit.get("secondary")))
    windows: dict[str, Any] = {}
    if primary:
        windows["primary"] = primary
    if secondary:
        windows["secondary"] = secondary
    individual = _window_from_payload(
        (payload.get("spend_control") or {}).get("individual_limit")
        if isinstance(payload.get("spend_control"), dict)
        else None
    )
    if individual:
        windows["individual_limit"] = individual
    if not windows:
        raise RuntimeError("codex usage response had no usable windows")
    preferred = primary or secondary or individual or {}
    return {
        "used_percentage": preferred.get("used_percentage"),
        "resets_at": preferred.get("resets_at") or "",
        "windows": windows,
    }


def _select_rate_limit_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    snake = payload.get("rate_limit")
    if isinstance(snake, dict):
        return snake
    candidates: list[dict[str, Any]] = []
    default = payload.get("rateLimits")
    if isinstance(default, dict):
        candidates.append(default)
    elif isinstance(default, list):
        candidates.extend(
            value for value in default if isinstance(value, dict))
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for key, value in by_id.items():
            if isinstance(value, dict) and str(key).lower() == CODEX:
                return value
        mapped = [value for value in by_id.values() if isinstance(value, dict)]
        for candidate in mapped:
            if str(candidate.get("limitId") or "").lower() == CODEX:
                return candidate
        candidates.extend(mapped)
    explicit = [candidate for candidate in candidates
                if str(candidate.get("limitId") or "").lower() == CODEX]
    if explicit:
        return explicit[0]
    five_hour_candidates = []
    for candidate in candidates:
        primary = _window_from_payload(candidate.get("primary"))
        if primary and primary.get("window_minutes") == 300:
            five_hour_candidates.append(candidate)
    if len(five_hour_candidates) == 1:
        return five_hour_candidates[0]
    # A single sparse notification has no bucket map but is authoritative for
    # this Codex app-server connection. Multi-bucket ambiguity fails closed.
    if not isinstance(by_id, dict) and len(candidates) == 1:
        return candidates[0]
    return None


def record_classified_usage_limit(provider_id: str) -> dict[str, Any] | None:
    """Persist one classified Codex quota terminal and return its typed event.

    Raw provider error text remains on the agent terminal row. The classifier
    proves quota/billing exhaustion but not which provider window exhausted,
    so this never parses text or attaches the terminal to five-hour evidence.
    It records fresh hard-limit evidence with unknown quantities and scope.
    """
    if provider_id != CODEX:
        return None
    identity_observed = True
    try:
        token, account_id = _read_codex_auth()
        auth_generation_id, account_scope_ref = _codex_identity(token, account_id)
    except Exception:  # noqa: BLE001 - terminal evidence still remains valid
        auth_generation_id, account_scope_ref = "auth-unknown", None
        identity_observed = False
    provider_instance_id = _provider_instance_id(CODEX)
    database = db.conn()
    observed_at = _now_ms()
    window_id = _window_id(
        provider_instance_id=provider_instance_id,
        auth_generation_id=auth_generation_id,
        account_scope_ref=account_scope_ref,
        window_kind="unknown", window_minutes=None, resets_at=None)
    used = None
    resets_at = None
    database.execute("BEGIN IMMEDIATE")
    try:
        rotation_events = (
            _invalidate_rotated_auth(
                database, provider_instance_id=provider_instance_id,
                auth_generation_id=auth_generation_id,
                observed_at=observed_at)
            if identity_observed else [])
        database.execute(
            """INSERT INTO provider_usage_windows (
                   provider_instance_id,provider_id,auth_generation_id,
                   account_scope_ref,window_id,window_kind,scope_kind,unit,
                   used_percentage,resets_at,observed_at,source_kind,
                   source_detail
               ) VALUES (?,?,?,?,?,'unknown','account','percent',NULL,NULL,
                         ?,'classified_terminal','classified quota terminal')
               ON CONFLICT(provider_instance_id,auth_generation_id,window_id)
               DO UPDATE SET observed_at=excluded.observed_at,
                   source_kind=excluded.source_kind,
                   source_detail=excluded.source_detail""",
            (provider_instance_id, CODEX, auth_generation_id,
             account_scope_ref, window_id, observed_at),)
        episode = _open_episode(
            database, provider_instance_id=provider_instance_id,
            provider_id=CODEX, auth_generation_id=auth_generation_id,
            account_scope_ref=account_scope_ref, window_id=window_id,
            observed_at=observed_at)
        event = _insert_limit_event(
            database, episode=episode, kind="hard_limit",
            threshold_id=None, used_percentage=used, resets_at=resets_at,
            observed_at=observed_at, freshness="fresh",
            source_kind="classified_terminal")
        database.execute("COMMIT")
        if event:
            return {
                **event, "_new": True,
                "_additional_events": rotation_events,
            }
        current = database.execute(
            """SELECT * FROM provider_limit_events
                WHERE episode_id=? AND kind='hard_limit'
                ORDER BY observed_at DESC LIMIT 1""",
            (episode["episode_id"],),).fetchone()
        return ({
                    **_event_payload(dict(current)), "_new": False,
                    "_additional_events": rotation_events,
                }
                if current else None)
    except Exception:
        database.execute("ROLLBACK")
        raise
