"""Report a hook's swallowed failure without content (HOOK-01).

A hook must never stop the user's turn, so it catches broad exceptions and
keeps its exit code, stdout and return value. It must not swallow them
silently either: `report` records the hook, the event and the exception's
class (plus SQLite's error name when there is one), never its message, in
Clarp's event log. If even that fails, it appends one line to the hook log
file the other hooks already use. It writes nothing to stdout or stderr,
so no runner (Claude Code, Codex, agy) can surface it to the model or the
apps, and it never raises.
"""
from __future__ import annotations

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
        # Imported here, not at module level: _clarp_lib resolves the home
        # directory at import, which raises in a container without one, and
        # importing this module must never fail (agy_state is fail-open).
        import _clarp_lib  # noqa: F401  — puts Clarp's `lib` on sys.path
        from lib.eventlog import emit
        emit(hook, event, level="error", detail=detail)
        return
    except Exception as emit_error:  # noqa: BLE001 - recorded in the hook log below
        unrecorded = type(emit_error).__name__
    try:
        from lib.paths import RuntimePaths
        # expanduser never raises; without a home it returns "~" unchanged.
        home = pathlib.Path(os.path.expanduser("~"))
        if not home.is_absolute():
            return
        log = RuntimePaths.from_home(home).hook_log
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {hook} {os.getpid()} {event} "
                    f"{detail['error']}{' ' + detail['code'] if 'code' in detail else ''} "
                    f"(event log unavailable: {unrecorded})\n")
    except (ImportError, OSError, RuntimeError, ValueError):
        # Clarp is not importable, there is no home, or the log is not
        # writable: nothing is left to write to, and a hook must not fail the
        # user's turn.
        return
