"""Moved out of server/lib/agent_rings.py by tools/refactor/split_apply.py (split 'agent-rings', recipe tools/refactor/splits/s9-agent-rings.toml).
agent_rings.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import contextlib
import json
import secrets
import threading
from typing import Any, Iterator
from . import db, events, settings_store
from .group_calls import CallError
from .log import log, log_exception
from .agent_rings_base import KEY, _audit, _context, _load, now_ms


def public(ring: dict) -> dict:
    out = {k: v for k, v in ring.items() if not k.startswith("_")}
    if ring.get("_user_call"):
        out["origin"] = "user_call"
    if ring.get("opening_mode") == LISTEN_FIRST:
        opening = opening_state(ring["ring_id"])
        if opening is not None:
            out["opening"] = _public_opening(opening)
    out["server_now"] = now_ms()
    return out


SAY = "say"
LISTEN_FIRST = "listen_first"
OPENING_MODES = (SAY, LISTEN_FIRST)
OPENING_KEY = "agent_ring.opening."
CALL_OPEN = "call-open:"
SILENCE_MS = 1250
OPEN_STATES = frozenset({"pending", "listening", "speech_latched"})
COMMITTED = frozenset({"greeting", "initiative_started"})
SILENCE_CLAIMS = frozenset({"silence", "no_mic", "not_ready"})
CLAIMS = frozenset({"listening", "speech_started", "speech_abandoned", "renew"}) | SILENCE_CLAIMS
ABANDON_REASONS = frozenset({"empty_capture", "capture_aborted"})
KEEP_REQUESTS = 64
_opening_lock = threading.Lock()


def _server_id() -> str:
    from .server_identity import server_id
    return server_id()


def _kick_queue() -> None:
    """Let the dispatcher see a newly admitted initiative (the runtime's own
    recovery pass; a Host with a runtime forwards it)."""
    ctx = _context.get("ctx")
    if ctx is None:
        return

    def run() -> None:
        try:
            from .turn_dispatch import TurnDispatchService
            TurnDispatchService(ctx).recover_queued()
        except Exception as exc:  # noqa: BLE001 - recovery retries on its own
            log_exception("agentRingOpeningKickFail", exc)
    threading.Thread(target=run, daemon=True).start()


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


_RECEIPT_ROWS = frozenset({"done", "failed", "interrupted", "ended"})


def _generation():
    """The current host generation, read-only (open/fstat/read/close of the boot
    id and the token). Default paths come only from host_generation's seams."""
    from . import host_generation
    return host_generation.read_host_generation()


def _gen_pair(read) -> dict | None:
    generation = getattr(read, "generation", None)
    if not generation:
        return None
    return {"boot": generation[0], "instance": generation[1]}


def _breach(stored: Any, gen: dict | None) -> str:
    """Why ``stored`` (a remembered host_gen) is not provably this host: ""
    when it is, else host_identity_unprovable / host_rebooted / db_restored."""
    if gen is None or not isinstance(stored, dict) or not all(
            isinstance(stored.get(k), str) and stored.get(k) for k in ("boot", "instance")):
        return "host_identity_unprovable"
    if stored["boot"] != gen["boot"]:
        return "host_rebooted"
    if stored["instance"] != gen["instance"]:
        return "db_restored"
    return ""


def _identity_breach(op: dict, gen: dict | None) -> str:
    """The opening's fence.host_gen against ``gen``. Not part of _fence_breach."""
    return _breach((op.get("fence") or {}).get("host_gen"), gen)


def _terminal_history(op: dict) -> bool:
    """Nothing launch-capable remains in flight: a fallback opening, an intent
    already cancelled or unknown, an intent whose row reached a receipt, or a
    resolved opening without an intent. A live opening (pending, listening,
    latched) with no intent yet is NOT history: it could still launch."""
    if op.get("state") == "fallback":
        return True
    intent = op.get("intent")
    if intent:
        if intent.get("state") in ("cancelled", "unknown"):
            return True
        row = _row(intent["admission_id"])
        return row is not None and str(row["status"]) in _RECEIPT_ROWS
    return op.get("state") not in OPEN_STATES


def _settle_unknown(op: dict, cause: str, *, retire_reason: str = "") -> None:
    """The breach outcome for an opening still in flight (in the caller's
    transaction): retired (an earlier reason kept), unknown; an intended,
    admitted or started intent unknown and its waiting or claimed row unknown,
    never withdrawn; started rows untouched. Never cancelled, never _supersede."""
    from . import turn_queue
    now = now_ms()
    if not op.get("retired"):
        op["retired"] = {"reason": retire_reason or cause, "at": now}
    if op["state"] != "fallback":
        op["state"] = "unknown"
    intent = op.get("intent")
    if intent and intent.get("state") in ("intended", "admitted", "started"):
        intent["state"] = "unknown"
        row = _row(intent["admission_id"])
        if row is not None and str(row["status"]) in ("queued", "parked", "claimed"):
            turn_queue.mark_unknown(intent["admission_id"])
    if not op.get("identity_breach"):
        op["identity_breach"] = {"cause": cause, "at": now}


def _identity_gate(op: dict, box: dict, *, retire_reason: str = "") -> str:
    """The first check at every opening site: "" when the host is provably the
    one the opening was made under. Otherwise the cause, after settling an
    in-flight opening unknown (dirty) or, for terminal history, writing nothing
    (A2'). Either way the caller DENIES: a cause is never permission."""
    cause = _identity_breach(op, box.get("gen"))
    if not cause:
        return ""
    if box.get("gen") is None:
        box["gen_unprovable"] = True
    if _terminal_history(op):
        box["history_kept"] = cause
    else:
        _settle_unknown(op, cause, retire_reason=retire_reason)
        box["dirty"] = True
    return cause


