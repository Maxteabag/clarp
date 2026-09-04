"""End-to-end HTTP tests using the dependency-injected server.

These run against a real ThreadingHTTPServer on a random local port, with a
ServerContext that swaps every external dependency (TTS, audio stream,
STT, filesystem paths) for fakes. No ElevenLabs calls,
no Whisper model — just the handler logic exercised end-to-end.

This is the payoff for the AddScoped-style DI refactor.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from types import SimpleNamespace

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))
from lib.audio_stream import AudioStream  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402

import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("claude_pwa_server", _SERVER_DIR / "server.py")
assert _spec and _spec.loader
server_module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(server_module)
server_module.BIND_ADDR = "127.0.0.1"
build_server = server_module.build_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_ctx(tmp_path):
    """A fully-stubbed ServerContext, ready to plug into the real server."""
    static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio = tmp_path / "audio"
    audio.mkdir()
    agents_path = tmp_path / "agents.json"  # legacy field on ctx; unused by DB
    from lib.agents import create_agent
    create_agent(persona="Mike",   voice_id="V_MIKE",
                 cwd=str(tmp_path), session="claude")
    create_agent(persona="Rachel", voice_id="V_RACHEL",
                 cwd=str(tmp_path), session="rachel")
    ctx = ServerContext(
        root=tmp_path,
        static=static,                       # reuse real static so /sw.js works
        audio_dir=audio,
        agents_path=agents_path,
        default_session="claude",
        tts=FakeTTSEngine(audio),
        stream=AudioStream(audio),
        stt=StubSTT(text="hello there", ends_terminal=True),
        roster_names=("Mike", "Rachel"),
    )
    return ctx


@pytest.fixture
def running_server(fake_ctx):
    """Start the real ThreadingHTTPServer with the fake ctx on a random port."""
    port = _free_port()
    srv = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    # Wait for socket to accept.
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/agents/snapshot", timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    yield base, fake_ctx, srv
    srv.shutdown()
    srv.server_close()


def _get(url, **kw):
    req = urllib.request.Request(url, **kw)
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status, r.read()


def _post(url, body: dict):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status, r.read()


def _post_with_headers(url, body: dict, headers: dict):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status, r.read()


def test_sse_event_seen_during_replay_is_not_delivered_again_as_live(fake_ctx):
    """Subscribe-before-replay prevents loss but needs an overlap fence.

    An event broadcast after subscription and before the replay query is both
    in SQLite and in the subscriber queue. Sending both copies can execute
    one-shot native actions (calendar/location requests) twice.
    """
    class ReplayOverlapStream(AudioStream):
        def __init__(self, audio_dir):
            super().__init__(audio_dir)
            self.injected = False

        def subscribe(self, maxsize=128):
            subscriber = super().subscribe(maxsize=maxsize)
            if not self.injected:
                self.injected = True
                self.broadcast({
                    "type": "calendar-request",
                    "request_id": "calendar-overlap",
                    "session": "claude",
                    "title": "Exactly once",
                })
            return subscriber

    fake_ctx.stream = ReplayOverlapStream(fake_ctx.audio_dir)
    port = _free_port()
    srv = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    copies = []
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/events", timeout=2
        ) as response:
            deadline = time.monotonic() + 2
            while len(copies) < 2 and time.monotonic() < deadline:
                line = response.readline().decode().strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line.removeprefix("data: "))
                if event.get("request_id") == "calendar-overlap":
                    copies.append(event)
    finally:
        srv.shutdown()
        srv.server_close()

    assert len(copies) == 1


def test_slow_clients_cannot_create_unbounded_request_threads(fake_ctx):
    """The prior overload reached hundreds of threads and exhausted host FDs."""
    port = _free_port()
    srv = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    sockets = []
    subscriber_count = 0
    try:
        for _ in range(48):
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            sock.sendall(
                b"GET /events HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n\r\n"
            )
            sockets.append(sock)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with fake_ctx.stream._subs_lock:  # noqa: SLF001
                subscriber_count = len(fake_ctx.stream._subs)  # noqa: SLF001
            if subscriber_count == len(sockets):
                break
            time.sleep(0.01)
    finally:
        for sock in sockets:
            sock.close()
        # Wake every SSE handler so closed sockets are noticed immediately.
        fake_ctx.stream.broadcast({"type": "test-cleanup"})
        srv.shutdown()
        srv.server_close()

    assert subscriber_count <= 32


def test_limited_device_cannot_read_arbitrary_host_files(running_server, tmp_path):
    """A limited token must not be able to read config and escalate to admin."""
    from urllib.parse import urlencode
    from lib import device_pairing

    base, ctx, _srv = running_server
    ctx.auth_token = "administrator-secret"
    issued = device_pairing.issue(device_name="Limited phone", scope="limited")
    device = device_pairing.exchange(issued["code"])
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    query = urlencode({
        "session": "claude",
        "root": str(tmp_path),
        "path": secret.name,
    })
    request = urllib.request.Request(
        f"{base}/agent-file?{query}",
        headers={"Authorization": f"Bearer {device['token']}"},
    )

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=2)

    assert error.value.code == 403


def test_access_log_redacts_query_credentials(capsys):
    handler = object.__new__(server_module.Handler)
    handler.address_string = lambda: "127.0.0.1"

    handler.log_message(
        '"%s" %s %s',
        "GET /?token=top-secret-token HTTP/1.1",
        "200",
        "-",
    )

    logged = capsys.readouterr().out
    assert "top-secret-token" not in logged
    assert "REDACTED" in logged


def test_transcription_provider_errors_remain_valid_json(running_server):
    base, ctx, _srv = running_server

    class ExplodingSTT(StubSTT):
        def transcribe_bytes(self, *_args, **_kwargs):
            raise RuntimeError('provider said "invalid"\nretry later')

    ctx.stt = ExplodingSTT(text="")
    try:
        _post_raw(
            base + "/transcribe",
            b"voice audio",
            {"Content-Type": "audio/webm"},
        )
        raise AssertionError("transcription error should return HTTP 500")
    except urllib.error.HTTPError as error:
        assert error.code == 500
        payload = json.loads(error.read())

    assert payload == {"error": 'provider said "invalid"\nretry later'}


def test_production_startup_requests_restart_heartbeat_recovery(
    fake_ctx, monkeypatch,
):
    from lib import heartbeat

    calls: list[bool] = []
    monkeypatch.setattr(
        heartbeat.HeartbeatScheduler,
        "run_restart_recovery_once",
        lambda _scheduler: calls.append(True) or 2,
    )
    srv = build_server(
        fake_ctx, _free_port(), bind_addr="127.0.0.1",
        restart_recovery=True)
    try:
        assert calls == [True]
    finally:
        srv.server_close()


def test_production_startup_marks_restart_interrupted_turns(
    fake_ctx, monkeypatch,
):
    """Issue #11: the previous process's in-flight turn is marked before the
    restart heartbeat asks the agent to carry on."""
    from lib import heartbeat, interrupted_turns

    order: list[str] = []
    monkeypatch.setattr(
        interrupted_turns, "recover_after_restart",
        lambda stream=None: order.append("mark") or [])
    monkeypatch.setattr(
        heartbeat.HeartbeatScheduler,
        "run_restart_recovery_once",
        lambda _scheduler: order.append("heartbeat") or 0,
    )
    srv = build_server(
        fake_ctx, _free_port(), bind_addr="127.0.0.1",
        restart_recovery=True)
    try:
        assert order == ["mark", "heartbeat"]
    finally:
        srv.server_close()


def test_injected_test_server_does_not_run_restart_recovery(
    fake_ctx, monkeypatch,
):
    from lib import heartbeat

    calls: list[bool] = []
    monkeypatch.setattr(
        heartbeat.HeartbeatScheduler,
        "run_restart_recovery_once",
        lambda _scheduler: calls.append(True) or 2,
    )
    srv = build_server(fake_ctx, _free_port(), bind_addr="127.0.0.1")
    try:
        assert calls == []
    finally:
        srv.server_close()


def test_server_restart_does_not_interrupt_healthy_external_runtime(
    fake_ctx, monkeypatch,
):
    """A server-only restart must leave runtime-owned work completely alone."""
    from lib import heartbeat, interrupted_turns

    calls: list[str] = []
    fake_ctx.runtime_client = SimpleNamespace(
        ping=lambda: True,
        recover_queued=lambda: 0,
    )
    monkeypatch.setattr(
        server_module, "resume_persisted_agents",
        lambda _ctx: calls.append("resume"))
    monkeypatch.setattr(
        interrupted_turns, "recover_after_restart",
        lambda stream=None: calls.append("mark") or [])
    monkeypatch.setattr(
        heartbeat.HeartbeatScheduler, "run_restart_recovery_once",
        lambda _scheduler: calls.append("heartbeat") or 0)

    srv = build_server(
        fake_ctx, _free_port(), bind_addr="127.0.0.1",
        restart_recovery=True)
    try:
        assert calls == []
    finally:
        srv.server_close()


def test_status_reports_external_runtime_health(fake_ctx):
    fake_ctx.runtime_client = SimpleNamespace(
        ping=lambda: True,
        recover_queued=lambda: 0,
        status=lambda: {
            "protocol_version": 1,
            "release_id": "runtime-42",
            "draining": False,
            "active": {"agent-1": "trace-1"},
        },
    )
    port = _free_port()
    srv = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(f"http://127.0.0.1:{port}/status")
        assert status == 200
        runtime = json.loads(body)["runtime"]
        assert runtime == {
            "available": True,
            "release_id": "runtime-42",
            "draining": False,
            "active_turns": 1,
        }
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_server_can_be_replaced_while_runtime_keeps_active_turn(
    fake_ctx, tmp_path,
):
    from lib.runtime_bridge import RuntimeClient, RuntimeRPCServer
    from lib.turn_dispatch import DispatchResult

    active = {}

    class PersistentDispatch:
        def dispatch(self, **kwargs):
            agent = __import__("lib.agents", fromlist=["get_by_session"]).get_by_session(
                kwargs["forced_session"])
            active[agent["agent_id"]] = kwargs["trace_id"]
            return DispatchResult(
                session=kwargs["forced_session"], backend=agent["backend"])

        def recover_queued(self):
            return 0

        def dispatch_queued(self, queue_id):
            return DispatchResult(session=queue_id, backend="claude")

    runtime_socket = tmp_path / "runtime.sock"
    runtime = RuntimeRPCServer(
        runtime_socket,
        dispatch_service=PersistentDispatch(),
        status_provider=lambda: {
            "active": dict(active), "spawning": [], "terminals": [],
            "queued": {},
        },
    )
    runtime_thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    runtime_thread.start()
    fake_ctx.runtime_client = RuntimeClient(runtime_socket)

    def start_http_server():
        port = _free_port()
        server = build_server(fake_ctx, port, bind_addr="127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{port}"

    first, first_url = start_http_server()
    try:
        status, body = _post(first_url + "/send", {
            "session": "claude",
            "text": "keep working through the update",
            "force_session": True,
            "hands_free": False,
            "synthesize_audio": False,
        })
        assert status == 200
        assert json.loads(body)["session"] == "claude"
    finally:
        first.shutdown()
        first.server_close()

    agent_id, trace_id = next(iter(active.items()))
    assert fake_ctx.runtime_client.status()["active"] == {agent_id: trace_id}

    replacement, replacement_url = start_http_server()
    try:
        status, _body = _get(replacement_url + "/agents/snapshot")
        assert status == 200
        assert fake_ctx.runtime_client.status()["active"] == {
            agent_id: trace_id}
    finally:
        replacement.shutdown()
        replacement.server_close()
        runtime.shutdown()
        runtime.server_close()


def test_runtime_owned_turn_completes_after_http_server_is_gone(
    fake_ctx, tmp_path,
):
    from lib import agents as agents_db
    from lib.runtime_bridge import RuntimeClient, RuntimeRPCServer
    from lib.runtime_events import RuntimeEventStream
    from lib.turn_dispatch import TurnDispatchService, clear_for_agent

    class RuntimeBackends:
        CLAUDE = "claude"

        def __init__(self):
            self.spawn = None

        def normalize(self, backend):
            return backend or "claude"

        def active_handles(self, _backend, _agent_id):
            return [SimpleNamespace(is_alive=lambda: self.spawn is not None)] \
                if self.spawn is not None else []

        def interrupt(self, _backend, _agent_id):
            return 0

        def steer_turn(self, *_args, **_kwargs):
            return False

        def spawn_turn(self, _backend, **kwargs):
            self.spawn = kwargs

    runtime_backends = RuntimeBackends()
    runtime_ctx = SimpleNamespace(
        default_session="claude",
        agents_path=fake_ctx.agents_path,
        stream=RuntimeEventStream(),
        runtime_client=None,
    )
    runtime_dispatch = TurnDispatchService(
        runtime_ctx, backend_registry=runtime_backends, home=tmp_path,
        uuid_factory=lambda: "runtime-conversation",
    )
    runtime_socket = tmp_path / "turn-runtime.sock"
    runtime = RuntimeRPCServer(
        runtime_socket, dispatch_service=runtime_dispatch)
    runtime_thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    runtime_thread.start()
    fake_ctx.runtime_client = RuntimeClient(runtime_socket)

    port = _free_port()
    first = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    first_thread = threading.Thread(target=first.serve_forever, daemon=True)
    first_thread.start()
    try:
        status, _body = _post(f"http://127.0.0.1:{port}/send", {
            "session": "claude", "text": "finish after deployment",
            "force_session": True, "hands_free": False,
            "synthesize_audio": False,
        })
        assert status == 200
        assert runtime_backends.spawn is not None
    finally:
        first.shutdown()
        first.server_close()

    agent = agents_db.get_by_session("claude")
    before = fake_ctx.runtime_client.status()["active"]
    assert before.get(agent["agent_id"])

    # The provider callback is owned by the runtime and still commits the
    # terminal result after every HTTP server thread has stopped.
    runtime_backends.spawn["on_result"]({
        "duration_ms": 25,
        "last_agent_message": "finished without interruption",
    })

    assert agents_db.latest_state(agent["agent_id"])["kind"] == "done"
    assert fake_ctx.runtime_client.status()["active"] == {}
    clear_for_agent(agent["agent_id"])
    runtime.shutdown()
    runtime.server_close()


def test_artifact_http_round_trip_and_pagination(running_server):
    base, _ctx, _srv = running_server
    status, body = _post(base + "/artifacts", {
        "session": "claude", "type": "research", "title": "Artifact test",
        "summary": "Visible in chat", "payload": {
            "content": "Finding", "source_count": 2, "sources": []},
    })
    assert status == 201
    artifact = json.loads(body)["artifact"]
    status, body = _get(base + "/artifacts?limit=1&offset=0")
    assert status == 200
    assert json.loads(body)["artifacts"][0]["artifact_id"] == artifact["artifact_id"]
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(base + "/artifacts?limit=bad&offset=0")
    assert error.value.code == 400


def test_background_job_snapshot_and_idempotent_cancel_http(
    running_server, monkeypatch,
):
    from lib import background_jobs

    base, _ctx, _srv = running_server
    dispatched: list[dict] = []

    class FakeDispatch:
        def __init__(self, _ctx):
            pass

        def dispatch(self, **kwargs):
            dispatched.append(kwargs)

    monkeypatch.setattr(server_module, "TurnDispatchService", FakeDispatch)
    background_jobs.upsert(
        session="rachel", job_id="http-watch", kind="email",
        title="Email watch")

    status, body = _get(base + "/background-jobs")
    payload = json.loads(body)
    assert status == 200
    assert payload["snapshot_revision"] > 0
    assert payload["observed_at"] > 0
    assert payload["jobs"][0]["status"] == "running"
    assert payload["jobs"][0]["worker_freshness"] == "fresh"

    status, body = _delete(base + "/background-jobs/http-watch")
    assert status == 200
    assert json.loads(body)["changed"] is True
    status, body = _delete(base + "/background-jobs/http-watch")
    assert status == 200
    assert json.loads(body)["changed"] is False
    assert len(dispatched) == 1
    assert "handle bg1:1:http-watch" in dispatched[0]["text"]
    assert "job-cancelled bg1:1:http-watch" in dispatched[0]["text"]
    assert "exact worker identity" in dispatched[0]["text"]
    assert dispatched[0]["requested_session"] == "rachel"
    assert dispatched[0]["forced_session"] == "rachel"


def test_server_update_http_registers_default_owner_session(
    running_server, monkeypatch,
):
    from lib import server_update

    base, _ctx, _srv = running_server
    owners = []
    monkeypatch.setattr(
        server_update, "request_update",
        lambda session: owners.append(session) or (
            202, {"ok": True, "status": "queued"}),
    )

    status, body = _post(base + "/server-update", {})

    assert status == 202
    assert json.loads(body)["status"] == "queued"
    assert owners == ["claude"]


def _put(url, body: dict):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status, r.read()


def _delete(url):
    req = urllib.request.Request(url, method="DELETE")
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status, r.read()


def _post_raw(url, body: bytes, headers: dict):
    req = urllib.request.Request(
        url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status, r.read()


def _post_truncated_raw(url: str, body: bytes, advertised_length: int,
                        headers: dict) -> tuple[int, bytes]:
    """Send an HTTP body prefix, then EOF while keeping the read side open."""
    parsed = urlsplit(url)
    assert parsed.hostname and parsed.port
    request_headers = {
        "Host": parsed.netloc,
        "Connection": "close",
        "Content-Length": str(advertised_length),
        **headers,
    }
    head = (
        f"POST {parsed.path} HTTP/1.1\r\n"
        + "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
        + "\r\n"
    ).encode()
    with socket.create_connection(
        (parsed.hostname, parsed.port), timeout=2,
    ) as client:
        client.sendall(head + body)
        client.shutdown(socket.SHUT_WR)
        response = bytearray()
        while chunk := client.recv(65536):
            response.extend(chunk)
    response_head, response_body = bytes(response).split(b"\r\n\r\n", 1)
    status = int(response_head.split(b" ", 2)[1])
    return status, response_body


def test_get_snapshot_returns_seeded_data(running_server):
    base, _ctx, _srv = running_server
    status, body = _get(base + "/agents/snapshot")
    assert status == 200
    data = json.loads(body)
    assert {a["session"] for a in data["agents"]} == {"claude", "rachel"}
    assert next(a for a in data["agents"] if a["session"] == "claude")["persona"] == "Mike"


def test_one_time_pairing_issues_revocable_device_credential(fake_ctx):
    from lib import device_pairing

    fake_ctx.auth_token = "administrator-token"
    port = _free_port()
    srv = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    issued = device_pairing.issue(scope="limited", ttl_seconds=600)
    try:
        status, body = _post(base + "/pairing/exchange", {
            "code": issued["code"], "device_name": "Test iPhone",
        })
        assert status == 201
        paired = json.loads(body)["device"]
        token = paired["token"]

        with pytest.raises(urllib.error.HTTPError) as unauthenticated:
            _get(base + "/server-info")
        assert unauthenticated.value.code == 401
        status, _body = _get(
            base + "/server-info",
            headers={"Authorization": f"Bearer {token}"})
        assert status == 200

        controller_events = fake_ctx.stream.subscribe()
        status, body = _post_with_headers(
            base + "/remote-action",
            {
                "action": "controller-event",
                "button": "secondary",
                "controller_event": "single-click",
            },
            {"Authorization": f"Bearer {token}"},
        )
        assert status == 200
        assert json.loads(body)["controller_event_id"]
        assert json.loads(controller_events.get(timeout=1))["action"] == "controller-event"
        fake_ctx.stream.unsubscribe(controller_events)

        request = urllib.request.Request(
            base + "/managed-skills",
            data=b'{"skill_id":"clarp-calendar","enabled":true}',
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
            method="POST")
        with pytest.raises(urllib.error.HTTPError) as limited:
            urllib.request.urlopen(request, timeout=2)
        assert limited.value.code == 403

        admin_headers = {"Authorization": "Bearer administrator-token"}
        status, body = _get(base + "/paired-devices", headers=admin_headers)
        assert status == 200
        assert json.loads(body)["devices"][0]["device_id"] == paired["device_id"]
        status, _ = _post_raw(
            base + "/paired-devices/revoke",
            json.dumps({"device_id": paired["device_id"]}).encode(),
            {"Content-Type": "application/json", **admin_headers})
        assert status == 200
        with pytest.raises(urllib.error.HTTPError) as revoked:
            _get(base + "/server-info",
                 headers={"Authorization": f"Bearer {token}"})
        assert revoked.value.code == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_tts_provider_endpoint_updates_explicit_provider(running_server):
    base, _ctx, _srv = running_server
    status, body = _get(base + "/tts/providers")
    assert status == 200
    assert {row["id"] for row in json.loads(body)["providers"]} >= {
        "cartesia", "elevenlabs", "deepgram", "none"}

    status, body = _post(base + "/tts/providers", {
        "provider": "cartesia", "fallback": "none", "voice": "",
    })
    assert status == 200
    payload = json.loads(body)
    assert payload["provider"] == "cartesia"
    assert payload["fallback"] == "none"
    config_path = pathlib.Path(os.environ["CLAUDE_PWA_CONFIG"])
    saved = __import__("tomllib").loads(config_path.read_text())
    assert saved["tts"] == {"provider": "cartesia", "fallback": "none"}


def test_custom_voice_adapter_is_discovered_previewed_and_selectable(
        running_server, tmp_path, monkeypatch):
    from lib import custom_tts_adapters, service_manager
    root = tmp_path / "tts-adapters.d"
    package = root / "custom.integration"
    package.mkdir(parents=True)
    executable = package / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import json, pathlib, sys, wave
request = json.load(sys.stdin)
if request["operation"] == "voices":
    print(json.dumps({"ok": True, "voices": [
        {"id": "voice-one", "name": "Voice One", "description": "Integration"}
    ]}))
else:
    with wave.open(request["output_path"], "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(bytes(8000))
    print(json.dumps({"ok": True}))
""")
    executable.chmod(0o755)
    (package / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "id": "custom.integration",
        "name": "Integration Adapter",
        "executable": "./adapter",
        "operations": ["voices", "preview", "synthesize"],
        "audio_format": "audio/wav",
        "default_voice": "voice-one",
    }))
    monkeypatch.setattr(custom_tts_adapters, "ROOT", root)
    monkeypatch.setattr(service_manager, "restart", lambda **_kwargs: None)
    class ImmediateTimer:
        daemon = True

        def __init__(self, _delay, callback):
            self.callback = callback

        def start(self):
            self.callback()

    monkeypatch.setattr("server.threading.Timer", ImmediateTimer)
    base, _ctx, _srv = running_server

    status, body = _get(base + "/tts/providers")
    assert status == 200
    adapter = next(
        row for row in json.loads(body)["providers"]
        if row["id"] == "custom.integration")
    assert adapter["custom"] is True
    assert adapter["supports_preview"] is True

    status, body = _get(base + "/voice-catalog")
    assert status == 200
    group = next(
        row for row in json.loads(body)["providers"]
        if row["id"] == "custom.integration")
    assert group["voices"][0]["name"] == "Voice One"

    status, body = _get(
        base + "/voice-preview?provider=custom.integration&id=voice-one")
    assert status == 200
    assert body.startswith(b"ID3")

    status, body = _post(base + "/tts/providers", {
        "provider": "custom.integration", "fallback": "none", "voice": "",
    })
    assert status == 200
    assert json.loads(body)["provider"] == "custom.integration"


