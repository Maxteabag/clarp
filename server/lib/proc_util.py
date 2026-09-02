"""Subprocess helpers shared by the turn runners."""
from __future__ import annotations

import collections
import subprocess
import threading


class StderrDrain:
    """Drain a child's stderr on a background thread into a bounded tail.

    Reading stderr only after ``proc.wait()`` deadlocks once the child fills
    the OS pipe buffer (64 KiB on Linux): the child blocks on write, never
    closes stdout, and the turn hangs forever (the Python docs warn about
    exactly this). Draining continuously keeps the child unblocked; we keep
    only the tail because that's all the error reporting ever used.
    """

    def __init__(self, proc: subprocess.Popen, *, tail_chars: int = 8192):
        self._chunks: collections.deque[str] = collections.deque()
        self._size = 0
        self._tail = max(256, int(tail_chars))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        stream = proc.stderr
        if stream is None:
            return
        self._thread = threading.Thread(
            target=self._pump, args=(stream,), daemon=True,
            name=f"stderr-drain-{proc.pid}")
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", "replace")
                with self._lock:
                    self._chunks.append(chunk)
                    self._size += len(chunk)
                    while self._size > self._tail and len(self._chunks) > 1:
                        dropped = self._chunks.popleft()
                        self._size -= len(dropped)
        except (OSError, ValueError):
            pass

    def text(self, timeout: float = 2.0) -> str:
        """Return the captured tail (after the child exits, waits briefly for
        the pump to see EOF so the last lines are included)."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._lock:
            return "".join(self._chunks)


def attach_stderr_drain(proc: subprocess.Popen) -> StderrDrain:
    drain = StderrDrain(proc)
    setattr(proc, "_stderr_drain", drain)
    return drain


def stderr_text(proc: subprocess.Popen) -> str:
    """Stderr tail for a finished child: from the drain if one was attached,
    else a direct read (only safe when no drain exists and output is small)."""
    drain = getattr(proc, "_stderr_drain", None)
    if drain is not None:
        return drain.text()
    try:
        return proc.stderr.read() if proc.stderr else ""
    except (OSError, ValueError):
        return ""
