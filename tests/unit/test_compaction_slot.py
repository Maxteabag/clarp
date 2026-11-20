"""The user's compaction holds the agent's turn slot (R148).

Sends that arrive while it runs wait behind it: none is lost, none starts on
top of it, and they run after it in the order they came, also across a
runtime restart. A Stop takes the slot back and leaves the agent usable.
"""
from __future__ import annotations

import pytest

from lib import turn_queue
from lib import turn_dispatch as td

from test_drain_pause_parity import World


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield
    td.reset_for_tests()


def _waiting(world):
    return [row["client_msg_id"] for row in turn_queue.pending(world.agent)]


def test_sends_during_a_compaction_wait_and_run_after_it_in_order(tmp_path):
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    world.send("u1")
    world.send("p1", origin="agent")
    world.send("q1", origin="agent", queue=True)
    world.send("u2")
    world.service.recover_queued()       # recovery does not start them either
    world._collect()
    assert world.started == []
    assert world.provider.spawned == []
    assert _waiting(world) == ["u1", "p1", "q1", "u2"]   # durable, in order
    td.finish_compaction(world.service, world.agent)
    world.settle()
    assert world.started == ["u1", "p1", "q1", "u2"]
    assert _waiting(world) == []


def test_a_user_send_during_a_compaction_survives_a_runtime_restart(tmp_path):
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    world.send("u1")
    world.send("u2")
    world.restart()                      # memory gone, the compaction with it
    assert world.started == ["u1", "u2"]
    assert _waiting(world) == []


def test_a_compaction_is_refused_while_the_agent_is_busy_or_has_work_waiting(tmp_path):
    world = World(tmp_path)
    world.send("t0")
    assert td.claim_for_compaction(world.agent) == "agent is busy — try again when idle"
    world.settle()
    assert td.claim_for_compaction(world.agent) == ""
    assert td.claim_for_compaction(world.agent) == "already compacting"
    td.finish_compaction(world.service, world.agent)
    assert td.compaction_holds_slot(world.agent) is False


def test_nothing_preempts_a_compaction(tmp_path):
    """No origin interrupts it: the provider is never asked to stop."""
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    for origin in ("user", "agent", "system"):
        world.send(f"x-{origin}", origin=origin)
    assert world.provider.spawned == []
    assert td.compaction_holds_slot(world.agent) is True


def test_stop_during_a_compaction_leaves_the_agent_usable(tmp_path):
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    world.send("u1")
    world.stop()
    # The compaction sees its slot is gone and ends; its finish is a no-op.
    assert td.compaction_holds_slot(world.agent) is False
    td.finish_compaction(world.service, world.agent)
    world._collect()
    started_or_waiting = set(world.started) | set(_waiting(world))
    assert "u1" in started_or_waiting    # not lost
    world.settle()
    world.send("u2")                     # the agent takes new work
    world._collect()
    assert "u2" in world.started
    world.settle()
    assert td.claim_for_compaction(world.agent) == ""   # and can be compacted again
    td.finish_compaction(world.service, world.agent)


def test_the_compaction_slot_is_not_freed_as_stale(tmp_path):
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    assert td.free_stale_slot(world.agent) is None
    assert td.compaction_holds_slot(world.agent) is True
    snapshot = td.snapshot_stop_state(world.agent)
    assert snapshot["trace_id"] == ""    # nothing for a strict Stop to confirm
    td.finish_compaction(world.service, world.agent)


# ---- a compaction that does not end when it should (R148, review item 2) -----

import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from lib import compaction  # noqa: E402
from lib.backend.base import CompactionStrategy  # noqa: E402


class _Live:
    def __init__(self):
        self.alive = True

    def is_alive(self):
        return self.alive


