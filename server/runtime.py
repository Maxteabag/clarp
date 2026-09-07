#!/usr/bin/env python3
"""Clarp's restart-independent agent execution service."""
from __future__ import annotations

import pathlib
import signal
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from lib.context import ServerContext  # noqa: E402
from lib.log import log, log_exception  # noqa: E402
from lib.paths import RuntimePaths  # noqa: E402
from lib.runtime_bridge import RuntimeRPCServer  # noqa: E402
from lib.runtime_events import RuntimeEventStream  # noqa: E402
from lib.runtime_release import (  # noqa: E402
    RuntimeReleaseMonitor,
    consume_clean_handoff,
    mark_clean_handoff,
    read_runtime_release_id,
)
from lib.runtime_startup import recover_runtime  # noqa: E402
from lib.turn_dispatch import TurnDispatchService  # noqa: E402


def main() -> int:
    ctx = ServerContext.production(connect_runtime=False)
    ctx.stream = RuntimeEventStream()
    paths = RuntimePaths.from_home(pathlib.Path.home())
    dispatch = TurnDispatchService(ctx)
    handoff_marker = paths.cache_dir / "runtime-clean-handoff"
    clean_handoff = consume_clean_handoff(handoff_marker)
    running_release_id = read_runtime_release_id(ctx.root)
    runtime = RuntimeRPCServer(
        paths.runtime_socket, dispatch_service=dispatch,
        release_id=running_release_id)
    release_monitor = RuntimeReleaseMonitor(
        runtime,
        running_release_id=running_release_id,
        desired_release_id=lambda: read_runtime_release_id(
            paths.data_dir / "current"),
        before_shutdown=lambda: mark_clean_handoff(handoff_marker),
    )

    # SIGTERM is used only for a deliberate runtime drain or host shutdown.
    # Raising here lets serve_forever unwind without calling shutdown() from
    # its own thread (which would deadlock socketserver).
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        recovered = recover_runtime(ctx, dispatch, clean_handoff=clean_handoff)
        log("runtimeReady", f"socket={paths.runtime_socket} recovery={recovered}")
        if running_release_id:
            release_monitor.start()
        runtime.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        log("runtimeStop", "runtime service stopping")
    except Exception as exc:  # noqa: BLE001
        log_exception("runtimeFatal", exc)
        return 1
    finally:
        release_monitor.stop()
        runtime.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
