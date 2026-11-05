"""Process-helper ABI and failure contracts against controlled OS boundaries."""
import ctypes
import hashlib
import json
import signal
import sys
from types import SimpleNamespace

import pytest

from lib import background_jobs as jobs
from tests.patching import patch_shared


@pytest.mark.parametrize("failure", [None, "size", "read", "library", "invalid_pid"])
def test_macos_procargs_sizes_then_reads_and_preserves_syscall_errors(monkeypatch, failure):
    raw = (2).to_bytes(4, sys.byteorder, signed=True) + b"/python\0\0python\0worker.py\0"
    calls = []

    def sysctl(mib, count, buffer, size, new_value, new_size):
        assert list(mib) == [1, 49, 42] and count == 3
        assert new_value is None and new_size == 0
        phase = "size" if buffer is None else "read"
        calls.append(phase)
        if phase == failure:
            return -1
        length = ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))
        if buffer is None:
            length.contents.value = len(raw)
        else:
            assert length.contents.value == len(raw)
            ctypes.memmove(buffer, raw, len(raw))
        return 0

    def library(name, *, use_errno):
        assert name == "synthetic-libc" and use_errno is True
        return SimpleNamespace(sysctl=sysctl)

    monkeypatch.setattr(jobs.ctypes.util, "find_library",
                        lambda name: None if failure == "library" else "synthetic-libc")
    monkeypatch.setattr(jobs.ctypes, "CDLL", library)
    monkeypatch.setattr(jobs.ctypes, "get_errno", lambda: 13)
    if failure == "invalid_pid":
        assert jobs._macos_procargs(0) == b"" and calls == []
    elif failure is None:
        assert jobs._macos_procargs(42) == raw
        assert calls == ["size", "read"]
    else:
        with pytest.raises(OSError) as caught:
            jobs._macos_procargs(42)
        if failure == "library":
            assert "libc not found" in str(caught.value) and calls == []
        else:
            assert caught.value.errno == 13
            assert calls == (["size"] if failure == "size" else ["size", "read"])


@pytest.mark.parametrize("reply_file", ["valid", "not_dict", "malformed", "missing"])
def test_adopted_watch_targets_accept_cli_forms_and_optional_reply_json(tmp_path, monkeypatch, reply_file):
    path = tmp_path / "reply.json"
    if reply_file != "missing":
        path.write_text({"valid": json.dumps({"slot": " Delta "}),
                         "not_dict": "[]", "malformed": "{"}[reply_file])
    argv = ["python", "/skills/watch_messages.py", "whatsapp", "--watch", "jid=Alpha",
            "--watch-name=Beta", "--from", "jid=Alpha", "--subject-keyword", "Gamma",
            "--subject-keyword=Gamma", "--reply-watch-json", str(path), "--reply-watch-json=" + str(path)]
    patch_shared(monkeypatch, jobs, "process_argv", lambda _pid: argv)
    labels = ["Alpha", "Beta", "Delta", "Gamma"] if reply_file == "valid" else ["Alpha", "Beta", "Gamma"]
    expected = ["message-watch-whatsapp-" + hashlib.sha256(
        ("owner\0whatsapp\0" + label).encode()).hexdigest()[:16] for label in labels]
    assert jobs.adopted_worker_job_ids(42, "owner") == expected


@pytest.mark.parametrize("argv", [["python", "other.py", "whatsapp"],
                                   ["python", "watch_messages.py"],
                                   ["python", "watch_messages.py", "unrelated"]])
def test_unrecognized_workers_are_not_adopted(monkeypatch, argv):
    patch_shared(monkeypatch, jobs, "process_argv", lambda _pid: argv)
    assert jobs.adopted_worker_job_ids(42, "owner") == []


@pytest.mark.parametrize("error_signal,error,stays_alive,expected", [
    (None, None, False, True),
    (signal.SIGTERM, ProcessLookupError, True, True),
    (signal.SIGTERM, PermissionError, True, False),
    (signal.SIGKILL, ProcessLookupError, True, True),
    (signal.SIGKILL, PermissionError, True, False),
    (None, None, True, False),
])
def test_termination_escalation_reports_disappearance_permission_and_timeout(
        monkeypatch, error_signal, error, stays_alive, expected):
    sent = []
    live = {"alive": True}

    def alive(pid, token):
        assert (pid, token) == (42, "fixed-start-token")
        return live["alive"]

    def send(pid, sig):
        assert pid == 42
        sent.append(sig)
        if sig == error_signal:
            raise error("synthetic signal failure")
        if sig == signal.SIGKILL and not stays_alive:
            live["alive"] = False

    patch_shared(monkeypatch, jobs, "worker_is_alive", alive)
    monkeypatch.setattr(jobs.os, "kill", send)
    assert jobs.terminate_worker(42, "fixed-start-token", term_timeout=0, kill_timeout=0,
                                 monotonic=lambda: 0,
                                 sleep=lambda _: pytest.fail("zero budget must not sleep")) is expected
    assert sent == ([signal.SIGTERM] if error_signal == signal.SIGTERM else [signal.SIGTERM, signal.SIGKILL])
