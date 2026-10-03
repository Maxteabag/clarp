"""APNs push notifications: device-token store + token-based HTTP/2 sender.

When an agent's turn completes (state_log kind == 'done'), the state watcher
calls `on_turn_done(session, persona)` which fans a "your turn" alert out to
every live iOS device token registered via POST /devices.

Two delivery paths, chosen by `Config.apns_transport()`:

- relay (the default whenever this Computer has an Audio Central credential):
  pushes go to Clarp Audio Central, which holds the developer's APNs key and
  sends to Apple. The Computer needs no .p8. See `_RelayTransport`.
- direct (development): token-based auth with a local .p8 APNs Auth Key →
  short-lived ES256 JWT, the same crypto PyJWT does for the App Store Connect
  client (see ios-native/scripts/testflight/asc.py).

Everything here is best-effort and defensive: if APNs isn't configured, or a
send fails, we log and move on — a push must never break a turn. APNs requires
HTTP/2, so we use httpx with the h2 backend.
"""
from __future__ import annotations

import pathlib
import json
import re
import hashlib
import ipaddress
import collections
import threading
import weakref
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

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
# Weak values: an entry exists only while some sender holds its lock (the
# `with` block keeps a strong reference), so the map does not grow by one lock
# per session ever notified. Concurrent senders still share one object.
_send_locks_guard = threading.Lock()
_send_locks: "weakref.WeakValueDictionary[str, threading.Lock]" = (
    weakref.WeakValueDictionary())


def _send_lock(session: str) -> threading.Lock:
    key = (session or "").strip() or "__global__"
    with _send_locks_guard:
        lock = _send_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _send_locks[key] = lock
        return lock


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


_PUSH_GRANT = re.compile(r"^pg1\.[0-9a-f]{64}\.[0-9a-f]{16}\.[0-9a-f]{64}$")


def _push_grant(raw: str) -> str:
    """A phone-issued Audio Central push grant, or "" when absent/malformed."""
    value = str(raw or "").strip()
    return value if _PUSH_GRANT.match(value) else ""


def register_token(token: str, session: str | None = None,
                    environment: str | None = None,
                    platform: str = "ios", base_url: str = "",
                    push_grant: str = "") -> None:
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
    grant_key = _push_grant(push_grant)
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
    # An app that sends no grant (older build, or attestation unavailable)
    # keeps any grant this token already has.
    if grant_key:
        # A grant follows the phone, not the token: after iOS rotates the
        # token, the old row would push to the same phone a second time.
        database.execute(
            """UPDATE device_tokens
                  SET disabled_at = ?
                WHERE push_grant = ?
                  AND token != ?
                  AND disabled_at IS NULL""",
            (now, grant_key, token),
        )
    database.execute(
        """INSERT INTO device_tokens
                (token, session, platform, environment, base_url, push_grant,
                 created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(token) DO UPDATE SET
                session     = excluded.session,
                platform    = excluded.platform,
                environment = excluded.environment,
                base_url    = CASE WHEN excluded.base_url != ''
                                   THEN excluded.base_url
                                   ELSE device_tokens.base_url END,
                push_grant  = CASE WHEN excluded.push_grant != ''
                                   THEN excluded.push_grant
                                   ELSE device_tokens.push_grant END,
                updated_at  = excluded.updated_at,
                disabled_at = NULL""",
        (token, session_key, platform_key, env, base_url_key, grant_key, now, now),
    )


