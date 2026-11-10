"""Moved out of server/lib/agent_live_tools.py by tools/refactor/split_apply.py (split 'agent-live-tools', recipe tools/refactor/splits/s9-agent-live.toml).
agent_live_tools.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import HTTPRedirectHandler
from .agent_live_settings import COMMAND_TIMEOUT_S, ROUTER_TIMEOUT_S, _loopback_url


# Schema v3.1: regi always answers within 20 s (queued if the send is still running); the Host waits 30 s.
DELEGATE_HTTP_TIMEOUT_S = 30
CALLBACK_HTTP_TIMEOUT_S = 10
CLARP_ADMIN_REFUSED = 4         # clarp-admin prompt: the Host refused it, nothing was sent


class Unreachable(Exception):
    """The target certainly did not take the request (connection refused, no such endpoint)."""


class Unconfirmed(Exception):
    """The request may have taken effect (a timeout, a redirect, an unclear exit): never resent blindly."""


class Refused(Exception):
    """The target answered and declined (a non-2xx answer, or a 2xx whose body says it failed)."""

    def __init__(self, detail: Any, code: int | None = None, body: dict | None = None):
        super().__init__(str(detail)[:120])
        self.code, self.body = code, body or {}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Unconfirmed(f"redirect {code} refused")


def _post(url: str, body: dict, timeout: float = COMMAND_TIMEOUT_S) -> dict:
    """POST JSON to a validated loopback URL: no proxy, no redirect, outcome classified truthfully.

    A 2xx counts only when its body does not say otherwise ({"ok": false} or an "error")."""
    from urllib.error import HTTPError, URLError
    from urllib.request import ProxyHandler, Request, build_opener
    if _loopback_url(url) is None:
        raise Refused("not a loopback URL")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(65536)
    except Unconfirmed:
        raise
    except HTTPError as exc:
        if exc.code in (404, 405):
            raise Unreachable(f"HTTP {exc.code}") from None
        try:
            answer = json.loads(exc.read(65536) or b"{}")
        except (OSError, ValueError):
            answer = {}
        raise Refused(f"HTTP {exc.code}", exc.code, answer if isinstance(answer, dict) else {}) from None
    except TimeoutError as exc:
        raise Unconfirmed(type(exc).__name__) from None
    except URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            raise Unreachable("connection refused") from None
        raise Unconfirmed(str(exc.reason)[:120]) from None
    except OSError as exc:
        raise Unconfirmed(type(exc).__name__) from None
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        raise Unconfirmed("unreadable answer") from None
    if not isinstance(value, dict):
        raise Unconfirmed("answer is not a JSON object")
    return value          # 2xx: the caller reads the app-level outcome (regi: result/sent; ring: ok)


def _get(url: str, timeout: float = COMMAND_TIMEOUT_S) -> dict:
    """A read-only GET to a validated loopback URL, with the same opener rules as _post."""
    from urllib.parse import urlsplit, urlunsplit
    from urllib.request import ProxyHandler, Request, build_opener
    parts = urlsplit(url)
    if _loopback_url(urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))) is None:
        raise Refused("not a loopback URL")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(Request(url, method="GET"), timeout=timeout) as response:
        value = json.loads(response.read(65536) or b"{}")
    if not isinstance(value, dict):
        raise Unconfirmed("answer is not a JSON object")
    return value


def _openai_router(api_key: str, body: dict) -> dict:
    from urllib.request import Request, urlopen
    request = Request("https://api.openai.com/v1/responses", json.dumps(body).encode(),
                      {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    with urlopen(request, timeout=ROUTER_TIMEOUT_S) as response:
        return json.load(response)


def _find_agent(session: str) -> dict | None:
    from . import agents
    return next((a for a in agents.list_agents() if a.get("session") == session and not a.get("archived_at")), None)
