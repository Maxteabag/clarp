#!/usr/bin/env python3
import json
import pathlib
import signal
import sys
import time

if "--version" in sys.argv:
    print("2.1.320 (Claude Code)")
    raise SystemExit(0)

def after(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None

sid = after("--session-id") or after("--resume") or "stand-in"
root = pathlib.Path(__file__).resolve().parent
incoming = json.loads(sys.stdin.readline())
prompt = incoming["message"]["content"][0]["text"]
whose = "fork" if "You are a short-lived fork of your own session." in prompt else "main"
said = next((line for line in prompt.splitlines() if line.startswith("U:")), "")
with (root / "launches").open("a") as lines:
    lines.write(json.dumps({"whose": whose, "said": said, "session": sid}) + "\n")

def stopped(*_):
    (root / f"{whose}-gone").write_text("stopped")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stopped)
# Claude Code is asked to stop with an interrupt first.
signal.signal(signal.SIGINT, stopped)

def send(kind, **fields):
    print(json.dumps({"type": kind, "session_id": sid, **fields}), flush=True)

if whose == "fork" and "ADMISSION" in said:
    (root / "fork-ready").touch()
    while not (root / "release-fork").exists() and not (root / "finish").exists():
        time.sleep(0.01)
    if "FAIL" in said:
        raise SystemExit(41)
send("system", subtype="init")
if whose == "main" and 'The fork replied: "Here, from the fork."' in prompt:
    (root / "fork-note-read").write_text(sid)
if whose == "fork" and "QUIET" not in prompt:
    # Streamed, as Claude Code streams: the reply is whole at its message_stop.
    def event(body):
        send("stream_event", event=body)
    event({"type": "message_start", "message": {"id": "msg_fork"}})
    event({"type": "content_block_start", "index": 0,
           "content_block": {"type": "text", "text": "Here, from the fork."}})
    event({"type": "message_stop"})
(root / f"{whose}-running").write_text(sid)
for _ in range(3000):
    if (root / "finish").exists() or (root / f"finish-{whose}").exists():
        break
    if whose == "fork" and (root / "fail-after-read").exists():
        (root / "failed-after-read").touch()
        raise SystemExit(42)
    time.sleep(0.02)
send("result", subtype="success", is_error=False, result="done")
(root / f"{whose}-gone").write_text("finished")
