from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from lib import agents, background_jobs


_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = _load(
    _ROOT / "skills/clarp-message-watch/scripts/watch_messages.py",
    "managed_message_watch_worker",
)
agent_bg = _load(_ROOT / "scripts/agent_bg.py", "managed_agent_bg")


def _agent_bg_run(cmd: list[str], timeout: int = 60, cwd: str | None = None):
    del timeout, cwd
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        returncode = agent_bg.main([str(cmd[0]), *cmd[1:]])
    return subprocess.CompletedProcess(
        cmd, returncode, stdout.getvalue(), stderr.getvalue())


def test_actual_worker_keeps_generation_handles_across_takeover(monkeypatch):
    agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session="nadia-test")
    identity = {"value": (100, "boot:old")}
    monkeypatch.setattr(
        background_jobs, "current_worker_identity", lambda: identity["value"])
    monkeypatch.setattr(worker, "run", _agent_bg_run)

    old_jobs = worker.register_jobs("nadia-test", "email", {"Yoga"})
    old_handle = old_jobs["Yoga"]
    assert old_handle.startswith("bg1:1:")

    identity["value"] = (200, "boot:new")
    new_jobs = worker.register_jobs("nadia-test", "email", {"Yoga"})
    new_handle = new_jobs["Yoga"]
    assert new_handle.startswith("bg1:2:")

    identity["value"] = (100, "boot:old")
    assert worker.job_is_active(old_handle) is False
    worker.finish_jobs("nadia-test", old_jobs)
    stable_id, _generation = agent_bg.parse_job_handle(new_handle)
    assert background_jobs.get(stable_id, reconcile=False)["status"] == "running"

    identity["value"] = (200, "boot:new")
    assert worker.job_is_active(new_handle) is True
    worker.finish_jobs("nadia-test", new_jobs)
    assert background_jobs.get(stable_id, reconcile=False)["status"] == "succeeded"


def test_actual_worker_gate_is_fail_closed_before_email_delivery(monkeypatch):
    notifications: list[str] = []
    envelope = {"id": "1", "subject": "Expected"}
    args = SimpleNamespace(
        subject_keyword=["Expected"],
        from_=[],
    )
    monkeypatch.setattr(worker, "list_envelopes", lambda _args: [envelope])
    monkeypatch.setattr(
        worker, "email_match", lambda _env, _senders, _keywords: "Expected")
    monkeypatch.setattr(
        worker, "notify_email",
        lambda _args, label, _env, _body: notifications.append(label))
    monkeypatch.setattr(worker, "read_email", lambda _args, _id: "body")

    assert worker.poll_himalaya(args, set(), False, {}) is False
    monkeypatch.setattr(
        worker, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("registry unavailable")))
    assert worker.poll_himalaya(
        args, set(), False, {"Expected": "bg1:1:expected"}) is False
    assert notifications == []


def test_whatsapp_seen_state_waits_for_successful_self_prompt(monkeypatch):
    handler = object.__new__(worker.WhatsappHandler)
    handler.watch_jid = {"chat": "Yoga"}
    handler.watch_name = {}
    handler.learned_jids = {}
    handler.reply_watch_path = None
    handler.job_ids = {"Yoga": "bg1:1:watch"}
    handler.seen = set()
    handler.cutoff = datetime(2020, 1, 1, tzinfo=timezone.utc)
    handler.instructions = "notify"
    saves: list[set[str]] = []
    handler._save_state = lambda: saves.append(set(handler.seen))
    monkeypatch.setattr(worker, "job_is_active", lambda _handle: True)
    delivered = {"ok": False}
    monkeypatch.setattr(worker, "send_prompt", lambda _session, _text: delivered["ok"])
    message = {
        "from_me": False, "id": "m1", "chat_jid": "chat",
        "sender_jid": "sender", "push_name": "Yoga", "reply_to_id": "",
        "timestamp": "2026-08-26T12:00:00Z", "text": "hello",
    }

    handler._handle(message)
    assert handler.seen == set()
    assert saves == []

    delivered["ok"] = True
    handler._handle(message)
    assert handler.seen == {"m1"}
    assert saves == [{"m1"}]


