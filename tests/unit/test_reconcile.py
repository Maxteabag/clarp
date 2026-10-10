"""Reconciliation layer (lib.reconcile invariants INV1-INV3).

Derived state (state_log kind, bound session, in-flight slot) must agree with
reality (live process / terminal / transcript on disk). These are
fault-injection tests: each one breaks an invariant and expects the repair."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import agents as agents_db  # noqa: E402
from lib import reconcile, turn_dispatch  # noqa: E402
from lib.audio_stream import AudioStream  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.snapshot import build_agent_snapshot  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402


def _agent(tmp_path, session="mike", backend="claude"):
    agent_id = agents_db.create_agent(
        persona=session.title(), voice_id="V", cwd=str(tmp_path),
        session=session, backend=backend)
    agents_db.start_runtime(agent_id, session)
    return agent_id


def _ctx(tmp_path, session):
    return ServerContext(
        root=tmp_path, static=tmp_path, audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json",
        default_session=session, tts=FakeTTSEngine(tmp_path / "audio"),
        stream=AudioStream(tmp_path / "audio"), stt=StubSTT(),
        roster_names=(session.title(),))


@pytest.fixture(autouse=True)
def _clean_dispatcher_state():
    turn_dispatch._INFLIGHT.clear()
    turn_dispatch._QUEUED.clear()
    turn_dispatch._CLAIMED_AT.clear()
    yield
    turn_dispatch._INFLIGHT.clear()
    turn_dispatch._QUEUED.clear()
    turn_dispatch._CLAIMED_AT.clear()


def test_inv1_stuck_thinking_without_process_is_repaired_at_snapshot_time(tmp_path):
    """Handoff fault #2: a turn died without its terminal callback. The
    snapshot must report busy=False and the stuck row must be repaired."""
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    snap = build_agent_snapshot(_ctx(tmp_path, "mike"))
    row = next(a for a in snap["agents"] if a["session"] == "mike")
    assert row["busy"] is False
    assert agents_db.latest_state(agent_id)["kind"] == "idle"
    assert agents_db.latest_state(agent_id)["detail"]["reason"] == "reconcile"


def test_inv1_live_process_keeps_busy(tmp_path, monkeypatch):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "tool")
    monkeypatch.setattr(reconcile.backends, "active_handles", lambda b, a: ["proc"])
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert repaired == {}
    assert agents_db.latest_state(agent_id)["kind"] == "tool"


def test_inv1_spawning_slot_counts_as_live(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    turn_dispatch._INFLIGHT[agent_id] = "t-spawn"
    turn_dispatch._CLAIMED_AT[agent_id] = 1.0
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path) == {}
    assert agents_db.latest_state(agent_id)["kind"] == "thinking"


def test_inv1_background_is_not_a_process_claim(tmp_path):
    """'background' is agent-declared out-of-band work with no server-visible
    process; reconciling it away would kill the Background-task indicator."""
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "background", {"label": "Watching CI"})
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path) == {}
    assert agents_db.latest_state(agent_id)["kind"] == "background"


def test_inv2_ghost_claude_session_is_unbound(tmp_path):
    """Handoff fault #11 generalised: a bound session with no transcript on
    disk would be resumed forever (exits instantly, rc=0). Unbind it."""
    agent_id = _agent(tmp_path)
    agents_db.bind_backend_session(agent_id, "ghost-uuid-1")
    assert agents_db.live_backend_session(agent_id) == "ghost-uuid-1"
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert repaired.get("ghost_session") == "ghost-uuid-1"
    assert agents_db.live_backend_session(agent_id) == ""


def test_inv2_real_transcript_keeps_binding(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.bind_backend_session(agent_id, "real-uuid-1")
    proj = tmp_path / ".claude" / "projects" / "-tmp-proj"
    proj.mkdir(parents=True)
    (proj / "real-uuid-1.jsonl").write_text('{"type":"system","subtype":"init"}\n')
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert "ghost_session" not in repaired
    assert agents_db.live_backend_session(agent_id) == "real-uuid-1"


def test_inv2_stale_transcript_index_keeps_binding(tmp_path, monkeypatch):
    """The index can miss a transcript that is on disk (its watches outlived a
    re-pointed ~/.claude). Unbinding on that miss wiped the fleet's history."""
    from lib import claude_transcript
    agent_id = _agent(tmp_path)
    agents_db.bind_backend_session(agent_id, "real-uuid-2")
    proj = tmp_path / ".claude" / "projects" / "-tmp-proj"
    proj.mkdir(parents=True)
    (proj / "real-uuid-2.jsonl").write_text('{"type":"system","subtype":"init"}\n')
    monkeypatch.setattr(claude_transcript, "find_latest_jsonl", lambda *_a, **_k: None)
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert "ghost_session" not in repaired
    assert agents_db.live_backend_session(agent_id) == "real-uuid-2"


def test_inv3_dead_inflight_slot_is_freed(tmp_path):
    agent_id = _agent(tmp_path)
    turn_dispatch._INFLIGHT[agent_id] = "dead-trace"
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert repaired.get("slot") == "dead-trace"
    assert agent_id not in turn_dispatch._INFLIGHT


def test_inv3_leaves_queued_and_terminal_slots_alone(tmp_path):
    agent_id = _agent(tmp_path)
    turn_dispatch._INFLIGHT[agent_id] = "t1"
    turn_dispatch._QUEUED[agent_id] = ["spec"]
    assert turn_dispatch.free_stale_slot(agent_id) is None
    turn_dispatch._QUEUED.clear()
    turn_dispatch._INFLIGHT[agent_id] = turn_dispatch._TERMINAL_SENTINEL
    assert turn_dispatch.free_stale_slot(agent_id) is None


