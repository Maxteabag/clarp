"""What one short answer costs a client on the wire, on an isolated release Host (issue 879).

A stand-in for Claude Code answers each message with a short reply streamed as Claude Code
streams it (a little thinking, then about forty text deltas over two seconds). A client that
does what the apps do listens on `/events?live=<its chat>` and, on each `transcript-updated` of
its chat, fetches the delta from `/log`, both asking for gzip. Per answer it counts the bytes
that came over the wire from the send to the end of the turn: the event stream (its HTTP
chunks, as sent), the `/log` answers (headers and body as sent) and the send's own answer. It
prints the median of several answers, split by event type, once for each way of listening:

  apps    as the apps listen today (live item ops of the open chat, a /log delta per wake-up)
  slim    with what issue 879 adds, where the Host has it: no text deltas, the stream gzipped,
          and the changed rows carried in transcript-updated (a /log only when they were not)

Run through `tools/build wire-read`.
"""
import gzip
import http.client
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import threading
import time
import zlib

import scratch

ANSWERS = 7
SESSION = "probe"

# The stand-in: Claude Code's stream-json with partial messages, for one message and then EOF.
STANDIN = r'''
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
reply = ("Yes. The build finished at 14:02 and every test passed; the release is in the queue "
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
'''


def request(address, method, path, body=None, headers=None):
    """An answer's status, headers, body as it came, and its bytes on the wire."""
    conn = http.client.HTTPConnection(address, timeout=60)
    try:
        conn.request(method, path, body, {"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        data = response.read()
        head = sum(len(f"{name}: {value}\r\n") for name, value in response.getheaders()) + 19
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), data, head + len(data)
    finally:
        conn.close()


def decoded(headers, data):
    return gzip.decompress(data) if headers.get("content-encoding") == "gzip" else data


class Stream:
    """One /events connection read off its socket, so that what is counted is what was sent."""

    def __init__(self, address, query, last_event_id):
        host, port = address.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)))
        head = (f"GET /events?{query} HTTP/1.1\r\nHost: {address}\r\nAccept: text/event-stream\r\n"
                f"Accept-Encoding: gzip\r\nCache-Control: no-store\r\n")
        if last_event_id:
            head += f"Last-Event-ID: {last_event_id}\r\n"
        self.sock.sendall((head + "\r\n").encode())
        self.lock = threading.Lock()
        self.events = []  # (wire bytes counted when it arrived, decoded event)
        self.wire = 0
        self.last_id = last_event_id
        self.gzip = None
        self.closed = False
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        raw = b""
        while b"\r\n\r\n" not in raw:
            got = self.sock.recv(65536)
            if not got:
                return
            raw += got
        head, raw = raw.split(b"\r\n\r\n", 1)
        headers = {line.split(b":", 1)[0].strip().lower(): line.split(b":", 1)[1].strip()
                   for line in head.split(b"\r\n")[1:] if b":" in line}
        if headers.get(b"content-encoding") == b"gzip":
            self.gzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
        with self.lock:
            self.wire += len(head) + 4
        text = b""
        while True:
            # One HTTP chunk at a time.
            while b"\r\n" not in raw:
                got = self.sock.recv(65536)
                if not got:
                    self.closed = True
                    return
                raw += got
            size_line, raw = raw.split(b"\r\n", 1)
            size = int(size_line.split(b";")[0], 16)
            while len(raw) < size + 2:
                got = self.sock.recv(65536)
                if not got:
                    self.closed = True
                    return
                raw += got
            chunk, raw = raw[:size], raw[size + 2:]
            with self.lock:
                self.wire += len(size_line) + 2 + size + 2
            if size == 0:
                self.closed = True
                return
            text += self.gzip.decompress(chunk) if self.gzip else chunk
            while b"\n\n" in text:
                block, text = text.split(b"\n\n", 1)
                data = b"".join(line[5:].lstrip() for line in block.split(b"\n") if line.startswith(b"data:"))
                ident = [line[3:].strip() for line in block.split(b"\n") if line.startswith(b"id:")]
                if not data:
                    continue
                event = json.loads(data)
                with self.lock:
                    if ident:
                        self.last_id = ident[-1].decode()
                    self.events.append((self.wire, event))

    def mark(self):
        with self.lock:
            return self.wire, len(self.events)

    def since(self, at):
        with self.lock:
            return self.events[at:]

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def wait(predicate, seconds, what):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError(f"no {what} within {seconds} s")


