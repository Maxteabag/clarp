"""Requests between agents that come back to the requester.

``clarp-admin prompt`` is one-way by design: a recipient's answer stays in its
own chat (see agent_conversations). A request needs more: the requester must
get the result, hear about a real blocker such as an approval wait, and not
wait forever when nothing comes. This module adds that on top of the existing
records only:

* the request is an ordinary agent message whose stable client id
  (``peer-req-<id>``) is its correlation record: it says who asked whom;
* the requester's own goal can wait on it as a dependency
  (``peer:<id>``), so goal recovery wakes the requester with the result, or
  at the deadline, and honours its busy, paused and native-binding rules;
* replies are explicit (``clarp-admin reply``): nothing the recipient says in
  its own chat is relayed, and acknowledgements are not sent at all.

A result goes to a goal waiting on the request; with none, it is delivered as
a message under a stable id, so a duplicate is the same message. Progress and
blockers are messages; the goal keeps waiting for the result.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

from . import agents, task_plans
from .db import conn, now_ms

KINDS = ("result", "blocked", "progress")
MAX_UPDATES = 5  # blocked/progress messages per request: inform, never flood
MAX_DEADLINE_S = 30 * 86400
DEFAULT_DEADLINE_S = 6 * 3600
MAX_EVIDENCE = 8000
_ID = re.compile(r"r[0-9a-f]{12}\Z")


class RequestError(ValueError):
    """Refused before anything was stored or sent; the message says why."""


def new_id() -> str:
    return "r" + secrets.token_hex(6)


def request_client_id(request_id: str) -> str:
    return f"peer-req-{request_id}"


def dependency_key(request_id: str) -> str:
    return f"peer:{request_id}"


def _agent(session: str, role: str) -> dict:
    agent = agents.get_by_session(session) if session else None
    if not agent or agent.get("deleted_at"):
        raise RequestError(f"no such {role} session: {session or '(empty)'}")
    if agent.get("archived_at"):
        raise RequestError(f"{agent['persona']} ({session}) is archived")
    return agent


def request_text(request_id: str, requester: dict, text: str) -> str:
    who = f"{requester['persona']} ({requester['session']})"
    return (f"[Request {request_id} from {who}. Your answers in your own chat do not "
            f"reach {requester['persona']}. When you have the result, or are blocked "
            f"(for example waiting for an approval), say so with: clarp-admin reply "
            f"--request {request_id} --from <your session> --kind result|blocked|progress "
            f"--text '...'. No acknowledgement is needed.]\n\n{text}")


def check_deadline(deadline_s: int) -> int:
    if not 1 <= int(deadline_s) <= MAX_DEADLINE_S:
        raise RequestError("deadline must be 1 to 2592000 seconds")
    return int(deadline_s)


def wait_on(requester: dict, plan_id: str, request_id: str, recipient: dict,
            *, deadline_s: int = DEFAULT_DEADLINE_S) -> dict:
    """Make the requester's own goal wait for this request's result.

    Refuses (RequestError) when the goal is not the requester's, not active
    and enrolled for recovery, or already waits on a dependency; another
    dependency is never replaced. A scheduled timer is replaced by this wait,
    whose deadline then wakes the requester instead.
    """
    check_deadline(deadline_s)
    plan = task_plans.get(plan_id)
    if not plan:
        raise RequestError(f"no such goal: {plan_id}")
    goal = plan.get("goal") or {}
    if plan.get("agent_id") != requester["agent_id"]:
        raise RequestError(f"goal {plan_id} belongs to {plan.get('session')}, not {requester['session']}")
    if not goal or plan.get("status") != "active" or not plan.get("recovery_enabled"):
        raise RequestError(f"goal {plan_id} is {plan.get('status')} and "
                           f"{'enrolled' if plan.get('recovery_enabled') else 'not enrolled'} for recovery")
    cont = goal.get("continuation") or {}
    if cont.get("state") in ("attention", "blocked", "not_enrolled"):
        raise RequestError(f"goal recovery is {cont['state']}: {cont.get('reason') or ''}")
    if cont.get("state") == "waiting" and cont.get("dependency_key") != dependency_key(request_id):
        raise RequestError("goal already waits on "
                           + str(cont.get("dependency_key") or cont.get("job_handle") or "another dependency"))
    point = goal.get("checkpoint") or {}
    read = (f"When woken by request {request_id} to {recipient['persona']}: read the "
            f"dependency result (clarp-goal get {plan_id}); it is their answer, or the deadline.")
    own = [p for p in (point.get("next_work") or "").split("\n\n")
           if p and not p.startswith("When woken by request ")]
    try:
        return _arm(plan, requester, request_id, recipient, deadline_s, point, own, read)
    except ValueError as error:  # the goal changed under us: nothing is sent
        raise RequestError(f"goal {plan_id} changed while arming the wait ({error}); try again") from error


def _arm(plan, requester, request_id, recipient, deadline_s, point, own, read):
    from . import goal_ledger, turn_identity
    plan_id = plan["plan_id"]
    caller = turn_identity.caller_agent_id()
    with goal_ledger.acting_as("owner", requester["agent_id"],
                               verified=caller == requester["agent_id"]):
        return _arm_wait(plan, plan_id, request_id, recipient, deadline_s, point, own, read)


def _arm_wait(plan, plan_id, request_id, recipient, deadline_s, point, own, read):
    return task_plans._goal_mutate(plan_id, revision=plan["revision"], action="checkpoint", data={
        "progress": point.get("progress") or f"Asked {recipient['persona']} (request {request_id})",
        "next_work": "\n\n".join(own + [read]),
        "continuation": {"kind": "dependency", "key": dependency_key(request_id),
                         "reason": f"Waiting for {recipient['persona']} to answer request {request_id}",
                         "due_at": now_ms() + int(deadline_s) * 1000}})


NOT_DELIVERED = "was not delivered"


def abandon_wait(plan_id: str, request_id: str, error: str) -> bool:
    """The Host refused the request: settle the wait truthfully, not silently.

    Only for a definite refusal; after a timeout or a server error the request
    may have arrived, and the wait (with its deadline) must stay.
    """
    for _ in range(3):
        plan = task_plans.get(plan_id)
        cont = ((plan or {}).get("goal") or {}).get("continuation") or {}
        if not (cont.get("state") == "waiting" and cont.get("dependency_key") == dependency_key(request_id)):
            return False
        try:
            from . import goal_ledger, turn_identity
            with goal_ledger.acting_as("owner", plan["agent_id"],
                                       verified=turn_identity.caller_agent_id() == plan["agent_id"]):
                task_plans._goal_mutate(plan_id, revision=plan["revision"], action="dependency", data={
                    "key": dependency_key(request_id), "outcome": "failed",
                    "evidence": f"request {request_id} {NOT_DELIVERED}: {error}"[:MAX_EVIDENCE]})
            return True
        except ValueError:
            continue
    return False


def find_request(replier: dict, request_id: str) -> dict:
    """The requester of ``request_id``, which must have been sent to ``replier``."""
    if not _ID.fullmatch(request_id or ""):
        raise RequestError(f"not a request id: {request_id}")
    row = conn().execute(
        "SELECT sender_agent_id FROM messages WHERE agent_id=? AND message_id=? AND role='user'",
        (replier["agent_id"], "u-" + request_client_id(request_id))).fetchone()
    if not row or not row["sender_agent_id"]:
        raise RequestError(f"request {request_id} was not sent to {replier['session']}")
    requester = agents.get_by_agent_id(row["sender_agent_id"])
    if not requester or requester.get("deleted_at"):
        raise RequestError(f"the agent that sent request {request_id} no longer exists")
    if requester.get("archived_at"):
        raise RequestError(f"{requester['persona']}, who sent request {request_id}, is archived")
    from . import goal_ledger
    if goal_ledger.is_delegate_of(replier["agent_id"], requester["agent_id"]):
        # Bookkeeping records beside the principal's goal; it never answers into it.
        raise RequestError("a bookkeeping delegate does not reply into its principal's goal")
    return requester


def waiting_goal(requester: dict, request_id: str) -> dict | None:
    """The requester's goal that names this request, whatever its state."""
    row = conn().execute(
        """SELECT plan_id FROM task_plans WHERE agent_id=? AND status IN ('active','paused','blocked')
             AND json_extract(goal_json,'$.continuation.dependency_key')=?
           ORDER BY updated_at DESC LIMIT 1""",
        (requester["agent_id"], dependency_key(request_id))).fetchone()
    return task_plans.get(row["plan_id"]) if row else None


