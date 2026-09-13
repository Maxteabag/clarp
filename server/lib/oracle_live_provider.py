"""Fixed Live provider endpoints; no caller-selected URL/model or auth fallback."""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .oracle_calls import validate_offer

API_SESSIONS = "https://api.openai.com/v1/live/sessions"
SUBSCRIPTION_CALLS = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"


class ProviderUnavailable(RuntimeError):
    def __init__(self, message, *, orphan_session_id=None):
        super().__init__(message)
        self.orphan_session_id = orphan_session_id


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credentials(mode, cfg):
    if mode == "api":
        key = cfg.openai_key()
        if not key:
            raise ProviderUnavailable("Oracle API voice requires an OpenAI key on this Host")
        return {"Authorization": "Bearer " + key}
    if mode != "subscription":
        raise ProviderUnavailable("Unsupported Oracle voice account")
    try:
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        value = json.loads((root / "auth.json").read_text())
        if value.get("auth_mode") not in ("chatgpt", "chatgptAuthTokens"):
            raise ValueError("wrong auth mode")
        token = value["tokens"]["access_token"]
        account = value["tokens"]["account_id"]
        if not all(isinstance(x, str) and x and x.strip() == x and "\n" not in x and "\r" not in x for x in (token, account)):
            raise ValueError("invalid credential")
        encoded = token.split(".")[1]
        expiry = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))["exp"]
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry < time.time() + 90:
            raise ValueError("refresh required")
    except Exception:
        # The account is owned by Codex. Never rotate or replace its credentials
        # here, and never change to the metered API on a failed subscription.
        raise ProviderUnavailable("Oracle subscription voice needs a current Codex ChatGPT login on this Host") from None
    return {"Authorization": "Bearer " + token, "chatgpt-account-id": account,
            "OpenAI-Alpha": "quicksilver=v2", "session-id": str(uuid.uuid4()),
            "thread-id": str(uuid.uuid4()), "x-session-id": str(uuid.uuid4())}


def available(mode, cfg):
    try:
        credentials(mode, cfg)
        return True
    except ProviderUnavailable:
        return False


def _attach(url, headers, *, timeout=12):
    import websocket
    connection = websocket.create_connection(url, header=headers, suppress_origin=True,
                                             timeout=timeout, enable_multithread=True)
    connection.settimeout(.25)
    return connection


def negotiate(sdp, *, wire, session, cfg, open_http=None, attach=None):
    """One paid HTTP create, then attach before returning the SDP to the phone.

    Never retry creation after an ambiguous network failure. The call manager
    retains the attempt tombstone. A failed attach gets a bounded cleanup-only
    attach attempt; an unresolved provider ID remains explicit in diagnostics.
    """
    validate_offer(sdp)
    headers = credentials(wire.mode, cfg)
    subscription = wire.mode == "subscription"
    body = {"session": session, "sdp": sdp} if subscription else {
        "session": session, "transport": {"type": "webrtc", "sdp": sdp}}
    request = Request(SUBSCRIPTION_CALLS if subscription else API_SESSIONS,
                      json.dumps(body).encode(), {**headers, "Content-Type": "application/json"})
    try:
        with (open_http or build_opener(_NoRedirect()).open)(request, timeout=25) as response:
            raw = response.read(256001)
            response_headers = {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        raise ProviderUnavailable(f"Oracle {wire.mode} voice creation failed (HTTP {exc.code})") from None
    except Exception:
        raise ProviderUnavailable("Oracle voice creation could not be confirmed; start a new attempt to retry") from None
    if len(raw) > 256000:
        raise ProviderUnavailable("Oracle provider returned an oversized session response")
    try:
        if subscription:
            answer = raw.decode()
            location = urlparse(response_headers.get("location", ""))
            if (location.netloc and location.netloc not in ("api.openai.com", "chatgpt.com")) or location.query or location.fragment:
                raise ValueError("invalid location")
            ident = response_headers.get("openai-session-id") or location.path.rsplit("/", 1)[-1]
        else:
            result = json.loads(raw)
            ident = result["session"]["id"]
            if result["transport"]["type"] != "webrtc": raise ValueError("invalid transport")
            answer = result["transport"]["sdp"]
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", ident):
            raise ValueError("invalid session identity")
        validate_offer(answer)
        if any(value.removeprefix("Bearer ") in answer for key, value in headers.items()
               if key in ("Authorization", "chatgpt-account-id")):
            raise ValueError("private credential reflected")
    except Exception:
        raise ProviderUnavailable("Oracle provider returned an invalid session contract") from None
    url = "wss://api.openai.com/v1/live/" + ident if subscription else API_SESSIONS.replace("https:", "wss:") + "/" + ident + "/attach"
    connector = attach or _attach
    try:
        connection = connector(url, headers)
    except Exception:
        cleaned = False
        cleanup = None
        try:
            cleanup = connector(url, headers, timeout=4)
            cleanup.send(json.dumps({"type": "session.close"}))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    if json.loads(cleanup.recv()).get("type") == "session.closed":
                        cleaned = True; break
                except Exception:
                    continue
        except Exception:
            pass
        finally:
            if cleanup:
                try: cleanup.close()
                except Exception: pass
        raise ProviderUnavailable("Oracle sideband unavailable; " + ("session closed" if cleaned else "session cleanup unconfirmed"),
                                  orphan_session_id=None if cleaned else ident) from None
    return {"sdp": answer, "session_id": ident, "socket": connection}
