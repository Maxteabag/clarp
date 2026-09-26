import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.http_utils import redact_query_secrets  # noqa: E402


def test_redacts_token_query_values_without_dropping_other_params():
    out = redact_query_secrets("/events?token=secret&x=1")

    assert out == "/events?token=REDACTED&x=1"
    assert "secret" not in out


def test_redacts_token_after_other_query_params():
    out = redact_query_secrets("/events?_=123&token=abc")

    assert out == "/events?_=123&token=REDACTED"


def test_leaves_url_without_secrets_unchanged():
    assert redact_query_secrets("/agents/snapshot?x=1") == "/agents/snapshot?x=1"


# ---- Principal / Responder: the only handler surface a library route sees ----

from types import SimpleNamespace  # noqa: E402

from lib.http_utils import (  # noqa: E402
    ANONYMOUS, HandlerResponder, Principal, principal_of, require_full_scope, responder_of,
)


def test_full_scope_needs_all_three_facts():
    assert Principal(True, "full", "dev").full_scope
    assert not Principal(False, "full", "dev").full_scope
    assert not Principal(True, "limited", "dev").full_scope
    assert not Principal(True, "full", "").full_scope
    assert not ANONYMOUS.full_scope


def test_require_full_scope_returns_the_error_or_none():
    assert require_full_scope(Principal(True, "full", "dev")) is None
    assert require_full_scope(Principal(True, "limited", "dev"), message="Oracle needs full") == "Oracle needs full"
    assert "full-device" in require_full_scope(ANONYMOUS)


def test_principal_of_prefers_the_handler_constructor_then_falls_back():
    built = Principal(True, "full", "from-handler")
    assert principal_of(SimpleNamespace(principal=lambda: built)) is built
    legacy = SimpleNamespace(_request_auth_validated=True, _request_device_scope="full",
                             _request_principal="legacy")
    assert principal_of(legacy) == Principal(True, "full", "legacy")
    assert principal_of(SimpleNamespace()) == ANONYMOUS


def test_handler_responder_encodes_json_and_returns_the_handler_result():
    sent = []
    handler = SimpleNamespace(_send=lambda status, body, mime: sent.append((status, body, mime)) or "sent",
                              _read_json=lambda: {"a": 1})
    respond = responder_of(handler)
    assert isinstance(respond, HandlerResponder)
    assert respond.send_json(200, {"ok": True}) == "sent"
    assert respond.send_error(404, "gone") == "sent"
    assert respond.send_error(403, "Forbidden", content_type="text/plain") == "sent"
    assert respond.send_bytes(200, b"\x89PNG", "image/png") == "sent"
    assert respond.read_json() == {"a": 1}
    assert sent == [(200, b'{"ok": true}', "application/json"),
                    (404, b'{"error": "gone"}', "application/json"),
                    (403, b"Forbidden", "text/plain"),
                    (200, b"\x89PNG", "image/png")]
    custom = object()
    assert responder_of(SimpleNamespace(responder=lambda: custom)) is custom