def _wait_for(predicate, seconds=5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run_compaction(world, monkeypatch, *, result, handles, holders):
    """The compaction's background run for the World's agent, as the button
    starts it, with the provider stood in: ``result`` is what the compaction
    reported; ``handles``/``holders`` are what still works on the thread."""
    monkeypatch.setattr(compaction, "_POLL_SEC", 0.01)
    monkeypatch.setattr(compaction.backends, "active_handles", lambda _b, _a: handles)
    monkeypatch.setattr(compaction.backends, "turn_holders", lambda _b, _a: holders)
    monkeypatch.setattr(compaction, "_compact_codex", lambda _agent, _bsid: result)
    compaction._active.clear()
    assert td.claim_for_compaction(world.agent) == ""
    compaction._active.add("mike")
    strategy = CompactionStrategy((), "thread/compact/start", in_process=True)
    run = threading.Thread(target=compaction._run, daemon=True, args=(
        "mike", {"agent_id": world.agent}, "codex", "bs-x", str(world.tmp_path),
        strategy, world.service))
    run.start()
    assert _wait_for(lambda: world.agent in compaction.last_outcomes([world.agent]))
    return run


_TIMED_OUT = compaction.decide_codex(400_000, {
    "status": "timeout", "replaced": False, "after": None,
    "error": "Codex did not finish compacting within 180 s; it did not stop when asked; "
             "this agent's messages wait until that Codex process is restarted"})


def test_a_send_waits_until_the_interrupted_compaction_has_ended(tmp_path, monkeypatch):
    """Codex has not ended the interrupted compaction yet (its handle is live):
    the queued send starts only once it has."""
    world = World(tmp_path)
    handle = _Live()
    run = _run_compaction(world, monkeypatch, result=_TIMED_OUT, handles=[handle], holders=[])
    world.send("u1")
    world.send("u2")
    time.sleep(0.1)
    world._collect()
    assert world.started == [] and run.is_alive()
    # The outcome is recorded: the agent no longer reads as compacting, but
    # the slot is kept while Codex still works on the thread.
    assert td.compaction_holds_slot(world.agent) and not compaction.is_compacting("mike")
    handle.alive = False                 # Codex ended it
    run.join(5)
    assert not run.is_alive()
    world.settle()
    assert world.started == ["u1", "u2"]


def test_when_the_bounded_wait_runs_out_the_send_waits_for_the_stuck_process(tmp_path, monkeypatch):
    """The handle detached (its bounded wait ran out) but the stuck Codex
    process still holds the thread: the user's send stays queued (durable,
    visible as queued), the agent shows compacting, and last_compaction says
    the messages wait for that process; they run once it is recycled."""
    world = World(tmp_path)
    stuck = _Live()
    run = _run_compaction(world, monkeypatch, result=_TIMED_OUT, handles=[], holders=[stuck])
    world.send("u1")
    time.sleep(0.1)
    world._collect()
    assert world.started == []
    assert _waiting(world) == ["u1"]
    assert not compaction.is_compacting("mike")      # an ordinary queued send now
    told = compaction.last_outcomes([world.agent])[world.agent]
    assert told["outcome"] == "unknown"
    assert "messages wait until that Codex process is restarted" in told["detail"]
    stuck.alive = False                  # the stuck process was recycled
    run.join(5)
    world.settle()
    assert world.started == ["u1"]
    assert not compaction.is_compacting("mike")


def test_stop_during_that_wait_ends_the_compaction_and_the_agent_stays_usable(tmp_path, monkeypatch):
    world = World(tmp_path)
    stuck = _Live()
    run = _run_compaction(world, monkeypatch, result=_TIMED_OUT, handles=[], holders=[stuck])
    world.send("u1")
    world.stop()
    run.join(5)
    assert not run.is_alive()            # the wait ends with the Stop
    assert not td.compaction_holds_slot(world.agent)
    assert not compaction.is_compacting("mike")
    stuck.alive = False
    world._collect()
    assert "u1" in set(world.started) | set(_waiting(world))   # not lost
    world.settle()
    world.send("u2")
    world._collect()
    assert "u2" in world.started


def test_the_codex_client_waits_bounded_for_the_interrupted_compaction(monkeypatch):
    """_Client.compact after its time ran out: it interrupts, then waits for
    the end (bounded); without one, the handle's detach marks the agent."""
    from test_compaction_outcome import _AGENT, _client
    from lib import codex_app_server

    def started_only(client, method, _params):
        if method == "thread/compact/start":
            client._notification("turn/started", {"threadId": "thread-c", "turn": {"id": "c-turn"}})
        return {}

    # Codex ends it when asked (well before the detach would).
    monkeypatch.setattr(codex_app_server, "STOP_DETACH_SEC", 2.0)
    monkeypatch.setattr(codex_app_server, "COMPACT_STOP_GRACE_SEC", 1.0)
    client = _client(monkeypatch, started_only)
    client.recycle_if_quiet = lambda: None

    def interrupt(agent_id, handle=None, by_user=False):
        threading.Timer(0.05, client._notification, args=("turn/completed", {
            "threadId": "thread-c", "turn": {"id": "c-turn", "status": "interrupted"}})).start()
    monkeypatch.setattr(client, "interrupt_active", interrupt, raising=False)
    answer = client.compact(_AGENT, "thread-c", timeout=0.05)
    assert answer["status"] == "timeout" and "it stopped when asked" in answer["error"]
    assert client._actives == {}

    # Codex never ends it: the detach marks the agent; its sends wait (admission).
    client = _client(monkeypatch, started_only)
    client.recycle_if_quiet = lambda: None
    monkeypatch.setattr(codex_app_server, "STOP_DETACH_SEC", 0.05)
    monkeypatch.setattr(codex_app_server, "COMPACT_STOP_GRACE_SEC", 0.2)
    monkeypatch.setattr(client, "interrupt_active", lambda *a, **k: None, raising=False)
    answer = client.compact(_AGENT, "thread-c", timeout=0.05)
    assert answer["status"] == "timeout" and "did not stop when asked" in answer["error"]
    assert "agent-c" in client.detached_agents
    with pytest.raises(codex_app_server.CodexSessionStuck):
        codex_app_server.admit_turn(client, "agent-c")


def _orphan_ignoring_hup_and_term() -> int:
    """A process that is not our child and ignores HUP and TERM, like a CLI
    that does not die with its tmux session."""
    out = subprocess.run(["sh", "-c", "trap '' HUP TERM; sleep 30 >/dev/null 2>&1 & echo $!"],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def _open_fds() -> int:
    import os
    return len(os.listdir("/proc/self/fd"))


def test_a_terminal_compactions_cli_is_killed_through_its_pidfd(monkeypatch):
    """The Claude path's hard cap (and every other end): after the tmux kill
    the CLI must exit; one that does not is SIGKILLed through its pidfd."""
    import os
    monkeypatch.setattr(compaction, "_REAP_SEC", 0.2)
    pid = _orphan_ignoring_hup_and_term()
    proc = compaction._Process(pid, os.pidfd_open(pid))
    assert not proc.exited()
    assert compaction._reap(proc) is True
    assert proc.exited()
    proc.close()
    assert proc.fd is None


def test_a_reused_pid_is_never_signalled(monkeypatch):
    """The pidfd belongs to a CLI that has exited; its pid now names another
    process (this test's own, the worst case). Nothing is signalled."""
    import os
    child = subprocess.Popen(["true"])
    fd = os.pidfd_open(child.pid)
    child.wait()
    proc = compaction._Process(os.getpid(), fd)      # the pid "reused" by us
    sent = []
    monkeypatch.setattr(compaction.os, "kill", lambda *a: sent.append(a))
    assert proc.exited()
    assert compaction._reap(proc) is True
    assert proc.kill() is True                       # through the dead pidfd: reaches no one
    assert sent == []
    proc.close()


def test_an_unreaped_zombie_counts_as_gone():
    import os
    child = subprocess.Popen(["true"])
    proc = compaction._Process(child.pid, os.pidfd_open(child.pid))
    assert _wait_for(proc.exited)
    assert compaction._pid_alive(child.pid)          # kill(pid, 0) still says alive
    proc.close()
    child.wait()


def test_without_a_pidfd_nothing_is_signalled_and_the_wait_is_bounded(monkeypatch):
    monkeypatch.setattr(compaction, "_REAP_SEC", 0.2)
    pid = _orphan_ignoring_hup_and_term()
    sent = []
    real_kill = compaction.os.kill

    def kill(target, sig):
        sent.append(sig)
        return real_kill(target, sig)
    monkeypatch.setattr(compaction.os, "kill", kill)
    proc = compaction._Process(pid, None)
    started = time.time()
    assert compaction._reap(proc) is True            # released after the bound
    assert time.time() - started < 2.0
    assert set(sent) <= {0}                          # only liveness probes
    assert compaction._pid_alive(pid)                # not signalled
    real_kill(pid, 9)
    # The pane process without a pidfd API: no descriptor, no signal.
    monkeypatch.delattr(compaction.os, "pidfd_open")
    monkeypatch.setattr(compaction, "_pane_state", lambda _t: (pid, False))
    unwatched = compaction._pane_process("pwa-compact-x")
    assert unwatched is not None and unwatched.fd is None and unwatched.kill() is False


def test_pidfds_do_not_leak(monkeypatch):
    import os
    monkeypatch.setattr(compaction, "_REAP_SEC", 0.2)
    before = _open_fds()
    # The pane's pid changed between the two looks: the pidfd is closed at once.
    pid = _orphan_ignoring_hup_and_term()
    looks = iter([(pid, False), (pid + 1, False)])
    monkeypatch.setattr(compaction, "_pane_state", lambda _t: next(looks))
    assert compaction._pane_process("pwa-compact-x") is None
    assert _open_fds() == before
    # The normal path: opened, reaped, closed.
    monkeypatch.setattr(compaction, "_pane_state", lambda _t: (pid, False))
    proc = compaction._pane_process("pwa-compact-x")
    assert proc is not None and proc.fd is not None
    assert compaction._reap(proc) is True
    proc.close()
    assert _open_fds() == before
    # One that lingers is kept for the hold and closed when the run ends.
    lingering = compaction._Process(os.getpid(), os.pidfd_open(os.getpid()))
    compaction._lingering["s-x"] = lingering
    assert compaction._still_held("s-x", "agent-x", "claude") is True
    compaction._lingering.pop("s-x").close()
    assert _open_fds() == before


def test_a_second_request_while_the_last_compactions_process_ends_is_refused_in_words(
        tmp_path, monkeypatch):
    world = World(tmp_path)
    stuck = _Live()
    run = _run_compaction(world, monkeypatch, result=_TIMED_OUT, handles=[], holders=[stuck])
    monkeypatch.setattr(compaction.shutil, "which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr(compaction.agents_db, "live_backend_session", lambda _a: "bs-x")
    answer = compaction.compact_session("mike")
    assert answer == {"ok": False, "error": "the last compaction is still ending — try again shortly"}
    stuck.alive = False
    run.join(5)
