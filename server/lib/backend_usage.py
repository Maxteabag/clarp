"""Provider usage snapshots and classified limit episodes.

Claude usage is computed from Clarp's own per-turn accounting (lib.turn_usage):
no Claude endpoint reports quota, and the statusline that used to supply it
never runs under `-p`, the mode every dispatched turn uses.

Codex usage is fetched through the same read-only ChatGPT-plan endpoint the
open-source Codex CLI uses:

* codex-rs/backend-client/src/client/rate_limit_resets.rs:27-31,49-53
* codex-rs/backend-client/src/client.rs:179-182,212-225
* codex-rs/app-server/tests/suite/v2/rate_limits.rs:112-168

Installed Codex 0.149.1 additionally exposes ``account/rateLimits/read`` and
sparse ``account/rateLimits/updated`` snapshots. OpenAI documents that Codex
has five-hour and weekly limits and that reset/credit options vary by account:
https://help.openai.com/en/articles/11369540
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import pathlib
import subprocess
import select
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlsplit

from . import db, events, server_identity, settings_store
from .clock import now_ms as _now_ms
from .log import log, log_exception


from .backend_usage_store import (  # noqa: E402,F401  (split_apply: backend-usage)
    CLAUDE, CODEX, AGY, SCHEMA_VERSION, FRESH_MS, CODEX_REFRESH_MS, CLAUDE_REFRESH_MS,
    CODEX_FRESH_MS, WARNING_THRESHOLD, WARNING_THRESHOLD_ID, _IDENTITY_SECRET_KEY, CODEX_SOURCE_REF,
    QUOTA_WINDOW_NAMES, CLAUDE_USAGE_ENDPOINT, PROVIDER_COLLECTORS, _iso_from_epoch_seconds,
    _normalize_time, _to_float, _window_from_payload, _upsert, _row, _record_unknown, _freshness,
    _decode_raw, _quota_windows, _canonical_id, _identity_secret, _private_ref, _codex_identity,
    _computer_id, _provider_instance_id, _iso_to_ms, _window_id, _event_payload,
    _insert_limit_event, _open_episode, _invalidate_rotated_auth, _observe_auth_generation,
    _retire_omitted_windows, _resolve_unknown_terminals, _latest_completed_turn_ms,
    _codex_window_observed_ms, _record_codex_five_hour, _record_codex_weekly, _response_row,
    _OPEN_LIMITS_SQL, _expired_limits, _reconcile_expired_limits, _limit_projection,
    _window_by_duration,
)

from .backend_usage_claude import (  # noqa: E402,F401  (split_apply: backend-usage)
    _claude_response_row, _claude_config_dir, _read_claude_auth,
    _claude_account_marker, _active_claude_account_ref, _claude_needs_refresh,
)
def fetch_claude_usage(*, timeout: float = 8.0) -> dict[str, Any]:
    token, _expires, plan = _read_claude_auth()
    account_scope_ref = _active_claude_account_ref(create=True)
    request = urllib.request.Request(CLAUDE_USAGE_ENDPOINT, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    windows: dict[str, Any] = {}
    session = payload.get("five_hour")
    weekly = payload.get("seven_day_oauth_apps") or payload.get("seven_day")
    # This endpoint reports utilization in percent already: 1 means 1 %, not a
    # fraction to scale up. Scaling it turned a 1 % window into a full one.
    if isinstance(session, dict):
        used = _to_float(session.get("utilization"))
        if used is not None:
            windows["five_hour"] = {
                "used_percentage": used,
                "resets_at": _normalize_time(session.get("resets_at")),
                "window_minutes": 300,
            }
    if isinstance(weekly, dict):
        used = _to_float(weekly.get("utilization"))
        if used is not None:
            windows["seven_day"] = {
                "used_percentage": used,
                "resets_at": _normalize_time(weekly.get("resets_at")),
                "window_minutes": 10_080,
            }
    if not windows:
        raise RuntimeError("Claude usage endpoint returned no usable limits")
    preferred = windows.get("five_hour") or windows.get("seven_day") or {}
    raw_payload: dict[str, Any] = {"windows": windows, "plan_type": plan}
    if account_scope_ref:
        raw_payload["account_scope_ref"] = account_scope_ref
    _upsert(CLAUDE, used_percentage=preferred.get("used_percentage"),
            resets_at=preferred.get("resets_at") or "",
            source="anthropic-oauth-usage",
            raw=raw_payload)
    return {"windows": windows, "plan_type": plan}


def _structured_provider(provider_id: str, now_ms: int) -> dict[str, Any]:
    provider_instance_id = _provider_instance_id(provider_id)
    base = {
        "provider_id": provider_id,
        "provider_instance_id": provider_instance_id,
        "freshness": "unknown",
        "observed_at": None,
        "source": {
            "kind": "unavailable",
            "detail": "No structured usage evidence for this provider",
        },
        "windows": [],
    }
    if provider_id == CLAUDE:
        row = _row(CLAUDE)
        raw = _decode_raw(row)
        windows = _quota_windows(raw)
        if not row or not windows:
            return base
        stored_claude_ref = raw.get("account_scope_ref")
        if stored_claude_ref:
            active_claude_ref = _active_claude_account_ref(create=False)
            if active_claude_ref is not None and stored_claude_ref != active_claude_ref:
                return base
        freshness = _freshness(row, now_ms)
        observed_at = int(row.get("fetched_at") or 0)
        source = {
            "kind": "provider_reported",
            "detail": row.get("source") or "claude-statusline",
        }
        projected = []
        emitted_kinds: set[str] = set()
        for source_kind, kind in (
            ("five_hour", "five_hour"), ("primary", "five_hour"),
            ("seven_day", "seven_day"), ("secondary", "seven_day"),
        ):
            if kind in emitted_kinds:
                continue
            window = windows.get(source_kind)
            if not isinstance(window, dict):
                continue
            emitted_kinds.add(kind)
            projected.append({
                "window_id": _window_id(
                    provider_instance_id=provider_instance_id,
                    auth_generation_id="claude-statusline",
                    account_scope_ref=None, window_kind=kind,
                    window_minutes=window.get("window_minutes"),
                    resets_at=window.get("resets_at")),
                "kind": kind, "scope": "account", "unit": "percent",
                "used_percentage": window.get("used_percentage"),
                "resets_at": window.get("resets_at"),
                "observed_at": _normalize_time(observed_at / 1000),
                "freshness": freshness, "source": source,
                "limit": {
                    "state": (
                        "normal" if freshness == "fresh"
                        and window.get("used_percentage") is not None
                        else "unknown"),
                    "episode_id": None,
                    "provider_limit_event_id": None,
                },
            })
        return {
            **base, "freshness": freshness,
            "observed_at": _normalize_time(observed_at / 1000),
            "source": source, "windows": projected,
        }
    if provider_id == AGY:
        row = _row(AGY)
        windows = _agy_windows(row, provider_instance_id, now_ms) if row else []
        if not windows:
            return base
        return {**base, "freshness": _freshness(row, now_ms),
                "observed_at": _normalize_time(int(row.get("fetched_at") or 0) / 1000),
                "source": windows[0]["source"], "windows": windows}
    if provider_id != CODEX:
        return base
    database = db.conn()
    active_codex = _active_codex_identity(create=False)
    if active_codex is not None:
        active_gen, active_acct = active_codex
        current_identity = database.execute(
            """SELECT auth_generation_id FROM provider_usage_windows
                WHERE provider_instance_id=?
                  AND (account_scope_ref=? OR (account_scope_ref IS NULL AND auth_generation_id=?))
                ORDER BY observed_at DESC,rowid DESC LIMIT 1""",
            (provider_instance_id, active_acct, active_gen),).fetchone()
        if not current_identity:
            return base
        target_generation = current_identity["auth_generation_id"]
    else:
        current_identity = database.execute(
            """SELECT auth_generation_id FROM provider_usage_windows
                WHERE provider_instance_id=?
                ORDER BY observed_at DESC,rowid DESC LIMIT 1""",
            (provider_instance_id,),).fetchone()
        if not current_identity:
            return base
        target_generation = current_identity["auth_generation_id"]
    rows = database.execute(
        """SELECT * FROM provider_usage_windows
            WHERE provider_instance_id=? AND auth_generation_id=?
              AND window_kind IN ('five_hour','seven_day','unknown')
            ORDER BY observed_at DESC""",
        (provider_instance_id, target_generation),).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for raw in rows:
        window = dict(raw)
        latest.setdefault(window["window_kind"], window)
    if not latest:
        return base
    projected = []
    for kind in ("five_hour", "seven_day", "unknown"):
        window = latest.get(kind)
        if not window:
            continue
        age = max(0, now_ms - int(window["observed_at"]))
        freshness = "fresh" if age <= CODEX_FRESH_MS else "stale"
        reset_ms = _iso_to_ms(window.get("resets_at"))
        if reset_ms is not None and now_ms >= reset_ms:
            freshness = "stale"
        limit = (
            _limit_projection(
                database, provider_instance_id=provider_instance_id,
                auth_generation_id=window["auth_generation_id"],
                window_id=window["window_id"], freshness=freshness,
                used_percentage=window.get("used_percentage"))
            if kind in {"five_hour", "unknown"} else {
                "state": (
                    "normal" if freshness == "fresh"
                    and window.get("used_percentage") is not None
                    else "unknown"),
                "episode_id": None,
                "provider_limit_event_id": None,
            }
        )
        source = {
            "kind": window["source_kind"],
            "detail": window["source_detail"],
        }
        projected.append({
            "window_id": window["window_id"], "kind": kind,
            "scope": "account", "unit": "percent",
            "used_percentage": window.get("used_percentage"),
            "resets_at": window.get("resets_at"),
            "observed_at": _normalize_time(int(window["observed_at"]) / 1000),
            "freshness": freshness, "source": source, "limit": limit,
        })
    newest = max(latest.values(), key=lambda item: int(item["observed_at"]))
    overall_freshness = (
        "fresh" if projected and all(
            item["freshness"] == "fresh" for item in projected)
        else "stale"
    )
    return {
        **base,
        "freshness": overall_freshness,
        "observed_at": _normalize_time(int(newest["observed_at"]) / 1000),
        "source": {
            "kind": newest["source_kind"],
            "detail": newest["source_detail"],
        },
        "windows": projected,
    }


def get_backend_usage(*, refresh_codex: bool = True,
                      force_codex: bool = False) -> dict[str, Any]:
    limit_events: list[dict[str, Any]] = []
    if force_codex or _claude_needs_refresh():
        try:
            fetch_claude_usage()
        except Exception as exc:  # noqa: BLE001
            log_exception("claudeUsageRefreshFail", exc)
            _record_unknown(CLAUDE, "anthropic-oauth-usage", str(exc))
    if refresh_codex and (force_codex or _codex_needs_refresh()):
        try:
            refreshed = fetch_codex_usage()
            limit_events.extend(refreshed.get("limit_events") or [])
        except Exception as exc:  # noqa: BLE001
            log_exception("codexUsageRefreshFail", exc)
            try:
                refreshed = fetch_codex_usage_app_server()
                limit_events.extend(refreshed.get("limit_events") or [])
            except Exception as fallback_exc:  # noqa: BLE001
                log_exception("codexUsageAppServerFail", fallback_exc)
                _record_unknown(
                    CODEX, "codex-usage-endpoint",
                    f"direct: {exc}; app-server: {fallback_exc}")
    if force_codex or _agy_needs_refresh():
        if _agy_executable():
            try:
                fetch_agy_usage()
            except Exception as exc:  # noqa: BLE001
                log_exception("agyUsageRefreshFail", exc)
                _record_unknown(AGY, "agy-usage-command", str(exc))
    now = _now_ms()
    limit_events.extend(_reconcile_expired_limits(now))
    return {
        "schema_version": SCHEMA_VERSION,
        "computer_id": _computer_id(),
        "observed_at": _normalize_time(now / 1000),
        "capability_catalog_schema_version": 2,
        "providers": {
            provider_id: _structured_provider(provider_id, now)
            for provider_id in (*PROVIDER_COLLECTORS, AGY)
        },
        "limit_events": limit_events,
        # Temporary shape-compatible adapter for existing clients.
        "backends": [
            _claude_response_row(now),
            _response_row(CODEX, now),
        ]
    }


def exhausted_backends(now_ms: int | None = None) -> dict[str, dict[str, Any]]:
    """Providers that current evidence says cannot serve a new turn.

    Reads stored observations only, so the agent snapshot can call it on every
    build. A provider is listed when a fresh window is at or past 100% or
    carries an open hard limit; stale evidence never claims exhaustion, which
    keeps a refilled or switched account from staying flagged. Absent
    providers are either usable or unknown.
    """
    now = _now_ms() if now_ms is None else now_ms
    out: dict[str, dict[str, Any]] = {}
    for provider_id in PROVIDER_COLLECTORS:
        blocking = []
        for window in _structured_provider(provider_id, now)["windows"]:
            if window.get("freshness") != "fresh":
                continue
            used = window.get("used_percentage")
            hard = (window.get("limit") or {}).get("state") == "hard_limit"
            if hard or (used is not None and float(used) >= 100.0):
                blocking.append(window)
        is_codex = provider_id == CODEX
        provider_row = _row(CODEX) if is_codex else None
        block = _fresh_codex_block(now, row=provider_row) if is_codex else None
        if not blocking and block is None:
            continue
        if is_codex:
            obs_map = (
                _codex_window_observed_ms(
                    _provider_instance_id(CODEX),
                    [str(w["window_id"]) for w in blocking if w.get("window_id")],
                )
                if blocking else {}
            )
            block_fetched = int((provider_row or {}).get("fetched_at") or 0)
            candidate_stamps = [
                *(obs_map.get(str(w.get("window_id") or ""), _iso_to_ms(w.get("observed_at")) or 0)
                  for w in blocking),
                *([block_fetched] if block is not None else []),
            ]
            min_obs_ms = min(candidate_stamps) if candidate_stamps else 0
            done_ms = _latest_completed_turn_ms(CODEX, after_ms=min_obs_ms, up_to_ms=now)
            if done_ms is not None:
                if blocking:
                    blocking = [
                        w for w in blocking
                        if obs_map.get(str(w.get("window_id") or ""), _iso_to_ms(w.get("observed_at")) or 0) >= done_ms
                    ]
                if block is not None and block_fetched < done_ms:
                    block = None
        else:
            fetched_ms = int((_row(provider_id) or {}).get("fetched_at") or 0)
            done_ms = _latest_completed_turn_ms(provider_id, after_ms=fetched_ms, up_to_ms=now)
            if done_ms is not None and fetched_ms < done_ms:
                blocking = []
        if not blocking and block is None:
            continue
        if not blocking:
            out[provider_id] = {
                "state": "exhausted", "provider_id": provider_id,
                "reason": block["reason"], "window": "unknown",
                "resets_at": block.get("resets_at"), "observed_at": None,
            }
            continue
        # The provider is usable again only once its last blocking window
        # resets; one window without a reset time makes the whole answer
        # unknown rather than optimistic.
        resets = [window.get("resets_at") for window in blocking]
        resets_at = (
            max(resets, key=lambda value: _iso_to_ms(value) or 0)
            if all(resets) else None)
        named = next(
            (w for w in blocking if w.get("resets_at") == resets_at), blocking[0])
        out[provider_id] = {
            "state": "exhausted",
            "provider_id": provider_id,
            # credits_depleted: refill the workspace. usage_limit: wait.
            "reason": block["reason"] if block else "usage_limit",
            "window": named.get("kind") or "unknown",
            # A failed turn records a limit without a reset time; the
            # provider's own statement usually has one.
            "resets_at": resets_at or (block or {}).get("resets_at"),
            "observed_at": named.get("observed_at"),
        }
    return out


class UsageRefreshWorker:
    """Keeps provider usage current so exhaustion is known before a send.

    Usage was only fetched when a client opened the usage screen, so a
    provider could be out of quota for hours while every chat looked ready.
    """

    def __init__(self, *, stream, interval_s: float = 300.0) -> None:
        self._stream = stream
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict[str, Any] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="usage-refresh")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # Let startup settle before the first provider round-trip.
        delay = 20.0
        while not self._stop.wait(delay):
            delay = self._interval_s
            self.run_once()

    def run_once(self) -> None:
        try:
            payload = get_backend_usage()
            for event in payload.get("limit_events") or []:
                events.broadcast(self._stream, events.as_event(event))
            current = {
                provider: (row["window"], row["resets_at"])
                for provider, row in exhausted_backends().items()
            }
            if self._last is not None and current != self._last:
                # Snapshot rows carry the projection; this makes clients
                # refetch them instead of learning on the next failed send.
                events.broadcast(self._stream, events.agent_roster("backend-quota"))
            self._last = current
        except Exception as exc:  # noqa: BLE001
            log_exception("usageRefreshWorkerFail", exc)


from .backend_usage_agy import (  # noqa: E402,F401  (split_apply: backend-usage)
    AGY_WINDOW_KINDS, _agy_executable, _AGY_FETCH_LOCK, _AGY_LAST_ATTEMPT_MS, fetch_agy_usage,
    _run_agy_usage, _agy_windows, _agy_needs_refresh,
)
from .backend_usage_codex import (  # noqa: E402,F401  (split_apply: backend-usage)
    _active_codex_identity, _codex_needs_refresh, _codex_home, _read_codex_auth,
    _read_codex_chatgpt_base_url, _codex_usage_url, _codex_rpc,
    fetch_codex_usage_app_server, capture_codex_rate_limits,
    _codex_block_from_payload, _fresh_codex_block, _codex_snapshot_from_payload,
    _select_rate_limit_payload, record_classified_usage_limit,
)
def fetch_codex_usage(*, timeout: float = 8.0) -> dict[str, Any]:
    """Fetch and persist Codex ChatGPT-plan usage without refreshing tokens."""
    access_token, account_id = _read_codex_auth()
    auth_generation_id, account_scope_ref = _codex_identity(
        access_token, account_id)
    url = _codex_usage_url(_read_codex_chatgpt_base_url())
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": account_id,
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError(f"codex usage auth failed: HTTP {exc.code}") from exc
        raise

    return capture_codex_rate_limits(
        payload, source_detail=(
            "/wham/usage" if "/backend-api" in url else "/api/codex/usage"),
        identity=(auth_generation_id, account_scope_ref))


