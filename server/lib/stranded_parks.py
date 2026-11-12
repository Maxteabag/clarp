"""Tell the owner about a turn that waits for an account in vain.

An agent whose state says it waits for an account is resumed by the runtime's
account coordinator, which lists it under ``<pool>_account_recovery.waiting``
in its status. On 12 November 2026 a coordinator dropped a parked turn while
the agent kept showing the wait: nothing would ever resume it, and it was
found by accident four hours later.

This runs in the Host, on the agent scheduler's tick. Once a minute it reads
which agents say they wait; if any does, it asks the runtime who it holds. An
agent that waits while no coordinator lists it, at every check for five
minutes, is logged and gets one failed document in its own session, which
Updates shows as blocking. The alert is withdrawn when the turn leaves the
wait or the runtime lists it again, and opened again if it is then dropped
again; one the owner dismissed stays dismissed. It never dispatches, stops or
repairs a turn: Stop is the owner's.

Nothing is judged blind. A status that cannot be read raises no alarm and
starts the five minutes again, so a runtime that is restarting or handing
over is not mistaken for one that dropped a turn.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from . import agents, artifacts, backends, db
from .log import log, log_exception
from .voice_markup import strip_hidden_blocks

KIND = "stranded_park"
PREFIX = "stranded-park:"
AFTER_MS = 5 * 60_000
EVERY_MS = 60_000
_UNAVAILABLE_LOG_MS = 3_600_000
_CLOSED = {"completed", "cancelled", "expired"}


def runtime_status() -> dict[str, Any]:
    """The account coordinators' status: the runtime's, and this process's
    own. A Host that runs its own turns has only its own; beside a runtime
    its own hold nothing, and are read all the same so that a turn this
    process parked itself is never taken for a dropped one."""
    from . import turn_dispatch_state
    own = turn_dispatch_state.runtime_status()
    if backends._RUNTIME_CLIENT is None:
        return own
    status = dict(backends.runtime_status())
    status.update({f"host_{key}": value for key, value in own.items()
                   if key.endswith("_account_recovery")})
    return status


def _reference(agent_id: str, trace_id: str) -> str:
    return f"{PREFIX}{agent_id}:{trace_id}"


def _parked() -> dict[str, dict[str, Any]]:
    """reference -> the agents whose latest state is a wait for an account."""
    parked = {}
    for agent in agents.list_agents():
        state = agents.latest_state(agent["agent_id"]) or {}
        detail = state.get("detail") if isinstance(state.get("detail"), dict) else {}
        if detail.get("account_recovery") != "waiting":
            continue
        trace = str(detail.get("trace_id") or state.get("ts") or "")
        parked[_reference(agent["agent_id"], trace)] = {
            "agent_id": agent["agent_id"], "session": agent["session"],
            "name": strip_hidden_blocks(str(agent["persona"] or agent["session"]))[:80],
            "trace_id": trace, "parked_at": int(state.get("ts") or 0)}
    return parked


def _open_alerts() -> list[dict[str, Any]]:
    return [dict(row) for row in db.conn().execute(
        """SELECT artifact_id,reference_id FROM artifacts
            WHERE type='document' AND status='failed' AND reference_id LIKE ?
              AND deleted_at IS NULL AND json_valid(payload_json)
              AND json_extract(payload_json,'$.attention_kind')=?""",
        (PREFIX + "%", KIND))]


def _listed(status: dict[str, Any]) -> set[str]:
    """Every agent a coordinator holds parked, whichever pool."""
    return {str(agent) for key, value in status.items()
            if key.endswith("_account_recovery") and isinstance(value, dict)
            for agent in value.get("waiting") or ()}


class StrandedParks:
    def __init__(self, status: Callable[[], dict[str, Any]] = runtime_status):
        self._status = status
        self._due = 0
        self._absent_since: dict[str, int] = {}
        self._logged: set[str] = set()
        self._unavailable_logged = -_UNAVAILABLE_LOG_MS

    def tick(self, now: int) -> int:
        """Alerts raised or withdrawn by this check."""
        if now < self._due:
            return 0
        self._due = now + EVERY_MS
        parked = _parked()
        absent: dict[str, dict[str, Any]] = {}
        if parked:
            try:
                listed = _listed(self._status())
            except Exception as exc:  # noqa: BLE001 - any failed read means unknown
                self._absent_since.clear()
                if now - self._unavailable_logged >= _UNAVAILABLE_LOG_MS:
                    self._unavailable_logged = now
                    log_exception("strandedParkStatusUnavailable", exc, f"parked={len(parked)}")
                return 0
            absent = {reference: park for reference, park in parked.items()
                      if park["agent_id"] not in listed}
        self._absent_since = {reference: self._absent_since.get(reference, now)
                              for reference in absent}
        self._logged &= set(absent)
        changed = 0
        for alert in _open_alerts():
            if alert["reference_id"] not in absent:
                artifacts.update(alert["artifact_id"], {
                    "status": "cancelled",
                    "summary": "The turn ended or the runtime holds it again."})
                changed += 1
        for reference, park in absent.items():
            if now - self._absent_since[reference] >= AFTER_MS:
                changed += self._report(reference, park, now)
        return changed

    def _report(self, reference: str, park: dict[str, Any], now: int) -> int:
        minutes = max(0, now - park["parked_at"]) // 60_000 if park["parked_at"] else 0
        if reference not in self._logged:
            self._logged.add(reference)
            log("strandedPark",
                f"agent={park['agent_id']} session={park['session']} trace={park['trace_id']} "
                f"parked_min={minutes} unlisted_min={(now - self._absent_since[reference]) // 60_000}")
        artifact_id = "stranded-park-" + hashlib.sha256(reference.encode()).hexdigest()[:40]
        summary = ("This turn waits for an account, but the runtime no longer holds it, "
                   "so it will not continue on its own. Stop the agent and send the message again.")
        # The raw row: an alert the owner dismissed keeps this same turn from
        # being reported again, and one that is open is not written twice.
        # One this check withdrew (the runtime held the turn again for a
        # while) is opened again.
        existing = db.conn().execute(
            "SELECT status,archived_at,deleted_at FROM artifacts WHERE artifact_id=?",
            (artifact_id,)).fetchone()
        if existing is not None:
            if (existing["status"] != "cancelled" or existing["archived_at"] is not None
                    or existing["deleted_at"] is not None):
                return 0
            artifacts.update(artifact_id, {"status": "failed", "summary": summary})
            return 1
        diagnostic = (f"The agent has shown the wait for {minutes} minutes and no account "
                      f"coordinator lists it. agent={park['agent_id']} trace={park['trace_id']}")
        artifacts.create(
            session=park["session"], type="document", reference_id=reference,
            artifact_id=artifact_id, title=f"{park['name']} is stuck waiting for an account",
            summary=summary, status="failed",
            payload={"attention_kind": KIND, "trace_id": park["trace_id"],
                     "parked_at": park["parked_at"],
                     "content": f"{summary}\n\n{diagnostic}", "diagnostic": diagnostic,
                     "suggested_action": "stop_and_resend"})
        return 1
