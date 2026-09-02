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
import time
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlsplit

from . import db, server_identity
from .log import log, log_exception


CLAUDE = "claude"
CODEX = "codex"
AGY = "agy"
SCHEMA_VERSION = 1
FRESH_MS = 60 * 60 * 1000
CODEX_REFRESH_MS = 5 * 60 * 1000
CLAUDE_REFRESH_MS = 5 * 60 * 1000
CODEX_FRESH_MS = 15 * 60 * 1000
WARNING_THRESHOLD = 80.0
WARNING_THRESHOLD_ID = "five_hour_80_percent"
_IDENTITY_SECRET_KEY = "provider_usage_identity_secret"
CODEX_SOURCE_REF = (
    "openai/codex codex-rs/backend-client/src/client/"
    "rate_limit_resets.rs:27-31,49-53"
)
QUOTA_WINDOW_NAMES = {
    "five_hour",
    "seven_day",
    "primary",
    "secondary",
    "individual_limit",
}
CLAUDE_USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
PROVIDER_COLLECTORS = (CLAUDE, CODEX)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_from_epoch_seconds(value: Any) -> str:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return (
        _dt.datetime.fromtimestamp(ts, tz=_dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalize_time(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return _iso_from_epoch_seconds(value)
    raw = str(value).strip()
    if not raw:
        return ""
    try:
        return _iso_from_epoch_seconds(float(raw))
    except ValueError:
        pass
    # Keep already-readable ISO timestamps from Claude. Normalize trailing UTC
    # marker only; do not reject future Claude variants with offsets.
    return raw.replace("+00:00", "Z")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _window_from_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    used = _to_float(
        payload.get("used_percentage")
        if "used_percentage" in payload
        else payload.get("used_percent")
        if "used_percent" in payload
        else payload.get("usedPercent")
    )
    resets_at = _normalize_time(
        payload.get("resets_at")
        if "resets_at" in payload
        else payload.get("reset_at")
        if "reset_at" in payload
        else payload.get("resetsAt")
    )
    minutes = payload.get("window_minutes", payload.get("windowDurationMins"))
    if minutes is None and payload.get("limit_window_seconds") is not None:
        try:
            minutes = int(payload.get("limit_window_seconds")) // 60
        except (TypeError, ValueError):
            minutes = None
    if used is None and not resets_at:
        return None
    out: dict[str, Any] = {"used_percentage": used, "resets_at": resets_at}
    if minutes is not None:
        out["window_minutes"] = minutes
    return out


def _upsert(
    backend: str,
    *,
    used_percentage: float | None,
    resets_at: str,
    source: str,
    fetched_at: int | None = None,
    raw: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    db.conn().execute(
        """
        INSERT INTO backend_usage
            (backend, used_percentage, resets_at, source, fetched_at, raw, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(backend) DO UPDATE SET
            used_percentage = excluded.used_percentage,
            resets_at = excluded.resets_at,
            source = excluded.source,
            fetched_at = excluded.fetched_at,
            raw = excluded.raw,
            error = excluded.error
        """,
        (
            backend,
            used_percentage,
            resets_at or "",
            source,
            fetched_at or _now_ms(),
            json.dumps(raw or {}, separators=(",", ":"), sort_keys=True),
            error,
        ),
    )


def _row(backend: str) -> dict[str, Any] | None:
    row = db.conn().execute(
        """
        SELECT backend, used_percentage, resets_at, source, fetched_at, raw, error
          FROM backend_usage
         WHERE backend = ?
        """,
        (backend,),
    ).fetchone()
    return dict(row) if row else None


def _record_unknown(backend: str, source: str, error: str) -> None:
    current = _row(backend)
    if current and current.get("used_percentage") is not None:
        # A failed refresh does not erase the last positive observation. Its
        # age will truthfully decay fresh -> stale; the error remains visible.
        db.conn().execute(
            "UPDATE backend_usage SET error=? WHERE backend=?",
            (error[:500], backend),)
        return
    _upsert(
        backend,
        used_percentage=None,
        resets_at="",
        source=source,
        raw={},
        error=error[:500],
    )


def _freshness(row: dict[str, Any] | None, now_ms: int) -> str:
    if not row or row.get("used_percentage") is None:
        return "unknown"
    fetched_at = int(row.get("fetched_at") or 0)
    return "fresh" if now_ms - fetched_at <= FRESH_MS else "stale"


def _decode_raw(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    try:
        raw = json.loads(row.get("raw") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _quota_windows(raw: dict[str, Any]) -> dict[str, Any]:
    windows = raw.get("windows")
    if not isinstance(windows, dict):
        return {}
    return {
        name: value
        for name, value in windows.items()
        if name in QUOTA_WINDOW_NAMES and isinstance(value, dict)
    }


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


def fetch_claude_usage(*, timeout: float = 8.0) -> dict[str, Any]:
    token, _expires, plan = _read_claude_auth()
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
    if isinstance(session, dict):
        used = _to_float(session.get("utilization"))
        if used is not None:
            windows["five_hour"] = {
                "used_percentage": used if used > 1 else used * 100,
                "resets_at": _normalize_time(session.get("resets_at")),
                "window_minutes": 300,
            }
    if isinstance(weekly, dict):
        used = _to_float(weekly.get("utilization"))
        if used is not None:
            windows["seven_day"] = {
                "used_percentage": used if used > 1 else used * 100,
                "resets_at": _normalize_time(weekly.get("resets_at")),
                "window_minutes": 10_080,
            }
    if not windows:
        raise RuntimeError("Claude usage endpoint returned no usable limits")
    preferred = windows.get("five_hour") or windows.get("seven_day") or {}
    _upsert(CLAUDE, used_percentage=preferred.get("used_percentage"),
            resets_at=preferred.get("resets_at") or "",
            source="anthropic-oauth-usage", raw={"windows": windows, "plan_type": plan})
    return {"windows": windows, "plan_type": plan}


def _canonical_id(prefix: str, payload: dict[str, Any], *, size: int = 32) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()
    return f"{prefix}{hashlib.sha256(encoded).hexdigest()[:size]}"


def _identity_secret() -> bytes:
    database = db.conn()
    candidate = secrets.token_hex(32)
    database.execute("BEGIN IMMEDIATE")
    try:
        row = database.execute(
            "SELECT value FROM settings WHERE key=?",
            (_IDENTITY_SECRET_KEY,),).fetchone()
        if row is None:
            database.execute(
                """INSERT INTO settings(key,value,updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO NOTHING""",
                (_IDENTITY_SECRET_KEY, candidate, db.now_ms()),)
        elif not str(row["value"]).strip():
            database.execute(
                "UPDATE settings SET value=?,updated_at=? "
                "WHERE key=? AND TRIM(value)=''",
                (candidate, db.now_ms(), _IDENTITY_SECRET_KEY),)
        row = database.execute(
            "SELECT value FROM settings WHERE key=?",
            (_IDENTITY_SECRET_KEY,),).fetchone()
        database.execute("COMMIT")
    except Exception:
        database.execute("ROLLBACK")
        raise
    if row is None or not str(row["value"]).strip():
        raise RuntimeError("provider usage identity secret unavailable")
    return str(row["value"]).strip().encode()


def _private_ref(kind: str, value: str) -> str:
    digest = hmac.new(
        _identity_secret(), f"{kind}\0{value}".encode(), hashlib.sha256,
    ).hexdigest()
    return f"{kind}-{digest[:24]}"


def _codex_identity(access_token: str, account_id: str) -> tuple[str, str]:
    # PQUOTA v1 is deliberately conservative: token refresh/replacement rotates
    # auth_observation_generation even for the same account. The provider gives
    # us no signed continuity evidence, so old episodes become unknown rather
    # than being silently carried across credentials.
    return (
        _private_ref("auth", f"{account_id}\0{access_token}"),
        _private_ref("account", account_id),
    )


def _computer_id() -> str:
    return server_identity.get_server_info()["server_id"]


def _provider_instance_id(provider_id: str) -> str:
    return f"{_computer_id()}:{provider_id}"


def _iso_to_ms(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return int(parsed.timestamp() * 1000)


def _window_id(
    *, provider_instance_id: str, auth_generation_id: str,
    account_scope_ref: str | None, window_kind: str,
    window_minutes: int | None, resets_at: str | None,
) -> str:
    return _canonical_id("puw-", {
        "schema_version": 1,
        "provider_instance_id": provider_instance_id,
        "auth_generation_id": auth_generation_id,
        "account_scope_ref": account_scope_ref,
        "scope_kind": "account",
        "window_kind": window_kind,
        "unit": "percent",
        "window_minutes": window_minutes,
        "reset_semantics": resets_at or "provider_reset_unknown",
    })


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "provider-limit",
        "provider_limit_event_id": row["provider_limit_event_id"],
        "episode_id": row["episode_id"],
        "provider_instance_id": row["provider_instance_id"],
        "provider_id": row["provider_id"],
        "window_id": row["window_id"],
        "kind": row["kind"],
        "threshold_id": row.get("threshold_id"),
        "used_percentage": row.get("used_percentage"),
        "resets_at": row.get("resets_at"),
        "observed_at": _normalize_time(int(row["observed_at"]) / 1000),
        "freshness": row["freshness"],
        "source": {"kind": row["source_kind"]},
        "dedupe_key": row.get("dedupe_key"),
    }


def _insert_limit_event(
    database, *, episode: dict[str, Any], kind: str,
    threshold_id: str | None, used_percentage: float | None,
    resets_at: str | None, observed_at: int, freshness: str,
    source_kind: str,
) -> dict[str, Any] | None:
    current = database.execute(
        """SELECT current_kind,threshold_id,current_event_id
            FROM provider_limit_episodes WHERE episode_id=?""",
        (episode["episode_id"],),).fetchone()
    if (current and current["current_kind"] == kind
            and current["threshold_id"] == threshold_id):
        return None
    predecessor_event_id = current["current_event_id"] if current else None
    dedupe_key = _canonical_id("pld-", {
        "schema_version": 1,
        "episode_id": episode["episode_id"],
        "provider_instance_id": episode["provider_instance_id"],
        "auth_generation_id": episode["auth_generation_id"],
        "window_id": episode["window_id"],
        "kind": kind,
        "threshold_id": threshold_id,
        "predecessor_event_id": predecessor_event_id,
    }, size=48)
    event_id = f"ple-{uuid.uuid4()}"
    cursor = database.execute(
        """INSERT OR IGNORE INTO provider_limit_events (
               provider_limit_event_id,episode_id,provider_instance_id,
               provider_id,auth_generation_id,window_id,kind,threshold_id,
               used_percentage,resets_at,observed_at,freshness,source_kind,
               dedupe_key
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id, episode["episode_id"], episode["provider_instance_id"],
         episode["provider_id"], episode["auth_generation_id"],
         episode["window_id"], kind, threshold_id, used_percentage,
         resets_at, observed_at, freshness, source_kind, dedupe_key),)
    if cursor.rowcount == 0:
        existing = database.execute(
            "SELECT provider_limit_event_id FROM provider_limit_events "
            "WHERE dedupe_key=?", (dedupe_key,),).fetchone()
        database.execute(
            """UPDATE provider_limit_episodes
                  SET current_kind=?,threshold_id=?,current_event_id=?,
                      status=CASE WHEN ?='recovered' THEN 'resolved' ELSE status END,
                      resolved_at=CASE WHEN ?='recovered' THEN ? ELSE resolved_at END
                WHERE episode_id=?""",
            (kind, threshold_id,
             existing["provider_limit_event_id"] if existing else None,
             kind, kind, observed_at, episode["episode_id"]),)
        return None
    database.execute(
        """UPDATE provider_limit_episodes
              SET current_kind=?,threshold_id=?,current_event_id=?,
                  status=CASE WHEN ?='recovered' THEN 'resolved' ELSE status END,
                  resolved_at=CASE WHEN ?='recovered' THEN ? ELSE resolved_at END
            WHERE episode_id=?""",
        (kind, threshold_id, event_id, kind, kind, observed_at,
         episode["episode_id"]),)
    return _event_payload({
        "provider_limit_event_id": event_id,
        **episode,
        "kind": kind,
        "threshold_id": threshold_id,
        "used_percentage": used_percentage,
        "resets_at": resets_at,
        "observed_at": observed_at,
        "freshness": freshness,
        "source_kind": source_kind,
        "dedupe_key": dedupe_key,
    })


def _open_episode(
    database, *, provider_instance_id: str, provider_id: str,
    auth_generation_id: str, account_scope_ref: str | None,
    window_id: str, observed_at: int, allow_account_rebind: bool = False,
) -> dict[str, Any]:
    window_clause = (
        "AND w.window_kind='five_hour'"
        if allow_account_rebind else "AND e.window_id=?")
    params: tuple[Any, ...] = (
        provider_instance_id, auth_generation_id,
        account_scope_ref, account_scope_ref,
        *((window_id,) if not allow_account_rebind else ()),
    )
    row = database.execute(
        f"""SELECT e.* FROM provider_limit_episodes e
              LEFT JOIN provider_usage_windows w
                ON w.provider_instance_id=e.provider_instance_id
               AND w.auth_generation_id=e.auth_generation_id
               AND w.window_id=e.window_id
            WHERE e.provider_instance_id=? AND e.auth_generation_id=?
              AND e.status='open' AND e.scope_kind='account'
              AND (w.account_scope_ref IS ? OR w.account_scope_ref=?)
              {window_clause}
            ORDER BY e.opened_at DESC LIMIT 1""", params).fetchone()
    if row:
        episode = dict(row)
        if episode["window_id"] != window_id:
            database.execute(
                "UPDATE provider_limit_episodes SET window_id=? WHERE episode_id=?",
                (window_id, episode["episode_id"]),)
            episode["window_id"] = window_id
        return episode
    episode = {
        "episode_id": f"plp-{uuid.uuid4()}",
        "provider_instance_id": provider_instance_id,
        "provider_id": provider_id,
        "auth_generation_id": auth_generation_id,
        "window_id": window_id,
        "scope_kind": "account",
    }
    database.execute(
        """INSERT INTO provider_limit_episodes (
               episode_id,provider_instance_id,provider_id,auth_generation_id,
               window_id,scope_kind,status,current_kind,threshold_id,opened_at,
               resolved_at,current_event_id
           ) VALUES (?,?,?,?,?,?,'open','unknown',NULL,?,NULL,NULL)""",
        (episode["episode_id"], provider_instance_id, provider_id,
         auth_generation_id, window_id, "account", observed_at),)
    return episode


def _invalidate_rotated_auth(
    database, *, provider_instance_id: str, auth_generation_id: str,
    observed_at: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    rows = database.execute(
        """SELECT * FROM provider_limit_episodes
            WHERE provider_instance_id=? AND auth_generation_id<>?
              AND status='open'""",
        (provider_instance_id, auth_generation_id),).fetchall()
    for raw in rows:
        episode = dict(raw)
        event = _insert_limit_event(
            database, episode=episode, kind="unknown", threshold_id=None,
            used_percentage=None, resets_at=None, observed_at=observed_at,
            freshness="unknown", source_kind="auth_generation_rotated")
        database.execute(
            """UPDATE provider_limit_episodes
                  SET status='invalidated',resolved_at=? WHERE episode_id=?""",
            (observed_at, episode["episode_id"]),)
        if event:
            events.append(event)
    return events


def _observe_auth_generation(
    *, provider_instance_id: str, auth_generation_id: str,
    observed_at: int,
) -> list[dict[str, Any]]:
    database = db.conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        events = _invalidate_rotated_auth(
            database, provider_instance_id=provider_instance_id,
            auth_generation_id=auth_generation_id,
            observed_at=observed_at)
        database.execute("COMMIT")
        return events
    except Exception:
        database.execute("ROLLBACK")
        raise


def _retire_omitted_windows(
    *, provider_instance_id: str, auth_generation_id: str,
    observed_kinds: set[str], observed_at: int,
) -> list[dict[str, Any]]:
    database = db.conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        rows = database.execute(
            """SELECT * FROM provider_usage_windows
                WHERE provider_instance_id=? AND auth_generation_id=?
                  AND window_kind IN ('five_hour','seven_day')""",
            (provider_instance_id, auth_generation_id),).fetchall()
        events: list[dict[str, Any]] = []
        for raw in rows:
            window = dict(raw)
            if window["window_kind"] in observed_kinds:
                continue
            episodes = database.execute(
                """SELECT * FROM provider_limit_episodes
                    WHERE provider_instance_id=? AND auth_generation_id=?
                      AND window_id=? AND status='open'""",
                (provider_instance_id, auth_generation_id,
                 window["window_id"]),).fetchall()
            for episode in episodes:
                event = _insert_limit_event(
                    database, episode=dict(episode), kind="unknown",
                    threshold_id=None, used_percentage=None, resets_at=None,
                    observed_at=observed_at, freshness="unknown",
                    source_kind="authoritative_window_omitted")
                if event:
                    events.append(event)
                database.execute(
                    """UPDATE provider_limit_episodes
                          SET status='invalidated',resolved_at=?
                        WHERE episode_id=?""",
                    (observed_at, episode["episode_id"]),)
            database.execute(
                """DELETE FROM provider_usage_windows
                    WHERE provider_instance_id=? AND auth_generation_id=?
                      AND window_id=?""",
                (provider_instance_id, auth_generation_id,
                 window["window_id"]),)
        database.execute("COMMIT")
        return events
    except Exception:
        database.execute("ROLLBACK")
        raise


def _record_codex_five_hour(
    snapshot: dict[str, Any], *, auth_generation_id: str,
    account_scope_ref: str, source_detail: str,
) -> list[dict[str, Any]]:
    primary = _window_by_duration(snapshot, 300)
    if not isinstance(primary, dict) or primary.get("window_minutes") != 300:
        return []
    used = _to_float(primary.get("used_percentage"))
    resets_at = str(primary.get("resets_at") or "") or None
    observed_at = _now_ms()
    provider_instance_id = _provider_instance_id(CODEX)
    window_id = _window_id(
        provider_instance_id=provider_instance_id,
        auth_generation_id=auth_generation_id,
        account_scope_ref=account_scope_ref,
        window_kind="five_hour", window_minutes=300,
        resets_at=resets_at)
    database = db.conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        events: list[dict[str, Any]] = []
        database.execute(
            """INSERT INTO provider_usage_windows (
                   provider_instance_id,provider_id,auth_generation_id,
                   account_scope_ref,window_id,window_kind,scope_kind,unit,
                   used_percentage,resets_at,observed_at,source_kind,source_detail
               ) VALUES (?,?,?,?,?,'five_hour','account','percent',?,?,?,?,?)
               ON CONFLICT(provider_instance_id,auth_generation_id,window_id)
               DO UPDATE SET used_percentage=excluded.used_percentage,
                   resets_at=excluded.resets_at,
                   observed_at=excluded.observed_at,
                   source_kind=excluded.source_kind,
                   source_detail=excluded.source_detail""",
            (provider_instance_id, CODEX, auth_generation_id,
             account_scope_ref, window_id, used, resets_at, observed_at,
             "provider_reported", source_detail),)
        if used is not None and used >= WARNING_THRESHOLD:
            episode = _open_episode(
                database, provider_instance_id=provider_instance_id,
                provider_id=CODEX, auth_generation_id=auth_generation_id,
                account_scope_ref=account_scope_ref, window_id=window_id,
                observed_at=observed_at, allow_account_rebind=True)
            kind = "hard_limit" if used >= 100 else "warning"
            threshold = None if kind == "hard_limit" else WARNING_THRESHOLD_ID
            event = _insert_limit_event(
                database, episode=episode, kind=kind,
                threshold_id=threshold, used_percentage=used,
                resets_at=resets_at, observed_at=observed_at,
                freshness="fresh", source_kind="provider_reported")
            if event:
                events.append(event)
        elif used is not None and used < WARNING_THRESHOLD:
            rows = database.execute(
                """SELECT e.* FROM provider_limit_episodes e
                      LEFT JOIN provider_usage_windows w
                        ON w.provider_instance_id=e.provider_instance_id
                       AND w.auth_generation_id=e.auth_generation_id
                       AND w.window_id=e.window_id
                    WHERE e.provider_instance_id=? AND e.auth_generation_id=?
                      AND e.scope_kind='account' AND e.status='open'
                      AND w.window_kind='five_hour'
                      AND (w.account_scope_ref IS ? OR w.account_scope_ref=?)""",
                (provider_instance_id, auth_generation_id,
                 account_scope_ref, account_scope_ref),).fetchall()
            for raw in rows:
                episode = dict(raw)
                if episode["window_id"] != window_id:
                    database.execute(
                        "UPDATE provider_limit_episodes SET window_id=? "
                        "WHERE episode_id=?",
                        (window_id, episode["episode_id"]),)
                    episode["window_id"] = window_id
                event = _insert_limit_event(
                    database, episode=episode, kind="recovered",
                    threshold_id=None, used_percentage=used,
                    resets_at=resets_at, observed_at=observed_at,
                    freshness="fresh", source_kind="provider_reported")
                if event:
                    events.append(event)
        database.execute("COMMIT")
        return events
    except Exception:
        database.execute("ROLLBACK")
        raise


def _record_codex_weekly(
    snapshot: dict[str, Any], *, auth_generation_id: str,
    account_scope_ref: str, source_detail: str,
) -> None:
    weekly = _window_by_duration(snapshot, 10_080)
    if (not isinstance(weekly, dict)
            or weekly.get("window_minutes") != 10_080):
        return
    observed_at = _now_ms()
    provider_instance_id = _provider_instance_id(CODEX)
    resets_at = str(weekly.get("resets_at") or "") or None
    window_id = _window_id(
        provider_instance_id=provider_instance_id,
        auth_generation_id=auth_generation_id,
        account_scope_ref=account_scope_ref,
        window_kind="seven_day", window_minutes=10_080,
        resets_at=resets_at)
    db.conn().execute(
        """INSERT INTO provider_usage_windows (
               provider_instance_id,provider_id,auth_generation_id,
               account_scope_ref,window_id,window_kind,scope_kind,unit,
               used_percentage,resets_at,observed_at,source_kind,source_detail
           ) VALUES (?,?,?,?,?,'seven_day','account','percent',?,?,?,?,?)
           ON CONFLICT(provider_instance_id,auth_generation_id,window_id)
           DO UPDATE SET used_percentage=excluded.used_percentage,
               resets_at=excluded.resets_at,observed_at=excluded.observed_at,
               source_kind=excluded.source_kind,
               source_detail=excluded.source_detail""",
        (provider_instance_id, CODEX, auth_generation_id,
         account_scope_ref, window_id,
         _to_float(weekly.get("used_percentage")), resets_at,
         observed_at, "provider_reported", source_detail),)


def _response_row(backend: str, now_ms: int) -> dict[str, Any]:
    row = _row(backend)
    raw = _decode_raw(row)
    if not row:
        return {
            "backend": backend,
            "used_percentage": None,
            "resets_at": None,
            "source": "none",
            "fetched_at": None,
            "freshness": "unknown",
            "error": "no data captured",
            "windows": {},
        }
    return {
        "backend": backend,
        "used_percentage": row.get("used_percentage"),
        "resets_at": row.get("resets_at") or None,
        "source": row.get("source") or "",
        "fetched_at": _normalize_time((row.get("fetched_at") or 0) / 1000),
        "freshness": _freshness(row, now_ms),
        "error": row.get("error") or "",
        "windows": _quota_windows(raw),
    }


def _reconcile_expired_limits(now_ms: int) -> list[dict[str, Any]]:
    database = db.conn()
    events: list[dict[str, Any]] = []
    database.execute("BEGIN IMMEDIATE")
    try:
        rows = database.execute(
            """SELECT e.*,w.used_percentage,w.resets_at
                 FROM provider_limit_episodes e
                 JOIN provider_usage_windows w
                   ON w.provider_instance_id=e.provider_instance_id
                  AND w.auth_generation_id=e.auth_generation_id
                  AND w.window_id=e.window_id
                WHERE e.status='open'
                  AND e.current_kind IN ('warning','hard_limit')
                  AND w.resets_at IS NOT NULL AND w.resets_at<>''""",
        ).fetchall()
        for raw in rows:
            episode = dict(raw)
            reset_ms = _iso_to_ms(episode.get("resets_at"))
            if reset_ms is None or now_ms < reset_ms:
                continue
            event = _insert_limit_event(
                database, episode=episode, kind="unknown",
                threshold_id=None, used_percentage=None,
                resets_at=episode.get("resets_at"), observed_at=now_ms,
                freshness="stale", source_kind="reset_passed")
            if event:
                events.append(event)
        database.execute("COMMIT")
    except Exception:
        database.execute("ROLLBACK")
        raise
    return events


def _limit_projection(
    database, *, provider_instance_id: str, auth_generation_id: str,
    window_id: str, freshness: str, used_percentage: float | None,
) -> dict[str, Any]:
    row = database.execute(
        """SELECT * FROM provider_limit_episodes
            WHERE provider_instance_id=? AND auth_generation_id=?
              AND window_id=?
            ORDER BY opened_at DESC LIMIT 1""",
        (provider_instance_id, auth_generation_id, window_id),).fetchone()
    if not row:
        state = (
            "normal" if freshness == "fresh" and used_percentage is not None
            else "unknown"
        )
        return {
            "state": state, "episode_id": None,
            "provider_limit_event_id": None,
        }
    episode = dict(row)
    state = episode.get("current_kind") or "unknown"
    if state in {"warning", "hard_limit"} and freshness != "fresh":
        state = "unknown"
    return {
        "state": state,
        "episode_id": episode["episode_id"],
        "provider_limit_event_id": episode.get("current_event_id"),
    }


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
    if provider_id != CODEX:
        return base
    database = db.conn()
    current_identity = database.execute(
        """SELECT auth_generation_id FROM provider_usage_windows
            WHERE provider_instance_id=?
            ORDER BY observed_at DESC,rowid DESC LIMIT 1""",
        (provider_instance_id,),).fetchone()
    if not current_identity:
        return base
    rows = database.execute(
        """SELECT * FROM provider_usage_windows
            WHERE provider_instance_id=? AND auth_generation_id=?
              AND window_kind IN ('five_hour','seven_day','unknown')
            ORDER BY observed_at DESC""",
        (provider_instance_id,
         current_identity["auth_generation_id"]),).fetchall()
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


def _claude_needs_refresh() -> bool:
    row = _row(CLAUDE)
    return not row or _now_ms() - int(row.get("fetched_at") or 0) > CLAUDE_REFRESH_MS


def _codex_needs_refresh() -> bool:
    row = _row(CODEX)
    if not row:
        return True
    now = _now_ms()
    structured = db.conn().execute(
        """SELECT observed_at FROM provider_usage_windows
            WHERE provider_instance_id=? AND window_kind='five_hour'
            ORDER BY observed_at DESC LIMIT 1""",
        (_provider_instance_id(CODEX),),).fetchone()
    if (not structured
            or now - int(structured["observed_at"]) > CODEX_REFRESH_MS):
        return True
    return now - int(row.get("fetched_at") or 0) > CODEX_REFRESH_MS


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
    snapshot = _codex_snapshot_from_payload(payload)
    five_hour_observed = _window_by_duration(snapshot, 300) is not None
    weekly_observed = _window_by_duration(snapshot, 10_080) is not None
    rotation_events = _observe_auth_generation(
        provider_instance_id=_provider_instance_id(CODEX),
        auth_generation_id=auth_generation_id,
        observed_at=_now_ms())
    authoritative_refresh = source_detail in {
        "/wham/usage", "/api/codex/usage", "account/rateLimits/read",
    }
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
        },
        fetched_at=legacy_fetched_at,
    )
    log(
        "codexUsageFetched",
        f"used={snapshot['used_percentage']} resets={snapshot['resets_at'] or '-'}",
    )
    snapshot["limit_events"] = rotation_events + retired_events + (
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


def _window_by_duration(
    snapshot: dict[str, Any], minutes: int,
) -> dict[str, Any] | None:
    windows = snapshot.get("windows")
    if not isinstance(windows, dict):
        return None
    for window in windows.values():
        if (isinstance(window, dict)
                and window.get("window_minutes") == minutes):
            return window
    return None


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
