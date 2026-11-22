"""Issue 360, R51: an agent started again in place (POST /agents with replace_sid)
while its turn ran kept that turn running; its answer went into the conversation
the restart ended. The turn is stopped first now. Unlike the user's Stop nothing
is held: what waited behind it runs in the new conversation."""
from __future__ import annotations

from lib import agents as agents_db
from lib import backends as backends_module
from lib import turn_dispatch as _td
from lib import turn_queue
from lib.agent_lifecycle import AgentLifecycleService
from lib.protocol import AgentState

from test_agent_lifecycle import _ctx
from test_turn_dispatch import _make_service


def _states(agent_id):
    return [(row["kind"], row["detail"]) for row in agents_db.conn().execute(
        "SELECT kind, detail FROM state_log WHERE agent_id=? ORDER BY state_id", (agent_id,))]


def test_restarting_an_agent_in_place_stops_its_turn_and_runs_what_waited(tmp_path, monkeypatch):
    service, fake, agent_id = _make_service(tmp_path)
    sessions = iter(f"backend-session-{n}" for n in range(1, 10))
    service.uuid_factory = lambda: next(sessions)
    # Every dispatcher the restart builds is this one, on the fake backends.
    monkeypatch.setattr(_td, "TurnDispatchService", lambda ctx, **_kw: service)
    interrupted = []

    def interrupt(backend, aid):                    # the SIGTERMed CLI exits
        interrupted.append(aid)
        fake.live = False
        return 1
    monkeypatch.setattr(backends_module, "interrupt", interrupt)
    service.dispatch(text="first", requested_session="mike", trace_id="t-old",
                     synthesize_audio=False)
    waiting = service.dispatch(text="waiting", requested_session="mike", trace_id="t-wait",
                               client_msg_id="m-wait", synthesize_audio=False, queue_if_busy=True)
    assert waiting.queued is True and len(fake.spawned) == 1

    AgentLifecycleService(_ctx(tmp_path)).create({"name": "Mike", "replace_sid": "mike"})

    assert interrupted == [agent_id]                              # the running turn was stopped
    assert any(kind == AgentState.INTERRUPTED and '"restart"' in (detail or "")
               for kind, detail in _states(agent_id))
    assert turn_queue.is_paused(agent_id) is False                # nothing is held
    assert [call["text"] for _b, call in fake.spawned] == ["first", "waiting"]
    old = fake.spawned[0][1]
    old["on_result"]({"duration_ms": 1})                          # the stopped turn's late end
    assert _td.runtime_status()["active"].get(agent_id) == "t-wait"


def test_restarting_an_idle_agent_stops_nothing(tmp_path, monkeypatch):
    service, fake, agent_id = _make_service(tmp_path)
    monkeypatch.setattr(_td, "TurnDispatchService", lambda ctx, **_kw: service)
    interrupted = []
    monkeypatch.setattr(backends_module, "interrupt",
                        lambda backend, aid: interrupted.append(aid) or 1)
    AgentLifecycleService(_ctx(tmp_path)).create({"name": "Mike", "replace_sid": "mike"})
    assert interrupted == [] and fake.spawned == []
    assert turn_queue.is_paused(agent_id) is False


def test_with_an_external_runtime_the_runtime_stops_the_turn_and_holds_nothing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                      session="mike", backend="claude")
    agents_db.start_runtime(agent_id, "mike")
    calls = []
    ctx = _ctx(tmp_path)
    ctx.runtime_client = SimpleNamespace(
        begin_stop=lambda aid, backend, **kw: calls.append((aid, kw)) or SimpleNamespace(
            trace_id="", terminated=0, dropped=0),
        recover_queued=lambda: calls.append("recover"))
    AgentLifecycleService(ctx).create({"name": "Mike", "replace_sid": "mike"})
    assert calls[0] == (agent_id, {"strict": False, "hold": False, "fence_user_receipts": False})
    assert turn_queue.is_paused(agent_id) is False