# --- INV3 with an external runtime ------------------------------------------
#
# The runtime owns the slot table then: the Host's own table is empty, and a
# slot the runtime still holds makes the agent look live here, so the old
# INV3 could never free anything. It asks the runtime to run its own guarded
# leak check instead, once the turn has visibly settled.

class _Runtime:
    def __init__(self, agent_id, *, releases=True, spawning=False, fail=False):
        self.agent_id = agent_id
        self.releases = releases
        self.fail = fail
        self.active = {agent_id: "tA"}
        self.spawning = [agent_id] if spawning else []
        self.asked = []

    def status(self):
        return {"active": dict(self.active), "spawning": list(self.spawning),
                "terminals": [], "queued": dict(getattr(self, "queued", {}))}

    def release_leaked_slots(self, agent_id):
        self.asked.append(agent_id)
        if self.fail:
            raise RuntimeError("runtime went away")
        if not self.releases:
            return {}
        self.active.pop(agent_id, None)
        return {agent_id: "tA"}


@pytest.fixture
def runtime_owned(monkeypatch):
    from lib import backends
    installed = []

    def install(runtime):
        backends.configure_runtime_client(runtime)
        turn_dispatch.configure_runtime_client(runtime)
        installed.append(runtime)
        return runtime

    reconcile._LEAK_CHECK_ASKED.clear()
    yield install
    backends.configure_runtime_client(None)
    turn_dispatch.configure_runtime_client(None)
    reconcile._LEAK_CHECK_ASKED.clear()


def _settled(agent_id, kind="done", ms_ago=10 * 60_000):
    from lib import db
    agents_db.record_state(agent_id, kind)
    db.conn().execute("UPDATE state_log SET ts = ts - ? WHERE agent_id = ?",
                      (ms_ago, agent_id))


def test_inv3_asks_the_runtime_to_release_a_settled_turns_slot(tmp_path, runtime_owned):
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime = runtime_owned(_Runtime(agent_id))

    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == [agent_id]
    assert repaired.get("slot") == "tA"


@pytest.mark.parametrize("case", ["running", "fresh_settle", "no_slot"])
def test_inv3_leaves_the_runtime_alone_while_a_turn_may_be_live(
        tmp_path, runtime_owned, case):
    agent_id = _agent(tmp_path)
    if case == "running":
        _settled(agent_id, kind="thinking")
    else:
        _settled(agent_id, ms_ago=1_000 if case == "fresh_settle" else 10 * 60_000)
    runtime = runtime_owned(_Runtime(agent_id, spawning=case == "spawning"))
    if case == "no_slot":
        runtime.active.clear()

    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == []
    assert "slot" not in repaired


def test_inv3_asks_the_runtime_about_a_settled_agents_spawning_slot(
        tmp_path, runtime_owned):
    # Pebble, 2026-10-10: a queued handoff failed on "database is locked" and
    # left the slot "spawning" with no launch behind it. Only the runtime
    # knows whether a launch or account recovery still owns such a slot.
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime = runtime_owned(_Runtime(agent_id, spawning=True))

    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == [agent_id]
    assert repaired.get("slot") == "tA"


def test_inv3_asks_the_runtime_about_queued_work_with_no_owner(tmp_path, runtime_owned):
    agent_id = _agent(tmp_path)
    _settled(agent_id, kind="idle")
    runtime = _Runtime(agent_id, releases=False)
    runtime.active.clear()
    runtime.queued = {agent_id: 8}
    runtime_owned(runtime)

    reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == [agent_id]


def test_inv3_asks_the_runtime_at_most_once_per_grace_window(tmp_path, runtime_owned):
    # Snapshots reconcile every agent on every read; a slot the runtime
    # decides to keep must not cost one RPC per read.
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime = runtime_owned(_Runtime(agent_id, releases=False))

    for _ in range(3):
        assert "slot" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == [agent_id]


def test_inv3_logs_a_failed_runtime_release(tmp_path, runtime_owned, monkeypatch):
    logged = []
    monkeypatch.setattr(reconcile, "log_exception",
                        lambda event, exc, detail="": logged.append((event, str(exc))))
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime_owned(_Runtime(agent_id, fail=True))

    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert "slot" not in repaired
    assert logged == [("reconcileSlotFail", "runtime went away")]


def test_reconcile_all_covers_every_agent(tmp_path):
    a = _agent(tmp_path, "mike")
    b = _agent(tmp_path, "rachel")
    agents_db.record_state(a, "thinking")
    agents_db.record_state(b, "compacting")
    assert reconcile.reconcile_all(home=tmp_path) == 2
    assert agents_db.latest_state(a)["kind"] == "idle"
    assert agents_db.latest_state(b)["kind"] == "idle"


def test_inv3_runtime_outage_costs_no_slot_check(tmp_path, runtime_owned, monkeypatch):
    logged = []
    monkeypatch.setattr(reconcile, "log_exception",
                        lambda event, exc, detail="": logged.append(event))
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime = runtime_owned(_Runtime(agent_id))
    monkeypatch.setattr(runtime, "status", lambda: (_ for _ in ()).throw(
        RuntimeError("runtime restarting")))

    assert "slot" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert runtime.asked == [] and "reconcileSlotFail" not in logged