def active_tokens() -> list[dict]:
    """Every live (non-disabled) device token."""
    from . import db
    rows = db.conn().execute(
        "SELECT token, session, environment, base_url, push_grant FROM device_tokens "
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


def _clear_grant(token: str) -> None:
    """Forget a grant the phone revoked; the row may still have another path."""
    from . import db
    db.conn().execute(
        "UPDATE device_tokens SET push_grant = '' WHERE token = ?", (token,))
    log("apnsGrantRevoked", f"{token[:12]}…")


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


_DEFAULT_BODY = "Done — your turn 👋"


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


def turn_done_payload(persona: str, session: str | None,
                      body: str | None = None, avatar_url: str | None = None,
                      avatar_custom: bool = False,
                      notification_id: str = "",
                      server_instance_id: str = "",
                      needs_response: bool = False) -> dict:
    """APNs payload for a finished-turn alert (Choice A: "your turn"). `body`,
    when given, previews what the agent just said; otherwise a generic prompt.

    `needs_response` is true only when the user has something to answer: an
    unresolved question or approval, or (from `decision_payload`) the decision
    being announced right now. Two distinct tiers, not one flat level for
    everything:

    - An ordinary reply is no more urgent than a WhatsApp message: it still
      banners, badges and threads, but arrives without a sound or vibration,
      so it respects Focus and Do Not Disturb rather than breaking through
      them.
    - A reply that leaves (or announces) something to answer plays the normal
      system alert sound. It never sets "time-sensitive": a stale pending
      request must not turn every later reply into an interruption. Only
      `decision_payload` may raise the level, for the decision itself, under
      `decision_needs_interruption`.
    """
    name = (persona or "").strip() or "Clarp"
    payload = {
        "aps": {
            "alert": {"title": name, "body": body or _DEFAULT_BODY},
            # Alert delivery and background synchronization are complementary:
            # the banner tells the user a reply arrived, while this hint gives
            # iOS a bounded chance to refresh the transcript and start its
            # speech before Clarp is opened again. APNs may throttle/drop the
            # wake, so foreground cursor recovery remains authoritative.
            "content-available": 1,
            # Deliberately "active", the ordinary messaging level. Clarp is a
            # chat app, so a reply is no more urgent than a WhatsApp message:
            # it respects Focus and Do Not Disturb rather than breaking through
            # them. "time-sensitive" is for alerts the user has asked to be
            # interrupted for, not for every finished turn.
            "interruption-level": "active",
            # Lets a Notification Service Extension rewrite the notification to
            # show the agent's avatar (WhatsApp-style). Ignored when no
            # extension is installed, so it's safe to send unconditionally.
            "mutable-content": 1,
        },
        "kind": "user-notification",
        "session": session or "",
        "persona": name,
        "needs_response": bool(needs_response),
    }
    if needs_response:
        payload["aps"]["sound"] = "default"
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
    # Live Activity pushes go to the app's dedicated liveactivity topic.
    topic = f"{bundle_id}.push-type.liveactivity" if push_type == "liveactivity" else bundle_id
    headers = {
        "authorization": f"bearer {auth}",
        "apns-topic": topic,
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


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------
class _DirectTransport:
    """Signs with the local .p8 and talks HTTP/2 to Apple."""
    name = "direct"

    def __init__(self, cfg):
        self._cfg = cfg
        self._auth = _auth_jwt(cfg)
        self._client = _pooled_client()

    def send(self, token: str, environment: str, payload: dict, *,
             push_type: str = "alert", priority: str = "10",
             collapse_id: str | None = None) -> tuple[int, str, str]:
        return _send_one(self._client, _host(environment), self._auth,
                         self._cfg.apns_bundle_id, token, payload,
                         push_type=push_type, priority=priority,
                         collapse_id=collapse_id)

    def reset(self) -> None:
        _reset_pooled_client()
        self._client = _pooled_client()


class _RelayUnavailable(Exception):
    """Audio Central could not take the push at all (down, not deployed, or
    not configured for push) — as opposed to Apple rejecting one token."""


# Tokens already bound to this Computer at Audio Central in this process,
# keyed by credential so a new credential re-registers. Registration is
# idempotent, so a restart simply re-registers each token once.
_RELAY_REGISTERED_LOCK = threading.Lock()
_RELAY_REGISTERED: set[tuple[str, str, str]] = set()
_RELAY_CLIENT_LOCK = threading.Lock()
_RELAY_CLIENT = None
_RELAY_CLIENT_CTOR = None


def _relay_client():
    global _RELAY_CLIENT, _RELAY_CLIENT_CTOR
    import httpx
    with _RELAY_CLIENT_LOCK:
        if _RELAY_CLIENT is None or _RELAY_CLIENT_CTOR is not httpx.Client:
            _close_quietly(_RELAY_CLIENT)
            # Longer than the Worker's own 10 s APNs deadline, so the relay
            # answers before we give up and a fallback cannot double-send.
            _RELAY_CLIENT = httpx.Client(timeout=20.0)
            _RELAY_CLIENT_CTOR = httpx.Client
        return _RELAY_CLIENT


def _reset_relay_state() -> None:
    """Test/ops helper: forget registrations and drop the relay connection."""
    global _RELAY_CLIENT, _RELAY_CLIENT_CTOR
    with _RELAY_REGISTERED_LOCK:
        _RELAY_REGISTERED.clear()
    with _RELAY_CLIENT_LOCK:
        client, _RELAY_CLIENT, _RELAY_CLIENT_CTOR = _RELAY_CLIENT, None, None
    _close_quietly(client)


class _RelayTransport:
    """Sends through Clarp Audio Central with this Computer's credential.

    The relay only delivers to tokens this Computer bound with
    POST /v1/push/devices, and decides production vs sandbox from that
    binding, so each token is registered (once per process) before its
    first push."""
    name = "relay"

    def __init__(self, cfg):
        self._base = cfg.audio_central_url.rstrip("/")
        self._credential = cfg.audio_central_credential
        self._credential_key = hashlib.sha256(
            self._credential.encode("utf-8")).hexdigest()[:16]
        self._headers = {"authorization": f"Bearer {self._credential}"}
        self._down: str | None = None

    def _post(self, method: str, path: str, body: dict):
        # One outage per batch: later tokens fail (or fall back) immediately
        # instead of each waiting out the timeout.
        if self._down:
            raise _RelayUnavailable(self._down)
        try:
            return self._checked(method, path, body)
        except _RelayUnavailable as e:
            self._down = str(e)
            raise

    def _checked(self, method: str, path: str, body: dict):
        try:
            resp = _relay_client().request(
                method, f"{self._base}{path}", headers=self._headers, json=body)
        except Exception as e:  # noqa: BLE001 - network failure
            raise _RelayUnavailable(f"{type(e).__name__}") from e
        if resp.status_code in (404, 405) or resp.status_code >= 500:
            raise _RelayUnavailable(f"{path} HTTP {resp.status_code}")
        return resp

    def _register(self, token: str, environment: str) -> bool:
        key = (self._credential_key, token, environment)
        with _RELAY_REGISTERED_LOCK:
            if key in _RELAY_REGISTERED:
                return True
        resp = self._post("POST", "/v1/push/devices", {
            "devices": [{"deviceToken": token, "environment": environment}]})
        if resp.status_code != 200:
            log("apnsRelayRegisterReject",
                f"status={resp.status_code} {_relay_error(resp)} token={token[:12]}…")
            return False
        with _RELAY_REGISTERED_LOCK:
            _RELAY_REGISTERED.add(key)
        return True

    def _forget(self, token: str) -> None:
        with _RELAY_REGISTERED_LOCK:
            for key in [k for k in _RELAY_REGISTERED if k[1] == token]:
                _RELAY_REGISTERED.discard(key)

    def send(self, token: str, environment: str, payload: dict, *,
             push_type: str = "alert", priority: str = "10",
             collapse_id: str | None = None) -> tuple[int, str, str]:
        environment = "sandbox" if (environment or "").lower() == "sandbox" else "production"
        notification = {
            "deviceToken": token,
            "pushType": push_type,
            "priority": priority,
            # APNs (and the relay) cap collapse ids at 64 bytes, not chars.
            "collapseId": (collapse_id or _collapse_id(payload)).encode(
                "utf-8")[:64].decode("utf-8", "ignore"),
            "payload": payload,
        }
        for attempt in range(2):
            if not self._register(token, environment):
                return 401, "RelayRegistrationRejected", ""
            resp = self._post("POST", "/v1/push/send", {"notifications": [notification]})
            if resp.status_code != 200:
                if resp.status_code == 401:
                    # A revoked credential: registrations it made are gone too.
                    self._forget(token)
                return resp.status_code, _relay_error(resp) or f"Relay{resp.status_code}", ""
            try:
                result = (resp.json().get("results") or [{}])[0]
            except Exception:  # noqa: BLE001
                return 502, "RelayBadResponse", ""
            status = int(result.get("status") or 0)
            reason = str(result.get("reason") or "")
            if reason == "RelayDeliveryFailed":
                # The relay is up but could not reach Apple: an outage of
                # the relay path, not a verdict on this token.
                self._down = "relay could not reach APNs"
                raise _RelayUnavailable(self._down)
            if reason == "DeviceNotRegistered" and attempt == 0:
                # The relay dropped the binding (revoked and reclaimed, or
                # evicted); bind again and retry once.
                self._forget(token)
                continue
            if status == 410 or reason in _DEAD_REASONS:
                self._forget(token)
            return status, reason, str(result.get("apnsId") or "")
        return 403, "DeviceNotRegistered", ""

    def reset(self) -> None:
        pass


def _relay_error(resp) -> str:
    try:
        return str(((resp.json() or {}).get("error") or {}).get("code") or "")[:64]
    except Exception:  # noqa: BLE001
        return ""


class _AutoTransport:
    """Relay first; fall back to the local .p8 when Audio Central cannot take
    pushes at all and a direct key is still configured (migration window)."""

    def __init__(self, cfg):
        self._cfg = cfg
        self._active = _RelayTransport(cfg)
        self.name = "relay"

    def send(self, token: str, environment: str, payload: dict, **kwargs) -> tuple[int, str, str]:
        try:
            return self._active.send(token, environment, payload, **kwargs)
        except _RelayUnavailable as e:
            if self._active.name != "relay" or not self._cfg.apns_direct_ready():
                raise
            log("apnsRelayFallback", f"reason={e} using=direct")
            self._active = _DirectTransport(self._cfg)
            self.name = "direct"
            return self._active.send(token, environment, payload, **kwargs)

    def reset(self) -> None:
        self._active.reset()


def _base_transport(cfg):
    """The transport for tokens without a phone grant."""
    kind = cfg.apns_transport()
    if kind == "relay":
        if cfg.apns_mode != "relay" and cfg.apns_direct_ready():
            return _AutoTransport(cfg)
        return _RelayTransport(cfg)
    if kind == "direct":
        return _DirectTransport(cfg)
    return None


class _GrantTransport:
    """Sends with a phone-issued push grant: no Computer credential and no
    Audio Central account. The grant names the phone, so no token is sent."""
    name = "grant"

    def __init__(self, cfg):
        self._base = cfg.audio_central_url.rstrip("/")
        self._down: str | None = None

    def send(self, grant: str, payload: dict, *, push_type: str = "alert",
             priority: str = "10", collapse_id: str | None = None) -> tuple[int, str, str]:
        if self._down:
            raise _RelayUnavailable(self._down)
        notification = {
            "pushType": push_type,
            "priority": priority,
            "collapseId": (collapse_id or _collapse_id(payload)).encode(
                "utf-8")[:64].decode("utf-8", "ignore"),
            "payload": payload,
        }
        try:
            resp = _relay_client().request(
                "POST", f"{self._base}/v1/push/send",
                headers={"authorization": f"Bearer {grant}"},
                json={"notifications": [notification]})
        except Exception as e:  # noqa: BLE001 - network failure
            self._down = type(e).__name__
            raise _RelayUnavailable(self._down) from e
        if resp.status_code in (404, 405) or resp.status_code >= 500:
            self._down = f"grant send HTTP {resp.status_code}"
            raise _RelayUnavailable(self._down)
        if resp.status_code == 401:
            # The phone revoked this server's grant: stop pushing to it.
            return 410, "GrantRevoked", ""
        if resp.status_code != 200:
            return resp.status_code, _relay_error(resp) or f"Relay{resp.status_code}", ""
        try:
            result = (resp.json().get("results") or [{}])[0]
        except Exception:  # noqa: BLE001
            return 502, "RelayBadResponse", ""
        reason = str(result.get("reason") or "")
        if reason == "RelayDeliveryFailed":
            self._down = "relay could not reach APNs"
            raise _RelayUnavailable(self._down)
        return int(result.get("status") or 0), reason, str(result.get("apnsId") or "")


class _Router:
    """Routes each device row: a phone grant when it has one, else this
    Computer's credential relay or local key. In auto mode a relay outage
    falls back to the local key for grant rows too."""

    def __init__(self, cfg):
        self._cfg = cfg
        self._base = _base_transport(cfg)
        self._grant = _GrantTransport(cfg) if cfg.apns_relay_url_ok() else None
        self._direct_fallback = None
        self.name = self._base.name if self._base else "grant"

    def send(self, token: str, environment: str, payload: dict, *,
             grant: str = "", **kwargs) -> tuple[int, str, str]:
        grant = _push_grant(grant)
        if grant and self._grant and self._cfg.apns_mode in ("auto", "relay"):
            self.name = "grant"
            try:
                result = self._grant.send(grant, payload, **kwargs)
                if result[1] != "GrantRevoked":
                    return result
                # The phone revoked this Host's grant. Forget it; disable the
                # token only when this Host has no other way to reach it.
                _clear_grant(token)
                if self._base is None:
                    return result
            except _RelayUnavailable as e:
                if self._cfg.apns_mode != "auto" or not self._cfg.apns_direct_ready():
                    raise
                if self._direct_fallback is None:
                    log("apnsRelayFallback", f"reason={e} using=direct")
                    self._direct_fallback = _DirectTransport(self._cfg)
                self.name = "direct"
                return self._direct_fallback.send(token, environment, payload, **kwargs)
        if self._base is None:
            return 0, "NoPushPath", ""
        self.name = self._base.name
        return self._base.send(token, environment, payload, **kwargs)

    def reset(self) -> None:
        if self._base:
            self._base.reset()
        if self._direct_fallback:
            self._direct_fallback.reset()


def _transport(cfg):
    """The transport for one batch of pushes, per `cfg.apns_transport()`."""
    if not cfg.apns_enabled():
        raise RuntimeError("push is not configured")
    return _Router(cfg)


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

    from . import desktop_presence
    if desktop_presence.active():
        log("apnsUserNotificationSuppressed", "reason=desktop-active")
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0,
                "suppressed": True, "reason": "desktop-active"}

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
    suppressed = False
    notification_id = str(notification.get("notification_id") or "")
    source_message_id = str(notification.get("source_message_id") or "")
    preview_hash = _preview_fingerprint(body)
    started = time.monotonic()
    with _send_lock(session):
        try:
            transport = _transport(cfg)
            for row in tokens:
                # A desktop may become active while this transport waited for
                # another send. Recheck immediately before each phone alert.
                if desktop_presence.active():
                    suppressed = True
                    log("apnsUserNotificationSuppressed", "reason=desktop-active")
                    break
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
                    bool(notification.get("needs_response")),
                )
                try:
                    status, reason, apns_id = transport.send(
                        tok, env, payload, grant=row.get("push_grant") or "")
                except Exception as e:  # noqa: BLE001 — one bad token shouldn't abort the batch
                    log_exception("apnsSendFail", e,
                                  detail=f"notification={notification_id} token={tok[:12]}…")
                    transport.reset()
                    failed += 1
                    continue
                log("apnsSendResult",
                    f"notification={notification_id} source={source_message_id} "
                    f"preview={preview_hash} session={session} status={status} "
                    f"via={transport.name} apns_id={apns_id or '-'} token={tok[:12]}…")
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
    result = {"enabled": True, "sent": sent, "failed": failed, "disabled": disabled}
    if suppressed:
        result.update(suppressed=True, reason="desktop-active")
    return result


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
        transport = _transport(cfg)
        for row in tokens:
            tok = row["token"]
            env = row.get("environment") or cfg.apns_environment
            try:
                status, reason, _apns_id = transport.send(
                    tok, env, payload, grant=row.get("push_grant") or "",
                    push_type="background", priority="5",
                    collapse_id=f"sync-{session}"[:64])
            except Exception as e:  # noqa: BLE001
                log_exception("apnsBackgroundSendFail", e, detail=tok[:12])
                transport.reset()
                failed += 1
                continue
            if status == 200:
                sent += 1
            elif status == 410 or reason in _DEAD_REASONS:
                disable_token(tok, reason or str(status))
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