def answer(address, mode, n, cursor):
    """One message and its answer. Returns the counts and the stream's last event id."""
    features = cursor["features"]
    query = f"live={SESSION}"
    if mode == "slim":
        if "events_slim" not in features:
            return None
        query += "&text=0&rows=1&gzip=1"
    stream = Stream(address, query, cursor["event_id"])
    try:
        # The replay ends with the word to look at the snapshot; from there it is live.
        wait(lambda: any(event.get("type") == "agent-roster" and "event_id" not in event
                         for _, event in stream.since(0)), 20, "end of replay")
        time.sleep(0.3)
        start_wire, start_at = stream.mark()
        counts = {"log_calls": 0, "log_bytes": 0, "send_bytes": 0}
        status, _, _, wire = request(address, "POST", "/send", json.dumps({
            "session": SESSION, "text": f"Is the build done? ({n})", "client_msg_id": f"wire-{n}-{time.time_ns()}"}))
        if status not in (200, 202):
            raise RuntimeError(f"/send answered {status}")
        counts["send_bytes"] = wire
        seen = start_at
        ended_at = None
        while True:
            events = stream.since(seen)
            seen += len(events)
            for _, event in events:
                kind = event.get("type")
                if kind == "transcript-updated" and event.get("session") == SESSION:
                    rows = event.get("rows")
                    if (mode == "slim" and isinstance(rows, list)
                            and (event.get("rows_from") or 1 << 62) <= cursor["revision"]):
                        cursor["revision"] = max(cursor["revision"], event.get("rows_latest") or 0)
                        counts["carried"] = counts.get("carried", 0) + 1
                        continue
                    if (event.get("head_revision") or 0) and event["head_revision"] <= cursor["revision"]:
                        continue
                    path = f"/log?session={SESSION}&limit=100&include_automated=1&include_tool_details=0&after_revision={cursor['revision']}"
                    status, headers, data, wire = request(address, "GET", path, headers={"Accept-Encoding": "gzip"})
                    if status != 200:
                        raise RuntimeError(f"/log answered {status}")
                    counts["log_calls"] += 1
                    counts["log_bytes"] += wire
                    cursor["revision"] = max(cursor["revision"], json.loads(decoded(headers, data))["latest_revision"])
                if kind == "agent-state" and event.get("session") == SESSION and event.get("kind") in ("done", "idle"):
                    ended_at = ended_at or time.monotonic()
            # What the Host sends after the turn's end (its last rows, the clip, a notice) counts too.
            if ended_at and time.monotonic() - ended_at > 2.0:
                break
            if stream.closed:
                raise RuntimeError("the event stream closed")
            time.sleep(0.02)
        end_wire, end_at = stream.mark()
        counts["events_bytes"] = end_wire - start_wire
        by_type = {}
        previous = start_wire
        for wire, event in stream.since(start_at)[: end_at - start_at]:
            kind = event.get("type", "?")
            if kind == "live":
                ops = {op.get("op") + ("." + op.get("field") if op.get("field") else "") for op in event.get("ops", [])}
                kind = "live " + "+".join(sorted(ops))
            by_type.setdefault(kind, [0, 0])
            by_type[kind][0] += 1
            by_type[kind][1] += wire - previous
            previous = wire
        counts["by_type"] = by_type
        counts["total"] = counts["events_bytes"] + counts["log_bytes"] + counts["send_bytes"]
        cursor["event_id"] = stream.last_id
        return counts
    finally:
        stream.close()


