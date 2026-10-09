"""Group calls: the Host-owned record of a multi-agent hands-free call.

The binding contract is docs/group-calls.md. A group call is Car Mode's
multi-agent conversation with the call UI: the phone captures and plays, and
the Host keeps who is in the call, who is on hold and who has the floor, so
Clarp agents can manage the call for the user (``clarp-admin call``).

Nothing here reads what the user said. The record changes only through an
explicit request (the app's buttons, or an agent's CLI call) and through
routing, which moves the floor to whoever an utterance was sent to.

One live call per principal, stored in ``settings_store`` so a call survives
an HTTP restart with no schema change.
"""
from __future__ import annotations

import difflib
import json
import threading
import time
import uuid
from typing import Any

from . import events, settings_store
from .log import log, log_exception

KEY = "group_call."
ADMINISTRATOR = "administrator"
MAX_PARTICIPANTS = 8
KEEP_ENDED = 20
STALE_MS = 12 * 60 * 60 * 1000
ACTIONS = frozenset({"add", "remove", "hold", "resume", "switch", "transfer"})
# Participant states that take speech and may hold the floor.
REACHABLE = frozenset({"active", "invited"})
# Spoken-name matching (docs/group-calls.md "Names").
MIN_PREFIX = 3
MIN_SIMILARITY = 0.75

_lock = threading.RLock()
_context: dict[str, Any] = {"stream": None}


class CallError(Exception):
    """A request the caller must hear about: HTTP status plus a short message."""

    def __init__(self, status: int, error: str, **extra: Any):
        super().__init__(error)
        self.status, self.error, self.extra = status, error, extra

    def body(self) -> dict:
        return {"ok": False, "error": self.error, **self.extra}


def now_ms() -> int:
    return int(time.time() * 1000)


def bind(ctx) -> None:
    """Remember where events go; called once the server context exists."""
    _context["stream"] = getattr(ctx, "stream", None)


def _host_id() -> str:
    from .server_identity import server_id
    return server_id()


# ---- persistence ---------------------------------------------------------

def _principals() -> list[str]:
    return [key[len(KEY):] for key in settings_store.keys_with_prefix(KEY)]


def _load(principal: str) -> dict:
    raw = settings_store.get(KEY + principal)
    data = json.loads(raw) if raw else {"calls": []}
    live = _live(data)
    if live is not None and now_ms() - live["_changed_at"] > STALE_MS:
        _finish(live, "stale")
        _commit(principal, data, live, {"action": "end", "session": "", "by": ""}, quiet_notices=True)
    return data