# --------------------------------------------------------------------------
# Decision requests
# --------------------------------------------------------------------------
# A question or approval request is the one thing an agent cannot finish
# without the user. Turn-end pushes only fire when the turn ends, and an agent
# that asks mid-turn keeps working, so without this the request sits unseen
# until the user happens to open the right chat.
def decision_payload(persona: str, session: str | None, title: str, question: str,
                     *, decision_id: str, artifact_id: str,
                     time_sensitive: bool = False, text_input: bool = False,
                     revision: int = 1, expires_at: int | None = None,
                     avatar_url: str | None = None, avatar_custom: bool = False,
                     server_instance_id: str = "") -> dict:
    """APNs payload for a newly created decision. Tapping it deep-links to the
    agent's conversation, where the request card is pinned."""
    # A decision is, by definition, something the user has to answer, so it
    # always carries the alert sound; `time_sensitive` below separately
    # decides whether it also breaks through Focus.
    payload = turn_done_payload(
        persona, session, (question or title or "").strip()[:500], avatar_url,
        avatar_custom, decision_notification_id(decision_id), server_instance_id,
        needs_response=True)
    alert = payload["aps"]["alert"]
    heading = (title or "").strip()
    if heading and heading != alert["body"]:
        alert["subtitle"] = heading[:120]
    # Blocking or time-sensitive requests are the alerts the user has asked to
    # be interrupted for; an ordinary question stays at the messaging level.
    payload["aps"]["interruption-level"] = "time-sensitive" if time_sensitive else "active"
    payload["reason"] = "decision"
    if text_input:
        # The app registers this category with a text-reply action, so the
        # answer (an SMS code, say) can be typed straight from the banner.
        payload["aps"]["category"] = "input-request"
        payload["response_type"] = "text_input"
        payload["revision"] = revision
        if expires_at is not None:
            payload["expires_at"] = expires_at
    payload["decision_id"] = decision_id
    payload["artifact_id"] = artifact_id
    return payload