@contextlib.contextmanager
def _opening_txn(ring_id: str) -> Iterator[dict]:
    """One transaction on the ring's opening row: ``box["op"]`` is the record
    (None if absent); set ``box["dirty"]`` to write it back. ``box["error"]``
    is raised after COMMIT (a refusal that still records something).

    ``box["gen"]`` is the host generation, read once before BEGIN and once
    right after it (N2); None when either read fails or they differ. These two
    reads are the only file syscalls under the write lock. Identity logs are
    written after COMMIT or ROLLBACK, outside the lock: once per call."""
    first = _generation()
    box: dict[str, Any] = {}
    try:
        with _opening_lock:
            con = db.conn()
            con.execute("BEGIN IMMEDIATE")
            try:
                second = _generation()
                g0, g1 = _gen_pair(first), _gen_pair(second)
                box["gen"] = g0 if g0 is not None and g0 == g1 else None
                box["gen_reason"] = (getattr(first, "reason", None) or getattr(second, "reason", None)
                                     or ("" if box["gen"] is not None else "changed during transaction"))
                row = con.execute("SELECT value FROM settings WHERE key = ?",
                                  (OPENING_KEY + ring_id,)).fetchone()
                box["op"] = json.loads(row[0]) if row else None
                yield box
                if box.get("dirty") and box["op"] is not None:
                    box["op"]["updated_at"] = now_ms()
                    settings_store.set_text(OPENING_KEY + ring_id,
                                            json.dumps(box["op"], ensure_ascii=False, separators=(",", ":")))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
    finally:
        if box.get("gen_unprovable"):
            log("hostGenerationUnprovable", f"{ring_id} {box.get('gen_reason') or 'unknown'}")
        if box.get("history_kept"):
            log("hostGenerationHistoryKept", f"{ring_id} {box['history_kept']}")
    if box.get("error") is not None:
        raise box["error"]


def opening_state(ring_id: str) -> dict | None:
    """The stored opening of a listen_first ring (all fields), or None."""
    raw = settings_store.get(OPENING_KEY + str(ring_id or ""))
    return json.loads(raw) if raw else None


def _openings() -> list[dict]:
    return [json.loads(raw) for raw in settings_store.values_with_prefix(OPENING_KEY)]


def _ring_for_token(token: Any) -> str:
    if not isinstance(token, str) or not token.startswith("opn_"):
        raise CallError(404, "unknown opening")
    for op in _openings():
        if op.get("token") == token:
            return op["ring_id"]
    raise CallError(404, "unknown opening")


def _public_intent(op: dict) -> dict | None:
    intent = op.get("intent")
    if not intent:
        return None
    return {"kind": intent["kind"], "admission_id": intent["admission_id"], "gen": intent["gen"],
            "state": intent["state"]}


def _public_opening(op: dict, *, secret: bool = False) -> dict:
    """The wire opening. The owner nonce goes only to the answering phone
    (answer and claim responses); the token also, plus opening-turn `audio`
    events (§11.B), never `agent-ring` events or reads."""
    out: dict[str, Any] = {"mode": LISTEN_FIRST, "ring_id": op["ring_id"], "call_id": op["call_id"]}
    if secret and op.get("token"):
        out["token"], out["owner_nonce"] = op["token"], op["owner_nonce"]
    out.update({"epoch": op["epoch"], "state": op["state"], "silence_ms": op["silence_ms"],
                "session": op["session"], "persona": op["persona"], "server_id": op["server_id"],
                "intent": _public_intent(op)})
    if op.get("late_greeting"):
        out["greeting"] = "ordinary"
    if op.get("retired"):
        out["retired"] = op["retired"]["reason"]
    return out


def _open_record(box: dict, ring: dict, principal: str, *, capable: bool,
                 host_gen: dict | None = None) -> None:
    """The opening record at answer (inside the answer's transaction):
    pending for a capable phone, fallback (today's answer, nothing
    dispatched) for any other. ``host_gen`` is the generation proven for this
    answer; None makes the record unprovable from birth (fail closed)."""
    from . import turn_queue
    now = now_ms()
    if box["op"] is None:
        # §11.L: the answer may pass a pause that exists now, nothing newer.
        fence = {"answered_at": now, "stop_generation": turn_queue.stop_generation(ring["agent_id"]),
                 "paused_at_answer": turn_queue.is_paused(ring["agent_id"]), "auth": _auth_proof(principal),
                 "host_gen": host_gen}
        box["op"] = {
            "ring_id": ring["ring_id"], "call_id": ring["call_id"], "principal": principal,
            "server_id": _server_id(), "agent_id": ring["agent_id"], "session": ring["session"],
            "persona": ring["persona"], "reason": ring["reason"],
            "token": "opn_" + secrets.token_hex(16) if capable else "",
            "owner_nonce": "own_" + secrets.token_hex(16) if capable else "",
            "epoch": 1, "state": "pending" if capable else "fallback", "silence_ms": SILENCE_MS,
            "capture": None, "latch": None, "intent": None, "greeting": None, "late_greeting": None,
            "retired": None, "requests": {}, "created_at": now, "fence": fence,
        }
        box["dirty"] = True


def _auth_proof(principal: str) -> dict | None:
    """The answering device's auth generation (its token hash), or None for a
    principal that is not a paired device (the local administrator)."""
    row = db.conn().execute("SELECT token_hash FROM paired_devices WHERE device_id = ?",
                            (principal,)).fetchone()
    return {"token_hash": str(row[0])} if row else None


def _fence_breach(op: dict) -> str:
    """§11.L, read in the caller's transaction: why the answer no longer
    authorizes the initiative, or "". A Stop or cancel newer than the answer,
    a ring, call or auth that is no longer the one answered, another Host."""
    from . import turn_queue
    if op.get("retired"):
        return op["retired"]["reason"]
    if op["server_id"] != _server_id():
        return "host_changed"
    fence = op.get("fence") or {}
    con = db.conn()
    records = con.execute("SELECT value FROM settings WHERE key = ?", (KEY,)).fetchone()
    ring = next((r for r in (json.loads(records[0]) if records else []) if r["ring_id"] == op["ring_id"]), None)
    if ring is None or ring["state"] != "answered" or ring["answered_by"] != op["principal"] \
            or ring["call_id"] != op["call_id"]:
        return "ring_changed"
    calls = con.execute("SELECT value FROM settings WHERE key = ?", ("group_call." + op["principal"],)).fetchone()
    live = calls and any(c["call_id"] == op["call_id"] and c["state"] == "live"
                         for c in json.loads(calls[0]).get("calls", []))
    if not live:
        return "call_ended"
    if fence.get("auth"):
        device = con.execute("SELECT token_hash, revoked_at FROM paired_devices WHERE device_id = ?",
                             (op["principal"],)).fetchone()
        if device is None or device["revoked_at"] is not None:
            return "auth_revoked"
        if str(device["token_hash"]) != fence["auth"]["token_hash"]:
            return "auth_changed"
    if "stop_generation" in fence and turn_queue.stop_generation(op["agent_id"]) != fence["stop_generation"]:
        return "stopped_after_answer"
    return ""


