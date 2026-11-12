"""Firebase Cloud Messaging (HTTP v1) for Android devices: the direct path.

The Android counterpart of the APNs direct path in `apns.py`. A Host with a
Google service account (``[fcm] service_account`` in config.toml) pushes to
the Android app itself:

1. sign an OAuth 2.0 JWT assertion (RS256) with the service account's key,
2. exchange it at the account's ``token_uri`` for an access token, cached
   until shortly before it expires,
3. ``POST https://fcm.googleapis.com/v1/projects/<project>/messages:send``
   with a **data-only, high-priority** message; the app draws the
   notification itself.

What deserves a push is decided in `user_notifications` / `apns` exactly as
for iPhones; this module only transports an approved push to one Android
token. `apns._Router` hands it every device row whose ``provider`` is
``fcm``. The data payload is the APNs payload flattened to strings
(`data_payload`, documented in docs/push-android.md).

Everything here is best-effort: a bad or missing service account is logged
once and turns into a failed send, never into an exception in a turn.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass

from .log import log

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# FCM caps a data message at 4096 bytes of keys and values. The JSON encoding
# measured here is larger than that count, so fitting it is conservative.
MAX_DATA_BYTES = 4096
# Bumped only when a key changes meaning; new keys do not bump it.
PAYLOAD_VERSION = "1"

# Refresh an access token this long before Google says it expires.
_TOKEN_MARGIN_SEC = 5 * 60
# One send: at most this many attempts for 429 / QUOTA_EXCEEDED / 5xx and
# network errors, waiting 1 s, then 2 s (or the server's Retry-After, capped).
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SEC = 1.0
_RETRY_AFTER_CAP_SEC = 10.0
_TIMEOUT_SEC = 10.0
# The whole of one attempt (token exchange or send): connect, TLS, writing and every read together.
# httpx's timeout is per read, so a Google that trickles a byte at a time would never trip it.
_ATTEMPT_DEADLINE_SEC = 10.0

# An FCM registration token: URL-safe base64 plus ':' (typically ~160 chars).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:\-]{20,4096}$")

# FCM refuses these data keys.
_RESERVED_KEYS = frozenset({"from", "notification", "message_type", "collapse_key"})

# Text that may be shortened to fit MAX_DATA_BYTES, in the order it is cut.
# Identifiers (session, agent_id, notification_id, ids, urls) never are.
_TRUNCATABLE = ("body", "opening_line", "subtitle", "title")

_RETRY_CODES = frozenset({"QUOTA_EXCEEDED", "UNAVAILABLE", "INTERNAL"})

_sleep = time.sleep   # tests replace this


def valid_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(str(token or "")))


# --------------------------------------------------------------------------
# Service account
# --------------------------------------------------------------------------
class ServiceAccountError(Exception):
    """The configured service account file is missing, unreadable or not a
    Google service account key."""


class FcmAuthError(Exception):
    """Google refused to issue an access token for the service account."""


@dataclass(frozen=True)
class ServiceAccount:
    project_id: str
    client_email: str
    private_key: str
    private_key_id: str
    token_uri: str
    path: str


_sa_lock = threading.Lock()
_sa_cache: tuple[tuple, ServiceAccount] | None = None


def load_service_account(path: str) -> ServiceAccount:
    """Read and check the service account JSON. Cached until the file changes."""
    global _sa_cache
    raw = str(path or "").strip()
    if not raw:
        raise ServiceAccountError("no service account configured ([fcm] service_account)")
    full = os.path.realpath(os.path.expanduser(raw))
    try:
        st = os.stat(full)
    except OSError as e:
        raise ServiceAccountError(f"service account file {raw}: {e.strerror or e}") from e
    identity = (full, st.st_mtime_ns, st.st_size)
    with _sa_lock:
        if _sa_cache and _sa_cache[0] == identity:
            return _sa_cache[1]
    try:
        with open(full, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise ServiceAccountError(f"service account file {raw}: {type(e).__name__}: {e}") from e
    if not isinstance(data, dict):
        raise ServiceAccountError(f"service account file {raw}: not a JSON object")
    if data.get("type") not in (None, "service_account"):
        raise ServiceAccountError(
            f"service account file {raw}: type is {data.get('type')!r}, not 'service_account'")
    missing = [k for k in ("project_id", "client_email", "private_key")
               if not str(data.get(k) or "").strip()]
    if missing:
        raise ServiceAccountError(f"service account file {raw}: missing {', '.join(missing)}")
    token_uri = str(data.get("token_uri") or DEFAULT_TOKEN_URI).strip()
    if not token_uri.startswith("https://"):
        raise ServiceAccountError(f"service account file {raw}: token_uri must be https")
    sa = ServiceAccount(
        project_id=str(data["project_id"]).strip(),
        client_email=str(data["client_email"]).strip(),
        private_key=str(data["private_key"]),
        private_key_id=str(data.get("private_key_id") or "").strip(),
        token_uri=token_uri,
        path=full,
    )
    with _sa_lock:
        _sa_cache = (identity, sa)
    return sa


def configured(cfg) -> bool:
    """True when the Host has a usable service account file (feature
    ``push_fcm``). Whether Google accepts it is only known at the first send."""
    path_of = getattr(cfg, "fcm_service_account_file", None)
    if not callable(path_of):
        return False          # a config without the [fcm] section's accessor
    try:
        load_service_account(path_of())
        return True
    except ServiceAccountError:
        return False
    except Exception as e:  # noqa: BLE001 - a capability probe never raises
        log("fcmConfigProbeFail", f"{type(e).__name__}: {e}")
        return False


# --------------------------------------------------------------------------
# Auth problems: logged once per distinct problem, never raised into a turn
# --------------------------------------------------------------------------
_problem_lock = threading.Lock()
_problem: str = ""


def report_auth_problem(message: str) -> None:
    """Log an auth/config problem once (again only when it changes)."""
    global _problem
    with _problem_lock:
        if message == _problem:
            return
        _problem = message
    log("fcmAuthFail", f"{message}; Android pushes are not delivered until this is fixed")


def _auth_recovered() -> None:
    global _problem
    with _problem_lock:
        if not _problem:
            return
        _problem = ""
    log("fcmAuthRecovered", "service account accepted; Android pushes resume")


def auth_problem() -> str:
    with _problem_lock:
        return _problem


# --------------------------------------------------------------------------
# OAuth access token
# --------------------------------------------------------------------------
_token_lock = threading.Lock()
_token_cache: tuple[str, float, tuple] | None = None   # (token, expires_at_epoch, identity)


def reset_token_cache() -> None:
    global _token_cache
    with _token_lock:
        _token_cache = None


def _reset_state() -> None:
    """Test/ops helper: forget tokens, the parsed account and logged problems."""
    global _sa_cache, _problem
    reset_token_cache()
    with _sa_lock:
        _sa_cache = None
    with _problem_lock:
        _problem = ""
    _reset_client()


def signed_assertion(sa: ServiceAccount, now: int | None = None) -> str:
    """The RS256 JWT assertion Google exchanges for an access token."""
    import jwt  # PyJWT[crypto]
    now = int(time.time()) if now is None else int(now)
    headers = {"kid": sa.private_key_id} if sa.private_key_id else None
    return jwt.encode(
        {"iss": sa.client_email, "scope": SCOPE, "aud": sa.token_uri,
         "iat": now, "exp": now + 3600},
        sa.private_key, algorithm="RS256", headers=headers)


def access_token(sa: ServiceAccount, client) -> str:
    """A cached OAuth access token for FCM; minted when absent or near expiry."""
    global _token_cache
    identity = (sa.path, sa.client_email, sa.private_key_id, sa.project_id, sa.token_uri)
    with _token_lock:
        now = time.time()
        if (_token_cache and _token_cache[2] == identity
                and now < _token_cache[1] - _TOKEN_MARGIN_SEC):
            return _token_cache[0]
        try:
            assertion = signed_assertion(sa, int(now))
        except Exception as e:  # noqa: BLE001 - a bad PEM is a config problem
            raise FcmAuthError(f"cannot sign with the service account key: "
                               f"{type(e).__name__}: {e}") from e
        try:
            resp = client.post(sa.token_uri, data={"grant_type": GRANT_TYPE,
                                                   "assertion": assertion})
        except Exception as e:  # noqa: BLE001
            raise FcmAuthError(f"token exchange failed: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            detail = ""
            try:
                body = resp.json() or {}
                detail = str(body.get("error_description") or body.get("error") or "")
            except Exception:  # noqa: BLE001 - the status alone is reported
                detail = str(getattr(resp, "text", "") or "")[:200]
            raise FcmAuthError(f"token exchange HTTP {resp.status_code}: {detail}".rstrip(": "))
        try:
            body = resp.json() or {}
            token = str(body["access_token"])
            expires_in = float(body.get("expires_in") or 3600)
        except Exception as e:  # noqa: BLE001
            raise FcmAuthError(f"token exchange: unreadable answer ({type(e).__name__})") from e
        _token_cache = (token, now + expires_in, identity)
        return token


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
_client_lock = threading.Lock()
_client = None
_client_ctor = None


class AttemptDeadline(Exception):
    """One attempt to reach Google ran out of its total time (_ATTEMPT_DEADLINE_SEC)."""


_deadline = threading.local()   # per sending thread: the monotonic time its current attempt must end by


class _attempt:
    """``with _attempt(seconds):`` bounds every network operation of the calling thread's request."""

    def __init__(self, seconds: float):
        self._seconds = seconds

    def __enter__(self):
        self._outer = getattr(_deadline, "at", None)
        _deadline.at = time.monotonic() + self._seconds
        return self

    def __exit__(self, *exc):
        _deadline.at = self._outer
        return False