def test_prompt_history_requires_configured_authentication(running_server):
    base, _ctx, _srv = running_server

    with pytest.raises(urllib.error.HTTPError) as error:
        _get(base + "/identity/prompt-history?session=claude")

    assert error.value.code == 503


def test_prompt_history_ingests_authenticated_send_and_excludes_legacy(
    fake_ctx, monkeypatch,
):
    from lib import agents as agents_db
    from lib import clarp_runner, message_store

    monkeypatch.setattr(
        clarp_runner,
        "spawn_turn",
        lambda **_kwargs: type("_FakeHandle", (), {"pid": 99})(),
    )
    agent = agents_db.get_by_session("claude")
    assert agent is not None
    message_store.record_user_message(
        agent_id=agent["agent_id"], backend_session_id="legacy-conversation",
        client_msg_id="u-spoofed-legacy", text="legacy spoof", origin="user",
    )
    message_store.record_user_message(
        agent_id=agent["agent_id"], backend_session_id="legacy-conversation",
        client_msg_id="decision-synthetic", text="decision payload", origin="user",
    )
    fake_ctx.auth_token = "secret-token"
    port = _free_port()
    srv = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            _get(base + "/identity/prompt-history?session=claude")
        assert error.value.code == 401

        status, _body = _post_raw(
            base + "/send",
            json.dumps({
                "session": "claude",
                "text": "the user's durable prompt",
                "client_msg_id": "u-user-http",
                "trace_id": "trace-user-http",
                "force_session": True,
            }).encode(),
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer secret-token",
            },
        )
        assert status == 200
        linked = agents_db.conn().execute(
            """SELECT m.prompt_admission_id,p.authenticated_at_admission,
                      p.cooperative_principal
                 FROM messages m
                 JOIN prompt_admissions p
                   ON p.admission_id = m.prompt_admission_id
                WHERE m.message_id = 'u-user-http'"""
        ).fetchone()
        assert linked is not None
        assert str(linked["prompt_admission_id"]).startswith("padm-")
        assert linked["authenticated_at_admission"] == 1
        assert linked["cooperative_principal"] == "user"
        for client_id, origin in (
            ("u-agent-http", "agent"),
            ("u-heartbeat-http", "heartbeat"),
            ("u-dreaming-http", "dreaming"),
            ("u-automation-http", "automation"),
        ):
            status, _body = _post_raw(
                base + "/send",
                json.dumps({
                    "session": "claude",
                    "text": f"{origin} generated",
                    "client_msg_id": client_id,
                    "trace_id": f"trace-{origin}",
                    "force_session": True,
                    "origin": origin,
                }).encode(),
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer secret-token",
                },
            )
            assert status == 200

        from lib.server_identity import get_server_info
        computer_id = get_server_info()["server_id"]
        status, body = _get(
            base + "/identity/prompt-history?session_id="
            + f"{computer_id}:claude",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert status == 200
        history = json.loads(body)
        assert history["contract"] == "user-prompt-history.v3"
        assert history["session"]["compatibility_session_slug"] == "claude"
        assert [row["text"] for row in history["prompts"]] == [
            "the user's durable prompt",
        ]
        assert history["prompts"][0]["prompt_origin"]["evidence"][
            "authenticated_at_admission"
        ] is True
        assert history["privacy"]["unknown_authorship_excluded"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_prompt_history_excludes_send_admitted_without_auth(
    running_server, monkeypatch,
):
    from lib import clarp_runner

    monkeypatch.setattr(
        clarp_runner,
        "spawn_turn",
        lambda **_kwargs: type("_FakeHandle", (), {"pid": 99})(),
    )
    base, ctx, _srv = running_server
    status, _body = _post(base + "/send", {
        "session": "claude",
        "text": "not authenticated at admission",
        "client_msg_id": "u-unauth-http",
        "trace_id": "trace-unauth-http",
        "force_session": True,
    })
    assert status == 200
    ctx.auth_token = "later-token"

    from lib.server_identity import get_server_info
    computer_id = get_server_info()["server_id"]
    status, body = _get(
        base + "/identity/prompt-history?session_id="
        + f"{computer_id}:claude",
        headers={"Authorization": "Bearer later-token"},
    )

    assert status == 200
    assert json.loads(body)["prompts"] == []


def test_prompt_history_preserves_routed_voice_admission(
    fake_ctx, monkeypatch,
):
    from lib import clarp_runner, orchestrator, settings_store

    settings_store.set_bool("orchestrator.enabled", True)
    monkeypatch.setattr(
        orchestrator,
        "call_model",
        lambda _packet, _settings: {
            "kind": "agent_message",
            "target_session": "rachel",
            "confidence": 0.99,
            "addressing": True,
            "text_to_send": "inspect the routed issue",
            "reason": "Rachel was explicitly addressed.",
        },
    )
    monkeypatch.setattr(
        clarp_runner,
        "spawn_turn",
        lambda **_kwargs: type("_FakeHandle", (), {"pid": 99})(),
    )
    fake_ctx.auth_token = "secret-token"
    port = _free_port()
    srv = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    original = "Rachel, inspect literal <team>code</team> please"
    try:
        status, body = _post_raw(
            base + "/send",
            json.dumps({
                "session": "claude",
                "text": original,
                "client_msg_id": "u-routed-voice",
                "trace_id": "trace-routed-voice",
                "hands_free": True,
                "transcription_id": "voice-route-1",
            }).encode(),
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer secret-token",
            },
        )
        assert status == 200
        assert json.loads(body)["session"] == "rachel"

        from lib.server_identity import get_server_info
        computer_id = get_server_info()["server_id"]
        status, body = _get(
            base + "/identity/prompt-history?session_id="
            + f"{computer_id}:rachel",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert status == 200
        history = json.loads(body)
        assert [row["text"] for row in history["prompts"]] == [original]
        assert history["prompts"][0]["prompt_origin"]["channel"] == "voice"
    finally:
        srv.shutdown()
        srv.server_close()


def test_get_diagnostics_health_returns_subsystem_state(running_server):
    from lib import health
    health.reset_for_tests()
    health.mark_success("tts_worker", now=123.0)
    base, _ctx, _srv = running_server

    status, body = _get(base + "/diagnostics/health")

    assert status == 200
    assert json.loads(body)["subsystems"]["tts_worker"]["last_success_at"] == 123.0


def test_diagnostics_settings_are_computer_owned_and_validated(running_server):
    base, _ctx, _srv = running_server

    status, body = _post(base + "/diagnostics/settings", {
        "enabled": True,
        "categories": ["requests", "database"],
    })
    assert status == 200
    settings = json.loads(body)["settings"]
    assert settings == {
        "enabled": True,
        "categories": ["requests", "database"],
        "retention_hours": 24,
        "rollup_retention_days": 30,
    }
    status, body = _get(base + "/diagnostics/settings")
    assert status == 200
    assert json.loads(body)["settings"] == settings

    with pytest.raises(urllib.error.HTTPError) as failure:
        _post(base + "/diagnostics/settings", {
            "enabled": True, "categories": ["unknown"],
        })
    assert failure.value.code == 400
    assert "unknown diagnostic categories" in json.loads(
        failure.value.read())["error"]


def test_disabled_client_diagnostics_are_accepted_without_capture(running_server):
    from lib import diagnostics_settings, telemetry
    base, _ctx, _srv = running_server
    diagnostics_settings.update({"enabled": False, "categories": []})
    status, body = _post(base + "/clog", {
        "events": [{"event": "ios.performance.frame", "detail": "private"}],
    })
    assert status == 200
    assert json.loads(body)["captured"] is False
    captured = telemetry.conn().execute(
        "SELECT count(*) FROM diagnostic_events "
        "WHERE source='client' AND event='ios.performance.frame'").fetchone()[0]
    assert captured == 0


def test_request_diagnostics_separate_handler_socket_and_database_time(
        running_server):
    from lib import diagnostics_settings, telemetry
    base, _ctx, _srv = running_server
    diagnostics_settings.update({
        "enabled": True, "categories": ["requests", "database"],
    })
    interaction_id = "12345678-1234-1234-1234-123456789abc"
    status, _body = _get(
        base + "/agents/snapshot",
        headers={"X-Clarp-Interaction-ID": interaction_id})
    assert status == 200
    row = None
    for _ in range(20):
        row = telemetry.conn().execute(
                """SELECT detail FROM diagnostic_events
                    WHERE source='server' AND event='httpRequest'
                      AND path='/agents/snapshot'
                      AND detail LIKE ?
                    ORDER BY event_id DESC LIMIT 1""",
                (f"%{interaction_id}%",)).fetchone()
        if row is not None:
            break
        time.sleep(0.01)
    assert row is not None
    phases = json.loads(row["detail"])["phases"]
    assert json.loads(row["detail"])["interaction_id"] == interaction_id
    assert phases["handler_ms"] >= 0
    assert phases["socket_write_ms"] >= 0
    assert phases["response_bytes"] > 0
    assert phases["database"]["query_count"] > 0
    assert phases["database"]["sqlite_ms"] >= 0
    assert "max_query" in phases["database"]


def test_get_backend_usage_reports_claude_from_turn_accounting(
        running_server, monkeypatch, tmp_path):
    """Claude usage comes from the turns Clarp ran, not from a statusline —
    a statusline never renders under `-p`, which is how every turn is spawned.
    """
    from lib import backend_usage, turn_usage

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex"))

    def offline(**_kwargs):
        raise RuntimeError("provider usage endpoints are unavailable in tests")

    for name in ("fetch_claude_usage", "fetch_codex_usage",
                 "fetch_codex_usage_app_server"):
        monkeypatch.setattr(backend_usage, name, offline)
    turn_usage.record(backend="claude", agent_id="a1", detail={
        "tokens_in": 1200, "tokens_out": 340, "cost_usd": 0.042,
        "duration_ms": 5100, "trace_id": "t-1",
    })
    base, _ctx, _srv = running_server

    status, body = _get(base + "/backend-usage")

    assert status == 200
    data = json.loads(body)
    assert data["schema_version"] == 1
    assert data["capability_catalog_schema_version"] == 2
    assert data["providers"]["codex"]["provider_instance_id"] \
        == f"{data['computer_id']}:codex"
    assert data["providers"]["codex"]["freshness"] == "unknown"
    # `providers` is a QUOTA view: percentages and reset times. Claude exposes
    # no quota endpoint and the statusline that used to guess at one is gone,
    # so "unknown" is the honest answer there — putting spend into a quota
    # shape would report a number that means something else. The spend figures
    # live in backends[].totals below.
    assert data["providers"]["claude"]["freshness"] == "unknown"
    assert data["providers"]["agy"]["freshness"] == "unknown"
    claude = next(row for row in data["backends"] if row["backend"] == "claude")
    codex = next(row for row in data["backends"] if row["backend"] == "codex")
    assert claude["source"] == "clarp-turn-accounting"
    # Spend, not headroom — no CLI reports the remaining allowance.
    assert claude["used_percentage"] is None
    assert claude["totals"]["five_hour"]["tokens_in"] == 1200
    assert claude["totals"]["five_hour"]["tokens_out"] == 340
    assert claude["totals"]["five_hour"]["turns"] == 1
    assert claude["freshness"] == "fresh"
    assert codex["freshness"] == "unknown"


def test_agent_model_options_endpoint_returns_backend_choices(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    from lib import provider_capabilities

    catalog = provider_capabilities._build_catalog(
        wall_time=1787738400,
        which=lambda binary: f"/bin/{binary}" if binary in {"codex", "agy"} else None,
        run=lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0,
            stdout=(
                "test-cli 1.0\n"
                if argv[-1] == "--version"
                else (
                    '{"models":[{"slug":"gpt-test","display_name":"GPT Test",'
                    '"visibility":"list","default_reasoning_level":"high",'
                    '"supported_reasoning_levels":['
                    '{"effort":"low"},{"effort":"high"}]}]}'
                    if "codex" in argv[0]
                    else "agy-test\tAGY Test\n"
                )
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        provider_capabilities, "capability_catalog",
        lambda **_kwargs: catalog,
    )

    status, body = _get(base + "/agent-model-options")

    assert status == 200
    response = json.loads(body)
    assert response["schema_version"] == 2
    assert response["freshness"] == "fresh"
    assert response["freshness_scope"] == "response_observation"
    assert response["providers"]["codex"]["cli_version"] == "test-cli 1.0"
    assert response["providers"]["codex"]["authenticated"] is None
    assert response["providers"]["codex"]["models"] == [{
        "id": "gpt-test",
        "label": "GPT Test",
        "default_effort": "high",
        "supported_efforts": ["low", "high"],
        "source": {
            "kind": "cli_probe",
            "detail": "codex debug models",
            "observed_at": response["observed_at"],
            "freshness": "fresh",
        },
    }]
    assert response["providers"]["agy"]["models"][0]["id"] == "agy-test"
    assert response["providers"]["agy"]["models"][0]["supported_efforts"] is None
    assert response["providers"]["agy"]["supported_efforts"] \
        == ["low", "medium", "high"]
    assert "backends" not in response


def test_agent_llm_accepts_agy_effort(running_server):
    base, _ctx, _srv = running_server
    from lib import agents as agents_db

    agent = agents_db.get_by_session("rachel")
    agents_db.update_agent(agent["agent_id"], backend="agy")
    status, body = _post(base + "/agent-llm", {
        "session": "rachel", "model": "", "effort": "high",
    })
    assert status == 200
    response = json.loads(body)
    assert response["backend"] == "agy"
    assert response["model"] == ""
    assert response["effort"] == "high"
    assert response["valid_efforts"] == ["low", "medium", "high"]


def test_agent_llm_rejects_effort_against_global_agy_model(
    running_server, monkeypatch,
):
    base, _ctx, _srv = running_server
    from lib import agents as agents_db
    from lib import config

    agent = agents_db.get_by_session("rachel")
    agents_db.update_agent(agent["agent_id"], backend="agy", model="", effort="")
    monkeypatch.setattr(
        config, "_CACHED",
        config.Config(agy_model="gemini-3.7-flash-low"))
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(base + "/agent-llm", {
            "session": "rachel", "effort": "high",
        })
    assert error.value.code == 400


@pytest.mark.parametrize("field,value", [("model", 48), ("effort", {"x": 1})])
def test_agent_llm_rejects_non_string_values(running_server, field, value):
    base, _ctx, _srv = running_server
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(base + "/agent-llm", {"session": "rachel", field: value})
    assert error.value.code == 400


def test_agent_heartbeat_endpoint_persists_server_flag(running_server):
    base, _ctx, _srv = running_server

    status, body = _post(base + "/agent-heartbeat", {
        "session": "claude",
        "heartbeat_enabled": True,
    })

    assert status == 200
    data = json.loads(body)
    assert data["heartbeat_enabled"] is True
    assert "heartbeat_interval_sec" not in data

    status, body = _get(base + "/agents/snapshot")
    assert status == 200
    agents = json.loads(body)["agents"]
    assert next(a for a in agents if a["session"] == "claude")["heartbeat_enabled"] is True


def test_agent_heartbeat_profile_returns_schedule_and_history(running_server):
    base, _ctx, _srv = running_server
    from lib import agents as agents_db, heartbeat, message_store

    agent = agents_db.get_by_session("claude")
    agents_db.update_agent(agent["agent_id"], heartbeat_enabled=True)
    message_store.store_transcript_turns(
        agent_id=agent["agent_id"], backend_session_id="heartbeat-profile",
        source_file="/tmp/heartbeat-profile.jsonl", turns=[
            {"role": "user", "text": heartbeat.HEARTBEAT_PROMPT, "timestamp": "1"},
            {"role": "assistant", "text": "HEARTBEAT_OK", "timestamp": "2"},
        ])

    status, body = _get(base + "/agent-heartbeat/status?session=claude")
    data = json.loads(body)

    assert status == 200
    assert data["schedule"]["enabled"] is True
    assert data["schedule"]["effective_interval_sec"] > 0
    assert data["history"][0]["text"] == "Heartbeat check: no action needed."

    _, snapshot_body = _get(base + "/agents/snapshot")
    agents = json.loads(snapshot_body)["agents"]
    assert next(a for a in agents if a["session"] == "claude")["heartbeat_enabled"] is True


def test_computer_heartbeat_endpoint_persists_full_policy_atomically(running_server):
    base, _ctx, _srv = running_server
    requested = {
        "heartbeat_interval_sec": 900,
        "heartbeat_backoff_strategy": "linear",
        "heartbeat_backoff_cap_sec": 3600,
        "heartbeat_dormant_after_noops": 0,
    }

    status, body = _post(base + "/heartbeat/settings", requested)

    assert status == 200
    data = json.loads(body)
    for key, value in requested.items():
        assert data["settings"][key] == value
    assert "durable goal/task plan" in data["heartbeat_prompt"]
    status, body = _get(base + "/heartbeat/settings")
    assert status == 200
    assert json.loads(body)["settings"] == requested

    with pytest.raises(urllib.error.HTTPError) as error:
        _post(base + "/heartbeat/settings", {
            **requested,
            "heartbeat_interval_sec": 4000,
            "heartbeat_backoff_cap_sec": 3000,
        })
    assert error.value.code == 400
    _, fresh_body = _get(base + "/heartbeat/settings")
    fresh = json.loads(fresh_body)["settings"]
    assert fresh["heartbeat_interval_sec"] == 900
    assert fresh["heartbeat_backoff_cap_sec"] == 3600


def test_agent_dreaming_endpoint_persists_server_flag(running_server):
    base, _ctx, _srv = running_server

    status, body = _post(base + "/agent-dreaming", {
        "session": "claude",
        "dreaming_enabled": True,
    })

    assert status == 200
    data = json.loads(body)
    assert data["dreaming_enabled"] is True
    assert data["dream_target_hour"] == 3
    assert data["dream_planned_rounds"] == 7
    assert data["dream_target_tokens"] == 70_000
    assert data["dream_min_directions"] == 3
    assert "Dream Digest" in data["dream_prompt"]
    assert "DREAMING_OK" in data["dream_prompt"]
    assert "no shared-tree" in data["dream_prompt"]

    status, body = _get(base + "/agents/snapshot")
    assert status == 200
    agents = json.loads(body)["agents"]
    assert next(a for a in agents if a["session"] == "claude")["dreaming_enabled"] is True


def test_agent_mute_endpoint_persists_server_flag(running_server):
    base, _ctx, _srv = running_server

    status, body = _post(base + "/agent-mute", {
        "session": "claude",
        "muted": True,
    })

    assert status == 200
    data = json.loads(body)
    assert data["muted"] is True

    status, body = _get(base + "/agents/snapshot")
    assert status == 200
    agents = json.loads(body)["agents"]
    assert next(a for a in agents if a["session"] == "claude")["muted"] is True


def test_team_nudging_endpoint_persists_server_flag(running_server):
    base, _ctx, _srv = running_server
    from lib import team_store

    team = team_store.create_team("Ops")

    status, body = _post(base + "/team-nudging", {
        "team_id": team["team_id"],
        "nudge_enabled": False,
    })

    assert status == 200
    data = json.loads(body)
    assert data["team"]["nudge_enabled"] is False

    status, body = _get(base + "/teams")
    assert status == 200
    teams = json.loads(body)["teams"]
    assert next(t for t in teams if t["team_id"] == team["team_id"])[
        "nudge_enabled"
    ] is False


def test_dreaming_runs_endpoint_returns_budget_contract(running_server):
    base, _ctx, _srv = running_server

    status, body = _get(base + "/dreaming/runs?session=claude")

    assert status == 200
    data = json.loads(body)
    assert data["runs"] == []
    assert data["contract"]["target_hour"] == 3
    assert data["contract"]["min_directions"] == 3
    assert data["contract"]["planned_directions"] == 3
    assert data["contract"]["planned_rounds"] == 7
    assert data["contract"]["target_tokens"] == 70_000


def test_favorite_paths_endpoint_returns_server_side_usage(running_server):
    base, ctx, _srv = running_server
    from lib import agents as agents_db

    agents_db.record_path_usage(str(ctx.root))
    agents_db.record_path_usage(str(ctx.root))
    agents_db.record_path_usage(str(ctx.root / "unused"))

    status, body = _get(base + "/favorite-paths?limit=2")

    assert status == 200
    paths = json.loads(body)["paths"]
    assert [row["path"] for row in paths] == [str(ctx.root), str(ctx.root / "unused")]
    assert paths[0]["use_count"] == 2


def test_location_round_trip_and_request_event(running_server):
    base, ctx, _srv = running_server
    events = ctx.stream.subscribe()
    try:
        status, body = _post(base + "/location", {
            "session": "rachel",
            "lat": 59.9139,
            "lng": 10.7522,
            "accuracy": 12.5,
        })
        assert status == 200
        stored = json.loads(body)
        assert stored["ok"] is True
        assert stored["session"] == "rachel"

        status, body = _get(base + "/location?session=rachel")
        assert status == 200
        fetched = json.loads(body)
        assert fetched["lat"] == 59.9139
        assert fetched["lng"] == 10.7522
        assert fetched["accuracy"] == 12.5
        assert isinstance(fetched["ts"], int)

        status, _ = _post(base + "/location/request", {"session": "rachel"})
        assert status == 200
        event = json.loads(events.get(timeout=1))
        assert event["type"] == "location-request"
        assert event["session"] == "rachel"
    finally:
        ctx.stream.unsubscribe(events)


def test_calendar_request_broadcasts_agent_calendar_event(running_server):
    base, ctx, _srv = running_server
    events = ctx.stream.subscribe()
    try:
        status, body = _post(base + "/calendar/request", {
            "session": "rachel",
            "title": "Planning",
            "start": "2026-06-24T15:00:00+02:00",
            "end": "2026-06-24T15:30:00+02:00",
            "time_zone": "Europe/Oslo",
            "location": "Office",
            "notes": "Bring agenda",
        })

        assert status == 200
        response = json.loads(body)
        assert response["ok"] is True
        assert response["request_id"].startswith("cal-")
        event = json.loads(events.get(timeout=1))
        assert event["type"] == "calendar-request"
        assert event["request_id"] == response["request_id"]
        assert event["session"] == "rachel"
        assert event["title"] == "Planning"
        assert event["start"] == "2026-06-24T15:00:00+02:00"
        assert event["end"] == "2026-06-24T15:30:00+02:00"
        assert event["time_zone"] == "Europe/Oslo"
        assert event["location"] == "Office"
        assert event["notes"] == "Bring agenda"
    finally:
        ctx.stream.unsubscribe(events)


def test_metrickit_hang_payload_is_preserved_with_stack_tree(
    running_server, tmp_path, monkeypatch,
):
    from lib import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.sqlite")
    base, _ctx, _srv = running_server
    payload = {
        "hangDiagnostics": [{
            "hangDuration": "2.7 sec",
            "callStackTree": {"callStacks": [{"threadAttributed": True}]},
        }],
        "crashDiagnostics": [],
    }

    status, body = _post(base + "/crash", payload)

    assert status == 200
    assert json.loads(body)["ok"] is True
    stored = list((tmp_path / "crashes").glob("ios-diagnostic-*.json"))
    assert len(stored) == 1
    assert json.loads(stored[0].read_text()) == payload


def test_location_rejects_out_of_range_coordinates(running_server):
    base, _ctx, _srv = running_server
    req = urllib.request.Request(
        base + "/location",
        data=json.dumps({"session": "rachel", "lat": 95, "lng": 10}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=2)
    assert exc.value.code == 400


def test_auth_accepts_cookie_for_headerless_transports(fake_ctx):
    fake_ctx.auth_token = "secret-token"
    port = _free_port()
    srv = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        req = urllib.request.Request(
            base + "/agents/snapshot",
            headers={"Cookie": "claude_pwa_token=secret-token"},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_large_json_responses_are_gzipped_when_requested(running_server):
    import gzip

    base, _ctx, _srv = running_server
    req = urllib.request.Request(
        base + "/agents/snapshot",
        headers={"Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=2) as response:
        encoded = response.read()
        assert response.headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(encoded))["agents"]


def test_post_send_dispatches_text_via_clarp_runner(running_server, monkeypatch):
    """/send spawns clarp -p for each turn.
    Monkeypatch the runner so we don't actually need clarp on PATH, and
    assert the handler called it with the user's text."""
    base, ctx, _srv = running_server
    calls: list[dict] = []
    class _FakeHandle:
        def __init__(self): self.pid = 99
    def fake_spawn(*, text, cwd, **metadata):
        calls.append({"text": text, "cwd": str(cwd),
                      **metadata})
        return _FakeHandle()
    from lib import clarp_runner
    monkeypatch.setattr(clarp_runner, "spawn_turn", fake_spawn)

    status, _ = _post(base + "/send", {"text": "hello mike", "session": "claude"})
    assert status == 200
    assert len(calls) == 1, (
        f"clarp_runner.spawn_turn should be called once per /send; "
        f"got {len(calls)} calls"
    )
    assert calls[0]["text"] == "hello mike"
    assert calls[0]["cwd"] == str(ctx.root)


def test_post_send_retry_with_same_client_id_dispatches_once(running_server, monkeypatch):
    """An ambiguous HTTP timeout may retry; admission must be idempotent."""
    base, _ctx, _srv = running_server
    calls: list[dict] = []

    class _FakeHandle:
        pid = 99

    def fake_spawn(*, text, **metadata):
        calls.append({"text": text, **metadata})
        return _FakeHandle()

    from lib import clarp_runner
    monkeypatch.setattr(clarp_runner, "spawn_turn", fake_spawn)
    payload = {
        "text": "deliver exactly once",
        "session": "claude",
        "client_msg_id": "stable-message-1",
    }

    first_status, _ = _post(base + "/send", payload)
    retry_status, _ = _post(base + "/send", payload)

    assert first_status == retry_status == 200
    assert len(calls) == 1


def test_successful_send_releases_cached_transcription(running_server, monkeypatch):
    base, _ctx, _srv = running_server

    class _FakeHandle:
        pid = 99

    from lib import clarp_runner, db
    monkeypatch.setattr(
        clarp_runner, "spawn_turn", lambda **_metadata: _FakeHandle())
    headers = {
        "Content-Type": "audio/webm",
        "X-Transcription-ID": "recording-cleanup-send",
    }
    assert _post_raw(base + "/transcribe", b"voice audio", headers)[0] == 200

    status, _ = _post(base + "/send", {
        "text": "hello there",
        "session": "claude",
        "client_msg_id": "u-recording-cleanup-send",
        "transcription_id": "recording-cleanup-send",
    })

    assert status == 200
    assert db.conn().execute(
        "SELECT 1 FROM transcription_results WHERE job_id = ?",
        ("recording-cleanup-send",),
    ).fetchone() is None


def test_discard_endpoint_releases_cached_transcription(running_server):
    base, _ctx, _srv = running_server
    headers = {
        "Content-Type": "audio/webm",
        "X-Transcription-ID": "recording-cleanup-discard",
    }
    assert _post_raw(base + "/transcribe", b"voice audio", headers)[0] == 200

    status, _ = _get(
        base + "/transcription-results/recording-cleanup-discard",
        method="DELETE",
    )

    assert status == 200
    from lib import db
    assert db.conn().execute(
        "SELECT 1 FROM transcription_results WHERE job_id = ?",
        ("recording-cleanup-discard",),
    ).fetchone() is None


def test_discard_waits_for_inflight_transcription_then_deletes(running_server):
    base, ctx, _srv = running_server
    started = threading.Event()
    release = threading.Event()

    class SlowSTT(StubSTT):
        def transcribe_bytes(self, *args, **kwargs):
            started.set()
            assert release.wait(timeout=2)
            return super().transcribe_bytes(*args, **kwargs)

    ctx.stt = SlowSTT(text="discard me")
    transcription_result = []
    discard_result = []

    def transcribe():
        transcription_result.append(_post_raw(
            base + "/transcribe", b"voice audio",
            {"Content-Type": "audio/webm",
             "X-Transcription-ID": "recording-inflight-discard"},
        ))

    def discard():
        discard_result.append(_get(
            base + "/transcription-results/recording-inflight-discard",
            method="DELETE",
        ))

    transcribe_thread = threading.Thread(target=transcribe)
    discard_thread = threading.Thread(target=discard)
    transcribe_thread.start()
    assert started.wait(timeout=1)
    discard_thread.start()
    time.sleep(0.05)
    release.set()
    transcribe_thread.join(timeout=2)
    discard_thread.join(timeout=2)

    assert transcription_result[0][0] == 200
    assert discard_result[0][0] == 200
    from lib import db
    assert db.conn().execute(
        "SELECT 1 FROM transcription_results WHERE job_id = ?",
        ("recording-inflight-discard",),
    ).fetchone() is None


def test_orchestrator_settings_round_trip(running_server):
    base, _ctx, _srv = running_server

    status, body = _get(base + "/orchestrator/settings")
    assert status == 200
    initial = json.loads(body)
    before = initial["settings"]
    assert before["voice_id"] == "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e"
    assert "ignored_decisions" in initial

    status, body = _post(base + "/orchestrator/settings", {
        "enabled": False,
        "fallback_only": True,
        "confidence_threshold": 0.84,
        "model": "gemini-flash-3.1",
        "effort": "low_latency",
        "timeout_ms": 1250,
    })

    assert status == 200
    after = json.loads(body)["settings"]
    assert after["enabled"] is False
    assert after["fallback_only"] is True
    assert after["confidence_threshold"] == 0.84
    assert after["model"] == "gemini-flash-3.1"
    assert after["timeout_ms"] == 1250


def test_dreaming_settings_round_trip(running_server):
    base, _ctx, _srv = running_server

    status, body = _get(base + "/dreaming/settings")
    assert status == 200
    initial = json.loads(body)
    assert initial["settings"]["dreams_per_night"] == 1
    assert initial["settings"]["direction_count"] == 3
    assert initial["settings"]["target_token_budget"] == 70000
    assert initial["limits"]["direction_count"] == [2, 8]

    status, body = _put(base + "/dreaming/settings", {
        "dreams_per_night": 4,
        "direction_count": 6,
        "target_token_budget": 180000,
    })

    assert status == 200
    updated = json.loads(body)["settings"]
    assert updated["dreams_per_night"] == 4
    assert updated["direction_count"] == 6
    assert updated["planned_rounds"] == 10
    assert updated["target_token_budget"] == 180000

    status, body = _get(base + "/dreaming/runs")
    assert status == 200
    assert json.loads(body)["contract"]["direction_count"] == 6


def test_orchestrator_settings_returns_ignored_decisions(running_server):
    base, _ctx, _srv = running_server
    from lib.db import conn, now_ms

    conn().execute(
        """INSERT INTO orchestrator_decisions (
               trace_id, utterance, requested_session, hands_free, enabled,
               provider, model, effort, latency_ms, context_hash,
               context_agent_count, context_message_count, decision_kind,
               target_session, confidence, addressing, mentioned_sessions_json,
               name_corrections_json, candidate_scores_json, reason,
               raw_response_json, final_action, fallback_used, phrase_key,
               error, created_at
           ) VALUES (
               'trace-ignore', 'nonsense background words', 'claude', 1, 1,
               'agy', 'gemini-flash-3.1', 'low_latency', 5, 'hash',
               2, 0, 'ignored', 'claude', 0.92, 0, '[]', '[]', '[]',
               'accidental dictation', '{}', 'ignored', 0, '', '', ?
           )""",
        (now_ms(),),
    )

    status, body = _get(base + "/orchestrator/settings")

    assert status == 200
    ignored = json.loads(body)["ignored_decisions"]
    assert ignored[0]["utterance"] == "nonsense background words"
    assert ignored[0]["final_action"] == "ignored"


def test_post_send_returns_orchestrator_clarification_without_dispatch(
    running_server, monkeypatch
):
    base, _ctx, _srv = running_server

    class FakeOrchestrator:
        def __init__(self, _ctx):
            pass

        def handle_send(self, **_kwargs):
            return SimpleNamespace(
                action="clarify",
                ok=True,
                session="claude",
                dispatch="",
                trace_id="trace",
                decision_id=123,
                decision={"kind": "ambiguous"},
                error="",
                status=200,
            )

    monkeypatch.setattr(server_module, "OrchestratorService", FakeOrchestrator)

    status, body = _post(base + "/send", {
        "text": "can you check this",
        "session": "claude",
        "hands_free": True,
    })

    assert status == 200
    data = json.loads(body)
    assert data["orchestrator"]["action"] == "clarify"
    assert data["dispatch"] == ""


def test_failed_delegation_endpoint_marks_request_as_fallback_without_dispatch(
    running_server, monkeypatch
):
    base, _ctx, _srv = running_server
    observed = []

    class FakeOrchestrator:
        def __init__(self, _ctx):
            pass

        def handle_send(self, **kwargs):
            observed.append(kwargs["fallback_request"])
            return SimpleNamespace(
                action="clarify",
                ok=True,
                session="mike",
                dispatch="",
                trace_id="trace",
                decision_id=124,
                decision={
                    "kind": "ambiguous",
                    "target_session": "mike",
                    "confidence": 0.61,
                },
                error="",
                status=200,
            )

    monkeypatch.setattr(server_module, "OrchestratorService", FakeOrchestrator)

    status, body = _post(base + "/orchestrator/route-delegation", {
        "text": "finish the thing we discussed",
        "session": "mike",
        "hands_free": True,
        "client_msg_id": "u-held",
    })

    assert status == 200
    data = json.loads(body)
    assert observed == [True]
    assert data["orchestrator"]["action"] == "clarify"
    assert data["dispatch"] == ""


def test_post_send_orchestrator_fallback_uses_legacy_dispatch(
    running_server, monkeypatch
):
    base, _ctx, _srv = running_server
    calls: list[dict] = []

    class FakeOrchestrator:
        def __init__(self, _ctx):
            pass

        def handle_send(self, **_kwargs):
            return SimpleNamespace(action=server_module.FINAL_FALLBACK)

    def fake_spawn(*, text, cwd, **metadata):
        calls.append({"text": text, "cwd": str(cwd), **metadata})
        return type("_FakeHandle", (), {"pid": 99})()

    from lib import clarp_runner
    monkeypatch.setattr(server_module, "OrchestratorService", FakeOrchestrator)
    monkeypatch.setattr(clarp_runner, "spawn_turn", fake_spawn)

    status, _ = _post(base + "/send", {
        "text": "hello fallback",
        "session": "claude",
        "hands_free": True,
    })

    assert status == 200
    assert calls and calls[0]["text"] == "hello fallback"


def test_server_info_is_stable(running_server):
    base, _ctx, _srv = running_server

    _, first = _get(base + "/server-info")
    _, second = _get(base + "/server-info")

    assert json.loads(first) == json.loads(second)
    assert json.loads(first)["server_id"]


def test_post_send_forwards_silent_turn_policy(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    calls: list[dict] = []

    def fake_spawn(*, text, cwd, **metadata):
        calls.append(metadata)
        return type("_FakeHandle", (), {"pid": 99})()

    from lib import clarp_runner
    monkeypatch.setattr(clarp_runner, "spawn_turn", fake_spawn)

    status, _ = _post(base + "/send", {
        "text": "silent", "session": "claude", "synthesize_audio": False,
    })

    assert status == 200
    marker = _claude_source_marker()
    assert marker.read_text().rstrip().endswith(" 0")


def _claude_source_marker() -> pathlib.Path:
    cache = pathlib.Path(os.environ.get(
        "CLARP_CACHE_DIR", pathlib.Path.home() / ".cache/clarp"))
    return cache / "source-markers" / "claude"


def _unlink_claude_source_marker() -> pathlib.Path:
    marker = _claude_source_marker()
    marker.unlink(missing_ok=True)
    return marker


def test_post_send_defaults_user_origin_to_audio(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    marker = _unlink_claude_source_marker()

    def fake_spawn(*, text, cwd, **metadata):
        return type("_FakeHandle", (), {"pid": 99})()

    from lib import clarp_runner
    monkeypatch.setattr(clarp_runner, "spawn_turn", fake_spawn)

    status, _ = _post(base + "/send", {"text": "normal", "session": "claude"})

    assert status == 200
    assert marker.read_text().rstrip().endswith(" 1")


def test_oracle_status_and_delegation_endpoints(running_server, monkeypatch):
    base, ctx, _srv = running_server
    from lib import oracle_delegations, oracle_realtime
    ctx.auth_token = "administrator-token"
    headers = {"Authorization": "Bearer administrator-token"}

    monkeypatch.setattr(oracle_realtime, "capability", lambda: {
        "available": True,
        "model": "gpt-realtime-2.1",
        "voice": "cedar",
        "transport": "clarp-websocket-proxy",
    })
    status, body = _get(base + "/oracle/status", headers=headers)
    assert status == 200
    assert json.loads(body)["available"] is True

    dispatched = {}

    def fake_dispatch(**kwargs):
        dispatched.update(kwargs)
        return {
            "delegation_id": kwargs["delegation_id"],
            "session": kwargs["session"],
            "status": "accepted",
        }

    monkeypatch.setattr(oracle_delegations, "dispatch", fake_dispatch)
    status, body = _post_with_headers(base + "/oracle/delegations", {
        "delegation_id": "integration-1",
        "session": "rachel",
        "request": "Check the deployment",
    }, headers)
    assert status == 200
    assert json.loads(body)["delegation"] == {
        "delegation_id": "integration-1",
        "session": "rachel",
        "status": "accepted",
    }
    assert dispatched["authenticated_at_admission"] is True


def test_oracle_delegation_ack_rejects_nonterminal_work(running_server):
    base, ctx, _srv = running_server
    from lib import agents, oracle_delegations
    ctx.auth_token = "administrator-token"
    headers = {"Authorization": "Bearer administrator-token"}
    agent = agents.get_by_session("rachel")
    oracle_delegations.begin(
        delegation_id="pending-1", trace_id="oracle-pending-1",
        client_msg_id="oracle-pending-1", agent_id=agent["agent_id"],
        session="rachel", request_text="Still working",
    )

    with pytest.raises(urllib.error.HTTPError) as error:
        _post_with_headers(base + "/oracle/delegations/ack", {
            "delegation_id": "pending-1",
        }, headers)
    assert error.value.code == 404


def test_oracle_cancel_agent_uses_all_durable_owner_rows(running_server):
    base, ctx, _srv = running_server
    from lib import agents, oracle_delegations
    ctx.auth_token = "administrator-token"
    headers = {"Authorization": "Bearer administrator-token"}
    agent = agents.get_by_session("rachel")
    for suffix in ("a", "b"):
        oracle_delegations.begin(
            delegation_id=f"cancel-agent-{suffix}",
            trace_id=f"oracle-cancel-agent-{suffix}",
            client_msg_id=f"oracle-cancel-agent-{suffix}",
            agent_id=agent["agent_id"], session="rachel",
            request_text=f"Durable work {suffix}")

    status, body = _post_with_headers(base + "/oracle/delegations/cancel", {
        "session": "rachel",
    }, headers)

    assert status == 200
    assert json.loads(body)["cancelled_count"] == 2
    assert oracle_delegations.get("cancel-agent-a")["status"] == "cancelled"
    assert oracle_delegations.get("cancel-agent-b")["status"] == "cancelled"


def test_oracle_cancel_failure_restores_live_turn_ownership(
    running_server, monkeypatch,
):
    base, ctx, _srv = running_server
    from lib import agents, backends, message_store, oracle_delegations, turn_dispatch
    ctx.auth_token = "administrator-token"
    headers = {"Authorization": "Bearer administrator-token"}
    agent = agents.get_by_session("rachel")
    trace_id = "oracle-cancel-stop-failure"
    oracle_delegations.begin(
        delegation_id="cancel-stop-failure", trace_id=trace_id,
        client_msg_id=trace_id, agent_id=agent["agent_id"],
        session="rachel", request_text="Must stay recoverable")
    message_store.record_user_message(
        agent_id=agent["agent_id"], backend_session_id="backend-1",
        client_msg_id=trace_id, text="Must stay recoverable")
    with turn_dispatch._TURN_LOCK:
        turn_dispatch._INFLIGHT[agent["agent_id"]] = trace_id
        queued_marker = object()
        turn_dispatch._QUEUED[agent["agent_id"]] = [queued_marker]
    admitted_during_stop = object()

    def fail_interrupt(*_args, **_kwargs):
        with turn_dispatch._TURN_LOCK:
            turn_dispatch._QUEUED.setdefault(agent["agent_id"], []).append(
                admitted_during_stop)
        raise RuntimeError("local interrupt failed")

    monkeypatch.setattr(backends, "interrupt", fail_interrupt)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            _post_with_headers(base + "/oracle/delegations/cancel", {
                "session": "rachel",
            }, headers)
        assert error.value.code == 502
        assert oracle_delegations.get("cancel-stop-failure")["status"] == "accepted"
        assert turn_dispatch.owns_inflight_trace(agent["agent_id"], trace_id)
        with turn_dispatch._TURN_LOCK:
            assert turn_dispatch._QUEUED[agent["agent_id"]] == [
                queued_marker, admitted_during_stop]
    finally:
        with turn_dispatch._TURN_LOCK:
            turn_dispatch._INFLIGHT.pop(agent["agent_id"], None)
            turn_dispatch._QUEUED.pop(agent["agent_id"], None)


def test_oracle_cancel_survives_post_interrupt_bookkeeping_failure(
    running_server, monkeypatch,
):
    base, ctx, _srv = running_server
    from lib import agents, oracle_delegations
    ctx.auth_token = "administrator-token"
    headers = {"Authorization": "Bearer administrator-token"}
    agent = agents.get_by_session("rachel")
    oracle_delegations.begin(
        delegation_id="bookkeeping-failure",
        trace_id="oracle-bookkeeping-failure",
        client_msg_id="oracle-bookkeeping-failure",
        agent_id=agent["agent_id"], session="rachel",
        request_text="Cancel despite UI bookkeeping")
    monkeypatch.setattr(
        agents, "record_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("state write failed")))

    status, body = _post_with_headers(
        base + "/oracle/delegations/cancel", {"session": "rachel"}, headers)

    assert status == 200
    assert json.loads(body)["cancelled_count"] == 1
    assert oracle_delegations.get("bookkeeping-failure")["status"] == "cancelled"


def test_oracle_cancel_zero_interrupt_restores_owned_turn(
    running_server, monkeypatch,
):
    base, ctx, _srv = running_server
    from lib import agents, backends, oracle_delegations, turn_dispatch
    ctx.auth_token = "administrator-token"
    headers = {"Authorization": "Bearer administrator-token"}
    agent = agents.get_by_session("rachel")
    trace_id = "oracle-zero-interrupt"
    oracle_delegations.begin(
        delegation_id="zero-interrupt", trace_id=trace_id,
        client_msg_id=trace_id, agent_id=agent["agent_id"],
        session="rachel", request_text="Do not falsely cancel")
    with turn_dispatch._TURN_LOCK:
        turn_dispatch._INFLIGHT[agent["agent_id"]] = trace_id
    monkeypatch.setattr(backends, "interrupt", lambda *_args, **_kwargs: 0)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            _post_with_headers(base + "/oracle/delegations/cancel", {
                "session": "rachel",
            }, headers)
        assert error.value.code == 502
        assert oracle_delegations.get("zero-interrupt")["status"] == "accepted"
        assert turn_dispatch.owns_inflight_trace(agent["agent_id"], trace_id)
    finally:
        with turn_dispatch._TURN_LOCK:
            turn_dispatch._INFLIGHT.pop(agent["agent_id"], None)


def test_limited_device_cannot_read_or_stream_oracle(running_server, monkeypatch):
    base, ctx, _srv = running_server
    from lib import device_pairing
    ctx.auth_token = "administrator-token"
    monkeypatch.setattr(device_pairing, "authenticate", lambda token: (
        {"device_id": "limited-phone", "scope": "limited"}
        if token == "limited-token" else None))
    headers = {"Authorization": "Bearer limited-token"}

    for path in ("/oracle/status", "/oracle/delegations", "/oracle/realtime"):
        with pytest.raises(urllib.error.HTTPError) as error:
            _get(base + path, headers=headers)
        assert error.value.code == 403


def test_auth_disabled_server_still_rejects_oracle_routes(running_server):
    base, ctx, _srv = running_server
    ctx.auth_token = ""

    for method, path in (
        ("get", "/oracle/status"),
        ("get", "/oracle/delegations"),
        ("post", "/oracle/delegations"),
    ):
        with pytest.raises(urllib.error.HTTPError) as error:
            if method == "get":
                _get(base + path)
            else:
                _post(base + path, {
                    "delegation_id": "unauthenticated",
                    "session": "rachel", "request": "Do not run",
                })
        assert error.value.code == 401


def test_oracle_results_cannot_cross_full_device_principals(
    running_server, monkeypatch,
):
    base, ctx, _srv = running_server
    from lib import agents, device_pairing, oracle_delegations
    ctx.auth_token = "administrator-token"
    monkeypatch.setattr(device_pairing, "authenticate", lambda token: (
        {"device_id": token, "scope": "full"}
        if token in {"phone-a", "phone-b"} else None))
    agent = agents.get_by_session("rachel")
    oracle_delegations.begin(
        delegation_id="private-a", trace_id="oracle-private-a",
        client_msg_id="oracle-private-a", agent_id=agent["agent_id"],
        session="rachel", request_text="Private result",
        owner_principal="phone-a")
    oracle_delegations.complete_for_trace(
        trace_id="oracle-private-a", message_id="answer-private-a",
        text="Only phone A should hear this")

    _, body = _get(base + "/oracle/delegations", headers={
        "Authorization": "Bearer phone-b",
    })
    assert json.loads(body)["delegations"] == []
    _, body = _get(base + "/oracle/delegations", headers={
        "Authorization": "Bearer phone-a",
    })
    assert [row["delegation_id"] for row in json.loads(body)["delegations"]] \
        == ["private-a"]
    with pytest.raises(urllib.error.HTTPError) as error:
        _post_with_headers(base + "/oracle/delegations/ack", {
            "delegation_id": "private-a",
        }, {"Authorization": "Bearer phone-b"})
    assert error.value.code == 404


def test_post_send_defaults_sender_origin_to_silent(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    marker = _unlink_claude_source_marker()

    def fake_spawn(*, text, cwd, **metadata):
        return type("_FakeHandle", (), {"pid": 99})()

    from lib import clarp_runner
    monkeypatch.setattr(clarp_runner, "spawn_turn", fake_spawn)

    status, _ = _post(base + "/send", {
        "text": "agent coordination",
        "session": "claude",
        "sender": "rachel",
    })

    assert status == 200
    assert marker.read_text().rstrip().endswith(" 0")


def test_post_send_allows_agent_origin_to_opt_into_audio(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    marker = _unlink_claude_source_marker()

    def fake_spawn(*, text, cwd, **metadata):
        return type("_FakeHandle", (), {"pid": 99})()

    from lib import clarp_runner
    monkeypatch.setattr(clarp_runner, "spawn_turn", fake_spawn)

    status, _ = _post(base + "/send", {
        "text": "agent coordination but voiced",
        "session": "claude",
        "sender": "rachel",
        "synthesize_audio": True,
    })

    assert status == 200
    assert marker.read_text().rstrip().endswith(" 1")


def test_post_transcribe_uses_stub_stt(running_server):
    base, ctx, _srv = running_server
    req = urllib.request.Request(
        base + "/transcribe",
        data=b"\x00" * 100,
        headers={"Content-Type": "audio/webm"}, method="POST")
    with urllib.request.urlopen(req, timeout=2) as r:
        body = json.loads(r.read())
    # Response now also carries a per-turn trace_id; check fixed fields only.
    assert body["text"] == "hello there"
    assert body["ends_terminal"] is True
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 16
    assert body["hands_free"] is False
    # Stub recorded the call.
    assert len(ctx.stt.calls) == 1
    assert ctx.stt.calls[0][2] == ""


def test_post_transcribe_retry_returns_cached_result_without_recomputing(running_server):
    base, ctx, _srv = running_server
    headers = {
        "Content-Type": "audio/webm",
        "X-Transcription-ID": "durable-recording-1",
    }

    first_status, first_body = _post_raw(
        base + "/transcribe", b"same audio", headers)
    second_status, second_body = _post_raw(
        base + "/transcribe", b"same audio", headers)

    assert first_status == second_status == 200
    first = json.loads(first_body)
    second = json.loads(second_body)
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["text"] == first["text"]
    assert second["trace_id"] == first["trace_id"]
    assert len(ctx.stt.calls) == 1


def test_incomplete_transcription_upload_does_not_poison_retry_cache(running_server):
    base, ctx, _srv = running_server
    full_audio = b"complete durable audio"
    headers = {
        "Content-Type": "audio/webm",
        "X-Transcription-ID": "durable-recording-short-read",
    }

    status, body = _post_truncated_raw(
        base + "/transcribe", full_audio[:8], len(full_audio), headers)

    assert status == 408
    assert json.loads(body) == {"error": "incomplete audio upload"}
    assert ctx.stt.calls == []
    from lib import db
    assert db.conn().execute(
        "SELECT 1 FROM transcription_results WHERE job_id = ?",
        ("durable-recording-short-read",),
    ).fetchone() is None

    retry_status, retry_body = _post_raw(
        base + "/transcribe", full_audio, headers)
    assert retry_status == 200
    assert json.loads(retry_body)["cached"] is False
    assert len(ctx.stt.calls) == 1


def test_concurrent_transcription_retry_coalesces_inflight_work(running_server):
    base, ctx, _srv = running_server
    started = threading.Event()
    release = threading.Event()

    class SlowSTT(StubSTT):
        def transcribe_bytes(self, *args, **kwargs):
            started.set()
            assert release.wait(timeout=2)
            return super().transcribe_bytes(*args, **kwargs)

    ctx.stt = SlowSTT(text="computed once", ends_terminal=True)
    headers = {
        "Content-Type": "audio/webm",
        "X-Transcription-ID": "durable-recording-inflight",
    }
    results = []

    def post():
        results.append(_post_raw(base + "/transcribe", b"same audio", headers))

    first = threading.Thread(target=post)
    second = threading.Thread(target=post)
    first.start()
    assert started.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(results) == 2
    bodies = [json.loads(body) for status, body in results if status == 200]
    assert sorted(body["cached"] for body in bodies) == [False, True]
    assert len({body["trace_id"] for body in bodies}) == 1
    assert len(ctx.stt.calls) == 1
    from lib import transcription_results
    assert "durable-recording-inflight" not in transcription_results._locks


def test_post_transcribe_rejects_job_id_reused_for_different_audio(running_server):
    base, ctx, _srv = running_server
    headers = {
        "Content-Type": "audio/webm",
        "X-Transcription-ID": "durable-recording-collision",
    }
    assert _post_raw(base + "/transcribe", b"first audio", headers)[0] == 200

    with pytest.raises(urllib.error.HTTPError) as error:
        _post_raw(base + "/transcribe", b"different audio", headers)

    assert error.value.code == 409
    assert len(ctx.stt.calls) == 1


def test_new_transcription_prunes_abandoned_results_after_retention(running_server):
    base, _ctx, _srv = running_server
    from lib import db, transcription_results
    db.conn().execute(
        "INSERT INTO transcription_results "
        "(job_id, request_sha256, response_json, created_at) VALUES (?, ?, ?, ?)",
        ("abandoned-recording", "hash", "{}",
         db.now_ms() - transcription_results._RETENTION_MS - 1),
    )

    status, _ = _post_raw(
        base + "/transcribe", b"new audio",
        {"Content-Type": "audio/webm", "X-Transcription-ID": "new-recording"},
    )

    assert status == 200
    assert db.conn().execute(
        "SELECT 1 FROM transcription_results WHERE job_id = 'abandoned-recording'"
    ).fetchone() is None


def test_transcription_capabilities_advertise_server_default(running_server):
    base, _ctx, _srv = running_server
    with urllib.request.urlopen(base + "/transcription-capabilities", timeout=2) as response:
        body = json.loads(response.read())
    assert body["available"] is True
    assert body["default_model"] == "server-default"
    assert body["models"][0]["id"] == "server-default"


def test_disabled_transcription_keeps_server_health_ready(running_server, monkeypatch):
    base, ctx, _srv = running_server
    from lib.stt import DisabledSTT
    from lib import stt as stt_module, transcription_models
    monkeypatch.setattr(transcription_models, "catalog_status", lambda: [])
    monkeypatch.setattr(stt_module, "installed_transcription_models", lambda: [])
    ctx.stt = DisabledSTT()
    with urllib.request.urlopen(base + "/diagnostics/health", timeout=2) as response:
        health_body = json.loads(response.read())
    with urllib.request.urlopen(base + "/transcription-capabilities", timeout=2) as response:
        capability_body = json.loads(response.read())
    assert health_body["ready"] is True
    assert health_body["checks"]["stt_ready"] is True
    assert capability_body["available"] is False


def test_post_transcribe_passes_selected_installed_model(running_server):
    base, ctx, _srv = running_server

    class SelectableSTT(StubSTT):
        def __init__(self):
            super().__init__(text="selected")
            self.selected = []

        def transcribe_model_bytes(self, model_id, audio_bytes, content_type,
                                   vocab_prompt, *, wait=0.0):
            self.selected.append(model_id)
            return self.transcribe_bytes(
                audio_bytes, content_type, vocab_prompt, wait=wait)

    ctx.stt = SelectableSTT()
    status, body = _post_raw(
        base + "/transcribe", b"\x00" * 100,
        {"Content-Type": "audio/webm", "X-Transcription-Model": "whisper:medium"},
    )
    assert status == 200
    assert json.loads(body)["text"] == "selected"
    assert ctx.stt.selected == ["whisper:medium"]


def test_custom_stt_adapter_is_discovered_and_serves_transcribe(
        running_server, tmp_path, monkeypatch):
    from lib import custom_stt_adapters
    from lib.stt import CustomAdapterSTT
    package = tmp_path / "stt-adapters/custom.integration-stt"
    package.mkdir(parents=True)
    executable = package / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
request = json.load(sys.stdin)
if request["operation"] == "models":
    print(json.dumps({"ok": True, "models": [
        {"id": "general", "name": "General", "weight": "remote"}
    ]}))
else:
    assert pathlib.Path(request["audio_path"]).is_file()
    print(json.dumps({"ok": True, "text": "custom result.",
                      "ends_terminal": True, "duration_seconds": 0.1}))
""")
    executable.chmod(0o755)
    (package / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "id": "custom.integration-stt",
        "name": "Integration STT",
        "executable": "./adapter",
        "operations": ["models", "transcribe"],
        "default_model": "general",
    }))
    monkeypatch.setattr(custom_stt_adapters, "ROOT", tmp_path / "stt-adapters")
    manifest = custom_stt_adapters.get("custom.integration-stt")
    assert manifest is not None
    base, ctx, _srv = running_server
    ctx.stt = CustomAdapterSTT(manifest, "general")

    status, body = _get(base + "/transcription-capabilities")
    assert status == 200
    payload = json.loads(body)
    assert payload["adapters"][0]["id"] == "custom.integration-stt"
    assert payload["models"][0]["custom"] is True

    status, body = _post_raw(
        base + "/transcribe", b"audio bytes",
        {"Content-Type": "audio/webm",
         "X-Transcription-Model": "custom.integration-stt:general"})
    assert status == 200
    assert json.loads(body)["text"] == "custom result."


def test_transcription_model_install_endpoint_starts_managed_task(
    running_server, monkeypatch
):
    base, _ctx, _srv = running_server
    from lib import transcription_models
    seen = []
    monkeypatch.setattr(
        transcription_models, "start_install",
        lambda model_id, session="", computer_id="", on_complete=None: seen.append(
            (model_id, computer_id)) or {
            "model_id": model_id, "status": "installing", "error": "",
        },
    )
    status, body = _post(base + "/transcription-models/install", {
        "model_id": "faster-whisper:medium",
    })
    assert status == 202
    assert json.loads(body)["status"] == "installing"
    assert seen[0][0] == "faster-whisper:medium"
    assert seen[0][1]


def test_transcription_install_endpoint_works_with_zero_agents(
    running_server, monkeypatch,
):
    base, _ctx, _srv = running_server
    from lib import (agents, background_jobs, server_identity, service_manager,
                     transcription_models)
    for agent in agents.list_agents():
        agents.soft_delete(agent["agent_id"])
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    monkeypatch.setattr(
        service_manager, "launch_detached", lambda *_args, **_kwargs: (True, ""))
    with transcription_models._lock:
        transcription_models._tasks.pop("faster-whisper:medium", None)

    status, body = _post(base + "/transcription-models/install", {
        "model_id": "faster-whisper:medium",
    })

    assert status == 202
    assert json.loads(body)["status"] == "installing"
    assert agents.list_agents() == []
    job = background_jobs.get(
        transcription_models.install_job_id("faster-whisper:medium"),
        reconcile=False)
    assert job["owner_kind"] == "computer"
    assert job["computer_id"] == server_identity.get_server_info()["server_id"]
    assert job["agent_id"] == ""
    assert job["session"] == ""


def test_transcription_install_endpoint_ignores_deleted_default_agent(
    running_server, monkeypatch,
):
    base, ctx, _srv = running_server
    from lib import (agents, background_jobs, server_identity, service_manager,
                     transcription_models)
    default = agents.get_by_session(ctx.default_session)
    assert default is not None
    agents.soft_delete(default["agent_id"])
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    monkeypatch.setattr(
        service_manager, "launch_detached", lambda *_args, **_kwargs: (True, ""))
    with transcription_models._lock:
        transcription_models._tasks.pop("faster-whisper:small.en", None)

    status, body = _post(base + "/transcription-models/install", {
        "model_id": "faster-whisper:small.en",
    })

    assert status == 202
    assert json.loads(body)["status"] == "installing"
    assert agents.get_by_session(ctx.default_session) is None
    job = background_jobs.get(
        transcription_models.install_job_id("faster-whisper:small.en"),
        reconcile=False)
    assert job["owner_kind"] == "computer"
    assert job["computer_id"] == server_identity.get_server_info()["server_id"]


def test_transcription_activation_recovery_reuses_loading_default(monkeypatch):
    from lib import config
    from lib.stt import WhisperSTT

    current = WhisperSTT("small.en", "int8")
    handler = object.__new__(server_module.Handler)
    handler.server = SimpleNamespace(ctx=SimpleNamespace(stt=current))
    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(
        whisper_enabled=True, whisper_model="small.en",
        whisper_compute="int8", whisper_isolate=False,
    ))
    threading.Thread(
        target=lambda: (time.sleep(0.02), current.load_done.set()),
        daemon=True,
    ).start()

    handler._activate_transcription_if_default("faster-whisper:small.en")

    assert handler.ctx.stt is current


def test_transcription_activation_timeout_reuses_same_loader(monkeypatch):
    from lib import config, stt as stt_module

    class NeverDone:
        def wait(self, timeout):
            assert timeout == 330.0
            return False

    class FakeWhisper:
        instances = 0

        def __init__(self, model_name, compute, model_source=None):
            del compute, model_source
            FakeWhisper.instances += 1
            self.default_model_id = f"faster-whisper:{model_name}"
            self.load_done = NeverDone()
            self.load_error = None

        def start_loading(self):
            pass

    class FakeUnavailable(FakeWhisper):
        pass

    class FakeDisabled(FakeWhisper):
        pass

    monkeypatch.setattr(stt_module, "WhisperSTT", FakeWhisper)
    monkeypatch.setattr(stt_module, "UnavailableSTT", FakeUnavailable)
    monkeypatch.setattr(stt_module, "DisabledSTT", FakeDisabled)
    monkeypatch.setattr(
        stt_module, "_installed_model_records",
        lambda: [{
            "id": "faster-whisper:small.en", "_local_path": "/model",
        }],
    )
    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(
        whisper_enabled=True, whisper_model="small.en",
        whisper_compute="int8", whisper_isolate=False,
    ))
    handler = object.__new__(server_module.Handler)
    handler.server = SimpleNamespace(ctx=SimpleNamespace(
        stt=FakeUnavailable("small.en", "int8")))

    with pytest.raises(RuntimeError, match="did not load"):
        handler._activate_transcription_if_default("faster-whisper:small.en")
    replacement = handler.ctx.stt
    assert FakeWhisper.instances == 2

    with pytest.raises(RuntimeError, match="still loading"):
        handler._activate_transcription_if_default("faster-whisper:small.en")
    assert handler.ctx.stt is replacement
    assert FakeWhisper.instances == 2


def test_transcription_activation_propagates_loader_error_before_retry(
    monkeypatch,
):
    from lib import config, stt as stt_module

    class Done:
        def wait(self, timeout):
            assert timeout == 330.0
            return True

    class FakeWhisper:
        instances = 0

        def __init__(self, model_name, compute, model_source=None):
            del compute, model_source
            FakeWhisper.instances += 1
            self.default_model_id = f"faster-whisper:{model_name}"
            self.load_done = Done()
            self.load_error = None

        def start_loading(self):
            pass

    class FakeUnavailable(FakeWhisper):
        pass

    monkeypatch.setattr(stt_module, "WhisperSTT", FakeWhisper)
    monkeypatch.setattr(stt_module, "UnavailableSTT", FakeUnavailable)
    monkeypatch.setattr(stt_module, "DisabledSTT", FakeWhisper)
    monkeypatch.setattr(
        stt_module, "_installed_model_records",
        lambda: [{
            "id": "faster-whisper:small.en", "_local_path": "/model",
        }],
    )
    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(
        whisper_enabled=True, whisper_model="small.en",
        whisper_compute="int8", whisper_isolate=False,
    ))
    current = FakeWhisper("small.en", "int8")
    current.load_error = RuntimeError("first loader failed")
    handler = object.__new__(server_module.Handler)
    handler.server = SimpleNamespace(ctx=SimpleNamespace(stt=current))

    with pytest.raises(RuntimeError, match="first loader failed"):
        handler._activate_transcription_if_default("faster-whisper:small.en")
    assert handler.ctx.stt is current
    assert FakeWhisper.instances == 1

    handler._activate_transcription_if_default("faster-whisper:small.en")
    assert handler.ctx.stt is not current
    assert FakeWhisper.instances == 2


def test_post_transcribe_preserves_hands_free_header(running_server):
    base, ctx, _srv = running_server
    from lib.agents import create_agent
    from lib.vocab import update_guidance
    create_agent(persona="Lena", voice_id="V_LENA", cwd=str(ctx.root), session="lena")
    update_guidance({
        "delegation_agent_names_enabled": True,
        "technical_glossary": "SwiftUI\nCloudflare",
    })

    status, body = _post_raw(
        base + "/transcribe",
        b"\x00" * 100,
        {"Content-Type": "audio/webm", "X-Hands-Free": "1"},
    )
    assert status == 200
    data = json.loads(body)
    assert data["hands_free"] is True
    prompt = ctx.stt.calls[-1][2]
    assert "Mike" in prompt
    assert "Rachel" in prompt
    assert "Lena" in prompt
    assert "SwiftUI" not in prompt

    status, body = _post_raw(
        base + "/transcribe",
        b"\x01" * 100,
        {"Content-Type": "audio/webm", "X-Hands-Free": "0"},
    )
    assert status == 200
    data = json.loads(body)
    assert data["hands_free"] is False
    # The glossary still drives ordinary (non-delegation) turns, but it is now
    # compiled through the context-pack budget rather than concatenated: terms
    # are fitted to the model's capacity and emitted best-last, because Whisper
    # weights the tail of `initial_prompt` most heavily.
    prompt = ctx.stt.calls[-1][2]
    assert "SwiftUI" in prompt and "Cloudflare" in prompt
    assert "Mike" not in prompt and "Rachel" not in prompt


def test_recoverable_clips_endpoint_returns_interrupted_session_audio(
    running_server, tmp_path,
):
    base, _ctx, _srv = running_server
    from lib import agents, clip_store
    agent = agents.get_by_session("rachel")
    clip_id = clip_store.record_clip(
        agent_id=agent["agent_id"], path=str(tmp_path / "reply.pcm"),
        producer_status="complete", runtime_id=lambda _agent_id: None)
    agents.record_sse_event({
        "type": "audio", "clip_id": clip_id, "session": "rachel",
        "url": f"/clips/{clip_id}/stream",
        "audio_format": {
            "encoding": "pcm_s16le", "sample_rate": 24000,
            "channels": 1, "bytes_per_sample": 2,
        },
    })
    clip_store.mark_clip_status(clip_id=clip_id, status="play-start")

    status, body = _get(base + "/clips/recoverable?session=rachel")

    assert status == 200
    payload = json.loads(body)
    assert [event["clip_id"] for event in payload["events"]] == [clip_id]


def test_transcription_guidance_is_computer_owned_and_editable(running_server):
    base, ctx, _srv = running_server
    from lib.agents import create_agent
    create_agent(persona="Lena", voice_id="V_LENA", cwd=str(ctx.root), session="lena")

    status, body = _get(base + "/transcription-guidance")
    assert status == 200
    initial = json.loads(body)
    assert initial["settings"]["delegation_agent_names_enabled"] is True
    assert "Lena" in initial["active_agent_names"]
    assert initial["defaults"]["technical_glossary"] == ""

    status, body = _post(base + "/transcription-guidance", {
        "delegation_agent_names_enabled": True,
        "technical_glossary": "SwiftUI\nCloudflare",
    })
    assert status == 200
    saved = json.loads(body)
    assert saved["settings"]["technical_glossary"] == "SwiftUI\nCloudflare"
    assert saved["delegation_effective_prompt"] == "Agent names: Mike, Rachel, Lena."
    assert saved["regular_effective_prompt"] == (
        "Technical vocabulary: SwiftUI Cloudflare.")
    assert saved["delegation_estimated_prompt_tokens"] <= saved["prompt_token_limit"]
    assert saved["regular_estimated_prompt_tokens"] <= saved["prompt_token_limit"]


def test_signed_notification_avatar_serves_custom_agent_without_app_auth(
    running_server, tmp_path,
):
    from urllib.parse import urlencode
    from lib import agents as agents_db
    from lib.avatar_urls import (
        avatar_content_version, notification_avatar_signature)

    base, ctx, _srv = running_server
    agent_id = agents_db.create_agent(
        persona="Nova", voice_id="V_NOVA", cwd=str(tmp_path), session="nova")
    avatar = tmp_path / "nova.jpg"
    avatar.write_bytes(b"custom-notification-avatar")
    agents_db.update_agent(agent_id, avatar_path=str(avatar))
    ctx.auth_token = "server-secret"
    version = avatar_content_version(avatar)
    expires_at = int(time.time()) + 600
    signature = notification_avatar_signature(
        ctx.auth_token, agent_id, version, expires_at)
    query = urlencode({"v": version, "exp": expires_at, "sig": signature})

    status, body = _get(
        f"{base}/notification-avatars/{agent_id}?{query}")
    assert status == 200
    assert body == avatar.read_bytes()

    with pytest.raises(urllib.error.HTTPError) as error:
        _get(f"{base}/notification-avatars/{agent_id}?v={version}&exp={expires_at}&sig=bad")
    assert error.value.code == 403


def test_device_registration_keeps_private_overlay_avatar_origin(running_server):
    from lib import apns

    base, _ctx, _srv = running_server
    status, _ = _post(base + "/devices", {
        "token": "device-token",
        "session": "claude",
        "environment": "production",
        "platform": "ios",
        "base_url": "http://192.0.2.10:7682/",
    })
    assert status == 200
    assert apns.active_tokens()[0]["base_url"] == "http://192.0.2.10:7682"


def test_post_preview_synthesizes_via_fake_tts(running_server):
    base, ctx, _srv = running_server
    status, _ = _post(base + "/preview",
                      {"voice_id": "V_DUMMY", "text": "hi there", "session": "claude"})
    # /preview calls ctx.tts.synthesize directly, which the FakeTTSEngine
    # captures into a deterministic MP3 file — no network involved.
    assert status == 200
    files = sorted(p.name for p in ctx.audio_dir.glob("*.mp3"))
    assert files, "expected one mp3 written by FakeTTSEngine"


def test_delete_agent_ignores_query_string_and_unknown_is_404(
    running_server, monkeypatch,
):
    base, _ctx, _srv = running_server
    from lib import backends

    monkeypatch.setattr(backends, "interrupt_any", lambda _agent_id: 0)
    status, _ = _delete(base + "/agents/rachel?token=not-part-of-session")
    assert status == 200

    from lib import agents as agents_db
    assert agents_db.get_by_session("rachel") is None
    with pytest.raises(urllib.error.HTTPError) as error:
        _delete(base + "/agents/rachel")
    assert error.value.code == 404


def test_post_agents_opens_runtime_row(running_server):
    base, ctx, _srv = running_server
    status, body = _post(base + "/agents", {
        "name": "Domi",
        "session": "domi",
        "cwd": str(ctx.root),
    })
    assert status == 200
    assert json.loads(body)["session"] == "domi"

    from lib import agents as agents_db
    agent = agents_db.get_by_session("domi")
    assert agent is not None
    runtime_id = agents_db.current_runtime_id(agent["agent_id"])
    assert runtime_id is not None
    runtimes = agents_db.conn().execute(
        "SELECT session, ended_at FROM runtimes WHERE runtime_id = ?",
        (runtime_id,),
    ).fetchone()
    assert runtimes["session"] == "domi"
    assert runtimes["ended_at"] is None


def test_post_agents_resume_exposes_history_immediately(running_server, monkeypatch,
                                                        tmp_path):
    base, ctx, _srv = running_server
    transcript = tmp_path / "session-existing.jsonl"
    transcript.write_text(json.dumps({
        "type": "user",
        "timestamp": "2026-05-31T10:00:00Z",
        "message": {"role": "user", "content": "Existing context"},
    }) + "\n")
    monkeypatch.setattr(
        server_module,
        "find_latest_jsonl",
        lambda session_id: transcript if session_id == "session-existing" else None,
    )

    status, _ = _post(base + "/agents", {
        "name": "Domi",
        "session": "domi",
        "cwd": str(ctx.root),
        "resume_session_id": "session-existing",
    })
    assert status == 200

    from lib import agents as agents_db
    agent = agents_db.get_by_session("domi")
    assert agents_db.live_backend_session(agent["agent_id"]) == "session-existing"

    status, body = _get(base + "/log?session=domi")
    assert status == 200
    assert json.loads(body)["turns"][0]["text"] == "Existing context"


def test_remote_action_broadcasts_to_sse(running_server):
    base, ctx, _srv = running_server
    q = ctx.stream.subscribe()
    status, _ = _post(base + "/remote-action", {"action": "record-toggle"})
    assert status == 200
    event = json.loads(q.get(timeout=1))
    ctx.stream.unsubscribe(q)
    assert event["type"] == "remote-action"
    # Input events act only at the instant they arrive. A reconnect must never
    # replay a stale toggle and unexpectedly start the microphone.
    assert not any(ev["type"] == "remote-action" for ev in ctx.stream.recent())


def test_controller_action_broadcasts_bounded_live_event(running_server):
    base, ctx, _srv = running_server
    q = ctx.stream.subscribe()
    status, body = _post(base + "/remote-action", {
        "action": "controller-event",
        "controller_id": "duo-one",
        "controller_event_id": "event-one",
        "button": "secondary",
        "controller_event": "swipe-right",
        "duration_ms": 780,
        "queued": False,
        "ignored": "not forwarded",
    })
    assert status == 200
    assert json.loads(body)["controller_event_id"] == "event-one"
    event = json.loads(q.get(timeout=1))
    ctx.stream.unsubscribe(q)
    assert event == {
        "type": "remote-action",
        "action": "controller-event",
        "controller_id": "duo-one",
        "controller_event_id": "event-one",
        "button": "secondary",
        "controller_event": "swipe-right",
        "duration_ms": 780,
        "age_ms": 0,
        "queued": False,
        "ts": event["ts"],
    }


def test_controller_action_rejects_unknown_gesture(running_server):
    base, _ctx, _srv = running_server
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(base + "/remote-action", {
            "action": "controller-event",
            "button": "primary",
            "controller_event": "shake",
        })
    assert error.value.code == 400
    assert json.loads(error.value.read())["error"] == "unknown controller_event"


def test_turn_queue_can_be_listed_edited_deleted_and_paused_by_stop(running_server):
    from lib import agents as agents_db, turn_queue
    base, _ctx, _srv = running_server
    agent = agents_db.get_by_session("claude")
    turn_queue.enqueue(
        queue_id="queue-http", agent_id=agent["agent_id"], session="claude",
        text="original", trace_id="trace-http", client_msg_id="queue-http",
        synthesize_audio=False, origin="user", sender_agent_id="")

    status, body = _get(base + "/turn-queue?session=claude")
    assert status == 200
    assert json.loads(body)["items"][0]["text"] == "original"

    status, _ = _put(base + "/turn-queue/queue-http", {"text": "edited"})
    assert status == 200
    assert turn_queue.get("queue-http")["text"] == "edited"

    status, _ = _post(base + "/stop", {"session": "claude"})
    assert status == 200
    assert turn_queue.get("queue-http") is not None
    assert turn_queue.is_paused(agent["agent_id"]) is True

    status, _ = _delete(base + "/turn-queue/queue-http")
    assert status == 200
    assert turn_queue.get("queue-http") is None


def test_stop_barrier_is_executed_by_external_runtime(fake_ctx):
    from lib import agents as agents_db, turn_queue
    from lib.runtime_bridge import StopLease

    calls = []

    class RuntimeOwner:
        def ping(self):
            return True

        def recover_queued(self):
            return 0

        def status(self):
            return {"active": {}, "spawning": [], "terminals": []}

        def begin_stop(self, agent_id, backend, *, strict, hold):
            calls.append(("begin", agent_id, backend, strict, hold))
            turn_queue.set_paused(agent_id, True)
            return StopLease(
                lease_id="stop-lease", trace_id="trace-runtime", terminated=1,
                dropped=0)

        def finish_stop(self, lease_id, cancelled_trace_ids=None):
            calls.append(("finish", lease_id, cancelled_trace_ids))

    fake_ctx.runtime_client = RuntimeOwner()
    agent = agents_db.get_by_session("claude")
    turn_queue.enqueue(
        queue_id="queue-runtime-stop", agent_id=agent["agent_id"],
        session="claude", text="later", trace_id="queue-trace",
        client_msg_id="queue-client", synthesize_audio=False,
        origin="user", sender_agent_id="")
    port = _free_port()
    srv = build_server(fake_ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post(
            f"http://127.0.0.1:{port}/stop", {"session": "claude"})
        assert status == 200
        assert json.loads(body)["terminated"] == 1
    finally:
        srv.shutdown()
        srv.server_close()

    assert calls == [
        ("begin", agent["agent_id"], agent["backend"], False, True),
        ("finish", "stop-lease", set()),
    ]
    assert turn_queue.get("queue-runtime-stop") is not None
    assert turn_queue.is_paused(agent["agent_id"])


def test_turn_queue_manual_send_endpoint(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    monkeypatch.setattr(
        server_module.TurnDispatchService,
        "dispatch_queued",
        lambda self, queue_id: SimpleNamespace(
            session="claude", queued=False, queue_depth=0, queue_revision=4),
    )

    status, body = _post(base + "/turn-queue/queue-http/send", {})

    assert status == 202
    assert json.loads(body)["session"] == "claude"


def test_decision_delivery_is_private_and_bypasses_user_queue(monkeypatch):
    from lib import artifacts
    dispatched = []
    delivered = []

    class FakeDispatch:
        def __init__(self, _ctx):
            pass

        def dispatch(self, **kwargs):
            dispatched.append(kwargs)

    monkeypatch.setattr(server_module, "TurnDispatchService", FakeDispatch)
    monkeypatch.setattr(artifacts, "pending_deliveries", lambda: [{
        "decision_id": "decision-1", "artifact_id": "artifact-1",
        "choice": "accepted", "session": "mike", "question": "Proceed?",
        "context": "", "reference_id": "", "payload_json": "{}",
    }])
    monkeypatch.setattr(artifacts, "mark_delivered", delivered.append)

    server_module._deliver_decision_rows(SimpleNamespace())

    assert delivered == ["decision-1"]
    assert dispatched[0]["origin"] == "automation"
    assert dispatched[0]["queue_if_busy"] is False
    assert dispatched[0]["forced_session"] == "mike"


def test_clip_ack_updates_clip_status(running_server):
    base, ctx, _srv = running_server
    from lib import agents as agents_db

    agent = agents_db.get_by_session("claude")
    clip = ctx.audio_dir / "123__claude.mp3"
    clip.write_bytes(b"mp3")
    clip_id = agents_db.record_clip(
        agent_id=agent["agent_id"], path=str(clip), trace_id="trace-clip")

    status, _ = _post(base + "/clips/ack", {
        "clip_id": clip_id,
        "status": "play-ok",
    })

    assert status == 200
    row = agents_db.conn().execute(
        "SELECT status, played_at FROM clips WHERE clip_id = ?",
        (clip_id,),
    ).fetchone()
    assert row["status"] == "play-ok"
    assert row["played_at"] is not None


def test_404_for_unknown_path(running_server):
    base, _ctx, _srv = running_server
    req = urllib.request.Request(base + "/nope")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=2).read()
    assert excinfo.value.code == 404
