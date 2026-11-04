"""RTO-07: a routed dispatch that fails transiently is retried by the client's
same-id resend and delivered once; a 4xx stays terminal with its own status.
Before, both became a final 500 receipt that every retry replayed."""
from __future__ import annotations

from types import SimpleNamespace

from lib import agents as agents_db
from lib import prompt_admissions, settings_store
from lib.db import conn
from lib.orchestrator import OrchestratorService
from lib.tts_engine import FakeTTSEngine
from lib.turn_dispatch import DispatchError


class _Stream:
    def broadcast(self, event):
        pass


def _ctx(tmp_path):
    settings_store.set_bool("orchestrator.enabled", True)
    settings_store.set_bool("orchestrator.fallback_only", False)
    tts = FakeTTSEngine(tmp_path / "audio")
    ctx = SimpleNamespace(default_session="mike", stream=_Stream(), tts=tts,
                          agents_path=tmp_path / "agents.json")
    ctx.speak_announcement = lambda text, voice_id, session=None: tts.synthesize(text, voice_id, session=session)
    return ctx


def _agents():
    agents_db.create_agent(persona="Mike", voice_id="mike-voice", cwd="/tmp", session="mike")
    agents_db.create_agent(persona="Antoni", voice_id="antoni-voice", cwd="/tmp", session="antoni")


def _model(calls):
    def model(_packet, _settings):
        calls.append(True)
        return {"kind": "agent_message", "target_session": "antoni", "confidence": 0.97,
                "addressing": True, "text_to_send": "please ship the settings screen",
                "reason": "Antoni owns the settings screen."}
    return model


def _send(service, dispatch, admission):
    # What the phone's outbox sends again: the same words, the same client id.
    return service.handle_send(
        text="Antoni, please ship the settings screen", requested_session="mike",
        trace_id="trace-hands-free", prompt_admission=admission, hands_free=True,
        synthesize_audio=True, dispatch=dispatch)


def _admission():
    return prompt_admissions.create(
        authenticated_at_admission=True, origin="user", sender_agent_id="", channel="voice",
        observed_at=1234, client_admission_id="u-phone-1", trace_id="trace-hands-free",
        original_text="Antoni, please ship the settings screen")


class _Dispatcher:
    """turn_dispatch as the orchestrator sees it: fails `failures` times, then
    delivers once and deduplicates every later same-id dispatch."""

    def __init__(self, failure):
        self.failure = failure
        self.attempts = []
        self.delivered = []

    def __call__(self, **kwargs):
        self.attempts.append(kwargs)
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        if kwargs["client_msg_id"] not in self.delivered:
            self.delivered.append(kwargs["client_msg_id"])
        return SimpleNamespace(session=kwargs["forced_session"], backend="claude")


def test_a_same_id_retry_after_a_503_delivers_exactly_once(tmp_path):
    _agents()
    models = []
    service = OrchestratorService(_ctx(tmp_path), model_call=_model(models))
    dispatch = _Dispatcher(DispatchError(503, "could not durably queue message"))
    admission = _admission()

    first = _send(service, dispatch, admission)
    assert (first.ok, first.status) == (False, 503), "the client is told to retry"
    asked = len(models)

    retry = _send(service, dispatch, admission)
    assert (retry.ok, retry.status, retry.action, retry.session) == (True, 200, "route", "antoni")
    assert dispatch.delivered == ["u-phone-1"]
    assert dispatch.attempts[1]["forced_session"] == "antoni"
    assert dispatch.attempts[1]["text"] == dispatch.attempts[0]["text"] == "please ship the settings screen"
    assert dispatch.attempts[1]["client_msg_id"] == "u-phone-1"

    again = _send(service, dispatch, admission)
    assert (again.ok, again.status) == (True, 200)
    assert dispatch.delivered == ["u-phone-1"], "exactly once"
    assert len(models) == asked, "the routing decision is not asked again"


def test_a_4xx_dispatch_failure_stays_terminal_with_its_own_status(tmp_path):
    _agents()
    models = []
    service = OrchestratorService(_ctx(tmp_path), model_call=_model(models))
    dispatch = _Dispatcher(DispatchError(413, "message too long for this agent"))
    admission = _admission()

    first = _send(service, dispatch, admission)
    assert (first.ok, first.status) == (False, 413)
    asked = len(models)
    retry = _send(service, dispatch, admission)
    assert (retry.ok, retry.status) == (False, 413)
    assert len(dispatch.attempts) == 1 and dispatch.delivered == []
    assert len(models) == asked


def test_routing_context_gets_the_retried_message_once(tmp_path):
    """Lead's amendment: every later same-id retry re-dispatches (turn_dispatch
    dedups it), but routing context never sees the message twice."""
    _agents()
    service = OrchestratorService(_ctx(tmp_path), model_call=_model([]))
    dispatch = _Dispatcher(DispatchError(503, "could not durably queue message"))
    admission = _admission()
    for _ in range(4):
        _send(service, dispatch, admission)
    rows = conn().execute(
        "SELECT text FROM agent_routing_messages WHERE session='antoni' AND role='user'").fetchall()
    assert [r["text"] for r in rows] == ["please ship the settings screen"]
