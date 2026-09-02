"""Minimal WebSocket server-side helpers.

We do exactly what the audio-streaming endpoint needs: handshake response
+ server→client binary frames + clean close. No continuation frames, no
extensions, no client→server messages. ~100 lines.

Why not a library: this is server-side only, single-direction, frame size
under our control. RFC 6455 §5.2 is short and the implementation is
short. Tests live next door (tests/unit/test_ws.py) — every code path
here has at least one pinning test.

WIRE FORMAT (per RFC 6455 §5.2):
    byte 0:  FIN(1) | RSV(3) | opcode(4)
    byte 1:  MASK(1) | length(7)
    if length == 126:   next 2 bytes are uint16 big-endian length
    if length == 127:   next 8 bytes are uint64 big-endian length
    if MASK == 1:       next 4 bytes are the masking key (client→server only)
    then `length` bytes of payload (XOR'd with masking key if MASK==1)

For server→client frames the MASK bit MUST be 0.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from typing import Mapping


# RFC 6455 §1.3. Concatenated with the client's Sec-WebSocket-Key, SHA-1'd,
# base64-encoded, and returned as Sec-WebSocket-Accept.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes (RFC 6455 §11.8).
_OP_CONTINUATION = 0x0
_OP_TEXT         = 0x1
_OP_BINARY       = 0x2
_OP_CLOSE        = 0x8
_OP_PING         = 0x9
_OP_PONG         = 0xA

_FIN_BIT = 0x80


# ---- Handshake ----------------------------------------------------------


def compute_accept(client_key: str) -> str:
    """Compute the Sec-WebSocket-Accept value for a client's
    Sec-WebSocket-Key. Pure function, no IO."""
    if not client_key:
        raise ValueError("client_key is empty")
    digest = hashlib.sha1((client_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake_response(client_key: str) -> bytes:
    """Build the full HTTP 101 Switching Protocols response. Caller is
    expected to write this to the raw socket *after* the request has been
    read."""
    if not client_key:
        raise ValueError("client_key is required for handshake_response")
    accept = compute_accept(client_key)
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii")


def is_websocket_upgrade(headers: Mapping[str, str]) -> bool:
    """Loose check that the incoming request is asking for a WebSocket
    upgrade. Tolerates case variations (Node clients + HTTP/2 send
    lowercase header names) and the (legal) `Connection: keep-alive,
    Upgrade` form some clients send."""
    # Build a lowered-key view so lookups are case-insensitive regardless
    # of whether the caller passed a plain dict (case-sensitive) or a
    # Message-style mapping.
    lower = {k.lower(): v for k, v in headers.items()}
    upgrade = (lower.get("upgrade") or "").strip().lower()
    if upgrade != "websocket":
        return False
    connection = (lower.get("connection") or "").strip().lower()
    # `Connection` can be a comma-separated list of tokens.
    tokens = {t.strip() for t in connection.split(",")}
    if "upgrade" not in tokens:
        return False
    if not (lower.get("sec-websocket-key") or "").strip():
        return False
    return True


# ---- Frame encoding -----------------------------------------------------


def _frame(opcode: int, payload: bytes) -> bytes:
    """Build a single, complete (FIN=1) unmasked frame. Server-side only."""
    head = bytes([_FIN_BIT | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        length_bytes = bytes([n])
    elif n <= 0xFFFF:
        length_bytes = bytes([126]) + struct.pack("!H", n)
    else:
        length_bytes = bytes([127]) + struct.pack("!Q", n)
    return head + length_bytes + payload


# Public opcode aliases for callers that read client frames.
OP_TEXT = _OP_TEXT
OP_BINARY = _OP_BINARY
OP_CLOSE = _OP_CLOSE
OP_PING = _OP_PING
OP_PONG = _OP_PONG

# Hard cap on a single client frame. Streaming audio frames are a few KB;
# anything past this is malformed/abusive and we bail rather than allocate.
_MAX_FRAME_BYTES = 4 * 1024 * 1024


def _read_exact(rfile, n: int) -> bytes | None:
    """Read exactly `n` bytes from a blocking file-like, or None on EOF."""
    chunks = []
    remaining = n
    while remaining > 0:
        b = rfile.read(remaining)
        if not b:
            return None
        chunks.append(b)
        remaining -= len(b)
    return b"".join(chunks)


def read_frame(rfile) -> tuple[int, bytes] | None:
    """Read ONE complete client→server frame from a blocking file-like.

    Returns `(opcode, unmasked_payload)`, or None on EOF / short read /
    oversized or unmasked frame (all of which mean "stop"). Control and
    data frames alike are returned; the caller dispatches on opcode.
    Continuation frames (fragmented messages) are not supported — our
    clients send each message as a single FIN frame.

    Per RFC 6455 §5.1 every client→server frame MUST be masked; an
    unmasked one is a protocol violation and we treat it as a stop.
    """
    hdr = _read_exact(rfile, 2)
    if hdr is None:
        return None
    b0, b1 = hdr[0], hdr[1]
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    length = b1 & 0x7F
    if length == 126:
        ext = _read_exact(rfile, 2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _read_exact(rfile, 8)
        if ext is None:
            return None
        length = struct.unpack("!Q", ext)[0]
    if length > _MAX_FRAME_BYTES or not masked:
        return None
    mask = _read_exact(rfile, 4)
    if mask is None:
        return None
    payload = _read_exact(rfile, length) if length else b""
    if payload is None:
        return None
    if length:
        payload = bytes(payload[i] ^ mask[i & 3] for i in range(length))
    return (opcode, payload)


def text_frame(text: str) -> bytes:
    """Encode `text` as a single UTF-8 TEXT frame (server→client)."""
    return _frame(_OP_TEXT, text.encode("utf-8"))


def pong_frame(payload: bytes = b"") -> bytes:
    """Encode a PONG control frame echoing `payload` (server→client)."""
    return _frame(_OP_PONG, bytes(payload))


def binary_frame(payload: bytes) -> bytes:
    """Encode `payload` as a single binary WebSocket frame.

    We deliberately reject str — silently UTF-8-encoding would mask the
    common mistake of passing a string when bytes were intended (especially
    awkward for audio data that might be 'mostly ASCII'-shaped at the
    start of an MP3 ID3 header)."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError(
            f"binary_frame payload must be bytes, got {type(payload).__name__}"
        )
    return _frame(_OP_BINARY, bytes(payload))


def close_frame(code: int | None = None, reason: str = "") -> bytes:
    """Encode a close control frame. Empty payload by default; with `code`,
    the payload is the 2-byte big-endian status code followed by optional
    UTF-8 reason text. RFC 6455 §7.4 reserves codes 1000-4999."""
    if code is None:
        return _frame(_OP_CLOSE, b"")
    if not 1000 <= code <= 4999:
        raise ValueError(f"close code {code} out of valid range 1000-4999")
    payload = struct.pack("!H", code)
    if reason:
        payload += reason.encode("utf-8")
    return _frame(_OP_CLOSE, payload)
