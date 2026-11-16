"""Keep a private, content-free trace of every hook failure (HOOK-01).

Append the mandatory hook-log line first, independent of detailed diagnostics.
Then attempt the optional event sink without intercepting process streams.
Existing sink diagnostics remain unchanged; sink failures must not change the
hook status. If neither destination is usable, remain fail-soft.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import time


def _detail(exc: BaseException) -> dict:
    detail = {"error": type(exc).__name__}
    code = getattr(exc, "sqlite_errorname", None)
    if code:
        detail["code"] = str(code)
    return detail


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

    # The mandatory trace was attempted. The optional mirror is best effort;
    # its existing writer diagnostics are outside this reporter's scope (#395).
    with contextlib.suppress(Exception):
        import _clarp_lib  # noqa: F401
        from lib.eventlog import emit
        emit(hook, event, level="error", detail=detail)
