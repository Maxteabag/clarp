"""Drain an old runtime only after a new runtime release is activated."""
from __future__ import annotations

import pathlib
import os
import threading
from collections.abc import Callable

from .log import log_exception


def read_runtime_release_id(root: pathlib.Path | str) -> str:
    root = pathlib.Path(root)
    if not (root / "RUNTIME_READY").is_file():
        return ""
    try:
        return (root / "RUNTIME_RELEASE_ID").read_text().strip()
    except OSError:
        return ""


def mark_clean_handoff(path: pathlib.Path | str) -> None:
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.next")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, b"clean\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    temporary.replace(target)


def consume_clean_handoff(path: pathlib.Path | str) -> bool:
    target = pathlib.Path(path)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


class RuntimeReleaseMonitor:
    def __init__(
        self,
        runtime,
        *,
        running_release_id: str,
        desired_release_id: Callable[[], str],
        before_shutdown: Callable[[], None] | None = None,
        interval_sec: float = 1.0,
    ):
        self.runtime = runtime
        self.running_release_id = str(running_release_id or "")
        self.desired_release_id = desired_release_id
        self.before_shutdown = before_shutdown or (lambda: None)
        self.interval_sec = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check_once(self) -> bool:
        desired = str(self.desired_release_id() or "")
        if not desired or desired == self.running_release_id:
            return False
        if not self.runtime.begin_drain_if_idle():
            return False
        self.before_shutdown()
        self.runtime.shutdown()
        return True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="runtime-release-monitor")
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            try:
                if self.check_once():
                    return
            except Exception as exc:  # noqa: BLE001 - retry on next poll
                log_exception("runtimeReleaseMonitorFail", exc)
