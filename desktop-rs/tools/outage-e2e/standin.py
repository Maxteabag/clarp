# A stand-in for Claude Code (stream-json with partial messages) for one message, then EOF:
# it answers "Reply to [<the question>]. ..." over about three seconds, so no model is asked.

import json, sys, time, uuid
args = sys.argv[1:]
session = None
for flag in ("--resume", "--session-id"):
    if flag in args:
        session = args[args.index(flag) + 1]
session = session or str(uuid.uuid4())
if "--version" in args:
    print("2.9.0 (stand-in)"); sys.exit(0)
def say(**event):
    print(json.dumps({**event, "session_id": session}), flush=True)
def stream(event):
    say(type="stream_event", event=event, parent_tool_use_id=None)
line = sys.stdin.readline()
say(type="system", subtype="init", model="stand-in", cwd=".", tools=["Bash"])
mid = "msg_" + uuid.uuid4().hex[:12]
stream({"type": "message_start", "message": {"id": mid, "role": "assistant", "model": "stand-in"}})
thought = "The user asks a short question. I answer it in a few sentences without tools."
stream({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
for n in range(0, len(thought), 16):
    stream({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": thought[n:n + 16]}})
    time.sleep(0.03)
stream({"type": "content_block_stop", "index": 0})
def asked(raw):
    try:
        content = json.loads(raw)["message"]["content"]
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        return str(content).strip()[:80]
    except Exception:
        return "?"
reply = ("Reply to [" + asked(line) + "]. " "Yes. The build finished at 14:02 and every test passed; the release is in the queue "
         "and should be installed within the hour. Nothing else is waiting for you, and I will "
         "tell you when the install is done. If you want it sooner, say so and I will ask the "
         "release owner to move it to the front of the queue.")
words = reply.split(" ")
stream({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
for n in range(0, len(words), 2):
    piece = " ".join(words[n:n + 2]) + (" " if n + 2 < len(words) else "")
    stream({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": piece}})
    time.sleep(0.06)
stream({"type": "content_block_stop", "index": 1})
say(type="assistant", message={"id": mid, "role": "assistant", "model": "stand-in",
    "content": [{"type": "thinking", "thinking": thought}, {"type": "text", "text": reply}],
    "usage": {"input_tokens": 1200, "output_tokens": 80}})
stream({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 80}})
stream({"type": "message_stop"})
say(type="result", subtype="success", is_error=False, result=reply, num_turns=1, duration_ms=2600,
    usage={"input_tokens": 1200, "output_tokens": 80})