def _enforce_fence(op: dict) -> str:
    """Retire (intent cancelled, a waiting row withdrawn) when the fence is
    breached; returns the reason or ""."""
    reason = _fence_breach(op)
    if reason and not op.get("retired"):
        _retire(op, reason)
    return reason


def _refuse(op: dict, status: int, error: str, **extra: Any) -> CallError:
    return CallError(status, error, opening=_public_opening(op), **extra)


def _retire(op: dict, reason: str) -> bool:
    """Retire the opening's output authority (in the caller's transaction).
    A tentative initiative is cancelled (its unclaimed row withdrawn); a
    committed outcome is kept in the record but fenced. An opening settled by
    an identity breach is never rewritten."""
    if op.get("retired") or op["state"] == "fallback" or op.get("identity_breach"):
        return False
    if op["state"] in OPEN_STATES:
        op["state"] = "retired"
    elif op["state"] == "initiative_queued":
        settled = _supersede(op)
        op["state"] = "initiative_started" if settled == "started" else "retired"
    elif op["state"] == "unknown":
        op["state"] = "retired"
    op["retired"] = {"reason": reason, "at": now_ms()}
    return True


# Queue statuses that prove the initiative's row was claimed and ran (or ran
# and ended): turn_queue.mark_started / settle_receipt / terminalize.
_STARTED_ROWS = frozenset({"started", "done", "failed", "interrupted", "ended"})
# Statuses of a row that is still waiting and unclaimed: queued, or held by a Stop.
_WAITING_ROWS = frozenset({"queued", "parked"})
# A row that is gone without having run: withdrawn here, cancelled by the queue.
_DROPPED_ROWS = frozenset({"withdrawn", "cancelled"})


def is_initiative(queue_id: str, trace_id: str = "") -> bool:
    """Whether this queue row is an initiative the Host itself admitted: its
    prompt admission (by primary key) is a ``call`` admission under the same
    id and trace. Only admit_initiative writes the ``call`` channel; a client
    can name a ``call-open:`` id or a trace on /send, never this pair."""
    queue_id = str(queue_id or "")
    if not queue_id.startswith(CALL_OPEN):
        return False
    row = db.conn().execute(
        """SELECT q.trace_id, a.channel, a.client_admission_id, a.trace_id AS admitted_trace
             FROM queued_turns q JOIN prompt_admissions a ON a.admission_id = q.prompt_admission_id
            WHERE q.queue_id = ?""", (queue_id,)).fetchone()
    return bool(row and row["channel"] == "call" and row["client_admission_id"] == queue_id
                and row["admitted_trace"] == row["trace_id"]
                and (not trace_id or row["trace_id"] == trace_id))


def is_foreign_message(prompt_admission_id: str) -> bool:
    """Positive evidence that a ``call-open:``-named send is someone's own
    message: its prompt admission (by primary key) was not written by
    admit_initiative. Without that evidence it is never treated as ordinary."""
    if not prompt_admission_id:
        return False
    row = db.conn().execute("SELECT channel FROM prompt_admissions WHERE admission_id = ?",
                            (prompt_admission_id,)).fetchone()
    return bool(row and row["channel"] != "call")


def _row_admission(queue_id: str) -> str:
    row = db.conn().execute("SELECT prompt_admission_id FROM queued_turns WHERE queue_id = ?",
                            (queue_id,)).fetchone()
    return str(row["prompt_admission_id"] or "") if row else ""


def _row(queue_id: str):
    """The exact queued row, by its primary key (bounded; P3)."""
    return db.conn().execute("SELECT status, claimed_at, trace_id FROM queued_turns WHERE queue_id = ?",
                             (queue_id,)).fetchone()


def _resolve_unknown(op: dict) -> None:
    """A claimed initiative came back to the gate, or its claim outlived its
    process: whether it reached the backend cannot be known. A durable,
    explicit unknown (§11.H): never success, never replayed. A retired
    opening stays retired."""
    from . import turn_queue
    intent = op["intent"]
    intent["state"] = "unknown"
    turn_queue.mark_unknown(intent["admission_id"])
    if not op.get("retired"):
        op["state"] = "unknown"


def _withdraw_row(queue_id: str) -> bool:
    from . import turn_queue
    return turn_queue.withdraw_unclaimed(queue_id)


def _supersede(op: dict) -> str:
    """A greeting, speech onset or retire against initiative_queued, decided
    from evidence read in the caller's transaction (§11.H): the intent, the
    exact admission's queued row and any turn under the intent's trace.

    ``cancelled``: nothing ran (no row yet, a waiting row now withdrawn, or a
    row dropped or deleted without start evidence); never resurrected.
    ``started``: positive claim/start evidence for this exact admission.
    ``unknown``: the evidence disagrees; the opening is ``unknown`` and no
    initiative runs (a waiting row is withdrawn, a claim is refused). After an
    identity breach: ``unknown``, and nothing is written."""
    if op.get("identity_breach"):
        return "unknown"
    intent = op["intent"]
    if intent["state"] in ("cancelled", "started", "unknown"):
        return intent["state"]
    if intent["state"] == "intended":
        intent["state"] = "cancelled"
        return "cancelled"
    # The intent is admitted: the dispatcher's gate (claim_initiative) has not
    # passed, and only that gate moves it to started with the row, in this same
    # transaction lock (P4-I). So nothing ran, and the exact row decides;
    # no turns or state history is read.
    row = _row(intent["admission_id"])
    status = str(row["status"]) if row is not None else ""
    if row is not None and row["trace_id"] != intent["trace_id"]:
        verdict = "unknown"                       # not this admission's row
    elif status in _STARTED_ROWS or status == "unknown":
        verdict = "unknown"                       # contradicts P4-I: never "not started"
    elif status in _WAITING_ROWS and row["claimed_at"] is None and _withdraw_row(intent["admission_id"]):
        verdict = "cancelled"
    elif row is None or status in _DROPPED_ROWS:
        verdict = "cancelled"
    elif status == "claimed":
        verdict = "cancelled"                     # a manual send's claim; the gate now refuses it
    else:
        verdict = "unknown"                       # a status we do not know
    if verdict == "unknown":
        _withdraw_row(intent["admission_id"])
        op["state"] = "unknown"
    intent["state"] = verdict
    return verdict


