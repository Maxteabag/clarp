"""Named timing knobs for server, hooks, and local watchers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServerTiming:
    sse_queue_timeout_sec: float = 10.0
    stop_escape_delay_sec: float = 0.15
    state_watcher_poll_sec: float = 0.25
    audio_recent_window_sec: float = 300.0
    audio_retain_sec: float = 600.0
    audio_janitor_interval_sec: float = 60.0
    # NOTE: the per-turn watchdog (idle/startup/post-init deadlines) was
    # removed in favour of preempt-kill-and-resume — see clarp_runner
    # 'No turn timer'. Its knobs lived here unreferenced until 2026-08-24.


@dataclass(frozen=True)
class HookTiming:
    pwa_source_fresh_window_sec: float = 10.0
    transcript_flush_delay_sec: float = 0.6


SERVER_TIMING = ServerTiming()
HOOK_TIMING = HookTiming()
SQLITE_CONNECT_TIMEOUT_SEC = 5.0
SQLITE_BUSY_TIMEOUT_MS = 5000