def _clamp(timeout):
    """``timeout`` shortened to what is left of the current attempt; raises when nothing is left."""
    at = getattr(_deadline, "at", None)
    if at is None:
        return timeout
    left = at - time.monotonic()
    if left <= 0:
        import httpcore
        raise httpcore.ReadTimeout("FCM attempt deadline exceeded")
    return left if timeout is None else min(timeout, left)


def _deadline_backend():
    """An httpcore network backend whose every connect, TLS handshake, read and write gets only the
    time left of the calling thread's attempt, so a slow or trickling Google is bounded in total."""
    import httpcore
    from httpcore._backends.sync import SyncBackend

    class Stream(httpcore.NetworkStream):
        def __init__(self, inner):
            self._inner = inner

        def read(self, max_bytes, timeout=None):
            return self._inner.read(max_bytes, _clamp(timeout))

        def write(self, buffer, timeout=None):
            return self._inner.write(buffer, _clamp(timeout))

        def close(self):
            return self._inner.close()

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            return Stream(self._inner.start_tls(ssl_context, server_hostname, _clamp(timeout)))

        def get_extra_info(self, info):
            return self._inner.get_extra_info(info)

    class Backend(httpcore.NetworkBackend):
        def __init__(self):
            self._inner = SyncBackend()

        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            return Stream(self._inner.connect_tcp(host, port, _clamp(timeout), local_address, socket_options))

        def connect_unix_socket(self, path, timeout=None, socket_options=None):
            return Stream(self._inner.connect_unix_socket(path, _clamp(timeout), socket_options))

        def sleep(self, seconds):
            return self._inner.sleep(seconds)

    return Backend()


