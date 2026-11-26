"""What one short answer costs a client on the wire (issue 879), and whether a gzipped event
stream comes event by event.

A stand-in for Claude Code answers each message with a short reply streamed as Claude Code
streams it (a little thinking, then about forty text deltas over two seconds). A client that
does what the apps do listens on `/events?live=<its chat>` and, on each `transcript-updated` of
its chat that it does not already hold, fetches the delta from `/log` with the apps' options
(`include_automated=0&include_tool_details=0&coordination=auto`, issue 928), asking for gzip throughout. Per answer it counts
the bytes that came over the wire from the send to two seconds after the turn's end: the event
stream (its HTTP chunks as sent), the `/log` answers (headers and body as sent) and the send's
answer. It prints the median of several answers, split by event type, for each way of listening
the Host offers:

  apps    as the apps listen today
  gzip    the same with `gzip=1` (feature `events_gzip`)
  rows    `gzip=1&rows=1`: rows carried in `transcript-updated` (feature `events_rows`)

A gzipped connection is watched by a plain one opened beside it: each event's arrival on both is
compared, so a stream that waited for a later event shows as lag. Then it opens connections that
take gzip and reads the Host's resident memory, for what one costs.

Run through `tools/build wire-read` (an isolated release Host on the build machine). With
`--remote BASE TOKEN SESSION` it measures arrival only, against a Host already running elsewhere
(over the tailnet, or through the relay at `https://<relay>/h/<host id>`), whose agent SESSION
answers as the stand-in does.
"""
import gzip
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import zlib

# What a client asks with to be sent an answer gzipped also from this machine: a Host answers a
# client on loopback that takes plain with plain (issue 932).
GZIP_ONLY = "gzip, identity;q=0"
ANSWERS = 7
SESSION = "probe"
HELD_OPEN = 40

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


class Target:
    """Where the Host is: its address, whether over TLS, a path before every route (a relay's
    `/h/<host id>`) and the token to present."""

    def __init__(self, base, token=""):
        parts = urlsplit(base if "://" in base else f"http://{base}")
        self.tls = parts.scheme == "https"
        self.host = parts.hostname
        self.port = parts.port or (443 if self.tls else 80)
        self.prefix = parts.path.rstrip("/")
        self.token = token

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=60)
        if self.tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        return sock

    def request(self, method, path, body=None, headers=None):
        """An answer's status, headers, body as it came, and its bytes on the wire."""
        kind = http.client.HTTPSConnection if self.tls else http.client.HTTPConnection
        conn = kind(self.host, self.port, timeout=60)
        try:
            conn.request(method, self.prefix + path, body,
                         {"Content-Type": "application/json", **self.headers(), **(headers or {})})
            response = conn.getresponse()
            data = response.read()
            head = sum(len(f"{name}: {value}\r\n") for name, value in response.getheaders()) + 19
            return (response.status, {k.lower(): v for k, v in response.getheaders()}, data,
                    head + len(data))
        finally:
            conn.close()


def decoded(headers, data):
    return gzip.decompress(data) if headers.get("content-encoding") == "gzip" else data


def event_key(event):
    """What names one event on two connections: its number, or a live event's place."""
    if event.get("event_id"):
        return ("id", event["event_id"])
    if event.get("type") == "live":
        return ("live", event.get("conv"), event.get("lseq"))
    return None


class Stream:
    """One /events connection read off its socket, so that what is counted is what was sent,
    each event with the moment it could be read."""

    def __init__(self, target, query, last_event_id):
        self.sock = target.connect()
        head = (f"GET {target.prefix}/events?{query} HTTP/1.1\r\nHost: {target.host}\r\n"
                f"Accept: text/event-stream\r\nAccept-Encoding: gzip\r\nCache-Control: no-store\r\n")
        for name, value in target.headers().items():
            head += f"{name}: {value}\r\n"
        if last_event_id:
            head += f"Last-Event-ID: {last_event_id}\r\n"
        self.sock.sendall((head + "\r\n").encode())
        self.lock = threading.Lock()
        self.events = []  # (wire bytes counted when it arrived, arrival, decoded event)
        self.chunks = 0
        self.wire = 0
        self.last_id = last_event_id
        self.gzip = None
        self.closed = False
        threading.Thread(target=self.read, daemon=True).start()

    def recv(self, raw):
        got = self.sock.recv(65536)
        if not got:
            self.closed = True
            raise EOFError
        return raw + got

    def read(self):
        try:
            self.read_all()
        except (EOFError, OSError, ValueError):
            self.closed = True

    def read_all(self):
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw = self.recv(raw)
        head, raw = raw.split(b"\r\n\r\n", 1)
        headers = {line.split(b":", 1)[0].strip().lower(): line.split(b":", 1)[1].strip()
                   for line in head.split(b"\r\n")[1:] if b":" in line}
        self.encoding = headers.get(b"content-encoding", b"").decode()
        if self.encoding == "gzip":
            self.gzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
        chunked = headers.get(b"transfer-encoding", b"").lower() == b"chunked"
        with self.lock:
            self.wire += len(head) + 4
        text = b""
        while True:
            if chunked:
                while b"\r\n" not in raw:
                    raw = self.recv(raw)
                size_line, raw = raw.split(b"\r\n", 1)
                size = int(size_line.split(b";")[0], 16)
                while len(raw) < size + 2:
                    raw = self.recv(raw)
                chunk, raw = raw[:size], raw[size + 2:]
                framing = len(size_line) + 4
                if size == 0:
                    self.closed = True
                    return
            else:
                if not raw:
                    raw = self.recv(raw)
                chunk, raw, framing = raw, b"", 0
            arrived = time.monotonic()
            with self.lock:
                self.wire += framing + len(chunk)
                self.chunks += 1
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
                    self.events.append((self.wire, arrived, event))

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


