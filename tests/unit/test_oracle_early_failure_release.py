"""An Oracle v2 call that fails before it is fully open still frees the device's claim."""

import io
import threading
from types import SimpleNamespace

import pytest

from lib import config, oracle_live, oracle_live_stable, oracle_realtime


class _Conversation:
    """Just enough of a conversation for serve()'s cleanup."""

    def __init__(self, *_args, **_kwargs):
        self.taken_over = threading.Event()
        self.stop = threading.Event()
        self.closed = threading.Event()
        self.closed.set()
        self.pool = SimpleNamespace(shutdown=lambda **_kw: None)
        self.counts = {}
        self.usage = SimpleNamespace(snapshot=lambda: {"voice_estimate_usd": 0})
        self.journal = None
        self.close_sent = True
        self.revision = 0

    def send(self, _event):
        pass

    def input(self, _event):
        pass

    def checkpoint(self):
        pass


class _Upstream:
    model = voice = "fixture"

    def settimeout(self, _seconds):
        pass

    def close(self):
        raise OSError("upstream already gone")


def _handler():
    return SimpleNamespace(
        headers={"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": "fixture"},
        _request_principal="phone", _request_auth_validated=True, _request_device_scope="full",
        path="/oracle/v2?handoff=hands_free", wfile=io.BytesIO(), rfile=io.BytesIO(),
        connection=SimpleNamespace(settimeout=lambda _s: None), ctx=SimpleNamespace())


def _common(module, monkeypatch):
    monkeypatch.setattr(module.ws, "is_websocket_upgrade", lambda _h: True)
    monkeypatch.setattr(module.config, "load", lambda: config.Config(openai_api_key="fixture"))
    monkeypatch.setattr(module, "AgentTools", lambda *a, **k: SimpleNamespace(execute=lambda *a: {}))
    monkeypatch.setattr(module, "Conversation", _Conversation)
    monkeypatch.setattr(module, "_send_http_error", lambda *a: None)


@pytest.fixture(autouse=True)
def _no_leftover_claim():
    yield
    oracle_realtime._ACTIVE_PRINCIPALS.pop("phone", None)
    oracle_live._CLOSING.discard("phone")
    oracle_live_stable._CLOSING.discard("phone")


def _assert_released():
    assert "phone" not in oracle_realtime._ACTIVE_PRINCIPALS


def test_stable_identity_write_failure_releases_the_claim(monkeypatch):
    # The phone's path dies just after the 101: writing the call identity raises.
    module = oracle_live_stable
    _common(module, monkeypatch)
    monkeypatch.setattr(module.oracle_memory, "open_thread", lambda *a, **k: None)
    monkeypatch.setattr(module, "_connect_voice", lambda *a: (_Upstream(), "openai"))
    monkeypatch.setattr(module.oracle_handoffs, "bind", lambda _ctx: None)

    def register(_conversation):
        raise OSError("broken pipe")

    monkeypatch.setattr(module.oracle_handoffs, "register", register)

    module.serve(_handler())

    _assert_released()
    assert "phone" not in module._CLOSING


def test_tinkered_early_failure_releases_the_claim(monkeypatch):
    module = oracle_live
    _common(module, monkeypatch)

    def contexts():
        raise OSError("broken pipe")

    memory = SimpleNamespace(reconcile=lambda: None, thread_id="t", contexts=contexts)
    monkeypatch.setattr(module.oracle_memory, "open_thread", lambda *a, **k: memory)
    import websocket
    monkeypatch.setattr(websocket, "create_connection", lambda *a, **k: _Upstream())

    module.serve(_handler())

    _assert_released()
    assert "phone" not in module._CLOSING