def _transport():
    """httpx's own transport (its error mapping and TLS defaults) over the deadline backend."""
    import httpcore
    import httpx
    transport = httpx.HTTPTransport()
    transport._pool = httpcore.ConnectionPool(          # the one documented knob httpx does not expose
        ssl_context=httpx.create_ssl_context(), network_backend=_deadline_backend())
    return transport


def http_client():
    """One pooled HTTPS client for Google; rebuilt when httpx.Client changes
    (tests swap in fakes)."""
    global _client, _client_ctor
    import httpx
    with _client_lock:
        if _client is None or _client_ctor is not httpx.Client:
            old, _client = _client, httpx.Client(timeout=_TIMEOUT_SEC, transport=_transport())
            _client_ctor = httpx.Client
            _close(old)
        return _client


def _reset_client() -> None:
    global _client, _client_ctor
    with _client_lock:
        old, _client, _client_ctor = _client, None, None
    _close(old)


def _close(client) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception as e:  # noqa: BLE001
        log("fcmClientCloseFail", f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------
def _string(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _size(data: dict[str, str]) -> int:
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _fit(data: dict[str, str], key: str) -> str:
    """The longest prefix of ``data[key]`` (plus "…") that brings the encoded
    map within MAX_DATA_BYTES, or "" when even that is not enough. A binary
    search on the encoded size, since JSON escapes make bytes a poor guide."""
    text = data[key]
    lo, hi = 0, len(text) - 1        # the whole text is already known not to fit

    def fits(n: int) -> bool:
        return _size({**data, key: text[:n] + "…"}) <= MAX_DATA_BYTES

    if not fits(0):
        return ""
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def data_payload(payload: dict, context: dict | None = None, *,
                 collapse_id: str = "", sent_at_ms: int | None = None) -> dict[str, str] | None:
    """The FCM data map for one APNs-shaped payload: every value a string.

    The ``aps`` dictionary becomes ``title``, ``body``, ``subtitle``,
    ``interruption_level``, ``sound``, ``category`` and ``thread_id``; every
    other top-level key is copied (booleans as "true"/"false", numbers as
    decimal strings, null dropped). ``context`` adds what the APNs payload
    leaves out (``agent_id``, ``unread``, ``badge``) without overriding it.
    Free text is shortened to keep the encoded map within MAX_DATA_BYTES;
    identifiers are never cut. None when it cannot fit (logged by the caller).
    """
    data: dict[str, str] = {"payload_version": PAYLOAD_VERSION}
    aps = payload.get("aps") if isinstance(payload.get("aps"), dict) else {}
    alert = aps.get("alert") if isinstance(aps.get("alert"), dict) else {}
    for src, dst in (("title", "title"), ("body", "body"), ("subtitle", "subtitle")):
        if alert.get(src) not in (None, ""):
            data[dst] = str(alert[src])
    for src, dst in (("interruption-level", "interruption_level"), ("sound", "sound"),
                     ("category", "category"), ("thread-id", "thread_id")):
        if aps.get(src) not in (None, ""):
            data[dst] = _string(aps[src]) or ""
    for key, value in payload.items():
        if key == "aps":
            continue
        text = _string(value)
        if text is not None:
            data[str(key)] = text
    for key, value in (context or {}).items():
        text = _string(value)
        if text is not None and key not in data:
            data[str(key)] = text
    if collapse_id:
        data.setdefault("collapse_id", collapse_id)
    data["sent_at"] = str(int(time.time() * 1000) if sent_at_ms is None else sent_at_ms)
    for key in list(data):
        if key in _RESERVED_KEYS or key.startswith(("google", "gcm")):
            data.pop(key)
    cuttable = list(_TRUNCATABLE)
    if data.get("kind") == "agent-ring":
        cuttable.append("reason")      # a ring's reason is free text, not an id
    for key in cuttable:
        if _size(data) <= MAX_DATA_BYTES:
            break
        if data.get(key):
            data[key] = _fit(data, key)
    return data if _size(data) <= MAX_DATA_BYTES else None


def message_body(token: str, data: dict[str, str], *, priority: str = "10",
                 collapse_key: str = "") -> dict:
    """The ``messages:send`` request body: data only, no ``notification``."""
    android: dict = {"priority": "HIGH" if str(priority) == "10" else "NORMAL"}
    if collapse_key:
        android["collapse_key"] = collapse_key
    return {"message": {"token": token, "data": data, "android": android}}


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------
def _error(resp) -> tuple[str, str, str, frozenset]:
    """(FCM errorCode, google.rpc status, message, invalid fields) from an error answer.

    The FCM v1 error body carries a ``google.firebase.fcm.v1.FcmError`` detail with ``errorCode`` and,
    for INVALID_ARGUMENT, a ``google.rpc.BadRequest`` detail whose ``fieldViolations[].field`` names
    the invalid request field as a dotted path from the request body (``message.token``)."""
    try:
        err = (resp.json() or {}).get("error") or {}
    except Exception:  # noqa: BLE001
        return "", "", str(getattr(resp, "text", "") or "")[:200], frozenset()
    code, fields = "", set()
    for detail in err.get("details") or []:
        if not isinstance(detail, dict):
            continue
        if detail.get("errorCode") and not code:
            code = str(detail["errorCode"])
        if str(detail.get("@type") or "").endswith("google.rpc.BadRequest"):
            for violation in detail.get("fieldViolations") or []:
                if isinstance(violation, dict) and violation.get("field"):
                    fields.add(str(violation["field"]))
    return code, str(err.get("status") or ""), str(err.get("message") or "")[:200], frozenset(fields)


def _retry_after(resp) -> float:
    try:
        raw = str((getattr(resp, "headers", {}) or {}).get("retry-after") or "").strip()
        value = float(raw) if raw.isascii() and raw.isdecimal() else 0.0
    except (TypeError, ValueError, AttributeError):
        value = 0.0
    return min(value, _RETRY_AFTER_CAP_SEC)


class Transport:
    """Sends one approved push to one Android token over FCM HTTP v1.

    ``send`` returns ``(status, reason, message_name)`` in the shape the APNs
    transports use, so the batch loops in `apns` treat both alike:
    ``(200, "", name)`` delivered; ``(410, "Unregistered", "")`` a dead
    token (the caller disables it); anything else a counted failure.
    """
    name = "fcm"

    def __init__(self, cfg, client=None):
        self._cfg = cfg
        self._client = client

    def _http(self):
        if self._client is None:
            self._client = http_client()
        return self._client

    def send(self, token: str, payload: dict, *, push_type: str = "alert",
             priority: str = "10", collapse_id: str | None = None,
             context: dict | None = None, **_ignored) -> tuple[int, str, str]:
        try:
            sa = load_service_account(self._cfg.fcm_service_account_file())
        except ServiceAccountError as e:
            report_auth_problem(str(e))
            return 0, "FcmAuthUnavailable", ""
        collapse = collapse_id or _payload_collapse_id(payload)
        data = data_payload(payload, context, collapse_id=collapse)
        if data is None:
            log("fcmPayloadTooLarge",
                f"kind={payload.get('kind') or '-'} notification={payload.get('notification_id') or '-'}")
            return 0, "PayloadTooLarge", ""
        # A background sync is a hint: only the newest one per session matters.
        body = message_body(token, data, priority=priority,
                            collapse_key=collapse if push_type == "background" else "")
        url = SEND_URL.format(project=sa.project_id)
        auth_retry = True
        attempt = 0
        while True:
            attempt += 1
            try:
                with _attempt(_ATTEMPT_DEADLINE_SEC):
                    bearer = access_token(sa, self._http())
            except FcmAuthError as e:
                report_auth_problem(f"{sa.client_email}: {e}")
                return 0, "FcmAuthUnavailable", ""
            try:
                with _attempt(_ATTEMPT_DEADLINE_SEC):
                    resp = self._http().post(url, headers={"authorization": f"Bearer {bearer}"},
                                             json=body)
            except Exception as e:  # noqa: BLE001 - network: retry, then let the batch log it
                if attempt >= _MAX_ATTEMPTS:
                    raise
                log("fcmSendRetry", f"attempt={attempt} error={type(e).__name__} token={token[:12]}…")
                _sleep(_BACKOFF_BASE_SEC * 2 ** (attempt - 1))
                continue
            status = resp.status_code
            if status == 200:
                _auth_recovered()
                try:
                    name = str((resp.json() or {}).get("name") or "")
                except Exception:  # noqa: BLE001 - delivered; the id is only for the log
                    name = ""
                return 200, "", name
            code, rpc_status, message, fields = _error(resp)
            if status == 401 and auth_retry:
                # A revoked or expired access token: mint a new one once.
                auth_retry = False
                reset_token_cache()
                attempt -= 1
                continue
            if code == "UNREGISTERED" or (status == 404 and not code):
                return 410, "Unregistered", ""
            if status == 400 and "INVALID_ARGUMENT" in (code, rpc_status) and "message.token" in fields:
                # Google's docs (FCM v1 ErrorCode, "manage tokens"): INVALID_ARGUMENT is an invalid
                # registration only when the payload is otherwise valid, and its google.rpc.BadRequest
                # names the invalid field. Only a violation of message.token is a dead token. A 400
                # about the payload, or one naming no field ("Request contains an invalid
                # argument."), keeps the token and counts as a failed push.
                return 410, "InvalidRegistration", ""
            if status in (401, 403) and code != "SENDER_ID_MISMATCH":
                report_auth_problem(f"{sa.client_email}: FCM send HTTP {status} "
                                    f"{code or rpc_status}: {message}")
                return status, code or rpc_status or f"HTTP{status}", ""
            retryable = status == 429 or status >= 500 or code in _RETRY_CODES
            if retryable and attempt < _MAX_ATTEMPTS:
                wait = max(_retry_after(resp), _BACKOFF_BASE_SEC * 2 ** (attempt - 1))
                log("fcmSendRetry", f"attempt={attempt} status={status} code={code or '-'} "
                                    f"wait={wait:g}s token={token[:12]}…")
                _sleep(wait)
                continue
            log("fcmSendReject", f"status={status} code={code or rpc_status or '-'} "
                                 f"message={message} token={token[:12]}…")
            return status, code or rpc_status or f"HTTP{status}", ""

    def reset(self) -> None:
        _reset_client()
        self._client = None


def _payload_collapse_id(payload: dict) -> str:
    """The id the app uses as notification tag: the APNs collapse id rule."""
    from .apns import _collapse_id
    return _collapse_id(payload)
