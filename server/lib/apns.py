"""APNs push notifications: device-token store + token-based HTTP/2 sender.

When an agent's turn completes (state_log kind == 'done'), the state watcher
calls `on_turn_done(session, persona)` which fans a "your turn" alert out to
every live iOS device token registered via POST /devices.

Auth is token-based (a .p8 APNs Auth Key → short-lived ES256 JWT), the same
crypto PyJWT does for the App Store Connect client (see
ios-native/scripts/testflight/asc.py). No certificates, no per-app key.

Everything here is best-effort and defensive: if APNs isn't configured, or a
send fails, we log and move on — a push must never break a turn. APNs requires
HTTP/2, so we use httpx with the h2 backend.
"""
from __future__ import annotations

import pathlib
import json
import hashlib
import ipaddress
import collections
import threading
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from .voice_markup import clean_for_display

from .log import log, log_exception

# APNs hosts. TestFlight + App Store builds use the production host; a debug
# build signed with a development profile would use sandbox.
_HOST_PRODUCTION = "https://api.push.apple.com"
_HOST_SANDBOX = "https://api.sandbox.push.apple.com"

# A provider JWT is valid 20–60 min; refresh well inside that window.
_TOKEN_TTL_SEC = 50 * 60

_jwt_lock = threading.Lock()
_jwt_cache: tuple[str, int] | None = None  # (token, minted_at_epoch)

# APNs requests are synchronous but notification classification is dispatched
# from independent daemon threads.  Serialize each conversation so an older,
# slower request can never arrive after (and collapse over) its successor.
_send_locks_guard = threading.Lock()
_send_locks: dict[str, threading.Lock] = {}


def _send_lock(session: str) -> threading.Lock:
    key = (session or "").strip() or "__global__"
    with _send_locks_guard:
        return _send_locks.setdefault(key, threading.Lock())


def _preview_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _server_instance_id() -> str:
    """Stable origin used by multi-server clients to disambiguate sessions."""
    try:
        from .server_identity import get_server_info
        return str(get_server_info().get("server_id") or "")
    except Exception:  # noqa: BLE001 - push delivery remains best effort
        return ""


# --------------------------------------------------------------------------
# Device-token store (device_tokens table, schema v20)
# --------------------------------------------------------------------------
def _device_base_url(raw: str) -> str:
    """Accept HTTPS or a private-overlay/LAN HTTP origin, never credentials."""
    value = str(raw or "").strip()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return ""
        if parsed.scheme == "https" and host:
            pass
        elif parsed.scheme == "http" and host:
            try:
                address = ipaddress.ip_address(host)
                carrier_grade_nat = address in ipaddress.ip_network("100.64.0.0/10")
                if not (address.is_private or address.is_link_local
                        or carrier_grade_nat) or address.is_loopback:
                    return ""
            except ValueError:
                if not host.lower().endswith(".local"):
                    return ""
        else:
            return ""
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    except (TypeError, ValueError):
        return ""


def register_token(token: str, session: str | None = None,
                    environment: str | None = None,
                    platform: str = "ios", base_url: str = "") -> None:
    """Upsert a device token. Re-registering clears any prior disabled flag.

    A reinstall can leave multiple live APNs tokens for the same app/session.
    Keep only the newest active token for a session+platform so one completed
    turn does not fan out duplicate pushes to the same physical device class.
    """
    from . import db
    token = (token or "").strip()
    if not token:
        raise ValueError("empty device token")
    env = (environment or "").strip().lower() or "production"
    session_key = (session or "").strip() or None
    platform_key = (platform or "ios").strip() or "ios"
    base_url_key = _device_base_url(base_url)
    now = db.now_ms()
    database = db.conn()
    if session_key:
        database.execute(
            """UPDATE device_tokens
                  SET disabled_at = ?
                WHERE session = ?
                  AND platform = ?
                  AND token != ?
                  AND disabled_at IS NULL""",
            (now, session_key, platform_key, token),
        )
    database.execute(
        """INSERT INTO device_tokens
                (token, session, platform, environment, base_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(token) DO UPDATE SET
                session     = excluded.session,
                platform    = excluded.platform,
                environment = excluded.environment,
                base_url    = CASE WHEN excluded.base_url != ''
                                   THEN excluded.base_url
                                   ELSE device_tokens.base_url END,
                updated_at  = excluded.updated_at,
                disabled_at = NULL""",
        (token, session_key, platform_key, env, base_url_key, now, now),
    )


