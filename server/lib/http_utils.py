"""Small HTTP helpers shared by server handlers and tests."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .log import log_exception


SECRET_QUERY_KEYS = {"token", "auth", "access_token", "refresh_token", "key", "code", "pairing_code"}


def redact_query_secrets(path: str) -> str:
    """Return `path` with sensitive query values replaced by REDACTED."""
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
