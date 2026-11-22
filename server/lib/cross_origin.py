"""Cross-origin reads for the web app's all-Hosts list (issue 651, feature cross_origin_read).

The web app is served by one Host. To list the chats of the user's other Hosts it reads two
routes of each of them from its own origin, so the browser asks those Hosts for CORS. This
module is the whole policy, and it is deliberately narrow:

- Origins: only those on this Host's explicit list, `[server] web_read_origins` (the user's
  own web app origins, e.g. "https://studio.example.ts.net"). Matched exactly as
  scheme://host[:port], after lowering the scheme and host and dropping a default port. No
  wildcard, no "null", no suffix match. With no list configured, nothing is allowed
  cross-origin: no CORS header is ever sent and a preflight is refused.
- Routes: GET /server-info and GET /agents/snapshot only (READ_PATHS). Every other route,
  and every method but GET, stays same-origin.
- Credentials: none. The request carries a paired device's token in the Authorization header
  and is authenticated exactly as any other request (a cross-origin request is never local, so
  the administrator's token is refused and only a paired device's token is accepted; a limited
  device gets what a limited device gets). Access-Control-Allow-Credentials is never sent, so
  a browser neither sends nor reads cookies on these requests; a token in the URL is never
  accepted from a remote caller (server.py).
- Preflight: allows exactly GET, and exactly the request headers Authorization and
  X-Clarp-Client. Anything else asked for refuses the whole preflight.

Nothing is exposed to a page beyond the JSON body of those two routes and the status code:
no response header is listed in Access-Control-Expose-Headers.
"""
from __future__ import annotations

from urllib.parse import urlsplit

READ_PATHS = frozenset({"/server-info", "/agents/snapshot"})
ALLOWED_REQUEST_HEADERS = ("authorization", "x-clarp-client")
ALLOW_HEADERS_VALUE = "Authorization, X-Clarp-Client"
MAX_AGE_SECONDS = 600
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(value) -> str:
    """'scheme://host[:port]' for an http(s) origin with nothing else in it, else ''."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or text.lower() == "null" or "*" in text:
        return ""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return ""
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return ""
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        return ""
    if ":" in host:                       # an IPv6 literal keeps its brackets
        host = f"[{host}]"
    return f"{scheme}://{host}" + ("" if port in (None, _DEFAULT_PORTS[scheme]) else f":{port}")


def configured_origins(values) -> frozenset[str]:
    """The valid entries of `[server] web_read_origins`; every other entry is dropped (and said)."""
    if values is None:
        return frozenset()
    if not isinstance(values, (list, tuple)):
        _say(f"ignored: expected a list of origins, got {type(values).__name__}")
        return frozenset()
    kept, refused = set(), []
    for value in values:
        origin = normalize_origin(value)
        if origin:
            kept.add(origin)
        else:
            refused.append(repr(value)[:80])
    if refused:
        _say("refused " + ", ".join(refused) + ": an origin is http(s)://host[:port] with no path, "
             "query, wildcard or credentials")
    return frozenset(kept)


def _say(detail: str) -> None:
    from .log import log
    log("config.web_read_origins", detail)


def allowed_origin(origin_header, path: str, method: str, allowed: frozenset[str]) -> str:
    """The origin to echo in Access-Control-Allow-Origin for this request, or '' (send none).
    The header must be an origin exactly as a browser serializes it (lowercase scheme and host,
    no default port, no trailing slash) and on the list."""
    if method != "GET" or path not in READ_PATHS or not allowed or not isinstance(origin_header, str):
        return ""
    origin = normalize_origin(origin_header)
    return origin if origin and origin == origin_header.strip() and origin in allowed else ""


def preflight(headers, path: str, allowed: frozenset[str]) -> dict[str, str] | None:
    """The headers of a successful preflight for this OPTIONS request, or None (refuse it)."""
    origin = allowed_origin(headers.get("Origin"), path, "GET", allowed)
    if not origin:
        return None
    if (headers.get("Access-Control-Request-Method") or "").strip() != "GET":
        return None
    asked = [h.strip().lower() for h in (headers.get("Access-Control-Request-Headers") or "").split(",") if h.strip()]
    if any(h not in ALLOWED_REQUEST_HEADERS for h in asked):
        return None
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET",
        "Access-Control-Allow-Headers": ALLOW_HEADERS_VALUE,
        "Access-Control-Max-Age": str(MAX_AGE_SECONDS),
        "Vary": "Origin",
    }
