"""Runner adapters must report one truthful, classifiable terminal outcome."""
from __future__ import annotations

import io
import json

from lib import clarp_runner, codex_runner, error_classify


class FakeProc:
    def __init__(self, lines=(), *, stderr="", returncode=0):
        self.stdout = iter(lines)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.pid = 4242

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def _callbacks():
    seen = []
    return (
        seen,
        lambda event: seen.append(("result", event)),
        lambda error: seen.append(("error", error)),
    )


def test_clarp_preserves_exit_code_when_stderr_is_not_empty():
    seen, on_result, on_error = _callbacks()

    clarp_runner._drain_stdout(  # noqa: SLF001
        FakeProc(stderr="backend crashed", returncode=3),
        None, on_result, on_error, "trace")

    assert [kind for kind, _ in seen] == ["error"]
    assert error_classify.classify_error(seen[0][1]) == error_classify.RUNNER_EXIT


def test_clarp_emits_exactly_one_terminal_callback_after_result():
    seen, on_result, on_error = _callbacks()
    line = json.dumps({
        "type": "result", "subtype": "success", "result": "done",
    }) + "\n"

    clarp_runner._drain_stdout(  # noqa: SLF001
        FakeProc([line], stderr="late shutdown error", returncode=1),
        None, on_result, on_error, "trace")

    assert [kind for kind, _ in seen] == ["result"]


def test_legacy_codex_abort_reaches_dispatcher_as_an_error():
    seen, on_result, on_error = _callbacks()
    line = json.dumps({"type": "turn_aborted"}) + "\n"

    codex_runner._drain_stdout(  # noqa: SLF001
        proc=FakeProc([line]), agent_id="", session="codex", trace_id="trace",
        backend_session_id="", handle=None, on_session_init=None,
        on_result=on_result, on_error=on_error, stream=None,
        enqueue=lambda **_kwargs: 0)

    assert [kind for kind, _ in seen] == ["error"]
    assert error_classify.classify_error(seen[0][1]) == error_classify.INTERRUPTED