def _check_live(op: dict, box: dict) -> bool:
    """Retire on a Host instance change; refuse a retired opening."""
    if not op.get("retired") and op["server_id"] != _server_id():
        _retire(op, "host_changed")
        box["dirty"] = True
    if op.get("retired"):
        box["error"] = _refuse(op, 409, "opening retired", reason=op["retired"]["reason"])
        return False
    return True


def _header(op: dict, cause: str) -> str:
    from .group_calls import opening_header
    return opening_header(op["reason"], cause, op["silence_ms"])


def _intend(op: dict, cause: str) -> None:
    previous = op.get("intent") or {}
    gen = int(previous.get("gen") or 0) + 1
    op["state"] = "initiative_queued"
    # One admission per intent generation (§11.I): a withdrawn gen never
    # collides with its successor on the UNIQUE admission key.
    op["intent"] = {"kind": "initiative", "admission_id": f"{CALL_OPEN}{op['ring_id']}:{gen}",
                    "gen": gen, "state": "intended", "cause": cause,
                    "trace_id": "callopen-" + secrets.token_hex(6)}


def _fingerprint(body: dict) -> str:
    keys = ("token", "epoch", "claim", "expected_epoch", "owner_nonce", "reason")
    return json.dumps({k: body.get(k) for k in keys}, sort_keys=True)


def _validate(body: dict) -> str:
    claim = body.get("claim")
    if not isinstance(claim, str) or claim not in CLAIMS:
        raise CallError(400, "bad claim")
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise CallError(400, "bad claim")
    if claim == "renew":
        if _int(body.get("expected_epoch")) is None or not isinstance(body.get("owner_nonce"), str):
            raise CallError(400, "bad claim")
    elif _int(body.get("epoch")) is None:
        raise CallError(400, "bad claim")
    return claim


def opening_claim(principal: str, body: dict) -> dict:
    """``POST /calls/ring/opening``: the phone reports readiness, speech onset,
    an abandoned utterance, no speech by its deadline, no mic, or renews the
    epoch. Returns ``{ok, opening, changed}``; refusals dispatch nothing."""
    if not isinstance(body, dict):
        raise CallError(400, "bad claim")
    claim = _validate(body)
    ring_id = _ring_for_token(body.get("token"))
    request_id = body["request_id"]
    queued = False
    with _opening_txn(ring_id) as box:
        op = box["op"]
        if op is None or op.get("token") != body.get("token"):
            raise CallError(404, "unknown opening")
        if op["principal"] != principal:
            raise _refuse(op, 403, "not the answering device")
        seen = op["requests"].get(request_id)
        cause = _identity_gate(op, box)
        if cause:
            # Before any replay: a breach never returns a stored (secret) answer.
            box["error"] = _refuse(op, 409, "opening retired", reason=cause)
        elif seen is not None:
            if seen["fingerprint"] != _fingerprint(body):
                raise _refuse(op, 400, "bad claim")
            box["result"] = (seen.get("opening") if claim == "renew" else None, False)
        elif _check_live(op, box):
            queued = _apply_claim(op, claim, body, box)
            if queued:
                reason = _enforce_fence(op)
                if reason:
                    queued = False
                    box["dirty"] = True
                    box["error"] = _refuse(op, 409, "opening retired", reason=reason)
            if box.get("error") is None:
                op["requests"][request_id] = {"fingerprint": _fingerprint(body), "at": now_ms()}
                if len(op["requests"]) > KEEP_REQUESTS:
                    oldest = sorted(op["requests"], key=lambda k: op["requests"][k]["at"])
                    for key in oldest[:len(op["requests"]) - KEEP_REQUESTS]:
                        op["requests"].pop(key, None)
                if claim == "renew":
                    op["requests"][request_id]["opening"] = _public_opening(op, secret=True)
                box["dirty"] = True
    if queued:
        _admit_after_resolve(ring_id)
    stored, changed = box.get("result", (None, True))
    if stored is not None:
        return {"ok": True, "opening": stored, "changed": changed}
    if changed:
        _audit({"outcome": "opening", "ring_id": ring_id, "claim": claim, "by": principal})
        _announce_opening(ring_id)
    return {"ok": True, "opening": _public_opening(opening_state(ring_id), secret=True), "changed": changed}