def active_tokens() -> list[dict]:
    """Every live (non-disabled) device token."""
    from . import db
    rows = db.conn().execute(
        "SELECT token, session, environment, base_url FROM device_tokens "
        "WHERE disabled_at IS NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def disable_token(token: str, reason: str = "") -> None:
    """Mark a token dead (APNs said 410 Unregistered / BadDeviceToken)."""
    from . import db
    db.conn().execute(
        "UPDATE device_tokens SET disabled_at = ? WHERE token = ?",
        (db.now_ms(), token),
    )
    log("apnsTokenDisabled", f"{reason or 'unknown'} {token[:12]}…")


def _mark_pushed(token: str) -> None:
    from . import db
    db.conn().execute(
        "UPDATE device_tokens SET last_push_at = ? WHERE token = ?",
        (db.now_ms(), token),
    )


# --------------------------------------------------------------------------
# Auth JWT
# --------------------------------------------------------------------------
def _auth_jwt(cfg) -> str:
    """A cached ES256 provider token for APNs. Refreshed every ~50 min."""
    global _jwt_cache
    import os
    import jwt  # PyJWT
    with _jwt_lock:
        now = int(time.time())
        if _jwt_cache and now - _jwt_cache[1] < _TOKEN_TTL_SEC:
            return _jwt_cache[0]
        key_path = os.path.expanduser(cfg.apns_key_file())
        with open(key_path) as fh:
            private_key = fh.read()
        token = jwt.encode(
            {"iss": cfg.apns_team_id, "iat": now},
            private_key,
            algorithm="ES256",
            headers={"kid": cfg.apns_key_id},
        )
        _jwt_cache = (token, now)
        return token


def reset_jwt_cache() -> None:
    """Test/ops helper: force the next send to mint a fresh provider token."""
    global _jwt_cache
    with _jwt_lock:
        _jwt_cache = None


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------
def _host(environment: str) -> str:
    return _HOST_SANDBOX if (environment or "").lower() == "sandbox" else _HOST_PRODUCTION


_PREVIEW_MAX = 180
_DEFAULT_BODY = "Done — your turn 👋"
# The DONE state routinely beats the final assistant message into the messages
# table (separate ingest pipeline), so the push body must wait for a message
# from THIS turn rather than grabbing the previous one.
_SETTLE_MARGIN_MS = 2000   # tolerate the message landing slightly before DONE


def _latest_assistant_text(agent_id: str | None, not_before: int = 0,
                           *, require_spoken: bool = False) -> str | None:
    """A clean one-line preview of the agent's most recent reply, for the push
    body. None if there's nothing to show (→ caller uses the default body).

    `not_before` (epoch ms) restricts to messages ingested at/after that time —
    pass this turn's boundary so a stale previous-turn reply can't be previewed
    while the new one is still being ingested."""
    if not agent_id:
        return None
    from . import db
    try:
        row = db.conn().execute(
            "SELECT text FROM messages "
            "WHERE agent_id = ? AND role = 'assistant' AND TRIM(text) != '' "
            "AND updated_at >= ? "
            "ORDER BY updated_at DESC, seq DESC LIMIT 1",
            (agent_id, not_before),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row or not row["text"]:
        return None
    raw = str(row["text"] or "")
    if require_spoken and "<speak" not in raw.lower():
        return None
    # One canonical cleaner for every user-facing surface (see lib.voice_markup):
    # strips <speak>, drops <vox> fillers, removes <break>/<speed> SSML. Without
    # this the push body leaked raw markup that the chat already hid.
    text = clean_for_display(row["text"], oneline=True)
    if not text:
        return None
    return text[:_PREVIEW_MAX - 1] + "…" if len(text) > _PREVIEW_MAX else text


_CLIENT_LOCK = threading.Lock()
_CLIENT = None
_CLIENT_CTOR = None


def _pooled_client():
    """One long-lived HTTP/2 connection to APNs, per Apple's guidance. A fresh
    TLS+h2 handshake per notification cost ~100-300 ms and churned
    connections; APNs expects providers to hold the connection open.
    Rebuilt whenever httpx.Client itself changes (tests swap in fakes)."""
    global _CLIENT, _CLIENT_CTOR
    import httpx
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_CTOR is not httpx.Client:
            _close_quietly(_CLIENT)
            _CLIENT = httpx.Client(http2=True, timeout=10.0)
            _CLIENT_CTOR = httpx.Client
        return _CLIENT


def _close_quietly(client) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass


def _reset_pooled_client() -> None:
    global _CLIENT, _CLIENT_CTOR
    with _CLIENT_LOCK:
        client, _CLIENT, _CLIENT_CTOR = _CLIENT, None, None
    _close_quietly(client)


def _avatar_details(
    cfg,
    persona: str,
    agent_id: str = "",
    device_base_url: str = "",
) -> tuple[str | None, bool]:
    """Absolute, device-reachable URL to the agent's avatar image, for the
    Notification Service Extension to fetch when it lacks a bundled copy.
    Prefer the exact origin registered by that iPhone. A configured public
    origin remains the fallback for older clients; arbitrary public HTTP and
    credential-bearing URLs are rejected."""
    if not (persona and persona.strip()):
        return None, False
    base = _device_base_url(device_base_url) or _device_base_url(
        getattr(cfg, "public_base_url", "") or "")
    if not base:
        return None, False

    identity = str(agent_id or "").strip()
    if identity:
        from . import agents as agents_db
        from .avatar_urls import (
            avatar_content_version,
            notification_avatar_signature,
        )

        row = agents_db.get_by_agent_id(identity)
        path = pathlib.Path(str((row or {}).get("avatar_path") or ""))
        if row and path.is_file():
            version = avatar_content_version(path)
            route = f"/avatars/{quote(identity, safe='')}"
            secret = str(getattr(cfg, "auth_token", "") or "")
            if secret:
                # APNs may hold an alert while a phone is offline. Keep the
                # image capability valid for one day, while scoping it to one
                # agent and one immutable content digest.
                expires_at = int(time.time()) + 24 * 60 * 60
                signature = notification_avatar_signature(
                    secret, identity, version, expires_at)
                route = f"/notification-avatars/{quote(identity, safe='')}"
                query = urlencode({
                    "v": version,
                    "exp": expires_at,
                    "sig": signature,
                })
            else:
                query = urlencode({"v": version})
            return f"{base}{route}?{query}", True

    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
    slug = "".join(
        character for character in persona.strip().lower()
        if character in allowed)
    return (f"{base}/static/avatars/{slug}.png", False) if slug else (None, False)


def _avatar_url(
    cfg, persona: str, agent_id: str = "", device_base_url: str = "",
) -> str | None:
    """Compatibility accessor for tests and callers interested only in URL."""
    return _avatar_details(cfg, persona, agent_id, device_base_url)[0]


def turn_done_payload(persona: str, session: str | None,
                      body: str | None = None, avatar_url: str | None = None,
                      avatar_custom: bool = False,
                      notification_id: str = "",
                      server_instance_id: str = "") -> dict:
    """APNs payload for a finished-turn alert (Choice A: "your turn"). `body`,
    when given, previews what the agent just said; otherwise a generic prompt."""
    name = (persona or "").strip() or "Clarp"
    payload = {
        "aps": {
            "alert": {"title": name, "body": body or _DEFAULT_BODY},
            "sound": "default",
            # Alert delivery and background synchronization are complementary:
            # the banner tells the user a reply arrived, while this hint gives
            # iOS a bounded chance to refresh the transcript and start its
            # speech before Clarp is opened again. APNs may throttle/drop the
            # wake, so foreground cursor recovery remains authoritative.
            "content-available": 1,
            # iOS 15+: time-sensitive breaks through Focus / idle batching so the
            # "your turn" alert lands immediately instead of up to a minute late.
            # Requires the matching app entitlement; iOS silently downgrades it to
            # "active" if that entitlement is missing.
            "interruption-level": "time-sensitive",
            # Lets a Notification Service Extension rewrite the notification to
            # show the agent's avatar (WhatsApp-style). Ignored when no
            # extension is installed, so it's safe to send unconditionally.
            "mutable-content": 1,
        },
        "kind": "user-notification",
        "session": session or "",
        "persona": name,
    }
    if notification_id:
        payload["notification_id"] = notification_id
    if server_instance_id:
        payload["server_instance_id"] = server_instance_id
    if session:
        payload["aps"]["thread-id"] = session
    if avatar_url:
        payload["avatar_url"] = avatar_url
        if avatar_custom:
            payload["avatar_custom"] = True
    return payload


def _collapse_id(payload: dict) -> str:
    # Every completed turn is a distinct notification. Reusing one collapse ID
    # per agent made APNs replace earlier legitimate messages whenever several
    # replies arrived close together. The durable notification ID still lets
    # retries of the *same* turn collapse without collapsing different turns.
    notification_id = str(payload.get("notification_id") or "").strip()
    if notification_id:
        return notification_id[:64]
    session = str(payload.get("session") or "").strip()
    if not session:
        return "turn-done"
    return f"turn-done-{session}"[:64]


def _send_one(client, base: str, auth: str, bundle_id: str,
              token: str, payload: dict, *, push_type: str = "alert",
              priority: str = "10",
              collapse_id: str | None = None) -> tuple[int, str, str]:
    """POST one notification. Returns (status_code, reason, APNs request ID).
    Background pushes MUST use push_type="background" + priority "5" (Apple
    rejects priority 10 for them)."""
    url = f"{base}/3/device/{token}"
    headers = {
        "authorization": f"bearer {auth}",
        "apns-topic": bundle_id,
        "apns-push-type": push_type,
        "apns-priority": priority,
        "apns-collapse-id": collapse_id or _collapse_id(payload),
    }
    resp = client.post(url, headers=headers, content=json.dumps(payload).encode())
    reason = ""
    if resp.status_code != 200:
        try:
            reason = (resp.json() or {}).get("reason", "")
        except Exception:  # noqa: BLE001
            reason = resp.text[:200]
    response_headers = getattr(resp, "headers", {}) or {}
    return resp.status_code, reason, str(response_headers.get("apns-id", ""))


# APNs reasons that mean "this token is permanently dead — stop sending".
_DEAD_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


def send_user_notification(notification: dict) -> dict:
    """Send APNs for an already-classified User notification.

    The notification policy has already decided badge/unread plus the
    per-agent mute override. This function only transports the approved push.
    """
    from . import config
    cfg = config.load()
    if not cfg.apns_enabled():
        return {"enabled": False, "sent": 0, "failed": 0, "disabled": 0}

    tokens = active_tokens()
    if not tokens:
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}

    if not notification.get("push"):
        log(
            "apnsUserNotificationSuppressed",
            f"{notification.get('persona') or ''} session={notification.get('session') or ''} "
            f"reason={notification.get('reason') or 'not-notifiable'}",
        )
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}

    body = str(notification.get("preview") or "").strip()
    if not body:
        log(
            "apnsUserNotificationSuppressed",
            f"{notification.get('persona') or ''} session={notification.get('session') or ''} "
            "reason=empty-preview",
        )
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}

    persona = str(notification.get("persona") or "Clarp")
    session = str(notification.get("session") or "")
    sent = failed = disabled = 0
    notification_id = str(notification.get("notification_id") or "")
    source_message_id = str(notification.get("source_message_id") or "")
    preview_hash = _preview_fingerprint(body)
    started = time.monotonic()
    with _send_lock(session):
        try:
            auth = _auth_jwt(cfg)
            client = _pooled_client()
            for row in tokens:
                tok = row["token"]
                env = row.get("environment") or cfg.apns_environment
                avatar_url, avatar_custom = _avatar_details(
                    cfg,
                    persona,
                    str(notification.get("agent_id") or ""),
                    str(row.get("base_url") or ""),
                )
                payload = turn_done_payload(
                    persona,
                    session,
                    body,
                    avatar_url,
                    avatar_custom,
                    notification_id,
                    _server_instance_id(),
                )
                try:
                    status, reason, apns_id = _send_one(
                        client, _host(env), auth, cfg.apns_bundle_id, tok, payload)
                except Exception as e:  # noqa: BLE001 — one bad token shouldn't abort the batch
                    log_exception("apnsSendFail", e,
                                  detail=f"notification={notification_id} token={tok[:12]}…")
                    _reset_pooled_client()
                    client = _pooled_client()
                    failed += 1
                    continue
                log("apnsSendResult",
                    f"notification={notification_id} source={source_message_id} "
                    f"preview={preview_hash} session={session} status={status} "
                    f"apns_id={apns_id or '-'} token={tok[:12]}…")
                if status == 200:
                    sent += 1
                    _mark_pushed(tok)
                elif status == 410 or reason in _DEAD_REASONS:
                    disable_token(tok, reason or str(status))
                    disabled += 1
                else:
                    failed += 1
                    log("apnsSendReject", f"{reason or status} token={tok[:12]}…")
        except Exception as e:  # noqa: BLE001
            log_exception("apnsBatchFail", e, detail=f"notification={notification_id}")
    log("apnsUserNotification",
        f"{persona} notification={notification_id} source={source_message_id} "
        f"preview={preview_hash} session={session} sent={sent} failed={failed} "
        f"disabled={disabled} duration_ms={int((time.monotonic() - started) * 1000)}")
    return {"enabled": True, "sent": sent, "failed": failed, "disabled": disabled}


