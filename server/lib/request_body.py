"""One cap on a request body, decided from the request line and the headers before any byte of
the body is read (issue 360, R81).

Every route that reads its body as JSON (``_read_json``) takes at most 16 MiB, four times the
largest body there is (an html_form, whose payload artifacts.py bounds at 4 MiB). A larger one is
answered 413 with one fixed sentence where the request is dispatched, before the handler reads it.
What happens to the refused body is the Host's own drain rule, the one a 401 already gets: up to
DRAINED_MOST it is read and thrown away (never parsed), so the client reads its 413; above that
nothing of it is read and the connection closes. Routes whose body is not JSON keep their own caps
where they read it. A named exception has its own number: /pairing/exchange is read before any
credential is.

A missing or non-numeric Content-Length keeps the reader's own answer, as before.

An accepted body over 2 MiB is logged with its route family and size only, so that the journals of
both Hosts show how close real traffic comes to any cap.
"""
from __future__ import annotations

import json

JSON_MOST = 16 * 1024 * 1024
LARGE = 2 * 1024 * 1024
DRAINED_MOST = 1024 * 1024      # the bound of server.Handler._drain_request_body
SENTENCE = "request body too large"
_WITH_BODY = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Not JSON: each reads its own body under its own cap (media_store.MAX_MEDIA_BYTES, /upload's
# 50 MiB, /transcribe's 64 MiB).
_BINARY = frozenset({("POST", "/media"), ("POST", "/upload"), ("POST", "/transcribe")})
NAMED = {("POST", "/pairing/exchange"): 64 * 1024}


def limit(method: str, path: str) -> int | None:
    """The most bytes a body of ``method path`` may have, or None when this cap is not its own."""
    if method not in _WITH_BODY or (method, path) in _BINARY:
        return None
    return NAMED.get((method, path), JSON_MOST)


def family(path: str) -> str:
    """The route family a log line names: the path's first segment, never an id or a query."""
    return "/" + path.lstrip("/").split("/", 1)[0]


def refused(responder, headers, method: str, path: str) -> bool:
    """Answer a body over its cap with 413 before the handler reads it; True when answered.
    ``responder`` answers (http_utils.Responder, rule 8); ``headers`` are the request's."""
    most = limit(method, path)
    if most is None:
        return False
    try:
        size = int(headers.get("Content-Length", ""))
    except ValueError:
        return False
    from .log import log
    if size > most:
        log("requestBodyTooLarge", f"family={family(path)} bytes={size}")
        # Answering drains the body first (Handler.send_response), or closes above DRAINED_MOST.
        closes = {"Connection": "close"} if size > DRAINED_MOST else None
        responder.send_bytes(413, json.dumps({"error": SENTENCE}).encode(), "application/json",
                             headers=closes)
        return True
    if size > LARGE:
        log("requestBodyLarge", f"family={family(path)} bytes={size}")
    return False