def decision_notification_id(decision_id: str) -> str:
    return f"decision-{decision_id}"[:64]


def decision_needs_interruption(decision: dict) -> bool:
    """Break through Focus only when the agent is blocked on this request AND
    has declared it time-sensitive (with a priority reason). Blocking alone,
    or a time-sensitive nice-to-have, is an ordinary notification."""
    return bool(decision.get("blocks_progress")) and decision.get("urgency") == "time_sensitive"


def send_decision_created(artifact: dict) -> dict:
    """Synchronously push a new decision request to all live tokens.

    Mirrors the turn-end transport: respects the per-agent push mute, stays
    quiet while a desktop is active, and prunes dead tokens. Never raises.
    """
    from . import agents as agents_db, config, desktop_presence
    cfg = config.load()
    if not cfg.apns_enabled():
        return {"enabled": False, "sent": 0, "failed": 0, "disabled": 0}
    tokens = active_tokens()
    if not tokens:
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}

    decision = artifact.get("decision") or {}
    decision_id = str(decision.get("decision_id") or "")
    artifact_id = str(artifact.get("artifact_id") or "")
    session = str(artifact.get("session") or "")
    persona = str(artifact.get("agent_name") or "Clarp")
    agent_id = str(artifact.get("agent_id") or "")
    if not decision_id or decision.get("status", "pending") != "pending":
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}
    agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
    if agent and agent.get("muted"):
        log("apnsDecisionSuppressed", f"{persona} session={session} decision={decision_id} reason=muted")
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0,
                "suppressed": True, "reason": "muted"}
    if desktop_presence.active():
        log("apnsDecisionSuppressed", f"{persona} session={session} decision={decision_id} reason=desktop-active")
        return {"enabled": True, "sent": 0, "failed": 0, "disabled": 0,
                "suppressed": True, "reason": "desktop-active"}

    title = str(artifact.get("title") or "")
    question = str(decision.get("question") or artifact.get("summary") or "")
    time_sensitive = decision_needs_interruption(decision)
    notification_id = decision_notification_id(decision_id)
    sent = failed = disabled = 0
    started = time.monotonic()
    with _send_lock(session):
        try:
            transport = _transport(cfg)
            for row in tokens:
                tok = row["token"]
                env = row.get("environment") or cfg.apns_environment
                avatar_url, avatar_custom = _avatar_details(
                    cfg, persona, agent_id, str(row.get("base_url") or ""))
                payload = decision_payload(
                    persona, session, title, question,
                    decision_id=decision_id, artifact_id=artifact_id,
                    time_sensitive=time_sensitive,
                    text_input=decision.get("response_type") == "text_input",
                    revision=int(decision.get("revision") or 1),
                    expires_at=decision.get("expires_at"),
                    avatar_url=avatar_url, avatar_custom=avatar_custom,
                    server_instance_id=_server_instance_id())
                try:
                    status, reason, apns_id = transport.send(
                        tok, env, payload, grant=row.get("push_grant") or "")
                except Exception as e:  # noqa: BLE001 — one bad token shouldn't abort the batch
                    log_exception("apnsSendFail", e,
                                  detail=f"notification={notification_id} token={tok[:12]}…")
                    transport.reset()
                    failed += 1
                    continue
                log("apnsSendResult",
                    f"notification={notification_id} decision={decision_id} "
                    f"session={session} status={status} via={transport.name} "
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
    log("apnsDecisionCreated",
        f"{persona} decision={decision_id} session={session} "
        f"time_sensitive={int(time_sensitive)} sent={sent} failed={failed} "
        f"disabled={disabled} duration_ms={int((time.monotonic() - started) * 1000)}")
    return {"enabled": True, "sent": sent, "failed": failed, "disabled": disabled}


def on_decision_created(artifact: dict) -> None:
    """Fire-and-forget push for a newly created decision. Cheap no-op when
    APNs isn't configured; never blocks or fails the creating request."""
    from . import config
    try:
        if not config.load().apns_enabled():
            return
    except Exception:  # noqa: BLE001
        return
    threading.Thread(
        target=send_decision_created, args=(dict(artifact),), daemon=True
    ).start()


def send_live_activity(payload: dict, priority: str = "5") -> dict:
    """Push one Live Activity content state to every registered activity
    token (docs/live-items.md §8). Best effort; never raises."""
    from . import config, live_activity
    cfg = config.load()
    rows = live_activity.tokens()
    if not rows or not cfg.apns_enabled():
        return {"enabled": cfg.apns_enabled(), "sent": 0, "failed": 0, "disabled": 0}
    sent = failed = disabled = 0
    try:
        transport = _transport(cfg)
        for row in rows:
            token = row["token"]
            try:
                status, reason, _apns_id = transport.send(
                    token, row.get("environment") or cfg.apns_environment, payload,
                    push_type="liveactivity", priority=priority,
                    collapse_id=f"live-activity-{row['activity_id']}"[:64])
            except Exception as e:  # noqa: BLE001
                log_exception("apnsLiveActivitySendFail", e, detail=token[:12])
                transport.reset()
                failed += 1
                continue
            if status == 200:
                sent += 1
            elif status == 410 or reason in _DEAD_REASONS:
                live_activity.unregister(token=token)
                disabled += 1
            else:
                log("apnsLiveActivityRejected", f"status={status} reason={reason}")
                failed += 1
    except Exception as e:  # noqa: BLE001
        log_exception("apnsLiveActivityFail", e)
    return {"enabled": True, "sent": sent, "failed": failed, "disabled": disabled}