def replayed(stream):
    return any(event.get("type") == "agent-roster" and "event_id" not in event
               for _, _, event in stream.since(0))


def lags(plain, packed):
    """How much later each event came on the gzipped connection than on the plain one, in ms."""
    seen = {}
    for _, arrived, event in plain:
        key = event_key(event)
        if key:
            seen.setdefault(key, arrived)
    return [(arrived - seen[key]) * 1000 for _, arrived, event in packed
            if (key := event_key(event)) in seen]


# How the apps read /log (issue 928): no automated rows, coordination as the chat says.
APP_LOG_OPTIONS = "include_automated=0&include_tool_details=0&coordination=auto"
QUERIES = {"apps": "", "gzip": "&gzip=1", "rows": "&gzip=1&rows=1&rows_automated=0&rows_coordination=auto"}
NEEDS = {"apps": None, "gzip": "events_gzip", "rows": "events_rows_options"}


def answer(target, session, mode, n, cursor):
    """One message and its answer: what it cost, and how late a gzipped stream was."""
    query = f"live={session}" + QUERIES[mode]
    stream = Stream(target, query, cursor["event_id"])
    witness = Stream(target, f"live={session}", cursor["event_id"]) if mode != "apps" else None
    try:
        wait(lambda: replayed(stream) and (witness is None or replayed(witness)), 20, "end of replay")
        time.sleep(0.3)
        start_wire, start_at = stream.mark()
        _, witness_at = witness.mark() if witness else (0, 0)
        counts = {"log_calls": 0, "log_bytes": 0, "send_bytes": 0, "carried": 0}
        status, _, _, wire = target.request("POST", "/send", json.dumps({
            "session": session, "text": f"Is the build done? ({n})", "client_msg_id": f"wire-{n}-{time.time_ns()}"}))
        if status not in (200, 202):
            raise RuntimeError(f"/send answered {status}")
        counts["send_bytes"] = wire
        seen = start_at
        ended_at = None
        while True:
            events = stream.since(seen)
            seen += len(events)
            for _, _, event in events:
                kind = event.get("type")
                if kind == "transcript-updated" and event.get("session") == session:
                    rows = event.get("rows")
                    if (isinstance(rows, list)
                            and (event.get("rows_from") or 1 << 62) <= cursor["revision"]):
                        cursor["revision"] = max(cursor["revision"], event.get("rows_latest") or 0)
                        counts["carried"] += 1
                        continue
                    if (event.get("head_revision") or 0) and event["head_revision"] <= cursor["revision"]:
                        continue
                    path = (f"/log?session={session}&limit=100&{APP_LOG_OPTIONS}"
                            f"&after_revision={cursor['revision']}")
                    status, headers, data, wire = target.request("GET", path, headers={"Accept-Encoding": GZIP_ONLY})
                    if status != 200:
                        raise RuntimeError(f"/log answered {status}")
                    counts["log_calls"] += 1
                    counts["log_bytes"] += wire
                    cursor["revision"] = max(cursor["revision"], json.loads(decoded(headers, data))["latest_revision"])
                if kind == "agent-state" and event.get("session") == session and event.get("kind") in ("done", "idle"):
                    ended_at = ended_at or time.monotonic()
            # What the Host sends after the turn's end (its last rows, the clip, a notice) counts too.
            if ended_at and time.monotonic() - ended_at > 2.0:
                break
            if stream.closed:
                raise RuntimeError("the event stream closed")
            time.sleep(0.02)
        end_wire, end_at = stream.mark()
        counts["events_bytes"] = end_wire - start_wire
        sent = stream.since(start_at)[: end_at - start_at]
        by_type = {}
        previous = start_wire
        for wire, _, event in sent:
            kind = event.get("type", "?")
            if kind == "live":
                ops = {op.get("op") + ("." + op.get("field") if op.get("field") else "") for op in event.get("ops", [])}
                kind = "live " + "+".join(sorted(ops))
            by_type.setdefault(kind, [0, 0])
            by_type[kind][0] += 1
            by_type[kind][1] += wire - previous
            previous = wire
        counts["by_type"] = by_type
        counts["events"] = len(sent)
        counts["total"] = counts["events_bytes"] + counts["log_bytes"] + counts["send_bytes"]
        counts["encoding"] = getattr(stream, "encoding", "")
        if witness:
            counts["lags"] = lags(witness.since(witness_at), sent)
        cursor["event_id"] = stream.last_id
        return counts
    finally:
        stream.close()
        if witness:
            witness.close()


