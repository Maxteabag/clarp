"""A caller that hung up before the runtime's answer is one log line, not a traceback (R147)."""
from __future__ import annotations

import errno
import time

import pytest

from lib import runtime_rpc_server


class _Pipe:
    def __init__(self, error):
        self.error = error

    def write(self, data):
        raise self.error


def _handler(error):
    handler = runtime_rpc_server._RuntimeRequestHandler.__new__(runtime_rpc_server._RuntimeRequestHandler)
    handler.wfile = _Pipe(error)
    return handler


@pytest.mark.parametrize("error", [BrokenPipeError(errno.EPIPE, "Broken pipe"),
                                   ConnectionResetError(errno.ECONNRESET, "reset")], ids=repr)
def test_a_caller_that_hung_up_is_one_line_with_the_method_and_the_wait(monkeypatch, error):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    _handler(error)._answer({"ok": True}, "status", time.monotonic() - 1.2)
    [(event, detail)] = lines
    assert event == "runtimeRpcCallerGone"
    assert detail.startswith(f"method=status error={type(error).__name__} elapsed_ms=")
    assert int(detail.rsplit("=", 1)[1]) >= 1200


def test_any_other_write_error_keeps_its_traceback(monkeypatch):
    monkeypatch.setattr("lib.log.log", lambda *a, **k: pytest.fail("no quiet line for another error"))
    with pytest.raises(OSError):
        _handler(OSError(errno.EIO, "I/O error"))._answer({"ok": True}, "status", time.monotonic())