def _apply_claim(op: dict, claim: str, body: dict, box: dict) -> bool:
    """One claim against a live opening (inside its transaction). Returns
    True when it queued an initiative intent; refusals go into ``box``."""
    state = op["state"]
    if claim == "renew":
        if state not in OPEN_STATES:
            box["error"] = _refuse(op, 409, "already resolved", outcome=state)
        elif body["expected_epoch"] != op["epoch"]:
            box["error"] = _refuse(op, 409, "stale epoch")
        elif body["owner_nonce"] != op["owner_nonce"]:
            box["error"] = _refuse(op, 409, "owner changed")
        else:
            # The latch and its captured-turn authority survive a renewal.
            op["epoch"] += 1
            op["owner_nonce"] = "own_" + secrets.token_hex(16)
        return False
    if body["epoch"] != op["epoch"]:
        box["error"] = _refuse(op, 409, "stale epoch")
        return False
    if claim == "speech_started" and state == "initiative_queued":
        verdict = _supersede(op)
        if verdict == "cancelled":
            op["state"] = "speech_latched"
            op["latch"] = {"epoch": op["epoch"], "at_ms": _int(body.get("at_ms")),
                           "capture_gen": _int(body.get("capture_gen"))}
        else:
            if verdict == "started":
                op["state"] = "initiative_started"
            box["dirty"] = True
            box["error"] = _refuse(op, 409, "already resolved", outcome=op["state"])
        return False
    if state not in OPEN_STATES:
        box["error"] = _refuse(op, 409, "already resolved", outcome=state)
        return False
    if claim == "listening":
        op["capture"] = {"capture_gen": _int(body.get("capture_gen")), "at_ms": _int(body.get("at_ms"))}
        if state == "pending":
            op["state"] = "listening"
        return False
    if claim == "speech_started":
        if state != "speech_latched":
            op["state"] = "speech_latched"
            op["latch"] = {"epoch": op["epoch"], "at_ms": _int(body.get("at_ms")),
                           "capture_gen": _int(body.get("capture_gen"))}
        else:
            box["result"] = (None, False)
        return False
    if claim == "speech_abandoned":
        if state != "speech_latched" or body.get("reason") not in ABANDON_REASONS:
            box["error"] = _refuse(op, 400, "bad claim")
            return False
        _intend(op, "speech_abandoned")
        return True
    # silence / no_mic / not_ready
    if state == "speech_latched":
        box["error"] = _refuse(op, 409, "speech latched")
        return False
    if claim == "silence":
        at_ms, watermark = _int(body.get("at_ms")), _int(body.get("watermark_ms"))
        if at_ms is None or watermark is None or at_ms < op["silence_ms"] or watermark < op["silence_ms"]:
            box["error"] = _refuse(op, 400, "bad claim")
            return False
    _intend(op, claim)
    return True


def greeting(principal: str, *, token: Any, epoch: Any, client_msg_id: str, trace_id: str,
             session: str, call_id: str) -> dict:
    """The phone's first captured utterance (``/send`` with ``call_opening``).

    Returns ``{route, session, opening, changed}``: ``greeting`` resolves the
    opening with this message, ``ordinary`` when the initiative already
    started (the utterance is then an ordinary call utterance). The message
    itself is admitted by the caller's ordinary dispatch, deduplicated by its
    client_msg_id. Refusals (CallError) dispatch nothing."""
    if _int(epoch) is None:
        raise CallError(400, "bad call_opening")
    ring_id = _ring_for_token(token)
    with _opening_txn(ring_id) as box:
        op = box["op"]
        if op is None or op.get("token") != token:
            raise CallError(404, "unknown opening")
        if op["principal"] != principal:
            raise _refuse(op, 403, "not the answering device")
        mine = (op.get("greeting") or {}).get("client_msg_id") == client_msg_id
        cause = _identity_gate(op, box)
        if cause:
            box["error"] = _refuse(op, 409, "opening retired", reason=cause)
        elif mine and not op.get("retired") and op["state"] == "greeting":
            box["result"] = ("greeting", False)
        elif _check_live(op, box):
            if session != op["session"] or call_id != op["call_id"]:
                box["error"] = _refuse(op, 409, "wrong call")
            elif epoch != op["epoch"]:
                box["error"] = _refuse(op, 409, "stale epoch")
            elif op["state"] in ("greeting", "fallback"):
                box["error"] = _refuse(op, 409, "already resolved", outcome=op["state"])
            else:
                verdict = "started" if op["state"] == "initiative_started" else None
                if op["state"] == "initiative_queued":
                    verdict = _supersede(op)
                elif op["state"] == "unknown":
                    verdict = "unknown"
                if verdict == "unknown":
                    # Whether the initiative ran is unknown: the utterance is
                    # delivered as an ordinary one, never as the opening.
                    box["result"] = ("ordinary", True)
                elif verdict == "started":
                    op["state"] = "initiative_started"
                    if not op.get("late_greeting"):
                        op["late_greeting"] = {"client_msg_id": client_msg_id, "trace_id": trace_id,
                                               "at": now_ms()}
                    box["result"] = ("ordinary", True)
                else:
                    op["state"] = "greeting"
                    op["greeting"] = {"client_msg_id": client_msg_id, "trace_id": trace_id,
                                      "admission": "pending", "at": now_ms()}
                    box["result"] = ("greeting", True)
                box["dirty"] = True
    route, changed = box["result"]
    op = opening_state(ring_id)
    if changed:
        _audit({"outcome": "opening", "ring_id": ring_id, "claim": "greeting", "route": route, "by": principal})
        _announce_opening(ring_id)
    return {"route": route, "session": op["session"], "opening": _public_opening(op), "changed": changed}


def greeting_routed(principal: str, *, token: Any, epoch: Any, client_msg_id: str, trace_id: str,
                    session: str, call_id: str) -> dict:
    """The greeting and its call route as ONE decision: under the call lock
    the call must be live with the caller reachable, then the opening commits
    and the caller takes the floor. A 409 never touches the opening, and the
    utterance is never re-routed elsewhere."""
    from . import group_calls
    with group_calls._lock:
        try:
            _who, _data, call = group_calls._locate(principal, call_id=call_id)
        except CallError as exc:
            raise CallError(409, "call ended", call_id=call_id) from exc
        part = group_calls._participant(call, session)
        if part is None or part["state"] not in group_calls.REACHABLE:
            raise CallError(409, "wrong call", reason="not_reachable", session=session)
        result = greeting(principal, token=token, epoch=epoch, client_msg_id=client_msg_id,
                          trace_id=trace_id, session=session, call_id=call_id)
        routed = group_calls.route(principal, call_id, result["session"])
        if routed["session"] != result["session"]:     # cannot happen under the lock
            raise CallError(409, "wrong call", reason="rerouted", session=routed["session"])
    return result


