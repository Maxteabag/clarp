"""One cap on a JSON request body, before any byte of it is read (issue 360, R81).

The client here speaks HTTP on a bare socket and sends the headers only: a Host that read the body
would wait for bytes that never come, so an answer at all is the proof that nothing was read."""
from __future__ import annotations

import json
import socket

import pytest

from tests.integration.test_server_di import (  # noqa: F401
    _free_port, _running_context_server, build_server, fake_ctx, running_server)

MiB = 1024 * 1024
WAIT = 10


@pytest.fixture
def guarded(fake_ctx):  # noqa: F811
    """A running Host that asks for credentials, as every real one does."""
    yield from _running_context_server(fake_ctx.with_(auth_token="owner-token"))


def _exchange(base, method, path, headers, body=b"", *, then=b""):
    """Send one request (``body`` may be shorter than its Content-Length); answer (status, headers,
    body, whether the Host then closed the connection, anything it answered after)."""
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=WAIT) as sock:
        head = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n" + "".join(
            f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
        try:
            sock.sendall(head.encode() + body + then)
        except (BrokenPipeError, ConnectionResetError):
            return "reset", {}, b"", None        # the Host closed while the body was still coming
        reader = sock.makefile("rb")
        try:
            status = int(reader.readline().split()[1])
        except (socket.timeout, TimeoutError):
            return None, {}, b"", None           # no answer: the Host is waiting for the body
        answered = {}
        while (line := reader.readline().strip()):
            name, _, value = line.decode().partition(":")
            answered[name.strip().lower()] = value.strip()
        text = reader.read(int(answered.get("content-length", "0")))
        sock.settimeout(2)
        try:
            rest = reader.read()                 # b"" once the Host closes
        except (socket.timeout, TimeoutError):
            rest = None                          # still open
        return status, answered, text, rest


OWNER = {"Authorization": "Bearer owner-token", "Content-Type": "application/json"}


@pytest.mark.parametrize("path", ["/artifacts", "/voice-events", "/send", "/clips/ack"])
def test_a_json_body_over_16_mib_is_refused_before_a_byte_is_read_and_the_connection_closes(
        guarded, path):
    base, _ctx, _srv = guarded
    status, headers, text, rest = _exchange(
        base, "POST", path, {**OWNER, "Content-Length": str(16 * MiB + 1)})
    assert status == 413
    assert json.loads(text) == {"error": "request body too large"}
    assert headers.get("connection", "").lower() == "close" and rest == b""


def test_a_body_of_exactly_16_mib_is_read_and_logged_by_its_family_and_size(guarded, capfd):
    base, _ctx, _srv = guarded
    body = json.dumps({"events": [], "pad": "x" * (16 * MiB - 25)}).encode()
    assert len(body) == 16 * MiB
    status, _headers, _text, _rest = _exchange(
        base, "POST", "/voice-events?secret=1", {**OWNER, "Content-Length": str(len(body))}, body)
    assert status not in (413, "reset", None), status        # read and answered, not refused
    err = capfd.readouterr().err
    assert f"requestBodyLarge family=/voice-events bytes={16 * MiB}" in err
    assert "secret" not in "".join(line for line in err.splitlines() if "requestBody" in line)


def test_a_body_of_2_mib_or_less_is_not_logged(guarded, capfd):
    base, _ctx, _srv = guarded
    body = json.dumps({"pad": "x" * (2 * MiB - 11)}).encode()
    assert len(body) == 2 * MiB
    _exchange(base, "POST", "/voice-events", {**OWNER, "Content-Length": str(len(body))}, body)
    assert "requestBody" not in capfd.readouterr().err


