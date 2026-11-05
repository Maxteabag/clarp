"""TP-05: the ghost-session guard ran at admission for every send. A new
agent's first turn binds a pre-minted session id whose transcript exists
only once the CLI has written it; a second send in that window found no
transcript, 'reset the ghost' and bound a new session, so the running turn
lost its conversation. The guard is for a binding nothing is using: it
leaves the session of a turn that is starting or running alone."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents, turn_dispatch as td


@pytest.fixture
def dispatch_case(tmp_path, monkeypatch):
    td._SLOTS.reset_for_tests()
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    agent = agents.create_agent(persona="Synthetic", voice_id="v", cwd=str(tmp_path),
                                session="synthetic", backend="claude")
    agents.start_runtime(agent, "synthetic")
    handle = SimpleNamespace(is_alive=lambda: True)

    class Backend:
        CLAUDE = "claude"

        def __init__(self):
            self.spawned = []
            self.live = False

        def normalize(self, value):
            return value or "claude"

        def active_handles(self, backend, agent_id):
            return [handle] if self.live else []

        def interrupt(self, backend, agent_id):
            return 1

        def spawn_turn(self, backend, **kwargs):
            self.spawned.append(kwargs)
            self.live = True
            return handle

    backend = Backend()
    ids = iter(["native-one", "native-two", "native-three"])
    ctx = SimpleNamespace(default_session="synthetic", agents_path=tmp_path / "unused.json",
                          stream=SimpleNamespace(broadcast=lambda _event: None))
    service = td.TurnDispatchService(ctx, backend_registry=backend, home=tmp_path,
                                     uuid_factory=lambda: next(ids), now=lambda: 12.5)
    yield service, backend, agent
    td._SLOTS.reset_for_tests()


def test_a_queued_send_keeps_the_session_of_a_turn_that_has_not_written_its_transcript(dispatch_case):
    service, backend, agent = dispatch_case
    service.dispatch(text="first", requested_session="synthetic", trace_id="t-one",
                     synthesize_audio=False)
    native = agents.live_backend_session(agent)
    assert native == "native-one" and len(backend.spawned) == 1
    queued = service.dispatch(text="second", requested_session="synthetic", trace_id="t-two",
                              synthesize_audio=False, queue_if_busy=True)
    assert queued.queued and len(backend.spawned) == 1
    assert agents.live_backend_session(agent) == native


def test_an_idle_agent_bound_to_a_session_without_transcript_still_starts_fresh(dispatch_case):
    """Control: the guard still drops a binding nothing is using."""
    service, backend, agent = dispatch_case
    agents.bind_backend_session(agent, "ghost-session")
    service.dispatch(text="hello", requested_session="synthetic", trace_id="t-fresh",
                     synthesize_audio=False)
    assert backend.spawned[-1]["backend_session_id"] == "native-one"
    assert backend.spawned[-1]["is_new_session"] is True
