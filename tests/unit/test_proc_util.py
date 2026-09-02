"""StderrDrain: a chatty child must never deadlock the turn (P6.4).

Reading stderr only after wait() hangs forever once the child fills the
64 KiB pipe buffer. The drain thread keeps the pipe flowing and preserves the
tail for error reporting.
"""
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.proc_util import StderrDrain, attach_stderr_drain, stderr_text  # noqa: E402

_CHATTY = (
    "import sys\n"
    "sys.stderr.write('E' * 300000 + 'TAIL-MARKER')\n"
    "sys.stderr.flush()\n"
    "print('stdout-ok')\n"
)


def test_chatty_stderr_child_does_not_deadlock():
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHATTY],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    attach_stderr_drain(proc)
    t0 = time.monotonic()
    out = proc.stdout.read()
    rc = proc.wait(timeout=10)
    assert rc == 0
    assert "stdout-ok" in out
    assert time.monotonic() - t0 < 10
    tail = stderr_text(proc)
    assert tail.endswith("TAIL-MARKER")
    assert len(tail) <= 8192 + 4096, "drain keeps a bounded tail, not the whole stream"


def test_drain_without_stderr_pipe_is_a_noop():
    proc = subprocess.Popen([sys.executable, "-c", "print('x')"],
                            stdout=subprocess.PIPE, stderr=None, text=True)
    drain = StderrDrain(proc)
    proc.wait(timeout=10)
    assert drain.text() == ""
    assert stderr_text(proc) == ""


def test_stderr_text_falls_back_to_direct_read_when_no_drain():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.write('short err')"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    proc.wait(timeout=10)
    assert stderr_text(proc) == "short err"
