#!/usr/bin/env python3
"""Deterministic provider process; the real dispatcher/runner own everything else."""
import json
import sys
import time
import threading
import uuid
import os
from pathlib import Path
from datetime import datetime, timezone


def record(method, params):
    """What a turn actually received, for tests of Host-supplied context."""
    root = os.environ.get("CLARP_QA_PROVIDER_ROOT")
    if root:
        with (Path(root) / "turn-requests.jsonl").open("a") as log:
            log.write(json.dumps({"method": method, **params}) + "\n")


_EMIT_LOCK = threading.Lock()


def emit(event):
    # One whole line per event: several threads write to stdout.
    with _EMIT_LOCK:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()


def _thread_lock(thread_id: str):
    """Exclusive flock when CODEX_HOME is set, matching real Codex writer locks."""
    home = os.environ.get("CODEX_HOME")
    if not home or not thread_id:
        return None
    directory = Path(home) / "thread-writer-locks"
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / f"{thread_id}.lock", "a")
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    return handle


def steer_modes():
    """Opt-in steer behaviours (all off by default): CLARP_QA_STEER_MODE or the
    provider root's ``steer-mode`` file, comma separated, read per call.

    late, mid, reopen, commentary-only: where a steer is drained relative to
      the turn's final answer (mirroring codex 0.159.2's pending-input drain
      before sampling and its leftover drain in on_task_finished);
    item-before-ack: the UserMessage item is emitted before the RPC answer;
    ack-error, ack-mismatch: the RPC answer;
    ack-after-terminal=ok|error|mismatch|never|exit: the turn completes
      without consuming the steer, then the RPC answer follows (or never
      comes, or the app-server exits while it is outstanding);
    ack-delay=SECONDS: the steer is queued; its RPC answer comes that late;
    consume (with ack-after-terminal): the steer is still drained;
    hold-after-steer=SECONDS: wait that long before draining a held steer;
    no-client-id: UserMessage items carry no clientId;
    restart: the app-server process exits once a steer is held.
    """
    raw = os.environ.get("CLARP_QA_STEER_MODE", "")
    root = os.environ.get("CLARP_QA_PROVIDER_ROOT")
    if root and (Path(root) / "steer-mode").is_file():
        raw = (Path(root) / "steer-mode").read_text()
    modes = {}
    for token in raw.replace("\n", ",").split(","):
        token = token.strip()
        if token:
            key, _, value = token.partition("=")
            modes[key] = value or True
    return modes


