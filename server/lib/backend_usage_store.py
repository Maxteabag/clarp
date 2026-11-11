"""Moved out of server/lib/backend_usage.py by tools/refactor/split_apply.py (split 'backend-usage', recipe tools/refactor/splits/s6-backends.toml).
backend_usage.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any
from . import db, events, server_identity, settings_store
from .clock import now_ms as _now_ms


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


def _canonical_id(prefix: str, payload: dict[str, Any], *, size: int = 32) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()
    return f"{prefix}{hashlib.sha256(encoded).hexdigest()[:size]}"


def _identity_secret() -> bytes:
    stored = settings_store.set_text_if_blank(
        _IDENTITY_SECRET_KEY, secrets.token_hex(32)).strip()
    if not stored:
        raise RuntimeError("provider usage identity secret unavailable")
    return stored.encode()


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


def _event_payload(row: dict[str, Any]) -> events.Event:
    return events.provider_limit(
        schema_version=SCHEMA_VERSION,
        provider_limit_event_id=row["provider_limit_event_id"],
        episode_id=row["episode_id"],
        provider_instance_id=row["provider_instance_id"],
        provider_id=row["provider_id"],
        window_id=row["window_id"],
        kind=row["kind"],
        threshold_id=row.get("threshold_id"),
        used_percentage=row.get("used_percentage"),
        resets_at=row.get("resets_at"),
        observed_at=_normalize_time(int(row["observed_at"]) / 1000),
        freshness=row["freshness"],
        source={"kind": row["source_kind"]},
        dedupe_key=row.get("dedupe_key"),
    )


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


def _resolve_unknown_terminals(
    *, provider_instance_id: str, auth_generation_id: str, observed_at: int,
) -> list[dict[str, Any]]:
    """Close synthetic 'unknown' hard-limit episodes when an authoritative refresh
    confirms the active account is allowed and all quota windows are below 100%."""
    database = db.conn()
    generations = (
        (auth_generation_id, "auth-unknown")
        if auth_generation_id != "auth-unknown"
        else (auth_generation_id,)
    )
    placeholders = ",".join("?" for _ in generations)
    pending = database.execute(
        f"""SELECT window_id, auth_generation_id FROM provider_usage_windows
             WHERE provider_instance_id=? AND window_kind='unknown'
               AND auth_generation_id IN ({placeholders})""",
        (provider_instance_id, *generations),
    ).fetchall()
    if not pending:
        return []
    database.execute("BEGIN IMMEDIATE")
    try:
        events: list[dict[str, Any]] = []
        episodes = database.execute(
            f"""SELECT e.* FROM provider_limit_episodes e
                  JOIN provider_usage_windows w
                    ON w.provider_instance_id=e.provider_instance_id
                   AND w.auth_generation_id=e.auth_generation_id
                   AND w.window_id=e.window_id
                 WHERE e.provider_instance_id=? AND e.status='open'
                   AND w.window_kind='unknown'
                   AND e.auth_generation_id IN ({placeholders})""",
            (provider_instance_id, *generations),
        ).fetchall()
        for raw in episodes:
            episode = dict(raw)
            event = _insert_limit_event(
                database, episode=episode, kind="recovered",
                threshold_id=None, used_percentage=None, resets_at=None,
                observed_at=observed_at, freshness="fresh",
                source_kind="provider_reported",
            )
            if event:
                events.append(event)
        database.execute(
            f"""DELETE FROM provider_usage_windows
                 WHERE provider_instance_id=? AND window_kind='unknown'
                   AND auth_generation_id IN ({placeholders})""",
            (provider_instance_id, *generations),
        )
        database.execute("COMMIT")
        return events
    except Exception:
        database.execute("ROLLBACK")
        raise


def _latest_completed_turn_ms(
    provider_id: str, *, up_to_ms: int | None = None,
) -> int | None:
    """Timestamp (ms) of the latest completed turn for `provider_id`, if any."""
    if up_to_ms is None:
        row = db.conn().execute(
            """SELECT MAX(s.ts) AS latest_ts
                 FROM state_log s
                 JOIN agents a ON a.agent_id = s.agent_id
                WHERE a.backend = ? AND s.kind = 'done'""",
            (provider_id,),
        ).fetchone()
    else:
        row = db.conn().execute(
            """SELECT MAX(s.ts) AS latest_ts
                 FROM state_log s
                 JOIN agents a ON a.agent_id = s.agent_id
                WHERE a.backend = ? AND s.kind = 'done' AND s.ts <= ?""",
            (provider_id, int(up_to_ms)),
        ).fetchone()
    if not row or row["latest_ts"] is None:
        return None
    return int(row["latest_ts"])


def _codex_window_observed_ms(
    provider_instance_id: str, window_ids: list[str],
) -> dict[str, int]:
    if not window_ids:
        return {}
    placeholders = ",".join("?" for _ in window_ids)
    rows = db.conn().execute(
        f"""SELECT window_id, observed_at
              FROM provider_usage_windows
             WHERE provider_instance_id = ?
               AND window_id IN ({placeholders})""",
        (provider_instance_id, *window_ids),
    ).fetchall()
    return {str(r["window_id"]): int(r["observed_at"]) for r in rows}


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


_OPEN_LIMITS_SQL = """SELECT e.*,w.used_percentage,w.resets_at
     FROM provider_limit_episodes e
     JOIN provider_usage_windows w
       ON w.provider_instance_id=e.provider_instance_id
      AND w.auth_generation_id=e.auth_generation_id
      AND w.window_id=e.window_id
    WHERE e.status='open'
      AND e.current_kind IN ('warning','hard_limit')
      AND w.resets_at IS NOT NULL AND w.resets_at<>''"""


def _expired_limits(database, now_ms: int) -> list[dict[str, Any]]:
    expired = []
    for raw in database.execute(_OPEN_LIMITS_SQL).fetchall():
        episode = dict(raw)
        reset_ms = _iso_to_ms(episode.get("resets_at"))
        if reset_ms is not None and now_ms >= reset_ms:
            expired.append(episode)
    return expired


def _reconcile_expired_limits(now_ms: int) -> list[dict[str, Any]]:
    database = db.conn()
    # Every usage GET lands here; only an episode whose reset has passed
    # needs the write lock, so look first on the autocommit connection.
    if not _expired_limits(database, now_ms):
        return []
    events: list[dict[str, Any]] = []
    database.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the lock: a concurrent GET may have closed them.
        for episode in _expired_limits(database, now_ms):
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