def test_whatsapp_catchup_delivers_only_after_active_handle_registration(
    monkeypatch, tmp_path,
):
    delivered: list[str] = []
    payload = json.dumps({
        "data": {"messages": [{
            "Chat": "jid", "SenderJID": "sender", "PushName": "Yoga",
            "Text": "stored reply", "Timestamp": "2026-08-26T12:00:00Z",
            "FromMe": False, "ID": "stored-1",
        }]},
    })

    def fake_run(cmd, timeout=60, cwd=None):
        del timeout, cwd
        if "messages" in cmd:
            return subprocess.CompletedProcess(cmd, 0, payload, "")
        if "job-active" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(
        worker, "send_prompt",
        lambda _session, text: delivered.append(text) is None)
    worker.WhatsappHandler.session = "nadia-test"
    worker.WhatsappHandler.watch_jid = {"jid": "Yoga"}
    worker.WhatsappHandler.watch_name = {}
    worker.WhatsappHandler.learned_jids = {}
    worker.WhatsappHandler.reply_watch_path = None
    worker.WhatsappHandler.instructions = "notify"
    worker.WhatsappHandler.state_file = tmp_path / "state.json"
    worker.WhatsappHandler.cutoff = datetime(2020, 1, 1, tzinfo=timezone.utc)
    worker.WhatsappHandler.seen = set()
    worker.WhatsappHandler.job_ids = {}
    args = SimpleNamespace(wacli_bin="wacli", catchup_limit=10)

    worker.reconcile_whatsapp_store(args)
    assert delivered == []
    assert worker.WhatsappHandler.seen == set()

    worker.WhatsappHandler.job_ids = {"Yoga": "bg1:2:watch"}
    worker.reconcile_whatsapp_store(args)
    assert len(delivered) == 1
    assert worker.WhatsappHandler.seen == {"stored-1"}


def test_himalaya_seen_state_waits_for_successful_self_prompt(monkeypatch):
    args = SimpleNamespace(
        subject_keyword=["Expected"], from_=[], notify_existing=True)
    envelope = {"id": "1", "subject": "Expected"}
    monkeypatch.setattr(worker, "list_envelopes", lambda _args: [envelope])
    monkeypatch.setattr(
        worker, "email_match", lambda _env, _senders, _keywords: "Expected")
    monkeypatch.setattr(worker, "job_is_active", lambda _handle: True)
    monkeypatch.setattr(worker, "read_email", lambda _args, _id: "body")
    delivered = {"ok": False}
    monkeypatch.setattr(
        worker, "notify_email",
        lambda _args, _label, _env, _body: delivered["ok"])
    seen: set[str] = set()

    assert worker.poll_himalaya(
        args, seen, False, {"Expected": "bg1:1:watch"}) is False
    assert seen == set()

    delivered["ok"] = True
    assert worker.poll_himalaya(
        args, seen, False, {"Expected": "bg1:1:watch"}) is True
    assert seen == {"1"}


def test_unexpected_whatsapp_child_exit_fails_live_handles(monkeypatch, tmp_path):
    commands: list[list[str]] = []

    def fake_run(cmd, timeout=60, cwd=None):
        del timeout, cwd
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    class FakeServer:
        def __init__(self, *_args, **_kwargs):
            self.server_address = ("127.0.0.1", 12345)
            self.stopped = threading.Event()

        def serve_forever(self):
            assert self.stopped.wait(timeout=1)

        def shutdown(self):
            self.stopped.set()

    class FailedChild:
        def wait(self):
            return 9

        def poll(self):
            return 9

        def terminate(self):
            raise AssertionError("exited child must not be terminated again")

    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(
        worker, "register_jobs",
        lambda _session, _provider, _labels: {"Yoga": "bg1:2:watch"})
    monkeypatch.setattr(worker, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda _cmd: FailedChild())
    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(worker, "state_path_for_session", lambda _session: tmp_path / "state.json")
    monkeypatch.setattr(worker, "load_wa_state", lambda _path: {})
    monkeypatch.setattr(worker, "reconcile_whatsapp_store", lambda _args: None)
    monkeypatch.setattr(worker.WhatsappHandler, "_save_state", lambda _self: None)
    args = SimpleNamespace(
        watch=["jid=Yoga"], watch_name=[], reply_watch_json=None,
        session="nadia-test", instructions="notify", log="/tmp/log",
        started_after=None, status="Watching WhatsApp", host="127.0.0.1",
        port=0, wacli_bin="wacli", stale_threshold="30s", max_reconnect=0,
        presence_mode="quiet", webhook_allow_private=True, catchup_limit=10,
    )

    assert worker.run_whatsapp(args) == 1
    fail_commands = [cmd for cmd in commands if "job-fail" in cmd]
    assert fail_commands == [[
            worker.STATUS_COMMAND, "nadia-test", "job-fail",
        "bg1:2:watch", "whatsapp_child_exited",
    ]]


