"""lib.dispatch_adapters: the origin, audio and idle policy every scheduler
turn goes through."""
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import artifacts, backends, compaction, dreaming, html_forms
from lib import dispatch_adapters as adapters_module
from lib.dispatch_adapters import DispatchAdapters, decision_queues_if_busy


class FakeService:
    def __init__(self, journal, result="dispatched"):
        self.journal = journal
        self.result = result

    def dispatch(self, **kwargs):
        self.journal.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def adapters():
    journal = []
    factories = []
    service = FakeService(journal)
    ctx = SimpleNamespace(runtime_client=None, stream=SimpleNamespace(broadcast=journal.append))

    def factory(received_ctx):
        factories.append(received_ctx)
        return service

    adapters = DispatchAdapters(ctx, factory, new_trace_id=lambda: "fresh-trace")
    adapters.journal, adapters.service, adapters.factories = journal, service, factories
    return adapters


def _agent(session="mike", **kw):
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd="/var/tmp",
                                      session=session, **kw)
    return agents_db.get_by_session(session)


def _busy(agent):
    agents_db.record_state(agent["agent_id"], "thinking", {"source": "test"})


def _silent_forced(kwargs, session):
    assert kwargs["synthesize_audio"] is False
    assert kwargs["requested_session"] == session
    assert kwargs["forced_session"] == session


def test_service_is_built_per_call_from_the_factory(adapters):
    adapters.leader_tick("mike", "tick")
    adapters.leader_tick("mike", "tick")
    assert adapters.factories == [adapters.ctx, adapters.ctx]


def test_leader_tick_is_silent_and_never_checks_busy(adapters):
    adapters.leader_tick("nobody", "review the team")
    (kwargs,) = adapters.journal
    _silent_forced(kwargs, "nobody")
    assert kwargs["origin"] == "leader_tick"
    assert kwargs["trace_id"] == "fresh-trace"
    assert kwargs["queue_if_busy"] is False


def test_heartbeat_only_reaches_idle_existing_agents(adapters):
    adapters.heartbeat("ghost", "hb")
    assert adapters.journal == []
    agent = _agent()
    _busy(agent)
    adapters.heartbeat("mike", "hb")
    assert adapters.journal == []
    agents_db.record_state(agent["agent_id"], "idle", {"source": "test"})
    adapters.heartbeat("mike", "hb")
    (kwargs,) = adapters.journal
    _silent_forced(kwargs, "mike")
    assert kwargs["origin"] == "heartbeat"


def test_decided_heartbeat_keys_by_request_and_never_queues(adapters):
    assert adapters.decided_heartbeat("mike", "wake", "janitor-demand-7") is True
    (kwargs,) = adapters.journal
    _silent_forced(kwargs, "mike")
    assert kwargs["origin"] == "heartbeat"
    assert kwargs["trace_id"] == kwargs["client_msg_id"] == "janitor-demand-7"
    assert kwargs["queue_if_busy"] is False
    adapters.service.result = None
    assert adapters.decided_heartbeat("mike", "wake", "janitor-demand-8") is False


def test_scheduled_job_requires_an_agent_but_not_idleness(adapters):
    adapters.scheduled_job("ghost", "due")
    assert adapters.journal == []
    _busy(_agent())
    adapters.scheduled_job("mike", "due")
    (kwargs,) = adapters.journal
    _silent_forced(kwargs, "mike")
    assert kwargs["origin"] == "automation"


def test_dream_requires_idle_agent_without_handles_or_compaction(adapters, monkeypatch):
    dreams = []
    monkeypatch.setattr(dreaming, "dispatch_isolated_dream",
                        lambda agent, text: dreams.append((agent["session"], text)) or True)
    handles = {"live": []}
    monkeypatch.setattr(backends, "active_handles", lambda backend, agent_id: handles["live"])
    compacting = {"on": False}
    monkeypatch.setattr(compaction, "is_compacting", lambda session: compacting["on"])

    assert adapters.dream("ghost", "dream") is False
    agent = _agent()
    handles["live"] = ["h"]
    assert adapters.dream("mike", "dream") is False
    handles["live"] = []
    compacting["on"] = True
    assert adapters.dream("mike", "dream") is False
    compacting["on"] = False
    _busy(agent)
    assert adapters.dream("mike", "dream") is False
    agents_db.record_state(agent["agent_id"], "idle", {"source": "test"})
    assert adapters.dream("mike", "dream") is True
    assert dreams == [("mike", "dream")]
    assert adapters.journal == []  # dreams never go through TurnDispatchService


def test_janitor_run_queues_behind_busy_janitor_and_tags_the_run(adapters):
    adapters.service.result = SimpleNamespace(queued=True)
    run = {"session": "sam", "trace_id": "janitor-run-1", "run_id": "run-1"}
    assert adapters.janitor_run(run, "clean up") == {"ok": True, "queued": True}
    (kwargs,) = adapters.journal
    _silent_forced(kwargs, "sam")
    assert kwargs["origin"] == "janitor"
    assert kwargs["trace_id"] == kwargs["client_msg_id"] == "janitor-run-1"
    assert kwargs["queue_if_busy"] is True
    assert kwargs["janitor_run_id"] == "run-1"


