import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.hook_runtime import app_session, HookLogger


def test_app_session_reads_injected_identity():
    assert app_session({"CLAUDE_PWA_SESSION": "rachel"}) == "rachel"


def test_app_session_ignores_unrelated_environment():
    assert app_session({"SHELL": "/bin/bash"}) == ""


def test_hook_logger_writes_plain_log_and_event(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr("lib.hook_runtime.app_session", lambda: "claude")
    logger = HookLogger("stop", tmp_path / "hook.log",
                        emit=lambda *a, **kw: events.append((a, kw)))

    logger.log("hello")

    assert "stop" in (tmp_path / "hook.log").read_text()
    assert "hello" in (tmp_path / "hook.log").read_text()
    assert events
    assert events[0][0][:2] == ("stop_hook", "log")
    assert events[0][1]["session"] == "claude"