def test_whatsapp_child_launch_failure_does_not_take_over_generation(
    monkeypatch, tmp_path,
):
    agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session="nadia-test")
    old = background_jobs.upsert(
        session="nadia-test", job_id="existing", kind="whatsapp", title="Yoga",
        worker_pid=100, worker_start_token="boot:old")
    register_calls = []

    class BoundServer:
        server_address = ("127.0.0.1", 12345)

        def __init__(self, *_args, **_kwargs):
            self.closed = False

        def server_close(self):
            self.closed = True

    monkeypatch.setattr(worker, "ThreadingHTTPServer", BoundServer)
    monkeypatch.setattr(
        worker.subprocess, "Popen",
        lambda _cmd: (_ for _ in ()).throw(RuntimeError("child launch failed")))
    monkeypatch.setattr(
        worker, "register_jobs",
        lambda *_args: register_calls.append(True) or {})
    monkeypatch.setattr(worker, "set_status", lambda *_args: None)
    monkeypatch.setattr(worker, "state_path_for_session", lambda _session: tmp_path / "state.json")
    monkeypatch.setattr(worker, "load_wa_state", lambda _path: {})
    monkeypatch.setattr(worker, "reconcile_whatsapp_store", lambda _args: None)
    args = SimpleNamespace(
        watch=["jid=Yoga"], watch_name=[], reply_watch_json=None,
        session="nadia-test", instructions="notify", log="/tmp/log",
        started_after=None, status="Watching WhatsApp", host="127.0.0.1",
        port=0, wacli_bin="wacli", stale_threshold="30s", max_reconnect=0,
        presence_mode="quiet", webhook_allow_private=True, catchup_limit=10,
    )

    try:
        worker.run_whatsapp(args)
        raise AssertionError("child launch failure should propagate")
    except RuntimeError as exc:
        assert "child launch failed" in str(exc)
    assert register_calls == []
    current = background_jobs.get("existing", reconcile=False)
    assert current["generation"] == old["generation"]
    assert current["status"] == "running"


def test_post_registration_whatsapp_startup_failure_fails_new_handles(
    monkeypatch, tmp_path,
):
    finishes = []

    class BoundServer:
        server_address = ("127.0.0.1", 12345)

        def __init__(self, *_args, **_kwargs):
            pass

        def server_close(self):
            pass

    class LiveChild:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    child = LiveChild()
    monkeypatch.setattr(worker, "ThreadingHTTPServer", BoundServer)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda _cmd: child)
    monkeypatch.setattr(
        worker, "register_jobs",
        lambda *_args: {"Yoga": "bg1:2:watch"})
    monkeypatch.setattr(worker, "heartbeat", lambda _session, _status, stop: stop.wait(1))
    monkeypatch.setattr(worker, "set_status", lambda *_args: None)
    monkeypatch.setattr(
        worker, "finish_jobs",
        lambda session, handles, **kwargs: finishes.append(
            (session, handles, kwargs)))
    monkeypatch.setattr(
        worker.signal, "signal",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("signal init failed")))
    monkeypatch.setattr(worker, "state_path_for_session", lambda _session: tmp_path / "state.json")
    monkeypatch.setattr(worker, "load_wa_state", lambda _path: {})
    monkeypatch.setattr(worker, "reconcile_whatsapp_store", lambda _args: None)
    args = SimpleNamespace(
        watch=["jid=Yoga"], watch_name=[], reply_watch_json=None,
        session="nadia-test", instructions="notify", log="/tmp/log",
        started_after=None, status="Watching WhatsApp", host="127.0.0.1",
        port=0, wacli_bin="wacli", stale_threshold="30s", max_reconnect=0,
        presence_mode="quiet", webhook_allow_private=True, catchup_limit=10,
    )

    try:
        worker.run_whatsapp(args)
        raise AssertionError("post-registration startup failure should propagate")
    except RuntimeError as exc:
        assert "signal init failed" in str(exc)
    assert finishes == [(
        "nadia-test", {"Yoga": "bg1:2:watch"},
        {"failed": True, "reason": "whatsapp_startup_failed"},
    )]
    assert child.terminated is True


def test_himalaya_heartbeat_is_independent_and_signal_finishes_handles(
    monkeypatch, tmp_path,
):
    heartbeat_started = threading.Event()
    handlers = {}
    finishes = []

    def fake_heartbeat(_session, _status, stop):
        heartbeat_started.set()
        stop.wait(timeout=1)

    def fake_poll(_args, _seen, _baseline, _job_ids):
        assert heartbeat_started.wait(timeout=1)
        handlers[worker.signal.SIGTERM]()
        return False

    monkeypatch.setattr(
        worker, "register_jobs",
        lambda _session, _provider, _labels: {"Yoga": "bg1:3:watch"})
    monkeypatch.setattr(worker, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(worker, "poll_himalaya", fake_poll)
    monkeypatch.setattr(worker, "set_status", lambda *_args: None)
    monkeypatch.setattr(worker, "load_seen", lambda _path: set())
    monkeypatch.setattr(worker, "save_seen", lambda _path, _seen: None)
    monkeypatch.setattr(
        worker.signal, "signal",
        lambda signal_number, callback: handlers.__setitem__(signal_number, callback))
    monkeypatch.setattr(
        worker, "finish_jobs",
        lambda session, handles, **kwargs: finishes.append(
            (session, handles, kwargs)))
    args = SimpleNamespace(
        from_=["sender@example.com=Yoga"], subject_keyword=[],
        state=str(tmp_path / "seen.json"), account="gmail", folder="INBOX",
        session="nadia-test", status="Watching email", once=False,
        interval=180, notify_existing=False,
    )

    assert worker.run_himalaya(args) == 0
    assert heartbeat_started.is_set()
    assert finishes == [("nadia-test", {"Yoga": "bg1:3:watch"}, {})]