def measure(address):
    _, _, data, _ = request(address, "GET", "/server-info")
    info = json.loads(data)
    features = set(info.get("features") or (info.get("capabilities") or {}).get("features") or [])
    status, _, data, _ = request(address, "POST", "/agents", json.dumps({"name": "Probe", "backend": "claude"}))
    if status not in (200, 201):
        raise RuntimeError(f"POST /agents answered {status}: {data[:200]!r}")
    for mode in ("apps", "slim"):
        cursor = {"event_id": None, "revision": 0, "features": features}
        _, headers, data, _ = request(address, "GET", f"/log?session={SESSION}&limit=100", headers={"Accept-Encoding": "gzip"})
        cursor["revision"] = json.loads(decoded(headers, data))["latest_revision"]
        results = []
        for n in range(ANSWERS):
            counts = answer(address, mode, n, cursor)
            if counts is None:
                print(f"{mode}: this Host has no feature events_slim", flush=True)
                break
            results.append(counts)
        if not results:
            continue
        # The first answer of a chat is not a short answer of a running chat: leave it out.
        kept = results[1:] if len(results) > 2 else results
        med = lambda key: statistics.median(r[key] for r in kept)
        print(f"{mode}: {med('total') / 1000:.1f} KB an answer (median of {len(kept)}): "
              f"events {med('events_bytes') / 1000:.1f} KB, /log {med('log_bytes') / 1000:.1f} KB in "
              f"{med('log_calls'):.0f} calls, /send {med('send_bytes') / 1000:.1f} KB", flush=True)
        kinds = sorted({k for r in kept for k in r["by_type"]})
        for kind in kinds:
            count = statistics.median(r["by_type"].get(kind, [0, 0])[0] for r in kept)
            size = statistics.median(r["by_type"].get(kind, [0, 0])[1] for r in kept)
            print(f"  {kind}: {count:.0f} events, {size / 1000:.2f} KB", flush=True)


def main(binary):
    with scratch.directory("clarp-wire-read-") as name:
        root = Path(name)
        standin = root / "claude-standin"
        standin.write_text(f"#!{sys.executable}\n{STANDIN}")
        standin.chmod(0o755)
        cmd = [binary, "--listen", "127.0.0.1:0", "--data", root / "data", "--run", root / "run",
               "--cwd", root, "--engine", "sqlite", "--accounts", "none"]
        env = {"HOME": name, "TMPDIR": name, "PATH": str(root / "no-programs"),
               "CLARP_HEARTBEATS_DISABLED": "1", "CLARP_CLAUDE_PROGRAM": str(standin)}
        with subprocess.Popen(cmd, cwd=root, env=env, stderr=subprocess.PIPE, text=True) as host:
            try:
                prefix = "clarp-host listens on "
                startup = host.stderr.readline().strip()
                if not startup.startswith(prefix):
                    raise RuntimeError("scratch Host failed to listen")
                threading.Thread(target=lambda: [None for _ in host.stderr], daemon=True).start()
                measure(startup[len(prefix):])
            finally:
                host.kill()


if __name__ == "__main__":
    # The runner creates the network namespace. Hide the manager sockets under /run
    # and other processes under /proc before starting even the disposable Host.
    if os.getpid() != 1:
        os.execvp("unshare", ["unshare", "--mount", "--pid", "--fork", "--kill-child",
                             "--mount-proc", "sh", "-ec",
                             'mount -t tmpfs tmpfs /run; exec "$@"', "wire-read",
                             sys.executable, __file__, *sys.argv[1:]])
    if socket.if_nameindex() != [(1, "lo")] or any(Path("/run").iterdir()):
        raise SystemExit("run through tools/build wire-read in its private namespaces")
    main(Path(sys.argv[1]).resolve())
