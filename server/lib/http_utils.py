"""Small HTTP helpers shared by server handlers and tests."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .log import log_exception


SECRET_QUERY_KEYS = {"token", "auth", "access_token", "refresh_token", "key", "code", "pairing_code"}


# A secret key right after any '?' or '&' anywhere in the path, also in a path a
# client malformed (a second '?', or '&' with no '?'), which the query parse
# does not see as a query parameter. Its value runs to the next separator.
_SECRET_ANYWHERE = re.compile(
    r"([?&](?:" + "|".join(sorted(map(re.escape, SECRET_QUERY_KEYS))) + r")=)[^&?#\s]*",
    re.IGNORECASE)


def redact_query_secrets(path: str) -> str:
    """Return `path` with sensitive query values replaced by REDACTED, in a
    well-formed query and after any '?' or '&' a malformed one leaves."""
    return _SECRET_ANYWHERE.sub(r"\1REDACTED", _redact_parsed_query(path))


def _redact_parsed_query(path: str) -> str:
    parts = urlsplit(path)
    if not parts.query:
        return path
    pairs = []
    changed = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SECRET_QUERY_KEYS:
            pairs.append((key, "REDACTED"))
            changed = True
        else:
            pairs.append((key, value))
    if not changed:
        return path
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(pairs, doseq=True), parts.fragment))


def send_plain_http_error(handler, code: int, message: str, *, log_event: str) -> None:
    """Reply to a hijacked/streaming request with a plain-text error and close.

    Socket failures are logged under `log_event` (with the status code as
    detail) rather than raised, since the peer may already be gone.
    """
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(message)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(message.encode("utf-8"))
    except OSError as e:
        log_exception(log_event, e, detail=str(code))


# ---- The handler/library boundary ------------------------------------------
#
# Library modules never read `handler._request_*` or call `handler._send`
# themselves. The handler builds one `Principal` per request and hands the
# library a `Responder`; the library decides and answers through those.
#
# server.Handler builds both through `principal()` and `responder()`. The
# fallbacks in `principal_of()`/`responder_of()` serve test doubles that stub
# the auth gate's attributes instead of subclassing Handler; this is the one
# place in server/lib allowed to know those attribute names.


@dataclass(frozen=True)
class Principal:
    """Who is asking, as the auth gate decided it once per request."""
    authenticated: bool = False
    device_scope: str = ""
    principal: str = ""

    @property
    def full_scope(self) -> bool:
        return self.authenticated and self.device_scope == "full" and bool(self.principal)


ANONYMOUS = Principal()


def principal_of(handler) -> Principal:
    build = getattr(handler, "principal", None)
    if callable(build):
        return build()
    return Principal(
        authenticated=bool(getattr(handler, "_request_auth_validated", False)),
        device_scope=str(getattr(handler, "_request_device_scope", "") or ""),
        principal=str(getattr(handler, "_request_principal", "") or ""),
    )


def from_an_agent(headers) -> bool:
    """Whether a request comes from an agent: it carries a turn token (``X-Clarp-Turn``, an
    agent's own turn) or names a paired peer Host (``X-Clarp-Peer``, whose agents asked). Both
    are claims the Host makes itself and never takes from outside (relay_connector.APP_HEADERS).
    A route only the user may use answers such a request ``403 {"error": "agents_may_not"}``:
    deleting or marking gallery images, hiding coordination (R178)."""
    return bool((headers.get("X-Clarp-Turn") or "").strip()
                or (headers.get("X-Clarp-Peer") or "").strip())


def require_full_scope(principal: Principal, *,
                       message: str = "This route requires full-device authentication") -> str | None:
    """The error to answer with, or None when `principal` may proceed."""
    return None if principal.full_scope else message


class Responder(Protocol):
    """What a library route needs from the HTTP layer to answer a request."""

    def send_json(self, status: int, value: Any) -> Any: ...

    def send_bytes(self, status: int, body: bytes, content_type: str, *,
                   headers: Mapping[str, str] | None = None) -> Any: ...

    def send_error(self, status: int, message: str, *, content_type: str = "application/json") -> Any: ...

    def read_json(self) -> dict | None: ...


class HandlerResponder:
    """Responder over a server.Handler. Returns what the handler's send returns
    so unit tests that stub `_send` can inspect the reply."""

    def __init__(self, handler) -> None:
        self._handler = handler

    def send_json(self, status: int, value: Any) -> Any:
        return self._handler._send(status, json.dumps(value).encode(), "application/json")

    def send_bytes(self, status: int, body: bytes, content_type: str, *,
                   headers: Mapping[str, str] | None = None) -> Any:
        if headers:
            return self._handler._send(status, body, content_type, extra_headers=dict(headers))
        return self._handler._send(status, body, content_type)

    def send_error(self, status: int, message: str, *, content_type: str = "application/json") -> Any:
        if content_type == "application/json":
            return self.send_json(status, {"error": message})
        return self._handler._send(status, message.encode("utf-8"), content_type)

    def read_json(self) -> dict | None:
        return self._handler._read_json()


def responder_of(handler) -> Responder:
    build = getattr(handler, "responder", None)
    if callable(build):
        return build()
    return HandlerResponder(handler)
