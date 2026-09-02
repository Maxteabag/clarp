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


def test_reconcile_all_covers_every_agent(tmp_path):
    a = _agent(tmp_path, "mike")
    b = _agent(tmp_path, "rachel")
    agents_db.record_state(a, "thinking")
    agents_db.record_state(b, "compacting")
    assert reconcile.reconcile_all(home=tmp_path) == 2
    assert agents_db.latest_state(a)["kind"] == "idle"
    assert agents_db.latest_state(b)["kind"] == "idle"
