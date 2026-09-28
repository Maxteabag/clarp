"""Live OpenCode turns through a (fake) ``opencode serve``.

The fake binary is a real HTTP server on a random port with basic auth and a
chunked ``/event`` stream, so these tests exercise the supervisor, the client
and the drain end to end without a model."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import time

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db  # noqa: E402
from lib.backend import opencode_serve  # noqa: E402
from lib.backend.registry import by_id  # noqa: E402
from lib.turn_lifecycle import TurnEvent  # noqa: E402

OPENCODE = by_id("opencode")

FAKE = r'''#!/usr/bin/env python3
import base64, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ["FAKE_LOG"]
if sys.argv[1] == "run":
    with open(LOG, "a") as f:
        f.write(json.dumps({"run": sys.argv[1:]}) + "\n")
    print(json.dumps({"type": "text", "sessionID": "ses_run",
                      "part": {"text": "from run"}}))
    sys.exit(0)
if os.environ.get("FAKE_SERVE_FAIL"):
    sys.exit(3)
with open(os.environ["FAKE_PID"], "w") as f:
    f.write(str(os.getpid()))
EVENTS = json.load(open(os.environ["FAKE_EVENTS"]))
PASSWORD = os.environ["OPENCODE_SERVER_PASSWORD"]
prompted = threading.Event()

def record(entry):
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def _auth(self):
        want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers()
            return False
        return True
    def _json(self, code, value):
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null") if n else None
    def do_GET(self):
        if not self._auth():
            return
        path = self.path.split("?")[0]
        record({"method": "GET", "path": self.path})
        if path == "/event":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            def send(event):
                raw = ("data: " + json.dumps(event) + "\n\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                self.wfile.flush()
            send({"type": "server.connected", "properties": {}})
            prompted.wait(10)
            for event in EVENTS:
                if event == "HANG":
                    while True:
                        time.sleep(0.2)
                send(event)
            while True:
                time.sleep(0.5)
                send({"type": "server.heartbeat", "properties": {}})
        if path.startswith("/session/"):
            sid = path.split("/")[2]
            if sid == "ses_old":
                return self._json(200, {"id": sid})
            return self._json(404, {"name": "NotFoundError"})
        self._json(404, {})
    def do_POST(self):
        if not self._auth():
            return
        body = self._body()
        path = self.path.split("?")[0]
        record({"method": "POST", "path": self.path, "body": body})
        if path == "/session":
            return self._json(200, {"id": "ses_new"})
        if path.endswith("/prompt_async"):
            prompted.set()
            self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
            return
        self._json(200, True)

server = ThreadingHTTPServer(("127.0.0.1", 0), H)
print(f"opencode server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
server.serve_forever()
'''


def _events(sid="ses_new"):
    def part(**fields):
        return {"type": "message.part.updated", "properties": {"part": {
            "sessionID": sid, "messageID": "msg_a", **fields}}}
    return [
        # The prompt's own part, before any message.updated names its role
        # (the order a real server uses).
        {"type": "message.part.updated", "properties": {"part": {
            "id": "prt_u", "sessionID": sid, "messageID": "msg_u", "type": "text",
            "text": "<speak>the prompt</speak>", "time": {"end": 1}}}},
        {"type": "session.status", "properties": {"sessionID": sid,
                                                  "status": {"type": "busy"}}},
        part(id="prt_s1", type="step-start"),
        part(id="prt_t1", type="text", text=""),
        {"type": "message.part.delta", "properties": {
            "sessionID": sid, "messageID": "msg_a", "partID": "prt_t1",
            "field": "text", "delta": "<speak>On it.</speak>"}},
        {"type": "message.part.delta", "properties": {
            "sessionID": sid, "messageID": "msg_a", "partID": "prt_t1",
            "field": "text", "delta": " Checking."}},
        part(id="prt_b", type="tool", tool="bash", callID="c1",
             state={"status": "pending", "input": {}}),
        part(id="prt_b", type="tool", tool="bash", callID="c1",
             state={"status": "running", "input": {"command": "make test"}}),
        {"type": "permission.asked", "properties": {"id": "per_1", "sessionID": sid}},
        part(id="prt_b", type="tool", tool="bash", callID="c1",
             state={"status": "completed", "input": {"command": "make test"},
                    "output": "ok"}),
        part(id="prt_f1", type="step-finish", reason="tool-calls", cost=0.1,
             tokens={"input": 5, "output": 2, "cache": {"read": 50, "write": 0}}),
        part(id="prt_s2", type="step-start"),
        part(id="prt_t2", type="text", text="All green.", time={"end": 1}),
        part(id="prt_f2", type="step-finish", reason="stop", cost=0.1,
             tokens={"input": 5, "output": 2, "cache": {"read": 50, "write": 0}}),
        {"type": "session.status", "properties": {"sessionID": sid,
                                                  "status": {"type": "idle"}}},
    ]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "opencode"
    binary.write_text(FAKE)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("FAKE_PID", str(tmp_path / "server.pid"))
    monkeypatch.setenv("FAKE_EVENTS", str(tmp_path / "events.json"))
    monkeypatch.delenv("CLARP_OPENCODE_LIVE", raising=False)
    monkeypatch.setattr(agents_db, "get_by_agent_id",
                        lambda _id: {"persona": "Sindre", "voice_id": "v"})
    monkeypatch.setattr(agents_db, "latest_turn_synthesize_audio", lambda _id: True)
    monkeypatch.setattr(agents_db, "get_focus", lambda: "a1")
    monkeypatch.setattr(agents_db, "get_trace", lambda _id: "t1")
    live_rows: list[str] = []
    monkeypatch.setattr(agents_db, "upsert_live_assistant_message",
                        lambda **kw: live_rows.append(kw["text"]) or {"changed": False})
    transitions: list[tuple[str, dict]] = []
    monkeypatch.setattr(OPENCODE, "_transition",
                        lambda _agent, event, detail: transitions.append((event, detail)))

    class Fake:
        dir = tmp_path
        live = live_rows
        states = transitions

        @staticmethod
        def script(events):
            (tmp_path / "events.json").write_text(json.dumps(events))

        @staticmethod
        def requests():
            path = tmp_path / "log.jsonl"
            if not path.exists():
                return []
            return [json.loads(line) for line in path.read_text().splitlines()]

        @staticmethod
        def server_pid():
            return int((tmp_path / "server.pid").read_text())

    return Fake


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is dead for our purposes.
    try:
        return "Z" not in pathlib.Path(f"/proc/{pid}/stat").read_text().split()[2]
    except OSError:
        return False


def _start(fake, **kwargs):
    results, errors, spoken, sessions = [], [], [], []
    handle = OPENCODE.start_turn(
        text="hello", cwd=fake.dir, agent_id="a1", session="s", trace_id="t1",
        on_session_init=lambda sid: sessions.append(sid) or True,
        on_result=results.append, on_error=errors.append,
        enqueue=lambda **kw: spoken.append(kw["text"]) or 1, **kwargs)
    return handle, results, errors, spoken, sessions


def test_live_turn_streams_tools_text_and_speech_then_stops_the_server(fake):
    fake.script(_events())
    handle, results, errors, spoken, sessions = _start(
        fake, model="huggingface/deepseek-ai/DeepSeek-V4.1-Flash", effort="high")
    handle.drain_thread.join(timeout=20)
    assert not errors
    assert sessions == ["ses_new"]
    assert results == [{
        "last_agent_message": "All green.",
        "usage": {"input_tokens": 10, "output_tokens": 4,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
        "total_cost_usd": 0.2,
    }]
    # The acknowledgement was spoken from the first delta, not the prompt.
    assert spoken == ["On it."]
    assert fake.live[0] == "<speak>On it.</speak>"
    assert "the prompt" not in json.dumps(fake.live)
    tool = [d for e, d in fake.states if e == TurnEvent.TOOL_STARTED]
    assert [(d["tool"], d["input"]) for d in tool] == [("Bash", {"command": "make test"})]
    requests = fake.requests()
    create = next(r for r in requests if r["path"].startswith("/session?"))
    assert {"permission": "question", "action": "deny", "pattern": "*"} in create["body"]["permission"]
    prompt = next(r for r in requests if "/prompt_async" in r["path"])
    assert prompt["body"]["model"] == {"providerID": "huggingface",
                                       "modelID": "deepseek-ai/DeepSeek-V4.1-Flash"}
    assert prompt["body"]["variant"] == "high"
    assert prompt["body"]["parts"][0]["text"].endswith("hello")
    assert "directory=" in prompt["path"]
    assert any(r["path"].startswith("/permission/per_1/reply") and r["body"] == {"reply": "once"}
               for r in requests)
    assert handle.proc.poll() is not None
    assert not _alive(fake.server_pid())


def test_resume_uses_the_existing_session_and_a_missing_one_fails(fake):
    fake.script(_events("ses_old"))
    handle, results, errors, _spoken, sessions = _start(fake, backend_session_id="ses_old")
    handle.drain_thread.join(timeout=20)
    assert sessions == ["ses_old"] and results and not errors
    assert not any(r["path"].startswith("/session?") for r in fake.requests())

    fake.script(_events("ses_gone"))
    handle, results, errors, *_ = _start(fake, backend_session_id="ses_gone")
    handle.drain_thread.join(timeout=20)
    assert errors == ["OpenCode session ses_gone not found"] and not results
    assert not _alive(fake.server_pid())


def test_stop_aborts_the_session_and_leaves_nothing_running(fake):
    fake.script(_events()[:6] + ["HANG"])
    handle, results, errors, *_ = _start(fake)
    deadline = time.monotonic() + 15
    while handle.abort is None and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    pid = fake.server_pid()
    assert handle.terminate()
    handle.drain_thread.join(timeout=20)
    assert not results
    assert errors and "killed by signal" in errors[0]
    assert any(r["path"].startswith("/session/ses_new/abort") for r in fake.requests())
    assert handle.proc.poll() is not None
    assert not _alive(pid)


def test_provider_error_is_reported_after_the_turn_goes_idle(fake):
    fake.script([
        {"type": "session.status", "properties": {"sessionID": "ses_new",
                                                  "status": {"type": "busy"}}},
        {"type": "session.error", "properties": {"sessionID": "ses_new", "error": {
            "name": "APIError", "data": {"message": "Rate limit exceeded."}}}},
        {"type": "session.status", "properties": {"sessionID": "ses_new",
                                                  "status": {"type": "idle"}}},
    ])
    handle, results, errors, *_ = _start(fake)
    handle.drain_thread.join(timeout=20)
    assert errors == ["Rate limit exceeded."] and not results


def test_server_that_cannot_start_falls_back_to_opencode_run(fake, monkeypatch):
    monkeypatch.setenv("FAKE_SERVE_FAIL", "1")
    fake.script([])
    handle, results, errors, *_ = _start(fake)
    handle.drain_thread.join(timeout=20)
    assert not errors
    assert results[0]["last_agent_message"] == "from run"
    run = next(r["run"] for r in fake.requests() if "run" in r)
    assert run[:3] == ["run", "--format", "json"]


def test_live_can_be_switched_off(fake, monkeypatch):
    monkeypatch.setenv("CLARP_OPENCODE_LIVE", "0")
    fake.script([])
    handle, results, *_ = _start(fake)
    handle.drain_thread.join(timeout=20)
    assert results[0]["last_agent_message"] == "from run"


def test_supervisor_ends_the_server_when_the_runtime_goes_away(tmp_path):
    pid_file = tmp_path / "child.pid"
    child = (f"import os,time; open({str(pid_file)!r},'w').write(str(os.getpid())); "
             "time.sleep(60)")
    proc = subprocess.Popen(
        [sys.executable, "-c", opencode_serve.SUPERVISOR, sys.executable, "-c", child],
        stdin=subprocess.PIPE)
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    assert proc.stdin is not None
    proc.stdin.close()   # what the pipe does when the runtime dies
    proc.wait(timeout=10)
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


def test_reasoning_marks_the_agent_busy_and_is_never_reply_text(fake):
    sid = "ses_new"
    events = _events()
    finish = next(i for i, e in enumerate(events)
                  if e["properties"].get("part", {}).get("id") == "prt_f1")
    events[finish:finish] = [
        {"type": "message.part.updated", "properties": {"part": {
            "id": "prt_r", "sessionID": sid, "messageID": "msg_a", "type": "reasoning",
            "text": ""}}},
        {"type": "message.part.delta", "properties": {
            "sessionID": sid, "messageID": "msg_a", "partID": "prt_r",
            "field": "text", "delta": "<speak>private thought</speak>"}},
    ]
    fake.script(events)
    handle, results, errors, spoken, _sessions = _start(fake)
    handle.drain_thread.join(timeout=20)
    kinds = [event for event, _detail in fake.states]
    tool = kinds.index(TurnEvent.TOOL_STARTED)
    assert TurnEvent.TEXT_STREAMED in kinds[tool + 1:]
    assert "private thought" not in json.dumps([results, spoken, fake.live])
    assert not errors