def _holds(cont: dict) -> bool:
    """Exactly when the goal layer will take a result (task_plans dependency)."""
    if cont.get("state") == "waiting":
        return True
    return (cont.get("state") in ("paused", "blocked") and "paused_dependency_due_at" in cont
            and not cont.get("dependency_result"))


def reply_text(request_id: str, replier: dict, kind: str, text: str) -> str:
    label = {"result": "result", "blocked": "blocked", "progress": "progress"}[kind]
    note = ("" if kind == "result" else
            " The request is still open; its result will follow. No reply is needed.")
    return (f"[Reply to request {request_id} from {replier['persona']} "
            f"({replier['session']}): {label}.{note}]\n\n{text}")


def plan_reply(replier: dict, request_id: str, kind: str, text: str) -> dict[str, Any]:
    """Decide how this reply reaches the requester. Pure apart from reads.

    Returns ``{"action": "dependency", plan, data}`` for a result a goal still
    waits on, ``{"action": "delivered", plan}`` for a result already
    recorded there, else ``{"action": "message", payload}``.
    """
    if kind not in KINDS:
        raise RequestError(f"kind must be one of {', '.join(KINDS)}")
    if not text.strip():
        raise RequestError("a reply needs text")
    requester = find_request(replier, request_id)
    plan = waiting_goal(requester, request_id)
    cont = (plan or {}).get("goal", {}).get("continuation") or {}
    recorded = cont.get("dependency_result") or {}
    if kind == "result" and plan:
        # Only a result that came from a reply is this reply again; a
        # "not delivered" record or a deadline is not an answer.
        if (recorded.get("key") == dependency_key(request_id) and recorded.get("outcome") == "succeeded"
                and str(recorded.get("evidence", "")).startswith(f"[Reply to request {request_id} ")):
            return {"action": "delivered", "plan": plan, "requester": requester}
        # Waiting, or held for the resume of a user pause or block: the goal
        # takes it. A goal that already moved on (its deadline woke the
        # requester, say) cannot; then the result comes as a message rather
        # than being lost, even if the goal was paused since.
        if _holds(cont):
            return {"action": "dependency", "plan": plan, "requester": requester, "data": {
                "key": dependency_key(request_id), "outcome": "succeeded",
                "evidence": reply_text(request_id, replier, kind, text)[:MAX_EVIDENCE]}}
    if kind != "result" and plan and cont.get("state") in ("paused", "blocked"):
        raise RequestError(f"{requester['persona']}'s goal is {cont['state']} by the user; only the result "
                           "is kept for the resume, so send it with --kind result when you have it")
    if kind != "result" and not _already_sent(requester, plan_message(
            replier, request_id, kind, text, requester=requester)["client_msg_id"]):
        sent = conn().execute(
            "SELECT count(*) FROM messages WHERE agent_id=? AND message_id LIKE ?",
            (requester["agent_id"], f"u-peer-%-{request_id}-%")).fetchone()[0]
        if sent >= MAX_UPDATES:
            raise RequestError(f"{MAX_UPDATES} updates already went to {requester['persona']} for request "
                               f"{request_id}; send the result, or one when something changes materially")
    return {"action": "message", "requester": requester,
            "payload": plan_message(replier, request_id, kind, text, requester=requester)}