def app_server():
    """JSON-RPC stdio app-server with per-thread flock and per-turn cancel.

    Same-process ``thread/resume`` of a thread this process already holds
    succeeds (real Codex). A second process against that thread fails with
    ``-32600 already has an active writer``. Each ``thread/start`` without
    a threadId mints a new id so one process can own many threads.
    """
    held_locks = {}
    quota_file = Path(os.environ.get("CLARP_QA_PROVIDER_ROOT", "/nonexistent")) / "quota-mode"
    quota_mode = quota_file.read_text().strip() if quota_file.exists() else ""
    cancel_by_turn = {}
    # Steered input per turn, drained into UserMessage items like Codex does.
    pending_steers = {}
    steered_turns = set()
    steer_arrived = {}
    steer_answers = {}
    current_thread_for = {}
    write_lock = threading.Lock()
    current_thread = str(uuid.uuid4())
    goals = {}

    def goal_payload(thread_id):
        goal = goals.get(thread_id)
        if goal is None:
            return None
        return {"threadId": thread_id, "objective": goal["objective"], "status": goal["status"],
                "tokenBudget": None, "tokensUsed": goal["tokens"], "timeUsedSeconds": 1,
                "createdAt": goal["created"], "updatedAt": int(time.time() * 1000)}

    def goal_turn(thread_id):
        """Codex continues an active goal with a turn nobody requested."""
        time.sleep(0.05)
        goal = goals.get(thread_id)
        if goal is None or goal["status"] != "active":
            return
        turn_id = str(uuid.uuid4())
        emit({"method": "turn/started", "params": {
            "threadId": thread_id, "turn": {"id": turn_id, "threadId": thread_id}}})
        mid = str(uuid.uuid4())
        emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
              "item": {"id": mid, "type": "agentMessage", "text": "Goal step: " + goal["objective"]}}})
        goal["tokens"] += 7
        goal["status"] = "complete"
        emit({"method": "thread/goal/updated", "params": {
            "threadId": thread_id, "turnId": turn_id, "goal": goal_payload(thread_id)}})
        emit({"method": "turn/completed", "params": {
            "threadId": thread_id, "turn": {"id": turn_id, "threadId": thread_id, "status": "completed"}}})

    def sessions_root():
        return Path(os.environ.get("CLARP_QA_PROVIDER_ROOT", "/nonexistent")) / "sessions"

    def persist(thread_id, kind, payload):
        root_env = os.environ.get("CLARP_QA_PROVIDER_ROOT")
        if not root_env:
            return
        root = sessions_root()
        root.mkdir(parents=True, exist_ok=True)
        with (root / f"rollout-{thread_id}.jsonl").open("a") as stream:
            stream.write(json.dumps({
                "type": kind,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }) + "\n")

    def acquire(thread_id):
        existing = held_locks.get(thread_id)
        if existing is not None:
            return existing
        lock = _thread_lock(thread_id)
        if lock is False:
            return False
        if lock is not None:
            held_locks[thread_id] = lock
        return lock

    def drain_steers(turn_id, thread_id, modes):
        """Emit the turn's pending steered input as UserMessage items."""
        drained = pending_steers.pop(turn_id, [])
        base = {"threadId": thread_id, "turnId": turn_id}
        for client_id, text in drained:
            item = {"id": str(uuid.uuid4()), "type": "userMessage",
                    "content": [{"type": "text", "text": text}]}
            if client_id and not modes.get("no-client-id"):
                item["clientId"] = client_id
            emit({"method": "item/started", "params": {**base, "item": item}})
            emit({"method": "item/completed", "params": {**base, "item": item}})
        return drained

    def hold_for_steer(turn_id, seconds=10.0):
        arrived = steer_arrived.setdefault(turn_id, threading.Event())
        released = arrived.wait(seconds)
        record("qa/steer-hold", {"turnId": turn_id, "released": released})
        return released

    def complete(params, turn_id, stop, thread_id):
        time.sleep(0.05)  # let turn/start's response establish ownership
        base = {"threadId": thread_id, "turnId": turn_id}
        emit({"method": "turn/started", "params": {
            **base, "turn": {"id": turn_id, "threadId": thread_id}}})
        text = "QA reply: " + "".join(
            item.get("text", "") for item in params.get("input", []))
        if quota_mode in ("stale", "blocked", "unknown-blocked", "partial"):
            if quota_mode == "stale":
                quota_file.unlink(missing_ok=True)  # only a NEW process recovers
            emit({"method": "account/rateLimits/updated", "params": {
                "rateLimits": {"limitId": "premium", "primary": {"usedPercent": 100}}}})
            if quota_mode == "partial":
                emit({"method": "item/started", "params": {**base, "item": {
                    "id": turn_id, "type": "commandExecution", "command": "fixture"}}})
            emit({"method": "turn/completed", "params": {**base, "turn": {
                "id": turn_id, "status": "failed", "error": {"message": "You've hit your usage limit."}}}})
            return
        if "[qa-slow]" in text and stop.wait(3):
            emit({"method": "turn/completed", "params": {
                **base, "turn": {"id": turn_id, "status": "interrupted"}}})
            return
        # CLARP_QA_STREAM_DELAY paces the partial replies (seconds), so a client
        # can observe a reply while it streams; 0.15 by default.
        step_delay = float(os.environ.get("CLARP_QA_STREAM_DELAY", "0.15"))
        for part in (text[:8], text[:16], text):
            if stop.wait(step_delay):
                emit({"method": "turn/completed", "params": {
                    **base, "turn": {"id": turn_id, "status": "interrupted"}}})
                return
            emit({"method": "item/updated", "params": {**base, "item": {
                "id": turn_id, "type": "agentMessage", "text": part}}})
        modes = steer_modes()
        steering = bool(set(modes) & {"late", "mid", "reopen", "commentary-only", "restart",
                                      "ack-after-terminal"})
        # Input steered before the final is drained first and answered by it
        # (codex drains pending input before each sampling request).
        if drain_steers(turn_id, thread_id, modes) or turn_id in steered_turns:
            steering = True
        final = {"id": turn_id, "type": "agentMessage", "text": text}
        if steering:
            final["phase"] = "final_answer"   # codex 0.159 marks the answer
        emit({"method": "item/completed", "params": {**base, "item": final}})
        persist(thread_id, "event_msg", {
            "type": "agent_message", "message": text, "phase": "final_answer"})
        if set(modes) & {"late", "reopen", "commentary-only", "restart"} and hold_for_steer(turn_id):
            if modes.get("hold-after-steer"):
                time.sleep(float(modes["hold-after-steer"]))   # a window mid-turn
            if modes.get("restart"):
                record("qa/restart", {"turnId": turn_id})
                os._exit(3)           # the app-server dies mid-turn
            drain_steers(turn_id, thread_id, modes)
            if modes.get("reopen"):
                # Input drained before the turn ends reopens it: a new answer.
                emit({"method": "item/completed", "params": {**base, "item": {
                    "id": str(uuid.uuid4()), "type": "agentMessage",
                    "text": "QA second answer", "phase": "final_answer"}}})
            elif modes.get("commentary-only"):
                emit({"method": "item/completed", "params": {**base, "item": {
                    "id": str(uuid.uuid4()), "type": "agentMessage",
                    "text": "QA commentary", "phase": "commentary"}}})
            # "late": leftover input recorded by on_task_finished, no reply.
        elif modes.get("ack-after-terminal"):
            hold_for_steer(turn_id)   # the steer arrives but is not consumed
        emit({"method": "turn/completed", "params": {**base, "turn": {
            "id": turn_id, "status": "completed"}}})
        answer = steer_answers.pop(turn_id, None)
        if answer is not None:
            answer()

    for line in sys.stdin:
        request = json.loads(line)
        method, params = request.get("method"), request.get("params") or {}
        if "id" not in request:
            continue
        result = {}
        if method == "initialize":
            result = {"serverInfo": {"name": "fake-codex", "version": "0"}}
        elif method == "account/rateLimits/read":
            result = {"rateLimitsByLimitId": {
                "codex": {"limitId": "codex", "primary": {"usedPercent": 2}},
                **({"premium": {"limitId": "premium", "primary": {"usedPercent": 100}}}
                   if quota_mode == "blocked" else {}),
            }}
        elif method == "thread/start":
            thread_id = params.get("threadId") or str(uuid.uuid4())
            current_thread = thread_id
            lock = acquire(thread_id)
            if lock is False:
                emit({"id": request["id"], "error": {
                    "code": -32600,
                    "message": f"thread {thread_id} already has an active writer",
                }})
                continue
            persist(thread_id, "session_meta", {
                "id": thread_id, "cwd": params.get("cwd", "")})
            result = {"thread": {"id": thread_id}}
        elif method == "thread/resume":
            thread_id = params.get("threadId") or current_thread
            record(method, {"threadId": thread_id})
            rollout = sessions_root() / f"rollout-{thread_id}.jsonl"
            if rollout.is_file() and '"session_meta"' not in rollout.read_text():
                # Real Codex on a transit rollout without session_meta (Miso,
                # 2026-10-10): the thread store cannot read it.
                emit({"id": request["id"], "error": {
                    "code": -32603,
                    "message": ("failed to read thread: thread-store internal error: "
                                f"failed to read session metadata {rollout}: "
                                f"rollout {rollout} is empty"),
                }})
                continue
            current_thread = thread_id
            lock = acquire(thread_id)
            if lock is False:
                emit({"id": request["id"], "error": {
                    "code": -32600,
                    "message": f"thread {thread_id} already has an active writer",
                }})
                continue
            persist(thread_id, "session_meta", {
                "id": thread_id, "cwd": params.get("cwd", "")})
            result = {"thread": {"id": thread_id}}
        elif method == "thread/fork":
            # Real Codex forks a thread another process is writing (checked
            # against codex-cli 0.159): no writer lock, a copy under a new id.
            source = str(params.get("threadId") or "")
            root = Path(os.environ.get("CLARP_QA_PROVIDER_ROOT", "/nonexistent")) / "sessions"
            rollout = root / f"rollout-{source}.jsonl"
            if not source or not rollout.is_file():
                emit({"id": request["id"], "error": {
                    "code": -32600, "message": f"no rollout found for thread id {source}"}})
                continue
            thread_id = str(uuid.uuid4())
            (root / f"rollout-{thread_id}.jsonl").write_text(rollout.read_text())
            result = {"thread": {"id": thread_id, "forkedFromId": source}}
        elif method == "turn/start":
            thread_id = params.get("threadId") or current_thread
            record("turn/start", params)
            cancelled = threading.Event()
            turn_id = str(uuid.uuid4())
            cancel_by_turn[turn_id] = cancelled
            current_thread_for[turn_id] = thread_id
            result = {"turn": {"id": turn_id, "threadId": thread_id}}
            persist(thread_id, "event_msg", {"type": "user_message",
                "message": "".join(
                    item.get("text", "") for item in params.get("input", []))})
        elif method == "turn/interrupt":
            turn_id = str(params.get("turnId") or params.get("turn_id") or "")
            ev = cancel_by_turn.get(turn_id)
            if ev is not None:
                ev.set()
        elif method == "turn/steer":
            record("turn/steer", params)
            turn_id = str(params.get("expectedTurnId") or "")
            client_id = str(params.get("clientUserMessageId") or "")
            text = "".join(item.get("text", "") for item in params.get("input", []))
            modes = steer_modes()
            if modes.get("ack-error"):
                emit({"id": request["id"], "error": {
                    "code": -32600, "message": f"no active turn to steer: {turn_id}"}})
                continue
            if modes.get("ack-after-terminal"):
                outcome = str(modes["ack-after-terminal"])
                rid = request["id"]

                def answer(rid=rid, outcome=outcome, turn_id=turn_id):
                    if outcome == "ok":
                        emit({"id": rid, "result": {"turnId": turn_id}})
                    elif outcome == "ok-no-turn-id":
                        emit({"id": rid, "result": {}})   # an older codex answer
                    elif outcome == "mismatch":
                        emit({"id": rid, "result": {"turnId": "turn-elsewhere"}})
                    elif outcome == "error":
                        emit({"id": rid, "error": {"code": -32600,
                                                    "message": "expected turn is no longer active"}})
                    elif outcome == "exit":
                        os._exit(4)   # the reader is lost while the RPC is outstanding
                    # "never": no answer; the Host's own RPC timeout ends it.
                steer_answers[turn_id] = answer
                steered_turns.add(turn_id)
                if modes.get("consume"):
                    # The input is drained (consumed); only the answer is late.
                    pending_steers.setdefault(turn_id, []).append((client_id, text))
                steer_arrived.setdefault(turn_id, threading.Event()).set()
                continue
            steered_turns.add(turn_id)
            pending_steers.setdefault(turn_id, []).append((client_id, text))
            if modes.get("ack-delay"):
                # The RPC stays outstanding for a while (a Stop can race it).
                rid, delay = request["id"], float(modes["ack-delay"])

                def late_ack(rid=rid, delay=delay, turn_id=turn_id):
                    time.sleep(delay)
                    emit({"id": rid, "result": {"turnId": turn_id}})
                threading.Thread(target=late_ack, daemon=True).start()
                steer_arrived.setdefault(turn_id, threading.Event()).set()
                continue
            if modes.get("item-before-ack"):
                # The turn loop can drain the input before the RPC answer.
                drain_steers(turn_id, current_thread_for.get(turn_id, current_thread), modes)
            steer_arrived.setdefault(turn_id, threading.Event()).set()
            result = {"turnId": "turn-elsewhere" if modes.get("ack-mismatch") else turn_id}
        elif method == "thread/goal/set":
            thread_id = params.get("threadId") or current_thread
            goal = goals.get(thread_id) or {"objective": "", "status": "active", "tokens": 0,
                                            "created": int(time.time() * 1000)}
            if params.get("objective"):
                goal["objective"] = params["objective"]
            if params.get("status"):
                goal["status"] = params["status"]
            goals[thread_id] = goal
            result = {"goal": goal_payload(thread_id)}
            emit({"method": "thread/goal/updated", "params": {
                "threadId": thread_id, "goal": goal_payload(thread_id)}})
            if goal["status"] == "active":
                threading.Thread(target=goal_turn, args=(thread_id,), daemon=True).start()
        elif method == "thread/goal/get":
            thread_id = params.get("threadId") or current_thread
            result = {"goal": goal_payload(thread_id)}
        elif method == "thread/goal/clear":
            thread_id = params.get("threadId") or current_thread
            result = {"cleared": goals.pop(thread_id, None) is not None}
            emit({"method": "thread/goal/cleared", "params": {"threadId": thread_id}})
        emit({"id": request["id"], "result": result})
        if method == "turn/start":
            threading.Thread(
                target=complete,
                args=(params, turn_id, cancelled, thread_id),
                daemon=True,
            ).start()


if __name__ == "__main__":
    if "app-server" in sys.argv:
        app_server()
        raise SystemExit(0)
    prompt = sys.argv[-1]
    args = sys.argv[1:]
    session = args[args.index("resume") + 1] if "resume" in args else str(uuid.uuid4())
    emit({"type": "thread.started", "thread_id": session})
    emit({"type": "turn.started"})
    reply = "QA reply: " + prompt
    mid = str(uuid.uuid4())
    for part in (reply[:8], reply[:16], reply):
        emit({"type": "item.updated", "item": {"id": mid, "type": "agent_message", "text": part}})
        time.sleep(0.15)
    emit({"type": "item.completed", "item": {"id": mid, "type": "agent_message", "text": reply}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 10}})