def test_pairing_over_64_kib_is_refused_and_its_body_thrown_away_never_parsed(guarded, monkeypatch, capfd):
    """Without credentials, up to 1 MiB the Host's own drain reads the body and discards it, as for a
    401, so the client reads its 413 and the connection is kept."""
    from lib import device_pairing
    base, _ctx, _srv = guarded
    monkeypatch.setattr(device_pairing, "exchange", lambda *a, **k: pytest.fail("never parsed"))
    for size in (64 * 1024 + 1, 1024 * 1024):         # one byte over the cap; the drain's own bound
        body = b"{" + b" " * (size - 1)                 # not even JSON: it is never parsed
        status, headers, text, rest = _exchange(
            base, "POST", "/pairing/exchange",
            {"Content-Type": "application/json", "Content-Length": str(size)}, body)
        assert status == 413 and json.loads(text) == {"error": "request body too large"}, size
        assert "connection" not in headers and rest is None, size     # read, so the connection stays
    assert "requestJsonParseFail" not in capfd.readouterr().err


def test_pairing_one_byte_over_1_mib_is_refused_without_a_byte_read_and_closed(guarded):
    base, _ctx, _srv = guarded
    status, headers, text, rest = _exchange(
        base, "POST", "/pairing/exchange",
        {"Content-Type": "application/json", "Content-Length": str(1024 * 1024 + 1)})
    assert status == 413 and json.loads(text) == {"error": "request body too large"}
    assert headers.get("connection", "").lower() == "close" and rest == b""


def test_a_client_that_sends_its_whole_pairing_body_first_reads_its_413(guarded):
    import http.client
    base, _ctx, _srv = guarded
    host, port = base.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=WAIT)
    try:
        connection.request("POST", "/pairing/exchange", body=b"x" * (100 * 1024),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 413 and json.loads(response.read()) == {"error": "request body too large"}
    finally:
        connection.close()


def test_a_real_pairing_is_still_accepted(guarded):
    from lib import device_pairing
    base, _ctx, _srv = guarded
    issued = device_pairing.issue(scope="limited", ttl_seconds=600)
    body = json.dumps({"code": issued["code"], "device_name": "Test iPhone"}).encode()
    status, _headers, text, _rest = _exchange(
        base, "POST", "/pairing/exchange",
        {"Content-Type": "application/json", "Content-Length": str(len(body))}, body)
    assert status == 201, text


def test_without_credentials_an_ordinary_json_route_still_answers_401_without_reading(guarded):
    """Credentials are checked before dispatch; an unauthenticated body is never read there."""
    base, _ctx, _srv = guarded
    status, _headers, _text, rest = _exchange(
        base, "POST", "/artifacts",
        {"Content-Type": "application/json", "Content-Length": str(20 * MiB)})
    assert status == 401 and rest == b""


@pytest.mark.parametrize("path, size", [("/upload", 60 * MiB), ("/media", 80 * MiB),
                                        ("/transcribe", 70 * MiB)])
def test_a_binary_route_keeps_its_own_cap_and_answer(guarded, path, size):
    base, _ctx, _srv = guarded
    status, _headers, text, _rest = _exchange(
        base, "POST", path, {**OWNER, "Content-Type": "application/octet-stream",
                             "Content-Length": str(size)})
    assert status == 400 and json.loads(text)["error"] == "bad size"


@pytest.mark.parametrize("length", ["abc", "1e9"])
def test_a_length_that_is_not_a_number_keeps_the_readers_400(guarded, length):
    base, _ctx, _srv = guarded
    status, _headers, text, _rest = _exchange(
        base, "POST", "/clips/ack", {**OWNER, "Content-Length": length})
    assert status == 400 and json.loads(text)["error"] == "bad json"


def test_a_relay_style_client_that_sends_17_mib_gets_an_answer_not_a_hang(guarded):
    """What the relay's http.client sees: it sends the whole body before reading. The Host answers
    413 without reading; the client either reads that answer or finds the connection reset."""
    import http.client
    base, _ctx, _srv = guarded
    host, port = base.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=WAIT)
    outcome = ""
    try:
        connection.request("POST", "/artifacts", body=b"x" * (17 * MiB), headers=OWNER)
        response = connection.getresponse()
        outcome = f"{response.status} {response.read().decode()}"
    except (ConnectionResetError, BrokenPipeError) as exc:
        outcome = type(exc).__name__
    finally:
        connection.close()
    print("RELAY-STYLE OUTCOME:", outcome)
    assert outcome.startswith("413") or outcome in {"ConnectionResetError", "BrokenPipeError"}
