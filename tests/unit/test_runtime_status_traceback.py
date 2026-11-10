"""10 Nov 2026: during a runtime outage one remembered status failure was
raised once per agent, each raise added its frames to that exception, and
`reconcileSpawningFail` logged a traceback of about 3.2 MB per agent."""
from __future__ import annotations

import pytest

from lib import agents as agents_db
from lib import backends, eventlog, reconcile
from lib.runtime_bridge import RuntimeUnavailable

AGENTS = 40


class _DownRuntime:
    def __init__(self):
        self.calls = 0

    def status(self):
        self.calls += 1
        raise RuntimeUnavailable("synthetic outage")

    def status_within(self, total_s):
        return self.status()


@pytest.fixture
def runtime_down(monkeypatch):
    runtime = _DownRuntime()
    monkeypatch.setattr(backends, "_clock", lambda: 100.0)    # one shared window for the whole test
    backends.configure_runtime_client(runtime)
    try:
        yield runtime
    finally:
        backends.configure_runtime_client(None)


def _spawning_fail_tracebacks(monkeypatch, tmp_path, *, scoped: bool) -> list[str]:
    logged: list[str] = []

    def record(source, event, **kw):
        if event == "reconcileSpawningFail":
            logged.append(kw["detail"]["traceback"])

    monkeypatch.setattr(eventlog, "emit", record)
    ids = [agents_db.create_agent(persona=f"Agent{n}", voice_id="V", cwd=str(tmp_path),
                                  session=f"agent{n}", backend="claude") for n in range(AGENTS)]

    def sweep():
        for agent_id in ids:
            assert reconcile.has_live_work(agent_id, "claude") is True   # cannot tell: no repair

    if scoped:
        with backends.runtime_status_scope(timeout=1.0):
            sweep()
    else:
        sweep()
    return logged


@pytest.mark.parametrize("scoped", [True, False], ids=["snapshot-scope", "shared-window"])
def test_the_logged_traceback_does_not_grow_with_the_number_of_agents(
        monkeypatch, tmp_path, runtime_down, scoped):
    logged = _spawning_fail_tracebacks(monkeypatch, tmp_path, scoped=scoped)

    assert len(logged) == AGENTS
    assert runtime_down.calls == 1, "the failure is still remembered: one RPC for the whole sweep"
    sizes = [len(text) for text in logged]
    assert sizes[-1] == sizes[0], f"first {sizes[0]} bytes, last {sizes[-1]}"
    assert max(sizes) < 4096
    # Each one still shows where the RPC failed and who asked.
    assert all("synthetic outage" in text and "_slot_is_spawning" in text and "in status" in text
               for text in logged)
