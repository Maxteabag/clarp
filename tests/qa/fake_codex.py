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
    cancel_by_turn = {}
    current_thread = str(uuid.uuid4())

    def persist(thread_id, kind, payload):
        root_env = os.environ.get("CLARP_QA_PROVIDER_ROOT")
        if not root_env:
            return
        root = Path(root_env) / "sessions"
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
        time.sleep(0.05)  # let turn/start's response establish ownership
        base = {"threadId": thread_id, "turnId": turn_id}
        emit({"method": "turn/started", "params": {
            **base, "turn": {"id": turn_id, "threadId": thread_id}}})
        text = "QA reply: " + "".join(
            item.get("text", "") for item in params.get("input", []))
        if "[qa-slow]" in text and stop.wait(3):
            emit({"method": "turn/completed", "params": {
                **base, "turn": {"id": turn_id, "status": "interrupted"}}})
            return
        for part in (text[:8], text[:16], text):
            if stop.wait(0.15):
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
        elif method == "turn/start":
            thread_id = params.get("threadId") or current_thread
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
            result = {}
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