def greeting_admitted(ring_id: str, client_msg_id: str, *, ok: bool) -> None:
    """The greeting's ordinary dispatch returned: its admission is confirmed,
    or unknown on an ambiguous result (the phone's retry completes it)."""
    try:
        with _opening_txn(ring_id) as box:
            greeting_record = (box["op"] or {}).get("greeting") or {}
            if box["op"] is not None and _identity_gate(box["op"], box):
                pass                                    # bookkeeping only: the admission is not flipped
            elif greeting_record.get("client_msg_id") == client_msg_id and greeting_record.get("admission") != "confirmed":
                greeting_record["admission"] = "confirmed" if ok else "unknown"
                box["dirty"] = True
    except Exception as exc:  # noqa: BLE001 - bookkeeping only
        log_exception("agentRingGreetingAdmitFail", exc, f"ring={ring_id}")


def admit_initiative(ring_id: str) -> str:
    """The admitter: in one transaction, insert the initiative's prompt
    admission and queued row and mark the intent admitted, but only while the
    intent is still ``intended`` (a greeting, retire or earlier admit fences
    it). Returns ``admitted``, ``adopted`` (an admission row already existed)
    or ``fenced``."""
    from . import prompt_admissions, turn_queue
    with _opening_txn(ring_id) as box:
        op = box["op"]
        intent = (op or {}).get("intent")
        if op is not None and _identity_gate(op, box):
            box["result"] = "fenced"
            return "fenced"
        if not intent or intent["state"] != "intended" or op.get("retired"):
            box["result"] = "fenced"
            return "fenced"
        if _enforce_fence(op):                      # a newer Stop/cancel/retire wins
            box["dirty"] = True
            box["result"] = "fenced"
            return "fenced"
        admission_id = intent["admission_id"]
        con = db.conn()
        existing = con.execute(
            "SELECT admission_id, channel, trace_id FROM prompt_admissions"
            " WHERE agent_id = ? AND client_admission_id = ?",
            (op["agent_id"], admission_id)).fetchone()
        row = _row(admission_id)
        if ((existing is not None and (existing["channel"] != "call"
                                       or existing["trace_id"] != intent["trace_id"]))
                or (row is not None and row["trace_id"] != intent["trace_id"])):
            # Someone else's message already holds this admission id (a client
            # can name it on /send): it is not this initiative. The opening is
            # fenced to unknown with no initiative; that message stays theirs,
            # an ordinary one.
            intent["state"] = "unknown"
            if not op.get("retired"):
                op["state"] = "unknown"
            box["dirty"] = True
            box["result"] = "fenced"
            return "fenced"
        intent["state"] = "admitted"
        box["dirty"] = True
        if existing is not None:
            box["result"] = "adopted"
        else:
            header = _header(op, intent["cause"])
            admission = prompt_admissions.create(
                authenticated_at_admission=True, origin="automation", sender_agent_id="",
                channel="call", observed_at=now_ms(), client_admission_id=admission_id,
                trace_id=intent["trace_id"], original_text=header)
            admission_row = prompt_admissions.record(admission, agent_id=op["agent_id"], session=op["session"])
            turn_queue.enqueue(
                queue_id=admission_id, agent_id=op["agent_id"], session=op["session"], text=header,
                trace_id=intent["trace_id"], client_msg_id=admission_id, synthesize_audio=True,
                origin="automation", sender_agent_id="", prompt_admission_id=admission_row)
            # Answering the call is the owner's fresh intent: a Stop's leftover
            # pause must not hold the opening while the phone listens.
            turn_queue.allow_paused_for(admission_id)
            box["result"] = "admitted"
    _audit({"outcome": "opening", "ring_id": ring_id, "admission": box["result"]})
    return box["result"]


def _admit_after_resolve(ring_id: str) -> None:
    """Admission runs outside the resolve transaction; a crash in between
    leaves the intent ``intended`` for recovery."""
    if admit_initiative(ring_id) in ("admitted", "adopted"):
        _kick_queue()


def claim_initiative(queue_id: str) -> bool:
    """The dispatcher's claim of a call-open queued row, before it spawns:
    queued -> claimed and the intent -> started in one transaction, only
    while this exact admission is the opening's admitted intent. A claimed
    initiative is never claimed again (P4): coming back here after any
    generic reset, release or retry, it is ``unknown`` (unless its row shows
    it ran, or a newer Stop/retire wins). Otherwise the row is withdrawn (if
    still waiting) and False: the dispatcher does not run it. A row that is
    not a Host-admitted initiative (a client's ``call-open:`` id) passes."""
    queue_id = str(queue_id or "")
    if not queue_id.startswith(CALL_OPEN):
        return True
    ring_id = queue_id[len(CALL_OPEN):].rsplit(":", 1)[0]
    with _opening_txn(ring_id) as box:
        op = box["op"]
        intent = (op or {}).get("intent") or {}
        row = _row(queue_id)
        if row is not None and is_foreign_message(_row_admission(queue_id)):
            box["result"] = True                    # someone's own message under that id
        elif op is None or row is None or intent.get("admission_id") != queue_id \
                or row["trace_id"] != intent.get("trace_id"):
            _withdraw_row(queue_id)
            box["result"] = False
        elif _identity_gate(op, box):
            box["result"] = False                   # never spawned; an in-flight row is now unknown
        elif intent.get("state") == "started":
            box["result"] = False                   # claimed for spawn before: never again
            if str(row["status"]) not in _STARTED_ROWS and row["status"] != "unknown":
                _enforce_fence(op)                  # a newer Stop/cancel/retire still wins
                _resolve_unknown(op)
                box["dirty"] = True
        elif intent.get("state") != "admitted" or op.get("retired"):
            _withdraw_row(queue_id)
            box["result"] = False
        elif _enforce_fence(op):                    # a newer Stop/cancel/retire wins: no spawn
            _withdraw_row(queue_id)
            box["dirty"] = True
            box["result"] = False
        else:
            from . import turn_queue
            if not turn_queue.claim_for_dispatch(queue_id):
                box["result"] = False
            else:
                intent["state"] = "started"
                if op["state"] == "initiative_queued":
                    op["state"] = "initiative_started"
                box["dirty"] = True
                box["result"] = True
    if box["result"] or box.get("dirty"):
        _announce_opening(ring_id)
    return box["result"]


