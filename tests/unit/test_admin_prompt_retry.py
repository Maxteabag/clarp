"""``clarp-admin prompt`` must survive a transient 503 from ``/send``.

2026-09-12: an agent scheduled its own continuation with ``prompt --delay 1m``;
the one-shot unit fired while the HTTP server could not reach the runtime
socket, got one 503, and the continuation was lost for good.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("prompt_admin", ROOT / "bin/clarp-admin.py")
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _http_error(request, code: int) -> HTTPError:
    return HTTPError(request.full_url, code, "boom", {}, io.BytesIO(b""))


@pytest.fixture
def transport(monkeypatch):
    """Fake urlopen driven by a list of outcomes; records every request body."""
    state = {"outcomes": [], "bodies": [], "sleeps": []}
    monkeypatch.setattr(admin, "server_connection", lambda: ("http://127.0.0.1:1", "tok"))

    def fake_urlopen(request, timeout=15):
        state["bodies"].append(json.loads(request.data) if request.data else None)
        outcome = state["outcomes"].pop(0)
        if isinstance(outcome, int):
            raise _http_error(request, outcome)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    monkeypatch.setattr(admin.urllib.request, "urlopen", fake_urlopen)
    state["sleep"] = state["sleeps"].append
    return state


def test_send_retries_503_then_succeeds_with_same_client_msg_id(transport):
    transport["outcomes"] = [503, 503, b'{"ok": true, "session": "theo"}']
    result = admin.api_request(
        "POST", "/send", {"session": "theo", "client_msg_id": "m-1"},
        retries=5, sleep=transport["sleep"])
    assert result == {"ok": True, "session": "theo"}
    assert len(transport["bodies"]) == 3
    assert {b["client_msg_id"] for b in transport["bodies"]} == {"m-1"}
    assert transport["sleeps"] == [1.0, 2.0]


def test_connection_refused_is_retried_like_a_503(transport):
    transport["outcomes"] = [URLError(ConnectionRefusedError(111, "refused")), b"{}"]
    assert admin.api_request("POST", "/send", {}, retries=1, sleep=transport["sleep"]) == {}
    assert transport["sleeps"] == [1.0]


def test_client_errors_are_not_retried(transport):
    transport["outcomes"] = [403]
    with pytest.raises(HTTPError) as info:
        admin.api_request("POST", "/send", {}, retries=5, sleep=transport["sleep"])
    assert info.value.code == 403
    assert len(transport["bodies"]) == 1
    assert transport["sleeps"] == []


def test_gives_up_after_the_retry_budget(transport):
    transport["outcomes"] = [503, 503, 503]
    with pytest.raises(HTTPError):
        admin.api_request("POST", "/send", {}, retries=2, sleep=transport["sleep"])
    assert len(transport["bodies"]) == 3
    assert transport["sleeps"] == [1.0, 2.0]


def test_default_is_a_single_attempt(transport):
    transport["outcomes"] = [503]
    with pytest.raises(HTTPError):
        admin.api_request("GET", "/health", sleep=transport["sleep"])
    assert transport["sleeps"] == []


def test_cmd_prompt_sends_with_retries_and_a_stable_client_msg_id(monkeypatch, capsys):
    seen = {}

    def fake_api(method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body, kwargs=kwargs)
        return {"ok": True}

    monkeypatch.setattr(admin, "api_request", fake_api)
    args = SimpleNamespace(to="theo", text="carry on", from_session=None,
                           origin="automation", delay=None, server=None)
    assert admin.cmd_prompt(args) == 0
    assert (seen["method"], seen["path"]) == ("POST", "/send")
    assert seen["body"]["client_msg_id"].startswith("clarp-admin-")
    assert seen["body"]["origin"] == "automation"
    assert seen["kwargs"]["retries"] == admin.SEND_RETRIES >= 3
    assert json.loads(capsys.readouterr().out) == {"ok": True}
