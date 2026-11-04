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


def emit(event):
    print(json.dumps(event), flush=True)


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
    current_thread = str(uuid.uuid4())
    goals = {}
    # BACKENDS-06b knobs. A thread Clarp unsubscribed closes (lock released,
    # thread/closed) once no turn runs on it, after CLARP_QA_UNLOAD_DELAY
    # seconds ("never": it never closes); CLARP_QA_UNSUBSCRIBE=unsupported
    # answers -32601 like an app-server without the method.
    running = {}
    unloading = set()
    state_lock = threading.Lock()
    home = os.environ.get("CODEX_HOME")
    auth_file = Path(home) / "auth.json" if home else None
    auth_tag = auth_file.read_text().strip() if auth_file and auth_file.is_file() else ""

    def record_auth(kind, thread_id):
        if os.environ.get("CLARP_QA_RECORD_AUTH"):
            record(kind, {"threadId": thread_id, "authTag": auth_tag, "pid": os.getpid()})

    def turn_running(thread_id, delta):
        with state_lock:
            running[thread_id] = running.get(thread_id, 0) + delta

    def close_when_idle(thread_id):
        delay = os.environ.get("CLARP_QA_UNLOAD_DELAY", "0.1")
        if delay == "never":
            return
        time.sleep(float(delay))
        while True:
            with state_lock:
                if thread_id not in unloading:
                    return            # subscribed again
                if not running.get(thread_id):
                    unloading.discard(thread_id)
                    lock = held_locks.pop(thread_id, None)
                    break
            time.sleep(0.02)
        if lock:
            lock.close()
        emit({"method": "thread/closed", "params": {"threadId": thread_id}})

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
        stop = threading.Event()
        cancel_by_turn[turn_id] = stop
        turn_running(thread_id, 1)
        try:
            emit({"method": "turn/started", "params": {
                "threadId": thread_id, "turn": {"id": turn_id, "threadId": thread_id}}})
            record_auth("goal_turn", thread_id)
            # CLARP_QA_GOAL_TURN_HOLD keeps the continuation running that long.
            if stop.wait(float(os.environ.get("CLARP_QA_GOAL_TURN_HOLD", "0"))):
                emit({"method": "turn/completed", "params": {"threadId": thread_id,
                      "turn": {"id": turn_id, "threadId": thread_id, "status": "interrupted"}}})
                return
            mid = str(uuid.uuid4())
            emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                  "item": {"id": mid, "type": "agentMessage", "text": "Goal step: " + goal["objective"]}}})
            goal["tokens"] += 7
            if goal["status"] == "active":   # a pause during the turn stays a pause
                goal["status"] = "complete"
            emit({"method": "thread/goal/updated", "params": {
                "threadId": thread_id, "turnId": turn_id, "goal": goal_payload(thread_id)}})
            emit({"method": "turn/completed", "params": {
                "threadId": thread_id, "turn": {"id": turn_id, "threadId": thread_id, "status": "completed"}}})
        finally:
            turn_running(thread_id, -1)

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

    def complete(params, turn_id, stop, thread_id):
        turn_running(thread_id, 1)
        try:
            _complete(params, turn_id, stop, thread_id)
        finally:
            turn_running(thread_id, -1)
        # The idle hook: an active goal continues once the thread is idle.
        if (goals.get(thread_id) or {}).get("status") == "active":
            threading.Thread(target=goal_turn, args=(thread_id,), daemon=True).start()

    def _complete(params, turn_id, stop, thread_id):
        time.sleep(0.05)  # let turn/start's response establish ownership
        base = {"threadId": thread_id, "turnId": turn_id}
        emit({"method": "turn/started", "params": {
            **base, "turn": {"id": turn_id, "threadId": thread_id}}})
        text = "QA reply: " + "".join(
            item.get("text", "") for item in params.get("input", []))
        if "[qa-subagent]" in text:
            # A subagent thread codex spawned runs while this turn runs.
            sub_thread, sub_turn = str(uuid.uuid4()), str(uuid.uuid4())
            emit({"method": "turn/started", "params": {"threadId": sub_thread,
                  "turn": {"id": sub_turn, "threadId": sub_thread}}})
            stop.wait(float(os.environ.get("CLARP_QA_SUBAGENT_HOLD", "1")))
            emit({"method": "turn/completed", "params": {"threadId": sub_thread,
                  "turn": {"id": sub_turn, "threadId": sub_thread, "status": "completed"}}})
        if "[qa-self-interrupt]" in text:
            emit({"method": "turn/completed", "params": {**base, "turn": {
                "id": turn_id, "status": "interrupted"}}})
            return
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
        emit({"method": "item/completed", "params": {**base, "item": {
            "id": turn_id, "type": "agentMessage", "text": text}}})
        persist(thread_id, "event_msg", {
            "type": "agent_message", "message": text, "phase": "final_answer"})
        emit({"method": "turn/completed", "params": {**base, "turn": {
            "id": turn_id, "status": "completed"}}})

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
            with state_lock:
                unloading.discard(thread_id)
            persist(thread_id, "session_meta", {
                "id": thread_id, "cwd": params.get("cwd", "")})
            result = {"thread": {"id": thread_id}}
            # Real codex: a resumed idle thread with an active goal continues
            # it by itself (probe 128; app-server thread_processor.rs:4214-4217).
            if (goals.get(thread_id) or {}).get("status") == "active":
                threading.Thread(target=goal_turn, args=(thread_id,), daemon=True).start()
        elif method == "thread/unsubscribe":
            thread_id = str(params.get("threadId") or "")
            if os.environ.get("CLARP_QA_UNSUBSCRIBE") == "unsupported":
                emit({"id": request["id"], "error": {
                    "code": -32601, "message": "Method not found"}})
                continue
            with state_lock:
                loaded = thread_id in held_locks
                if loaded:
                    unloading.add(thread_id)
            result = {"status": "unsubscribed" if loaded else "notLoaded"}
            if loaded:
                threading.Thread(target=close_when_idle, args=(thread_id,), daemon=True).start()
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
            record_auth("turn/start.auth", thread_id)
            cancelled = threading.Event()
            turn_id = str(uuid.uuid4())
            cancel_by_turn[turn_id] = cancelled
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
            result = {}
        elif method == "thread/goal/set":
            thread_id = params.get("threadId") or current_thread
            goal = goals.get(thread_id) or {"objective": "", "status": "active", "tokens": 0,
                                            "created": int(time.time() * 1000)}
            if params.get("objective"):
                goal["objective"] = params["objective"]
            if params.get("status"):
                goal["status"] = params["status"]
            goals[thread_id] = goal
            emit({"id": request["id"], "result": {"goal": goal_payload(thread_id)}})
            emit({"method": "thread/goal/updated", "params": {
                "threadId": thread_id, "goal": goal_payload(thread_id)}})
            with state_lock:
                busy = bool(running.get(thread_id))
            if goal["status"] == "active" and not busy:
                threading.Thread(target=goal_turn, args=(thread_id,), daemon=True).start()
            continue
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