def release_unsent(queue_id: str, trace_id: str) -> bool:
    """The dispatcher's own proof, for this exact admission, that its launch
    failed before the backend was invoked (D2): the claim is undone, the row
    queued again and the intent admitted, so the normal first claim (with its
    fence) runs it once more. A retired opening withdraws the row instead."""
    from . import turn_queue
    queue_id = str(queue_id or "")
    if not queue_id.startswith(CALL_OPEN) or not is_initiative(queue_id, trace_id):
        return False
    ring_id = queue_id[len(CALL_OPEN):].rsplit(":", 1)[0]
    with _opening_txn(ring_id) as box:
        op = box["op"]
        intent = (op or {}).get("intent") or {}
        row = _row(queue_id)
        box["result"] = False
        if (op is None or row is None or intent.get("admission_id") != queue_id
                or intent.get("trace_id") != trace_id or intent.get("state") != "started"
                or row["status"] != "claimed"):
            return False
        if _identity_gate(op, box):
            return False                            # unknown, never queued again
        if op.get("retired"):
            turn_queue.withdraw_claimed(queue_id)   # the queue's own writer, in this transaction
            intent["state"] = "cancelled"
        else:
            turn_queue.release_claim(queue_id)
            intent["state"] = "admitted"
            if op["state"] == "initiative_started":
                op["state"] = "initiative_queued"
        box["dirty"] = True
        box["result"] = True
    _announce_opening(ring_id)
    return True


def authorize_parked_launch(queue_id: str, trace_id: str) -> str:
    """Admit again, just before it runs, an initiative the gate already claimed: when a first
    launch the account coordinator parked before the backend was invoked resumes (L2), and as
    the final check immediately before the spawn flag on every launch path (L2 r3). The answer
    may have stopped authorizing it since (a retire, the call ended, the ring, host or auth
    changed, a Stop after the answer).

    ``"launch"``: this exact Host-issued initiative (its own ``call`` admission and trace, intent
    ``started``, row ``claimed``) is still authorized. ``"cancelled"``: the fence was breached.
    The launch is PROVEN unsent (parked before the spawn flag), so, as release_unsent's retired
    branch, the row is withdrawn and the intent cancelled; the opening keeps its retire. Not
    unknown, not a failure. ``"unknown"``: the identity or state cannot be proven; the caller
    must never launch on it.

    Not eliminated: a retire or fence-source write committed after this transaction and before
    the provider handoff (claude's stdin write, the argv backends' Popen) still lets that one
    launch run. The spawn flag is possibly-sent bookkeeping, not delivery; closing the window
    needs a launch-commit mechanism this does not provide."""
    from . import turn_queue
    queue_id = str(queue_id or "")
    if not queue_id.startswith(CALL_OPEN) or not is_initiative(queue_id, trace_id):
        return "unknown"
    ring_id = queue_id[len(CALL_OPEN):].rsplit(":", 1)[0]
    with _opening_txn(ring_id) as box:
        op = box["op"]
        intent = (op or {}).get("intent") or {}
        row = _row(queue_id)
        box["result"] = "unknown"
        if (op is None or row is None or intent.get("admission_id") != queue_id
                or intent.get("trace_id") != trace_id or intent.get("state") != "started"
                or row["status"] != "claimed"):
            return "unknown"
        if _identity_gate(op, box):
            box["result"] = "unknown"               # never "launch", never "cancelled"
        elif _enforce_fence(op):
            turn_queue.withdraw_claimed(queue_id)   # the queue's own writer, in this transaction
            intent["state"] = "cancelled"
            box["dirty"] = True
            box["result"] = "cancelled"
        else:
            box["result"] = "launch"
    if box.get("dirty"):
        _announce_opening(ring_id)
    return box["result"]


def resolve_launch_failure(queue_id: str, trace_id: str) -> bool:
    """The dispatcher's report that this exact initiative's one launch failed after the
    backend had it (F1): it is never retried, redelivered or resumed, and what it did cannot
    be known. Only the Host-issued initiative (its own ``call`` admission, same trace) that the
    gate claimed for spawn; the opening, its intent and its row become the durable
    ``unknown``. A newer Stop, cancel or retire still wins: a retired opening stays retired."""
    from . import turn_queue
    queue_id = str(queue_id or "")
    if not queue_id.startswith(CALL_OPEN) or not is_initiative(queue_id, trace_id):
        return False
    ring_id = queue_id[len(CALL_OPEN):].rsplit(":", 1)[0]
    with _opening_txn(ring_id) as box:
        op = box["op"]
        intent = (op or {}).get("intent") or {}
        box["result"] = False
        if (op is None or intent.get("admission_id") != queue_id
                or intent.get("trace_id") != trace_id or intent.get("state") != "started"):
            return False
        if _identity_gate(op, box):
            box["result"] = bool(box.get("dirty"))
            return box["result"]
        _enforce_fence(op)
        intent["state"] = "unknown"
        turn_queue.mark_launched_call_open_unknown(queue_id)
        if not op.get("retired"):
            op["state"] = "unknown"
        box["dirty"] = True
        box["result"] = True
    _announce_opening(ring_id)
    return True