def _already_sent(requester: dict, client_id: str) -> bool:
    """A retry of an update that did arrive is the same message, not a new one."""
    return conn().execute("SELECT 1 FROM messages WHERE agent_id=? AND message_id=?",
                          (requester["agent_id"], "u-" + client_id)).fetchone() is not None


def plan_message(replier: dict, request_id: str, kind: str, text: str, *,
                 requester: dict | None = None) -> dict:
    """The reply as a message to the requester, under a stable client id: a
    retried or duplicated reply is the same message."""
    requester = requester or find_request(replier, request_id)
    digest = hashlib.sha256(f"{kind}\0{text}".encode()).hexdigest()[:12]
    client_id = f"peer-res-{request_id}" if kind == "result" else f"peer-{kind}-{request_id}-{digest}"
    return {"session": requester["session"], "text": reply_text(request_id, replier, kind, text),
            "force_session": True, "synthesize_audio": False, "hands_free": False,
            "origin": "agent", "sender": replier["session"], "client_msg_id": client_id}


def record_result(plan: dict, data: dict, *, replier: dict | None = None) -> dict:
    from . import goal_ledger, turn_identity
    replier_id = (replier or {}).get("agent_id", "")
    caller = turn_identity.caller_agent_id()
    with goal_ledger.acting_as("peer", replier_id, caller=caller,
                               verified=bool(replier_id) and caller == replier_id):
        result = task_plans._goal_mutate(plan["plan_id"], revision=plan["revision"],
                                         action="dependency", data=data)
    if replier:
        # A helper answering its parent reports, as a message to it would.
        from . import helper_agents
        helper_agents.note_agent_message(None, sender_agent_id=replier["agent_id"],
                                         target_agent_id=plan["agent_id"])
    return result


