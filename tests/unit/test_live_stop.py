"""A stopped or restart-interrupted turn settles on the live channel
(docs/live-items.md §1.2, §1.3): an explicit `interrupted` status op, the turn
ended with `worked_ms`, running items interrupted. On a split Host the stop is
recorded by the HTTP process while the hub runs in clarp-runtime, so the
transition must reach the runtime's hub (real-Host probe, 2026-10-03: the turn
stayed `running`/`thinking` forever)."""
from __future__ import annotations

import threading
import time

import pytest

from lib import agents as agents_db
from lib import backends, live_hub, turn_lifecycle
from lib.live_hub import LiveFanout, LiveHub
from lib.runtime_bridge import RuntimeClient, RuntimeRPCServer
from lib.turn_lifecycle import TurnEvent


class _Dispatch:
    pass


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def split_host(tmp_path):
    """A runtime with its hub behind its socket; this process plays the HTTP
    server (no hub of its own, a runtime client configured)."""
    fanout = LiveFanout()
    hub = LiveHub(sink=fanout.publish)
    runtime = RuntimeRPCServer(tmp_path / "rt.sock", dispatch_service=_Dispatch(),
                               status_provider=lambda: {})
    runtime.live_fanout, runtime.live_hub = fanout, hub
    threading.Thread(target=runtime.serve_forever, daemon=True).start()
    live_hub.install(None)
    backends.configure_runtime_client(RuntimeClient(tmp_path / "rt.sock"))
    try:
        yield hub
    finally:
        backends.configure_runtime_client(None)
        runtime.shutdown()
        runtime.server_close()


def _running_turn(hub, backend, *, tool_running):
    agent_id = agents_db.create_agent(persona="Nova", voice_id="v", cwd="/tmp",
                                      session=f"nova-{backend}", backend=backend)
    agents_db.start_runtime(agent_id, f"nova-{backend}")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id=f"tr-{backend}")
    turn_lifecycle.record(agent_id, "thinking")
    hub.begin_turn(agent_id=agent_id, session=f"nova-{backend}", conv="c", turn_id=f"tr-{backend}")
    hub.tool_start(agent_id, "t1", name="Bash", call_id="t1", category="exec", label="sleep 30")
    if not tool_running:
        hub.done(agent_id, "t1")
    return agent_id


@pytest.mark.parametrize("tool_running", [True, False], ids=["mid-tool", "between-tools"])
@pytest.mark.parametrize("backend", ["claude", "codex", "opencode", "agy"])
def test_a_stop_recorded_by_the_http_process_settles_the_runtime_hub(split_host, backend, tool_running):
    hub = split_host
    agent_id = _running_turn(hub, backend, tool_running=tool_running)
    # What POST /stop records in the HTTP process after the runtime stopped the turn.
    turn_lifecycle.try_transition(agent_id, TurnEvent.STOP_REQUESTED,
                                  {"source": "user_stop", "message": "Turn stopped"})
    assert _wait(lambda: hub.snapshot(agent_id=agent_id)["turn"]["status"] == "interrupted")
    snapshot = hub.snapshot(agent_id=agent_id)
    assert snapshot["activity"]["state"] == "interrupted"
    assert snapshot["turn"]["worked_ms"] is not None and snapshot["turn"]["ended_at_ms"]
    [tool] = snapshot["items"]
    assert tool["status"] == ("interrupted" if tool_running else "completed")


def test_a_runtime_restart_settles_the_turn_it_interrupted():
    agent_id = agents_db.create_agent(persona="Nova", voice_id="v", cwd="/tmp", session="nova")
    agents_db.start_runtime(agent_id, "nova")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-old")
    turn_lifecycle.record(agent_id, "tool")      # the turn the old runtime was running
    events = []
    hub = LiveHub(sink=events.append)          # the restarted runtime: no memory of the turn
    live_hub.install(hub)
    try:
        turn_lifecycle.transition(agent_id, TurnEvent.RESTART_INTERRUPTED, {"trace_id": "tr-old"})
        snapshot = hub.snapshot(agent_id=agent_id)
        assert snapshot["turn"]["turn_id"] == "tr-old"
        assert snapshot["turn"]["status"] == "interrupted" and snapshot["turn"]["worked_ms"] is not None
        assert snapshot["activity"]["state"] == "interrupted"
    finally:
        live_hub.install(None)
