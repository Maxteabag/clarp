"""Shared runtime helpers for Claude Code hooks."""
from __future__ import annotations

import os
import pathlib
import time
from collections.abc import Callable, Mapping
from typing import Any

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