def _save(principal: str, data: dict) -> None:
    ended = [c for c in data["calls"] if c["state"] != "live"]
    drop = {c["call_id"] for c in ended[:-KEEP_ENDED]} if len(ended) > KEEP_ENDED else set()
    data["calls"] = [c for c in data["calls"] if c["call_id"] not in drop]
    settings_store.set_text(KEY + principal, json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _live(data: dict) -> dict | None:
    return next((c for c in reversed(data["calls"]) if c["state"] == "live"), None)


def public(call: dict) -> dict:
    out = {key: value for key, value in call.items() if not key.startswith("_")}
    out["participants"] = [{k: v for k, v in p.items() if not k.startswith("_")}
                           for p in call["participants"]]
    out["server_now"] = now_ms()
    return out


def get_call(call_id: str) -> dict | None:
    """Any call, live or ended, by id (for diagnostics and tests)."""
    with _lock:
        for principal in _principals():
            for call in _load(principal)["calls"]:
                if call["call_id"] == call_id:
                    return public(call)
    return None


# ---- names -----------------------------------------------------------------

def _roster() -> list[dict]:
    """Agents that may join a call: live contacts that can be voice targets."""
    from . import agents as agents_db
    from . import oracle_contact
    return [a for a in oracle_contact.roster()
            if agents_db.interaction_capabilities(a)["can_voice_target"]]


def _letters(value: str) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _stem(session: str) -> str:
    """``mike`` for ``mike-86db``: the spoken part of a session id."""
    head, _, tail = str(session).rpartition("-")
    return _letters(head if head and tail.isalnum() and len(tail) <= 8 else session)


def _score(spoken: str, agent: dict) -> int:
    """3 exact, 2 prefix, 1 similar, 0 no match (larger is better)."""
    best = 0
    for name in {_letters(agent["persona"]), _stem(agent["session"])} - {""}:
        if spoken == name:
            return 3
        if len(spoken) >= MIN_PREFIX and (name.startswith(spoken) or spoken.startswith(name)):
            best = max(best, 2)
        elif difflib.SequenceMatcher(None, spoken, name).ratio() >= MIN_SIMILARITY:
            best = max(best, 1)
    return best


def _brief(agent: dict) -> dict:
    return {"session": str(agent["session"]), "persona": str(agent["persona"])}


def resolve_name(name: str, participants: list[str] | tuple[str, ...] = ()) -> dict:
    """The one agent a typed or spoken ``name`` means.

    Exact session ids and personas first (as ``oracle_contact.resolve_visible``),
    then a spoken approximation. Participants of the call win a tie with the
    rest of the roster. Raises CallError 404 unknown / 409 ambiguous.
    """
    from . import oracle_contact
    wanted = str(name or "").strip()
    roster = _roster()
    reachable = {a["session"] for a in roster}
    try:
        agent = oracle_contact.resolve_visible(wanted)
        if agent["session"] in reachable:
            return agent
    except ValueError as exc:
        if str(exc) == "ambiguous agent":
            exact = [a for a in roster if str(a["persona"]).casefold() == wanted.casefold()
                     and str(a.get("role") or "") != "helper" and not a.get("parent_agent_id")]
            inside = [a for a in exact if a["session"] in participants]
            if len(inside) == 1:
                return inside[0]
            raise CallError(409, "ambiguous agent", candidates=[_brief(a) for a in exact]) from exc
    spoken = _letters(wanted)
    if not spoken:
        raise CallError(404, "unknown agent")
    contacts = [a for a in roster if str(a.get("role") or "") != "helper" and not a.get("parent_agent_id")]
    scored = [(_score(spoken, a), a) for a in contacts]
    top = max((score for score, _ in scored), default=0)
    found = [a for score, a in scored if score == top and score > 0]
    if len(found) > 1:
        inside = [a for a in found if a["session"] in participants]
        found = inside if len(inside) == 1 else found
    if not found:
        raise CallError(404, "unknown agent")
    if len(found) > 1:
        raise CallError(409, "ambiguous agent", candidates=[_brief(a) for a in found])
    return found[0]


# ---- record changes --------------------------------------------------------

def _participant(call: dict, session: str) -> dict | None:
    return next((p for p in call["participants"] if p["session"] == session), None)


def _set_state(call: dict, part: dict, state: str) -> bool:
    if part["state"] == state:
        return False
    part["state"], part["changed_at"] = state, now_ms()
    return True


def _give_floor(call: dict, session: str) -> bool:
    changed = False
    if call["floor"] != session:
        call["floor"] = session
        call["_floor_order"] = [s for s in call.get("_floor_order", []) if s != session] + [session]
        changed = True
    return changed


def _pass_floor(call: dict) -> None:
    """The floor holder can no longer take speech: hand the floor on."""
    floor = _participant(call, call["floor"]) if call["floor"] else None
    if floor is not None and floor["state"] in REACHABLE:
        return
    for session in reversed(call.get("_floor_order", [])):
        part = _participant(call, session)
        if part is not None and part["state"] in REACHABLE:
            call["floor"] = session
            return
    first = next((p for p in call["participants"] if p["state"] in REACHABLE), None)
    call["floor"] = first["session"] if first else ""
    if first:
        call["_floor_order"] = call.get("_floor_order", []) + [first["session"]]


def _finish(call: dict, reason: str) -> None:
    for part in call["participants"]:
        _set_state(call, part, "left")
    call["state"], call["reason"], call["floor"] = "ended", reason, ""
    call["ended_at"] = now_ms()


def _commit(principal: str, data: dict, call: dict, change: dict, *, quiet_notices: bool = False,
            notices: dict[str, str] | None = None) -> None:
    """One change: revision, persistence, event, agent-log notices."""
    call["revision"] += 1
    call["_changed_at"] = now_ms()
    _save(principal, data)
    log("groupCall", f"call={call['call_id']} action={change['action']} session={change['session'] or '-'} "
        f"by={change['by'] or '-'} state={call['state']} floor={call['floor'] or '-'} revision={call['revision']}")
    _emit(call, change)
    if not quiet_notices:
        _notify(call, notices or {})


def _emit(call: dict, change: dict) -> None:
    stream = _context["stream"]
    if stream is None:
        log("groupCallNoStream", f"call={call['call_id']} revision={call['revision']}")
        return
    wire = public(call)
    try:
        events.broadcast(stream, events.group_call(
            **{key: wire.get(key) for key in events.FIELDS[events.SSEType.GROUP_CALL] if key != "change"},
            change=dict(change)))
    except Exception as exc:  # noqa: BLE001 - the record is already saved
        log_exception("groupCallEmitFail", exc, f"call={call['call_id']}")


def _notify(call: dict, notices: dict[str, str]) -> None:
    """Quiet system rows in the chats of the agents a change concerns."""
    from . import agents as agents_db
    from . import message_store
    stream = _context["stream"]
    for session, text in notices.items():
        part = _participant(call, session)
        if part is None:
            continue
        try:
            backend_session_id = agents_db.live_backend_session(part["agent_id"])
            if not backend_session_id:
                continue
            row = message_store.record_call_notice(
                agent_id=part["agent_id"], backend_session_id=backend_session_id,
                notice_id=f"{call['call_id']}:{call['revision']}:{part['agent_id']}",
                text="Group call: " + text)
            if row is not None and stream is not None:
                events.broadcast(stream, events.transcript_updated(
                    agent_id=part["agent_id"], session=session, backend_session_id=backend_session_id))
        except Exception as exc:  # noqa: BLE001 - a notice never fails the change
            log_exception("groupCallNoticeFail", exc, f"call={call['call_id']} session={session}")


def _others(call: dict, *exclude: str) -> list[dict]:
    return [p for p in call["participants"] if p["session"] not in exclude and p["state"] != "left"]


def _names(parts: list[dict]) -> str:
    names = [p["persona"] for p in parts]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _broadcast_notice(call: dict, subject: str, own: str, others: str) -> dict[str, str]:
    """``own`` for the subject's chat, ``others`` for everyone else still in the call."""
    out = {p["session"]: others for p in _others(call, subject)}
    out[subject] = own
    return out


# ---- which call --------------------------------------------------------------

def _locate(caller: str, *, call_id: str = "", principal: str = "") -> tuple[str, dict, dict]:
    """(principal, data, live call) a change applies to, or CallError 404/409."""
    if caller != ADMINISTRATOR:
        candidates = [caller]
    elif principal:
        candidates = [principal]
    else:
        candidates = _principals()
    found = []
    for who in candidates:
        data = _load(who)
        live = _live(data)
        if live is not None and (not call_id or live["call_id"] == call_id):
            found.append((who, data, live))
    if not found:
        raise CallError(404, "no live call")
    if len(found) > 1:
        raise CallError(409, "several live calls")
    return found[0]


def _start_principal(caller: str, principal: str) -> str:
    if caller != ADMINISTRATOR:
        return caller
    if principal:
        return principal
    newest = (0, "")
    for who in _principals():
        for call in _load(who)["calls"]:
            newest = max(newest, (int(call.get("started_at") or 0), who))
    if not newest[1]:
        raise CallError(409, "principal required: no device has made a call yet")
    return newest[1]


# ---- operations ---------------------------------------------------------------

def snapshot(caller: str, *, principal: str = "") -> dict:
    """``{"call": live call or None}`` for the caller (or the administrator's pick)."""
    with _lock:
        try:
            _, _, live = _locate(caller, principal=principal)
        except CallError as exc:
            if exc.error == "no live call":
                return {"call": None, "server_now": now_ms()}
            raise
        return {"call": public(live), "server_now": now_ms()}


def start(caller: str, agents: list[str], *, request_id: str = "", principal: str = "",
          by: str = "") -> dict:
    """Start a call with 1..MAX_PARTICIPANTS agents; the first gets the floor."""
    names = [str(a).strip() for a in (agents or []) if str(a or "").strip()]
    if not names:
        raise CallError(400, "agents required")
    with _lock:
        who = _start_principal(caller, principal)
        data = _load(who)
        live = _live(data)
        request_id = str(request_id or "").strip()[:128]
        if live is not None and request_id and live.get("_request_id") == request_id:
            return _result(live, False, "The call is already up.")
        resolved: list[dict] = []
        for name in names:
            agent = resolve_name(name)
            if all(a["session"] != agent["session"] for a in resolved):
                resolved.append(agent)
        if len(resolved) > MAX_PARTICIPANTS:
            raise CallError(409, f"a call holds at most {MAX_PARTICIPANTS} agents")
        if live is not None:
            _finish(live, "superseded")
            _commit(who, data, live, {"action": "end", "session": "", "by": by},
                    notices={p["session"]: "the call ended." for p in live["participants"]})
        now = now_ms()
        call = {
            "call_id": "gcl_" + uuid.uuid4().hex, "host_id": _host_id(), "principal": who,
            "state": "live", "reason": "", "revision": 0, "started_at": now, "ended_at": None,
            "floor": resolved[0]["session"],
            "participants": [{"session": str(a["session"]), "agent_id": str(a["agent_id"]),
                              "persona": str(a["persona"]), "state": "active" if i == 0 else "invited",
                              "joined_at": now, "changed_at": now} for i, a in enumerate(resolved)],
            "_floor_order": [resolved[0]["session"]], "_request_id": request_id, "_changed_at": now,
        }
        data["calls"].append(call)
        everyone = _names(call["participants"])
        _commit(who, data, call, {"action": "start", "session": call["floor"], "by": by},
                notices={p["session"]: f"a call started with {everyone}." for p in call["participants"]})
        return _result(call, True, f"Started a call with {everyone}.")


def change(caller: str, action: str, agent: str, *, call_id: str = "", principal: str = "",
           by: str = "") -> dict:
    """Add, remove, hold, resume, switch to, or transfer to one participant."""
    if action not in ACTIONS:
        raise CallError(400, f"unknown action {action!r}")
    if not str(agent or "").strip():
        raise CallError(400, "agent required")
    with _lock:
        who, data, call = _locate(caller, call_id=call_id, principal=principal)
        target = resolve_name(agent, [p["session"] for p in call["participants"]])
        session, persona = str(target["session"]), str(target["persona"])
        part = _participant(call, session)
        present = part is not None and part["state"] != "left"
        if action in ("remove", "hold", "resume") and not present:
            raise CallError(409, "not in the call", session=session, persona=persona)
        changed, summary, notices = _apply(call, action, target, part)
        if changed:
            if action == "remove" and not any(p["state"] in REACHABLE | {"on_hold"}
                                              for p in call["participants"]):
                _finish(call, "empty")
                notices = {**notices, **{p["session"]: "the call ended." for p in call["participants"]
                                         if p["session"] != session}}
                summary += " That was the last one, so the call ended."
            _commit(who, data, call, {"action": action, "session": session, "by": by}, notices=notices)
        return _result(call, changed, summary)


def _apply(call: dict, action: str, target: dict, part: dict | None) -> tuple[bool, str, dict]:
    """Mutate ``call`` for one action. Returns (changed, summary, notices)."""
    session, persona = str(target["session"]), str(target["persona"])
    changed = False
    notices: dict[str, str] = {}
    if action in ("add", "switch", "transfer"):
        if part is None:
            if sum(p["state"] != "left" for p in call["participants"]) >= MAX_PARTICIPANTS:
                raise CallError(409, f"a call holds at most {MAX_PARTICIPANTS} agents")
            now = now_ms()
            part = {"session": session, "agent_id": str(target["agent_id"]), "persona": persona,
                    "state": "invited", "joined_at": now, "changed_at": now}
            call["participants"].append(part)
            changed = True
            notices = _broadcast_notice(call, session, f"you joined the call with "
                                        f"{_names(_others(call, session)) or 'the user'}.", f"{persona} joined.")
        elif part["state"] in ("left", "on_hold"):
            changed |= _set_state(call, part, "active")
            notices = _broadcast_notice(call, session, "you are back in the call.", f"{persona} is back.")
    if action == "add":
        if not call["floor"] and changed:
            _give_floor(call, session)
        summary = f"{persona} joined the call." if changed else f"{persona} is already in the call."
        return changed, summary, notices
    if action == "transfer":
        held = _participant(call, call["floor"]) if call["floor"] and call["floor"] != session else None
        if held is not None:
            _set_state(call, held, "on_hold")
            notices[held["session"]] = f"you are on hold; the user is talking to {persona}."
            changed = True
        if part["state"] == "invited":
            changed |= _set_state(call, part, "active")
        changed |= _give_floor(call, session)
        notices.setdefault(session, "the user is talking to you now.")
        summary = (f"Put {held['persona']} on hold and called {persona}." if held is not None
                   else f"You're talking to {persona}.")
        return changed, summary, notices
    if action == "switch":
        if part["state"] == "invited":
            changed |= _set_state(call, part, "active")
        changed |= _give_floor(call, session)
        notices.setdefault(session, "the user is talking to you now.")
        return changed, f"You're talking to {persona} now.", notices if changed else {}
    if action == "resume":
        changed = _set_state(call, part, "active")
        if changed and not call["floor"]:
            _give_floor(call, session)
        notices = _broadcast_notice(call, session, "you are back in the call.", f"{persona} is back.")
        return changed, (f"{persona} is back in the call." if changed else f"{persona} is not on hold."), \
            notices if changed else {}
    if action == "hold":
        changed = _set_state(call, part, "on_hold")
        _pass_floor(call)
        notices = _broadcast_notice(call, session, "you are on hold.", f"{persona} is on hold.")
        return changed, (f"{persona} is on hold." if changed else f"{persona} is already on hold."), \
            notices if changed else {}
    # remove
    changed = _set_state(call, part, "left")
    _pass_floor(call)
    notices = _broadcast_notice(call, session, "you left the call.", f"{persona} left.")
    return changed, f"{persona} left the call.", notices


def end(caller: str, *, call_id: str = "", principal: str = "", by: str = "") -> dict:
    """End the caller's call. Ending an already ended call is a no-op."""
    with _lock:
        if call_id:
            ended = _ended(caller, call_id, principal)
            if ended is not None:
                return _result(ended, False, "The call has already ended.")
        who, data, call = _locate(caller, call_id=call_id, principal=principal)
        present = [p["session"] for p in call["participants"] if p["state"] != "left"]
        _finish(call, "ended")
        _commit(who, data, call, {"action": "end", "session": "", "by": by},
                notices={s: "the call ended." for s in present})
        return _result(call, True, "The call has ended.")


def _ended(caller: str, call_id: str, principal: str) -> dict | None:
    who = caller if caller != ADMINISTRATOR else principal
    for candidate in ([who] if who else _principals()):
        for call in _load(candidate)["calls"]:
            if call["call_id"] == call_id and call["state"] != "live":
                return call
    return None


def route(caller: str, call_id: str, session: str) -> dict:
    """Where a /send carrying ``call_id`` goes (docs/group-calls.md "Routing speech").

    Returns ``{"session": recipient, "reachable": [sessions]}``: ``session``
    when it can take speech, else the floor. The recipient takes the floor and
    an invited one becomes active. Raises CallError 409 "call ended" or
    "nobody to talk to".
    """
    with _lock:
        try:
            who, data, call = _locate(caller, call_id=call_id)
        except CallError as exc:
            raise CallError(409, "call ended", call_id=call_id) from exc
        reachable = [p["session"] for p in call["participants"] if p["state"] in REACHABLE]
        recipient = session if session in reachable else call["floor"]
        if not recipient:
            raise CallError(409, "nobody to talk to", call_id=call_id)
        _take_floor(who, data, call, recipient)
        return {"session": recipient, "reachable": reachable}


def routed(caller: str, call_id: str, session: str) -> None:
    """The orchestrator sent an utterance in the call to ``session``: it takes the floor."""
    with _lock:
        try:
            who, data, call = _locate(caller, call_id=call_id)
        except CallError:
            return
        _take_floor(who, data, call, session)


def _take_floor(who: str, data: dict, call: dict, session: str) -> None:
    part = _participant(call, session)
    if part is None or part["state"] not in REACHABLE:
        return
    changed = _set_state(call, part, "active")
    changed |= _give_floor(call, session)
    if changed:
        _commit(who, data, call, {"action": "route", "session": session, "by": ""}, quiet_notices=True)


def _result(call: dict, changed: bool, summary: str) -> dict:
    return {"ok": True, "call": public(call), "changed": changed, "summary": summary}
