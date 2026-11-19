"""A request body the Host does not read is never read as the next request (issue 360, R86).

The reader reads Content-Length bytes and nothing else. A body framed any other way (chunked, two
lengths, a length with a sign or spaces) was left on a kept-alive connection and parsed as the next
request. Each request below carries a second, authenticated request where the Host must not read it
as one: it answers once, and the connection closes."""
from __future__ import annotations

import pytest

from tests.integration.test_request_body_cap import OWNER, _exchange, guarded  # noqa: F401
from tests.integration.test_server_di import fake_ctx  # noqa: F401

HIDDEN = b"GET /server-info HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer owner-token\r\n\r\n"
CHUNKED = hex(len(HIDDEN))[2:].encode() + b"\r\n" + HIDDEN + b"\r\n0\r\n\r\n"


def _answered_once_and_closed(rest):
    assert rest == b"", "the hidden request was answered" if rest else "the connection stayed open"


@pytest.mark.parametrize("method, path", [("POST", "/clips/ack"), ("GET", "/server-info")])
def test_a_chunked_body_is_never_read_as_the_next_request(guarded, method, path):
    base, _ctx, _srv = guarded
    status, _headers, text, rest = _exchange(
        base, method, path, {**OWNER, "Transfer-Encoding": "chunked"}, CHUNKED)
    assert status in (200, 400, 404), text
    _answered_once_and_closed(rest)


def test_a_body_with_a_length_and_a_transfer_encoding_is_never_read_as_the_next_request(guarded):
    base, _ctx, _srv = guarded
    status, _headers, _text, rest = _exchange(
        base, "POST", "/clips/ack",
        {**OWNER, "Content-Length": "5", "Transfer-Encoding": "chunked"}, CHUNKED)
    assert status == 400
    _answered_once_and_closed(rest)


@pytest.mark.parametrize("length", ["-5", "+0", "0 ", "1_0"])
def test_a_length_with_a_sign_spaces_or_an_underscore_gets_the_readers_400_and_closes(guarded, length):
    base, _ctx, _srv = guarded
    status, _headers, text, rest = _exchange(
        base, "POST", "/clips/ack", {**OWNER, "Content-Length": length}, HIDDEN)
    assert status == 400 and b"bad json" in text
    _answered_once_and_closed(rest)


def test_two_lengths_are_never_read_by_the_first_alone(guarded):
    base, _ctx, _srv = guarded
    host, port = base.removeprefix("http://").split(":")
    import socket
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(b"POST /clips/ack HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer owner-token\r\n"
                     b"Content-Type: application/json\r\nContent-Length: 2\r\n"
                     b"Content-Length: " + str(2 + len(HIDDEN)).encode() + b"\r\n\r\n{}" + HIDDEN)
        sock.settimeout(3)
        received = b""
        try:
            while chunk := sock.recv(65536):
                received += chunk
        except (socket.timeout, TimeoutError):
            pytest.fail("the connection stayed open")
    assert received.count(b"HTTP/1.1 ") == 1, received[:300]


def test_without_credentials_a_chunked_body_is_not_read_as_the_next_request_either(guarded):
    base, _ctx, _srv = guarded
    status, _headers, _text, rest = _exchange(
        base, "POST", "/artifacts", {"Transfer-Encoding": "chunked"}, CHUNKED)
    assert status == 401
    _answered_once_and_closed(rest)


def test_an_ordinary_kept_alive_connection_still_serves_its_next_request(guarded):
    base, _ctx, _srv = guarded
    status, _headers, _text, rest = _exchange(
        base, "POST", "/clips/ack", {**OWNER, "Content-Length": "2"}, b"{}",
        then=HIDDEN.replace(b"\r\n\r\n", b"\r\nConnection: close\r\n\r\n"))
    assert status in (200, 400)
    assert rest and b"server_id" in rest          # pipelined, framed by a plain length: served
