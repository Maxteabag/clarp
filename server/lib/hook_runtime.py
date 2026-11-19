"""Shared runtime helpers for Claude Code hooks."""
from __future__ import annotations

import os
import pathlib
import time
from collections.abc import Callable, Mapping
from typing import Any

# Names that say whose turn a process runs in or who it is. A child this Host starts never takes
# them from the Host's own environment: the Host may itself have been started inside another
# turn (by hand, from an agent's shell, or inside a turn the Rust Host runs), and an inherited
# variable is not an identity (R96, R111). Each launcher clears them, then sets its own.
INHERITED_IDENTITY_ENV = (
    "CLARP_RUST_MANAGED_TURN",      # the Rust Host's turn: our hooks stay out (plugin/hooks/_clarp_lib.py)
    "CLARP_PROVIDER_TURN",          # the turn token (lib.provider_background_jobs.TURN_ENV)
    "CLARP_AGY_MANAGED_TURN",       # an agy turn the stream owns (lib.backend.agy_hooks)
    "CLAUDE_PWA_SESSION",           # the agent's session (app_session below)
    "CLARP_SESSION",                # the session skills and clarp-admin take as the caller's
    "CLAUDE_CODE_SESSION_ID",       # the Claude conversation a process was started from (adopt)
    "CLARP_BACKGROUND_ORIGIN",      # the tool call a background job was started by
    "CLARP_BACKGROUND_WORKER_PID",  # the worker a background job reports as
)


def without_inherited_identity(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` with every name of INHERITED_IDENTITY_ENV removed: the base a launcher adds the
    child's own identity to."""
    return {key: value for key, value in env.items() if key not in INHERITED_IDENTITY_ENV}


def app_session(env: Mapping[str, str] | None = None) -> str:
    """Return the durable app session injected into a dispatched turn."""
    env = env if env is not None else os.environ
    return (env.get("CLAUDE_PWA_SESSION") or "").strip()


class HookLogger:
    def __init__(self, name: str, log_file: pathlib.Path,
                 emit: Callable[..., Any] | None = None):
        self.name = name
        self.log_file = log_file
        self.emit = emit

    def log(self, msg: str) -> None:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {self.name:<8} "
                        f"{os.getpid()} {msg}\n")
        except OSError:
            pass
        if self.emit is None:
            return
        try:
            self.emit(
                f"{self.name}_hook", "log",
                session=app_session() or None,
                detail={"msg": msg},
            )
        except Exception:
            pass