def test_janitor_run_refuses_without_runtime_support(adapters):
    adapters.ctx.runtime_client = SimpleNamespace(status=lambda: {"capabilities": {}})
    with pytest.raises(RuntimeError):
        adapters.janitor_run({"session": "sam", "trace_id": "t", "run_id": "r"}, "x")
    assert adapters.journal == []


def test_decision_queue_policy():
    assert decision_queues_if_busy({"choice": "accepted"}) is False
    assert decision_queues_if_busy({"choice": "rejected"}) is False
    assert decision_queues_if_busy({"choice": "dismissed"}) is True
    assert decision_queues_if_busy({"choice": "accepted", "response_type": "single_choice"}) is True


def test_decision_rows_deliver_forms_as_user_and_decisions_as_automation(adapters, monkeypatch):
    marked = []
    monkeypatch.setattr(html_forms, "pending", lambda: [
        {"submission_id": "form-1", "session": "mike", "prompt": "form filled"}])
    monkeypatch.setattr(html_forms, "mark_delivered", lambda sid: marked.append(("form", sid)))
    monkeypatch.setattr(artifacts, "pending_deliveries", lambda: [
        {"decision_id": "d-1", "session": "rachel", "choice": "accepted"},
        {"decision_id": "d-2", "session": "rachel", "choice": "expired"}])
    monkeypatch.setattr(artifacts, "format_delivery_prompt", lambda pending: "answer " + pending["decision_id"])
    monkeypatch.setattr(artifacts, "mark_delivered", lambda did: marked.append(("decision", did)))

    adapters.deliver_decision_rows()

    form, first, second = adapters.journal
    _silent_forced(form, "mike")
    assert form["origin"] == "user"
    assert form["client_msg_id"] == "html-form-form-1"
    assert form["queue_if_busy"] is True
    _silent_forced(first, "rachel")
    assert first["origin"] == "automation"
    assert first["client_msg_id"] == "decision-d-1"
    assert first["queue_if_busy"] is False
    assert second["queue_if_busy"] is True
    assert marked == [("form", "form-1"), ("decision", "d-1"), ("decision", "d-2")]


def test_delivery_failures_are_logged_and_leave_rows_pending(adapters, monkeypatch):
    logged = []
    monkeypatch.setattr(adapters_module, "log_exception",
                        lambda event, exc, detail="": logged.append((event, detail)))
    monkeypatch.setattr(artifacts, "pending_deliveries", lambda: [
        {"decision_id": "d-1", "session": "rachel", "choice": "accepted"}])
    monkeypatch.setattr(artifacts, "format_delivery_prompt", lambda pending: "x")
    monkeypatch.setattr(artifacts, "mark_delivered", lambda did: pytest.fail("marked a failed delivery"))
    monkeypatch.setattr(html_forms, "mark_delivered", lambda sid: pytest.fail("marked a failed form"))
    adapters.service.result = RuntimeError("runtime down")

    assert adapters.deliver_pending_decisions() == set()
    adapters.deliver_html_form({"submission_id": "f-1", "session": "mike", "prompt": "p"})

    assert logged == [("decisionWakeFail", "d-1"), ("htmlFormDeliveryFail", "f-1")]


def test_recover_janitor_quota_prefers_the_runtime(adapters, monkeypatch):
    calls = []
    adapters.ctx.runtime_client = SimpleNamespace(
        recover_janitor_quota=lambda *args: calls.append(("runtime", args)) or "ok")
    assert adapters.recover_janitor_quota("codex", "o", 3, "a") == "ok"
    adapters.ctx.runtime_client = None
    from lib import janitor_autonomy
    monkeypatch.setattr(janitor_autonomy, "runtime_recover",
                        lambda *args: calls.append(("local", args)) or "local-ok")
    assert adapters.recover_janitor_quota("codex", "o", 3, "a") == "local-ok"
    assert calls == [("runtime", ("codex", "o", 3, "a")), ("local", ("codex", "o", 3, "a"))]


def test_janitor_after_tick_throttles_attention_reconcile(monkeypatch):
    from lib import janitor_attention
    from lib import audio_bookkeeper
    clock = {"now": 100.0}
    events = []
    drained = []
    monkeypatch.setattr(audio_bookkeeper, "drain", lambda: drained.append(True))
    monkeypatch.setattr(janitor_attention, "reconcile", lambda: True)
    ctx = SimpleNamespace(runtime_client=None, stream=SimpleNamespace(broadcast=events.append))
    adapters = DispatchAdapters(ctx, lambda ctx: None, monotonic=lambda: clock["now"])

    adapters.janitor_after_tick()
    adapters.janitor_after_tick()
    clock["now"] += 20.0
    adapters.janitor_after_tick()

    assert len(drained) == 3
    assert [event["kind"] for event in events] == ["janitor-attention", "janitor-attention"]