def resolve_stale_claims(cutoff_ms: int | None) -> set[str]:
    """Settle call-open rows still ``claimed`` (claimed_at at or before
    ``cutoff_ms``; every one when None, as at boot, where a process boundary is
    never proof). Only Host-admitted initiatives are examined; their ids are
    returned so the generic reset leaves them alone. A claim is never proof
    either way: the current intent ``started`` becomes unknown; ``admitted``
    (a manual send's claim, the gate never passed) is queued again;
    ``cancelled`` or an older generation is withdrawn."""
    from . import turn_queue
    sql = "SELECT queue_id, trace_id FROM queued_turns WHERE status = 'claimed' AND queue_id LIKE 'call-open:%'"
    args: tuple = ()
    if cutoff_ms is not None:
        sql += " AND COALESCE(claimed_at, 0) <= ?"
        args = (cutoff_ms,)
    examined: set[str] = set()
    for candidate in db.conn().execute(sql, args).fetchall():
        queue_id = str(candidate["queue_id"])
        ring_id = queue_id[len(CALL_OPEN):].rsplit(":", 1)[0]
        if (is_foreign_message(_row_admission(queue_id))
                or settings_store.get(OPENING_KEY + ring_id) is None):
            continue                                # no opening owns it: the generic reset does
        examined.add(queue_id)
        with _opening_txn(ring_id) as box:
            op = box["op"]
            intent = (op or {}).get("intent") or {}
            row = _row(queue_id)
            if row is None or row["status"] != "claimed":
                continue
            if op is None:
                turn_queue.mark_unknown(queue_id)
            elif _identity_gate(op, box):
                pass                                    # settled unknown, or history kept; still examined
            elif intent.get("admission_id") == queue_id and row["trace_id"] != intent.get("trace_id"):
                turn_queue.mark_unknown(queue_id)       # not this admission's row: never decided here
            elif intent.get("admission_id") != queue_id:
                turn_queue.withdraw_claimed(queue_id)   # an older generation: never the current one
            elif intent.get("state") == "started":
                _resolve_unknown(op)
            elif intent.get("state") == "admitted":
                turn_queue.release_claim(queue_id)
            elif intent.get("state") == "cancelled":
                turn_queue.withdraw_claimed(queue_id)
            else:
                turn_queue.mark_unknown(queue_id)
            box["dirty"] = True
        _announce_opening(ring_id)
    return examined


def _recover_one(ring_id: str) -> int:
    op = opening_state(ring_id)
    state = (op or {}).get("intent", {}) or {}
    if not op or op.get("retired") or state.get("state") not in ("intended", "admitted"):
        return 0
    if state["state"] == "admitted":
        # Admitted but not yet claimed: the same fence; a newer Stop retires
        # it now (the held row withdrawn) instead of waiting for a claim.
        with _opening_txn(ring_id) as box:
            if box["op"] is not None and (box["op"].get("intent") or {}).get("state") == "admitted":
                if _identity_gate(box["op"], box):
                    pass                                # unknown; never admitted to run
                elif _enforce_fence(box["op"]):
                    box["dirty"] = True
        return 0
    try:
        result = admit_initiative(ring_id)
    except Exception as exc:  # noqa: BLE001 - the next read or boot retries
        log_exception("agentRingOpeningRecoverFail", exc, f"ring={ring_id}")
        return 0
    if result in ("admitted", "adopted"):
        _kick_queue()
        return 1
    return 0


def recover_openings() -> int:
    """At Host start (and lazily per ring on read): admit every initiative
    whose intent is still ``intended``. Never acts on anything else."""
    return sum(_recover_one(op["ring_id"]) for op in _openings()
               if (op.get("intent") or {}).get("state") in ("intended", "admitted") and not op.get("retired"))


def _retire_where(match, reason: str) -> int:
    count = 0
    for op in _openings():
        if not match(op) or op.get("retired"):
            continue
        with _opening_txn(op["ring_id"]) as box:
            if box["op"] is None:
                pass
            elif _identity_gate(box["op"], box, retire_reason=reason):
                # In flight: settled unknown with the writer's reason. Terminal
                # history: kept as stored and not counted.
                if box.get("dirty"):
                    count += 1
            elif _retire(box["op"], reason):
                box["dirty"] = True
                count += 1
        kept = box.get("history_kept")
        _audit({"outcome": "opening", "ring_id": op["ring_id"], "retired": reason,
                **({"history_kept": kept} if kept else {})})
        _announce_opening(op["ring_id"])
    return count


def retire_for_call(call_id: str, reason: str) -> int:
    """The call ended, was replaced or superseded: its opening retires."""
    return _retire_where(lambda op: op.get("call_id") == call_id, reason)


def retire_for_principal(principal: str, reason: str) -> int:
    """The answering device was unpaired or its auth revoked."""
    return _retire_where(lambda op: op.get("principal") == principal, reason)


def _announce_opening(ring_id: str) -> None:
    """The ``agent-ring`` event with the opening's new state. Never raises."""
    stream = _context["stream"]
    if stream is None:
        return
    try:
        ring = next((r for r in _load() if r["ring_id"] == ring_id), None)
        if ring is None:
            return
        wire = public(ring)
        events.broadcast(stream, events.agent_ring(
            **{key: wire.get(key) for key in events.FIELDS[events.SSEType.AGENT_RING]}))
    except Exception as exc:  # noqa: BLE001
        log_exception("agentRingEmitFail", exc, f"ring={ring_id}")


def _opening_for_trace(trace_id: str) -> tuple[dict, str] | None:
    if not trace_id:
        return None
    for op in _openings():
        intent = op.get("intent") or {}
        if intent.get("trace_id") == trace_id:
            return op, "initiative"
        if (op.get("greeting") or {}).get("trace_id") == trace_id:
            return op, "greeting_reply"
    return None


def opening_audio(trace_id: str) -> dict | None:
    """``{call_id, opening}`` for a clip of an opening turn, else None
    (ordinary follow-ups are never tagged)."""
    found = _opening_for_trace(str(trace_id or ""))
    if found is None:
        return None
    op, kind = found
    admission_id = op["intent"]["admission_id"] if kind == "initiative" else op["greeting"]["client_msg_id"]
    return {"call_id": op["call_id"], "opening": {"ring_id": op["ring_id"], "token": op["token"],
                                                  "epoch": op["epoch"], "kind": kind,
                                                  "admission_id": admission_id}}


def skip_opening_synthesis(trace_id: str) -> bool:
    """Best effort: do not voice an opening clip whose output authority is
    gone (retired), or an initiative that was cancelled or that a greeting
    overtook after it started. The phone's fence stays authoritative."""
    found = _opening_for_trace(str(trace_id or ""))
    if found is None:
        return False
    op, kind = found
    if op.get("retired"):
        return True
    return kind == "initiative" and (op["intent"]["state"] in ("cancelled", "unknown") or bool(op.get("late_greeting")))