def latest_revision(target, session):
    _, headers, data, _ = target.request("GET", f"/log?session={session}&limit=100", headers={"Accept-Encoding": "gzip"})
    return json.loads(decoded(headers, data))["latest_revision"]


def report(mode, results):
    # The first answer of a chat is not a short answer of a running chat: leave it out.
    kept = results[1:] if len(results) > 2 else results
    med = lambda key: statistics.median(r[key] for r in kept)
    print(f"{mode}: {med('total') / 1000:.1f} KB an answer (median of {len(kept)}): "
          f"events {med('events_bytes') / 1000:.1f} KB in {med('events'):.0f} events "
          f"(Content-Encoding {kept[0]['encoding'] or 'none'}), /log {med('log_bytes') / 1000:.1f} KB "
          f"in {med('log_calls'):.0f} calls, /send {med('send_bytes') / 1000:.1f} KB, "
          f"rows carried in {med('carried'):.0f} events", flush=True)
    for kind in sorted({k for r in kept for k in r["by_type"]}):
        count = statistics.median(r["by_type"].get(kind, [0, 0])[0] for r in kept)
        size = statistics.median(r["by_type"].get(kind, [0, 0])[1] for r in kept)
        print(f"  {kind}: {count:.0f} events, {size / 1000:.2f} KB", flush=True)
    late = sorted(lag for r in kept for lag in r.get("lags", []))
    if late:
        print(f"  arrival against a plain connection beside it, over {len(late)} events: "
              f"median {statistics.median(late):+.1f} ms, p95 {late[int(len(late) * 0.95) - 1]:+.1f} ms, "
              f"latest {late[-1]:+.1f} ms", flush=True)


def resident_kb(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def held_open(target, session, pid):
    """What a connection that takes gzip costs the Host in memory, against one that does not,
    once each has been sent events (the packer's state is made with the first)."""
    out = {}
    for mode, query in (("plain", ""), ("gzip", "&gzip=1")):
        before = resident_kb(pid)
        streams = [Stream(target, f"live={session}{query}", None) for _ in range(HELD_OPEN)]
        wait(lambda: all(replayed(s) for s in streams), 30, "every connection's replay")
        time.sleep(1.0)
        out[mode] = (resident_kb(pid) - before) / HELD_OPEN
        for s in streams:
            s.close()
        time.sleep(1.5)
    print(f"memory a connection holds on the Host: {out['plain']:.0f} KB plain, {out['gzip']:.0f} KB "
          f"gzipped ({out['gzip'] - out['plain']:.0f} KB for its gzip state), over {HELD_OPEN} each", flush=True)


def measure(target, session, pid=None, modes=("apps", "gzip", "rows")):
    _, _, data, _ = target.request("GET", "/server-info")
    info = json.loads(data)
    features = set(info.get("features") or (info.get("capabilities") or {}).get("features") or [])
    for mode in modes:
        if NEEDS[mode] and NEEDS[mode] not in features:
            print(f"{mode}: this Host has no feature {NEEDS[mode]}", flush=True)
            continue
        cursor = {"event_id": None, "revision": latest_revision(target, session)}
        report(mode, [answer(target, session, mode, n, cursor) for n in range(ANSWERS)])
    if pid and "events_gzip" in features:
        held_open(target, session, pid)


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
                target = Target(startup[len(prefix):])
                status, _, data, _ = target.request("POST", "/agents", json.dumps({"name": "Probe", "backend": "claude"}))
                if status not in (200, 201):
                    raise RuntimeError(f"POST /agents answered {status}: {data[:200]!r}")
                measure(target, SESSION, host.pid)
            finally:
                host.kill()


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--remote":
        # A Host elsewhere: arrival and size only, with the agent it names.
        measure(Target(sys.argv[2], sys.argv[3]), sys.argv[4], modes=("apps", "gzip"))
        sys.exit(0)
    import scratch
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
