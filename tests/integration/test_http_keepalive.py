"""HTTP/1.1 transport-floor tests (networking refactor P1).

The server must speak HTTP/1.1 with persistent connections so the
tailscale-serve reverse proxy can pool upstream connections instead of paying
a TCP connect + handler thread + sqlite open per request (the failure behind
the listen-queue overflow incident). That only works if every response is
correctly framed and early responses (401 before the handler read the body)
drain the request body instead of desyncing the connection.

Runs against a real ThreadingHTTPServer with a stubbed ServerContext, same
pattern as test_upload_endpoint.py.
"""
import http.client
import json
import pathlib
import socket
import sys
import threading
import time
import urllib.request

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))
from lib.audio_stream import AudioStream, SubscriberQueue  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402

import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("claude_pwa_server_p1", _SERVER_DIR / "server.py")
assert _spec and _spec.loader
server_module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(server_module)
server_module.BIND_ADDR = "127.0.0.1"
build_server = server_module.build_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _boot(tmp_path, auth_token: str = ""):
    static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio = tmp_path / "audio"
    audio.mkdir(exist_ok=True)
    from lib.agents import create_agent
    create_agent(persona="Rachel", voice_id="V_RACHEL",
                 cwd=str(tmp_path), session="rachel")
    ctx = ServerContext(
        root=tmp_path,
        static=static,
        audio_dir=audio,
        agents_path=tmp_path / "agents.json",
        default_session="rachel",
        uploads_dir=tmp_path / "uploads",
        tts=FakeTTSEngine(audio),
        stream=AudioStream(audio),
        stt=StubSTT(text="hi", ends_terminal=True),
        roster_names=("Rachel",),
        auth_token=auth_token,
    )
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    probe = "/server-info" + (f"?token={auth_token}" if auth_token else "")
    for _ in range(50):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}{probe}", timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    return srv, port


@pytest.fixture
def open_server(tmp_path):
    srv, port = _boot(tmp_path)
    yield port
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def authed_server(tmp_path):
    srv, port = _boot(tmp_path, auth_token="sekrit")
    yield port
    srv.shutdown()
    srv.server_close()


def test_responses_are_http11_with_content_length(open_server):
    conn = http.client.HTTPConnection("127.0.0.1", open_server, timeout=3)
    conn.request("GET", "/server-info")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.version == 11
    assert resp.getheader("Content-Length") is not None
    assert (resp.getheader("Connection") or "").lower() != "close"
    resp.read()
    conn.close()


def test_keepalive_reuses_one_connection(open_server):
    conn = http.client.HTTPConnection("127.0.0.1", open_server, timeout=3)
    conn.request("GET", "/server-info")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 200
    sock_first = conn.sock
    assert sock_first is not None, "connection closed after first response"
    conn.request("GET", "/server-info")
    resp2 = conn.getresponse()
    resp2.read()
    assert resp2.status == 200
    assert conn.sock is sock_first, "second request did not reuse the socket"
    conn.close()


def test_unauthorized_post_with_body_does_not_poison_connection(authed_server):
    """A 401 rejected before the handler reads the body must drain it —
    otherwise the body bytes are parsed as the next request line and the
    persistent connection desyncs."""
    conn = http.client.HTTPConnection("127.0.0.1", authed_server, timeout=3)
    body = json.dumps({"text": "hello", "session": "rachel"}).encode()
    conn.request("POST", "/send", body=body,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 401
    sock_first = conn.sock
    assert sock_first is not None, "server closed instead of draining the body"
    conn.request("GET", "/server-info",
                 headers={"Authorization": "Bearer sekrit"})
    resp2 = conn.getresponse()
    resp2.read()
    assert resp2.status == 200, "connection desynced after early 401"
    assert conn.sock is sock_first
    conn.close()


def test_oversized_unread_body_closes_instead_of_draining(authed_server):
    """Bodies over the drain cap aren't worth reading just to keep the
    connection — the server must close (signalled or actual EOF), never hang
    or desync."""
    conn = http.client.HTTPConnection("127.0.0.1", authed_server, timeout=5)
    big = b"x" * (2 * 1024 * 1024)
    conn.request("POST", "/send", body=big,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 401
    closed = (resp.getheader("Connection") or "").lower() == "close" or resp.will_close
    if not closed:
        # Actual close is also acceptable: next request must fail cleanly.
        try:
            conn.request("GET", "/server-info",
                         headers={"Authorization": "Bearer sekrit"})
            conn.getresponse().read()
        except (http.client.HTTPException, OSError):
            closed = True
    assert closed, "server kept a connection with 2MB of unread body"
    conn.close()


def test_sse_stream_declares_connection_close(open_server):
    conn = http.client.HTTPConnection("127.0.0.1", open_server, timeout=3)
    conn.request("GET", "/events", headers={"Accept": "text/event-stream"})
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/event-stream")
    assert (resp.getheader("Connection") or "").lower() == "close"
    assert resp.getheader("Content-Length") is None
    conn.close()


def test_backpressure_eviction_flags_queue(tmp_path):
    """broadcast() to a full subscriber marks it evicted and removes it, so
    the SSE handler closes instead of ghost-pinging a dead stream."""
    audio = tmp_path / "audio-evict"
    audio.mkdir()
    stream = AudioStream(audio)
    q = stream.subscribe(maxsize=1)
    assert isinstance(q, SubscriberQueue)
    assert q.evicted is False
    stream.broadcast({"type": "agent-state", "session": "rachel", "state": "idle"})
    stream.broadcast({"type": "agent-state", "session": "rachel", "state": "busy"})
    assert q.evicted is True
    # Evicted queue no longer receives events.
    stream.broadcast({"type": "agent-state", "session": "rachel", "state": "idle"})
    assert q.qsize() == 1
    # A healthy subscriber added afterwards still works.
    q2 = stream.subscribe()
    stream.broadcast({"type": "agent-focus", "session": "rachel"})
    assert not q2.empty()
