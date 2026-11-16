"""Keep a private, content-free trace of every hook failure (HOOK-01).

Append the mandatory hook-log line first, independent of detailed diagnostics.
Then attempt the optional event sink. Its output and failures must never affect
hook stdout, stderr or status. If neither destination is usable, remain fail-soft.
"""
from __future__ import annotations

import contextlib
import io
import atexit
import os
import pathlib
import sys
import time


def _detail(exc: BaseException) -> dict:
    detail = {"error": type(exc).__name__}
    code = getattr(exc, "sqlite_errorname", None)
    if code:
        detail["code"] = str(code)
    return detail


def _quiet_exit_output() -> None:
    # Preserve the hook's output before deferred optional diagnostics run.
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()


def report(hook: str, event: str, exc: BaseException) -> None:
    detail = _detail(exc)
    try:
        # Bootstrap lazily: importing this reporter must also work without HOME.
        import _clarp_lib  # noqa: F401
        from lib.paths import RuntimePaths
        home = pathlib.Path(os.path.expanduser("~"))
        if home.is_absolute():
            log = RuntimePaths.from_home(home).hook_log
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {hook} {os.getpid()} {event} "
                        f"{detail['error']}{' ' + detail['code'] if 'code' in detail else ''}\n")
    except (ImportError, OSError, RuntimeError, ValueError):
        # The private file is unusable; still try the optional sink below.
        pass

    # The mandatory trace was attempted. Optional telemetry must not surface
    # writer diagnostics or errors, even when its own writers fail internally.
    with contextlib.suppress(Exception):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import _clarp_lib  # noqa: F401
            from lib.eventlog import emit
            # Do not move or replace the emitter's existing flush callback.
            # Only silence later shutdown diagnostics after preserving output.
            atexit.unregister(_quiet_exit_output)
            atexit.register(_quiet_exit_output)
            emit(hook, event, level="error", detail=detail)