# Background ("silent") sync pushes are hints, never delivery: iOS holds only
# the newest, discards them for force-quit apps, and throttles anything above
# roughly 2-3 per hour. Budget globally and space per session; the app's
# cursor resume on wake is what actually guarantees correctness.
_BG_BUDGET_LOCK = threading.Lock()
_BG_SENT_AT: collections.deque = collections.deque()      # monotonic seconds
_BG_LAST_BY_SESSION: dict[str, float] = {}
BG_MAX_PER_HOUR = 3
BG_MIN_SESSION_SPACING_SEC = 10 * 60


def _background_budget_allows(session: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    with _BG_BUDGET_LOCK:
        while _BG_SENT_AT and now - _BG_SENT_AT[0] > 3600:
            _BG_SENT_AT.popleft()
        if len(_BG_SENT_AT) >= BG_MAX_PER_HOUR:
            return False
        last = _BG_LAST_BY_SESSION.get(session)
        if last is not None and now - last < BG_MIN_SESSION_SPACING_SEC:
            return False
        _BG_SENT_AT.append(now)
        _BG_LAST_BY_SESSION[session] = now
        return True


def _reset_background_budget() -> None:
    with _BG_BUDGET_LOCK:
        _BG_SENT_AT.clear()
        _BG_LAST_BY_SESSION.clear()


def background_sync_payload(session: str, agent_id: str) -> dict:
    return {"aps": {"content-available": 1}, "kind": "sync",
            "session": session, "agent_id": agent_id}


def send_background_sync(session: str, agent_id: str = "") -> dict:
    """Wake a suspended app to sync its cursors (no alert). Never raises."""
    from . import config
    cfg = config.load()
    if not (cfg.apns_enabled() and getattr(cfg, "apns_background_sync", False)):
        return {"enabled": False, "sent": 0, "failed": 0, "disabled": 0}
    tokens = active_tokens()
    if not tokens:
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}
    if not _background_budget_allows(session):
        log("apnsBackgroundSkipped", f"session={session} reason=budget")
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0,
                "skipped": "budget"}
    payload = background_sync_payload(session, agent_id)
    sent = failed = disabled = 0
    try:
        auth = _auth_jwt(cfg)
        client = _pooled_client()
        for row in tokens:
            tok = row["token"]
            env = row.get("environment") or cfg.apns_environment
            try:
                status, reason, _apns_id = _send_one(
                    client, _host(env), auth, cfg.apns_bundle_id, tok, payload,
                    push_type="background", priority="5",
                    collapse_id=f"sync-{session}"[:64])
            except Exception as e:  # noqa: BLE001
                log_exception("apnsBackgroundSendFail", e, detail=tok[:12])
                _reset_pooled_client()
                client = _pooled_client()
                failed += 1
                continue
            if status == 200:
                sent += 1
            elif status == 410 or reason in ("BadDeviceToken", "Unregistered",
                                              "DeviceTokenNotForTopic"):
                disable_token(tok)
                disabled += 1
            else:
                failed += 1
    except Exception as e:  # noqa: BLE001
        log_exception("apnsBackgroundFail", e, detail=session)
    log("apnsBackgroundSync", f"session={session} sent={sent} failed={failed}")
    return {"enabled": True, "sent": sent, "failed": failed, "disabled": disabled}


