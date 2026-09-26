"""Callbacks the background schedulers use to start turns.

Every autonomous loop (team leader, heartbeat, dreaming, agent schedules,
janitors, decision delivery) ends up in `TurnDispatchService(ctx).dispatch`.
Before this module each of them carried its own closure in `build_server`
with the same three facts repeated: the turn targets one forced session, it
is silent (`synthesize_audio=False`) and it declares an `origin` so the
conversation read model can file it. This class states those once.

The service is constructed through `service_factory` on every call rather
than cached: `server.py` passes `lambda ctx: TurnDispatchService(ctx)`, so a
test that monkeypatches `server.TurnDispatchService` still intercepts every
scheduler dispatch.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import agents as agents_db
from . import backends
from . import trace as _trace
from .log import log_exception
from .protocol import SSEType
from .timing import SERVER_TIMING

# origin per scheduler; the read model files hidden traffic by these names.
ORIGIN_LEADER_TICK = "leader_tick"
ORIGIN_HEARTBEAT = "heartbeat"
ORIGIN_AUTOMATION = "automation"
ORIGIN_JANITOR = "janitor"
ORIGIN_USER = "user"


def decision_queues_if_busy(pending: dict) -> bool:
    """Administrative notices and question answers must preserve ongoing work.

    Legacy accepted/rejected approval replies retain their existing admission
    behavior. All question replies and non-answer notices enter the durable
    queue when the originating agent is busy; dismissing an inbox item must
    never interrupt an unrelated in-flight turn.
    """
    return (pending.get("response_type") == "single_choice"
            or pending.get("choice") not in {"accepted", "rejected"})


class DispatchAdapters:
    def __init__(self, ctx, service_factory: Callable[[Any], Any], *,
                 new_trace_id: Callable[[], str] = _trace.new_trace_id,
                 monotonic: Callable[[], float] = time.monotonic):
        self.ctx = ctx
        self._service = service_factory
        self._new_trace_id = new_trace_id
        self._monotonic = monotonic
        self._attention_due = 0.0

    # -- the one dispatch shape every scheduler shares -----------------------

    def _dispatch(self, session: str, text: str, *, origin: str,
                  trace_id: str = "", client_msg_id: str = "",
                  queue_if_busy: bool = False, **extra):
        """A silent turn forced onto `session`. Schedulers never route."""
        from .turn_dispatch import DispatchCommand
        return self._service(self.ctx).submit(DispatchCommand(
            text=text, requested_session=session, forced_session=session,
            trace_id=trace_id or self._new_trace_id(),
            client_msg_id=client_msg_id, synthesize_audio=False,
            origin=origin, queue_if_busy=queue_if_busy, **extra))

    @staticmethod
    def _idle_agent(session: str) -> dict | None:
        """The agent bound to `session` if it exists and has no turn running."""
        agent = agents_db.get_by_session(session)
        if not agent or agents_db.is_busy(agent["agent_id"]):
            return None
        return agent

    # -- scheduler callbacks --------------------------------------------------

    def leader_tick(self, session: str, text: str) -> None:
        """TeamLeaderScheduler: nudge an idle team leader."""
        self._dispatch(session, text, origin=ORIGIN_LEADER_TICK)

    def heartbeat(self, session: str, text: str) -> None:
        """HeartbeatScheduler: hidden heartbeat prompt, idle agents only."""
        if self._idle_agent(session) is None:
            return
        self._dispatch(session, text, origin=ORIGIN_HEARTBEAT)

    def decided_heartbeat(self, session: str, text: str, request_id: str) -> bool:
        """AutonomyJanitors: a decided wake-up keyed by its request id, never
        queued behind a busy agent. True when a turn was accepted."""
        result = self._dispatch(
            session, text, origin=ORIGIN_HEARTBEAT, trace_id=request_id,
            client_msg_id=request_id, queue_if_busy=False)
        return result is not None

    def dream(self, session: str, text: str) -> bool:
        """DreamingScheduler: run one isolated dream round if the agent is
        idle everywhere (no turn, no backend handle, no compaction)."""
        from . import dreaming, turn_dispatch
        agent = self._idle_agent(session)
        if (agent is None
                or backends.active_handles(agent.get("backend"), agent["agent_id"])
                or turn_dispatch.live_work(agent["agent_id"], session=session).compacting):
            return False
        return dreaming.dispatch_isolated_dream(agent, text)

    def scheduled_job(self, session: str, text: str) -> None:
        """AgentScheduleRunner: a due schedule for an agent that still exists."""
        if not agents_db.get_by_session(session):
            return
        self._dispatch(session, text, origin=ORIGIN_AUTOMATION)

    def janitor_run(self, run: dict, prompt: str) -> dict:
        """JanitorRunner: an admitted maintenance run, queued behind the
        janitor's own busy turn rather than dropped."""
        from .janitor_http import runtime_available
        if not runtime_available(self.ctx):
            raise RuntimeError("Janitor runtime support is unavailable")
        result = self._dispatch(
            run["session"], prompt, origin=ORIGIN_JANITOR,
            trace_id=run["trace_id"], client_msg_id=run["trace_id"],
            queue_if_busy=True, janitor_run_id=run["run_id"])
        return {"ok": True, "queued": result.queued}

    def janitor_after_tick(self) -> None:
        """JanitorRunner: housekeeping after every admission tick."""
        from . import janitor_attention
        from .audio_bookkeeper import drain as drain_audio_bookkeeping
        drain_audio_bookkeeping()
        # Failure alerts move on the scale of runs (minutes); scanning
        # janitor_runs on every 2 s admission tick was measurable idle CPU.
        now = self._monotonic()
        if now < self._attention_due:
            return
        self._attention_due = now + SERVER_TIMING.janitor_attention_interval_sec
        if janitor_attention.reconcile():
            self.ctx.stream.broadcast({"type": SSEType.AGENT_ROSTER,
                                       "kind": "janitor-attention"})

    def recover_janitor_quota(self, provider, owner_id, generation, approval_id):
        """AutonomyJanitors: quota recovery runs wherever dispatch runs."""
        runtime = getattr(self.ctx, "runtime_client", None)
        if runtime is not None:
            return runtime.recover_janitor_quota(provider, owner_id, generation, approval_id)
        from .janitor_autonomy import runtime_recover
        return runtime_recover(provider, owner_id, generation, approval_id)

    # -- decision delivery ----------------------------------------------------

    def deliver_html_form(self, submission: dict) -> None:
        from . import html_forms
        try:
            self._dispatch(
                submission["session"], submission["prompt"], origin=ORIGIN_USER,
                client_msg_id="html-form-" + submission["submission_id"],
                queue_if_busy=True)
            html_forms.mark_delivered(submission["submission_id"])
        except Exception as exc:
            log_exception("htmlFormDeliveryFail", exc, detail=submission["submission_id"])

    def deliver_decision(self, pending: dict) -> bool:
        """Wake the agent with one answered/expired decision. True when the
        row was marked delivered."""
        from . import artifacts
        decision_id = pending["decision_id"]
        text = artifacts.format_delivery_prompt(pending)
        try:
            self._dispatch(
                pending["session"], text, origin=ORIGIN_AUTOMATION,
                client_msg_id=f"decision-{decision_id}",
                queue_if_busy=decision_queues_if_busy(pending))
            artifacts.mark_delivered(decision_id)
            return True
        except Exception as exc:
            log_exception("decisionWakeFail", exc, detail=decision_id)
            return False

    def deliver_pending_decisions(self) -> set[str]:
        from . import artifacts
        return {pending["decision_id"] for pending in artifacts.pending_deliveries()
                if self.deliver_decision(pending)}

    def deliver_decision_rows(self) -> None:
        """One pass of the decision-delivery worker: HTML forms, then decisions."""
        from . import html_forms
        for submission in html_forms.pending():
            self.deliver_html_form(submission)
        self.deliver_pending_decisions()
