#!/usr/bin/env python3
"""A minimal scripted Clarp Host for controller probes (stdlib only).

Serves /server-info, /agents/snapshot, /log, /select, /send, /stop,
/agent-model-options and an /events SSE stream. A /send files the user row
under u-<client_msg_id>, appends an assistant reply, bumps the revision and
pushes transcript-updated. Every request is appended to --log as JSON lines.
Usage: fake_host.py --port-file PATH --log PATH
"""
import argparse
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "probe-token"
state_lock = threading.Lock()
revision = 2
agents = [
    {"agent_id": "a1", "session": "rachel", "persona": "Rachel", "backend": "claude",
     "latest_state": "idle", "alive": True, "last_activity": 2000, "conversation_id": "c-rachel", "head_revision": 2},
    {"agent_id": "a2", "session": "mike", "persona": "Mike", "backend": "codex",
     "latest_state": "idle", "alive": True, "last_activity": 1000, "conversation_id": "c-mike", "head_revision": 1},
]
turns = {
    "rachel": [
        {"id": "r1", "role": "user", "text": "Hello", "revision": 1, "timestamp": "2026-09-29T10:00:00Z"},
        {"id": "r2", "role": "assistant", "text": "Hi, how can I help?", "revision": 2, "timestamp": "2026-09-29T10:00:05Z"},
    ],
    "mike": [{"id": "m1", "role": "assistant", "text": "Mike here", "revision": 1, "timestamp": "2026-09-29T09:00:00Z"}],
}
subscribers = []
event_id = 0
log_path = None
outage_until = 0.0
CLOSE = object()


def record(entry):
    with open(log_path, "a") as handle:
        handle.write(json.dumps(entry) + "\n")


def broadcast(event):
    global event_id
    with state_lock:
        event_id += 1
        payload = f"id: {event_id}\ndata: {json.dumps(event)}\n\n".encode()
        for subscriber in list(subscribers):
            subscriber.put(payload)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def authorized(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def in_outage(self):
        # An outage looks like a dead Host: the connection closes unanswered.
        if time.time() < outage_until:
            self.close_connection = True
            return True
        return False

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if self.in_outage():
            return
        record({"method": "GET", "path": url.path, "query": query})
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        if url.path == "/server-info":
            return self.reply(200, {"name": "Fake Host", "clarp_version": "9.9.9"})
        if url.path == "/agents/snapshot":
            with state_lock:
                return self.reply(200, {"agents": agents, "personas": [{"id": "p", "name": "Paula"}]})
        if url.path == "/agent-model-options":
            return self.reply(200, {"backends": []})
        if url.path == "/log":
            session = query.get("session", "")
            with state_lock:
                rows = turns.get(session, [])
                after = int(query.get("after_revision", "-1"))
                chosen = [t for t in rows if t["revision"] > after] if after >= 0 else rows
                latest = max([t["revision"] for t in rows] or [0])
                cid = next((a["conversation_id"] for a in agents if a["session"] == session), "")
            return self.reply(200, {"conversation_id": cid, "turns": chosen, "latest_revision": latest,
                                    "has_more": False, "missing": False})
        if url.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            inbox = queue.Queue()
            with state_lock:
                subscribers.append(inbox)
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        chunk = inbox.get(timeout=5)
                    except queue.Empty:
                        chunk = b": ping\n\n"
                    if chunk is CLOSE:
                        self.close_connection = True
                        return
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with state_lock:
                    subscribers.remove(inbox)
            return
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        global outage_until
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if url.path == "/__control/outage":
            # Test control, outside the protocol: drop every stream and refuse
            # requests for `seconds`.
            outage_until = time.time() + float(body.get("seconds", 1))
            with state_lock:
                for subscriber in list(subscribers):
                    subscriber.put(CLOSE)
            return self.reply(200, {"ok": True})
        if self.in_outage():
            return
        record({"method": "POST", "path": url.path, "body": body})
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        if url.path in ("/select", "/stop"):
            return self.reply(200, {"ok": True})
        if url.path == "/send":
            global revision
            session = body["session"]
            if body.get("text") == "never-file":
                # Accepted but never filed: the client must time the send out.
                return self.reply(200, {"ok": True})
            with state_lock:
                revision += 1
                turns[session].append({"id": "u-" + body["client_msg_id"], "role": "user", "text": body["text"],
                                       "revision": revision, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                revision += 1
                turns[session].append({"id": f"reply-{revision}", "role": "assistant", "text": "Echo: " + body["text"],
                                       "revision": revision, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                for agent in agents:
                    if agent["session"] == session:
                        agent["head_revision"] = revision
            self.reply(200, {"ok": True})
            broadcast({"type": "agent-state", "session": session, "kind": "thinking", "ts": int(time.time() * 1000)})
            broadcast({"type": "transcript-updated", "session": session})
            broadcast({"type": "user-notification", "session": session, "persona": "Rachel", "preview": "Echo"})
            return
        return self.reply(404, {"error": "not found"})


def main():
    global log_path
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    log_path = args.log
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    with open(args.port_file, "w") as handle:
        handle.write(str(server.server_address[1]))
    server.serve_forever()


if __name__ == "__main__":
    main()
