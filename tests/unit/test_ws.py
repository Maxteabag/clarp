"""WebSocket server-side helpers — handshake + minimal frame encoding.

This is the wire-protocol unit. We only do what the audio-streaming
endpoint needs: server → client binary frames, server-side close. No
client→server messages, no continuation, no extensions, no fragmentation
(we control the chunk size at the source).

RFC 6455 references in comments where the value isn't obvious.
"""
from __future__ import annotations

import base64
import hashlib
import pathlib
import sys

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))


# ---- Handshake response --------------------------------------------------


def test_handshake_response_includes_correct_accept_header():
    """RFC 6455 §4.2.2: server takes the client's Sec-WebSocket-Key,
    concatenates the magic GUID, SHA-1, base64-encodes, returns as
    Sec-WebSocket-Accept. RFC ships a worked example we can pin against."""
    from lib import ws
    # The example from the RFC itself.
    client_key = "dGhlIHNhbXBsZSBub25jZQ=="
    expected_accept = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert ws.compute_accept(client_key) == expected_accept


def test_handshake_response_is_well_formed_http():
    from lib import ws
    resp = ws.handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
    text = resp.decode("ascii")
    assert text.startswith("HTTP/1.1 101 Switching Protocols\r\n")
    assert "Upgrade: websocket\r\n" in text
    assert "Connection: Upgrade\r\n" in text
    assert "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n" in text
    assert text.endswith("\r\n\r\n")


def test_handshake_response_rejects_missing_key():
    from lib import ws
    import pytest
    with pytest.raises(ValueError):
        ws.handshake_response("")


# ---- Binary frame encoding ----------------------------------------------


def test_binary_frame_small_payload():
    """RFC 6455 §5.2: FIN=1, RSV=0, opcode=0x2 (binary). For server→client
    frames the MASK bit MUST be 0 (clients must mask, servers must not).
    Small payloads (<126 bytes) encode length in the second byte directly."""
    from lib import ws
    payload = b"hello"
    frame = ws.binary_frame(payload)
    # First byte: 0x82 = FIN(0x80) | binary opcode(0x2)
    assert frame[0] == 0x82
    # Second byte: MASK=0, length=5
    assert frame[1] == 0x05
    # Payload directly follows.
    assert frame[2:] == payload


def test_binary_frame_medium_payload_uses_2_byte_length():
    """RFC 6455 §5.2: lengths 126-65535 use the 16-bit extended length form."""
    from lib import ws
    payload = b"A" * 200
    frame = ws.binary_frame(payload)
    assert frame[0] == 0x82                  # FIN | binary
    assert frame[1] == 126                   # length marker = 16-bit extension
    extended_len = int.from_bytes(frame[2:4], "big")
    assert extended_len == 200
    assert frame[4:] == payload


def test_binary_frame_large_payload_uses_8_byte_length():
    """Lengths >= 65536 use the 64-bit extended length form."""
    from lib import ws
    payload = b"B" * 70_000
    frame = ws.binary_frame(payload)
    assert frame[0] == 0x82
    assert frame[1] == 127                   # length marker = 64-bit extension
    extended_len = int.from_bytes(frame[2:10], "big")
    assert extended_len == 70_000
    assert frame[10:] == payload


def test_binary_frame_empty_payload_is_valid():
    """Zero-length binary frame is well-formed (FIN bit + opcode, length=0)."""
    from lib import ws
    frame = ws.binary_frame(b"")
    assert frame == bytes([0x82, 0x00])


def test_close_frame_normal():
    """RFC 6455 §5.5.1: close frame, opcode=0x8. Without a status code,
    the payload is empty."""
    from lib import ws
    frame = ws.close_frame()
    assert frame[0] == 0x88                  # FIN | close opcode (0x8)
    assert frame[1] == 0x00                  # MASK=0, length=0


def test_close_frame_with_status_code():
    """When given a status code, payload is the 2-byte big-endian code."""
    from lib import ws
    frame = ws.close_frame(1000)             # 1000 = normal closure
    assert frame[0] == 0x88
    assert frame[1] == 0x02                  # length 2
    assert int.from_bytes(frame[2:4], "big") == 1000


# ---- Failure modes (what happens when streaming doesn't work) -----------


def test_binary_frame_rejects_non_bytes():
    """Payload must be bytes — passing a str is a programmer error we want
    to surface loud, not silently encode 'as UTF-8'."""
    from lib import ws
    import pytest
    with pytest.raises(TypeError):
        ws.binary_frame("hello")             # type: ignore[arg-type]


def test_close_frame_rejects_out_of_range_status_code():
    """Status codes 1000-4999 are the valid range per RFC 6455 §7.4."""
    from lib import ws
    import pytest
    with pytest.raises(ValueError):
        ws.close_frame(999)
    with pytest.raises(ValueError):
        ws.close_frame(5000)


def test_is_websocket_upgrade_detects_required_headers():
    """The handshake gate checks Upgrade: websocket + Connection: Upgrade
    on the incoming request."""
    from lib import ws
    headers = {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": "abc123",
        "Sec-WebSocket-Version": "13",
    }
    assert ws.is_websocket_upgrade(headers) is True


def test_is_websocket_upgrade_rejects_missing_upgrade_header():
    from lib import ws
    headers = {"Connection": "Upgrade", "Sec-WebSocket-Key": "abc"}
    assert ws.is_websocket_upgrade(headers) is False


def test_is_websocket_upgrade_handles_case_insensitive_header_values():
    """Some clients send 'Upgrade: WebSocket' or 'connection: upgrade, keep-alive'."""
    from lib import ws
    headers = {
        "Upgrade": "WebSocket",
        "Connection": "keep-alive, Upgrade",
        "Sec-WebSocket-Key": "abc",
    }
    assert ws.is_websocket_upgrade(headers) is True


# ---- Reference implementation parity (sanity check) --------------------


def _reference_accept(key: str) -> str:
    """Independent re-derivation of the Accept header so the test isn't
    just asserting against itself."""
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    return base64.b64encode(
        hashlib.sha1((key + GUID).encode("ascii")).digest()
    ).decode("ascii")


def test_compute_accept_matches_independent_derivation():
    from lib import ws
    for key in ("abc", "xyz==", "long-random-string-with-padding=="):
        assert ws.compute_accept(key) == _reference_accept(key)