def send_turn_done(session: str | None, persona: str,
                   agent_id: str | None = None, done_ts: int = 0) -> dict:
    """Synchronously push a "your turn" alert to all live tokens. The body
    previews the agent's latest reply when available.

    Returns a summary dict {enabled, sent, failed, disabled}. Never raises —
    a push must not break the turn lifecycle.
    """
    from . import config, user_notifications
    cfg = config.load()
    if not cfg.apns_enabled():
        return {"enabled": False, "sent": 0, "failed": 0, "disabled": 0}

    tokens = active_tokens()
    if not tokens:
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}

    notification = user_notifications.classify_completed_turn(
        agent_id=agent_id or "",
        session=session or "",
        persona=persona,
        done_ts=done_ts,
    )
    if not notification.get("push") and getattr(cfg, "apns_background_sync", False):
        # No alert is warranted (focused / muted), but the conversation head
        # moved: nudge a suspended app to sync so it opens current.
        return send_background_sync(session or "", agent_id or "")
    return send_user_notification(notification)


def on_turn_done(session: str | None, persona: str,
                 agent_id: str | None = None, done_ts: int = 0) -> None:
    """Fire-and-forget "your turn" push. Spawns a daemon thread so the state
    watcher's poll loop is never blocked on a network round-trip. No-op (cheap)
    when APNs isn't configured."""
    from . import config
    try:
        if not config.load().apns_enabled():
            return
    except Exception:  # noqa: BLE001
        return
    threading.Thread(
        target=send_turn_done, args=(session, persona, agent_id, done_ts), daemon=True
    ).start()


def on_user_notification(notification: dict) -> None:
    """Fire-and-forget APNs transport for a persisted User notification."""
    from . import config
    try:
        if not notification.get("push") or not config.load().apns_enabled():
            return
    except Exception:  # noqa: BLE001
        return
    threading.Thread(
        target=send_user_notification, args=(notification,), daemon=True
    ).start()
