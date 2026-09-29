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
from urllib.parse import parse_qs, unquote, urlparse

TOKEN = "probe-token"
# Pairing mints this device token; it is accepted like TOKEN afterwards.
PAIRED_TOKEN = "cld_probe_paired_device"
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
jobs = []
# /agents behaviour: "modern" returns the created agent row; "session-only"
# returns just the id and shows the row only after /__control/publish-pending.
explanation_polls = {}
teams = [{"team_id": "t1", "name": "Core", "color": "#fff", "member_agent_ids": ["a1"], "leader": "a1"}]
team_messages = {"t1": [{"id": "tm1", "text": "Standup at 9"}]}
turn_queue = {"rachel": [{"queue_id": "q1", "text": "later please"}, {"queue_id": "q2", "text": "and this"}]}
create_mode = "modern"
next_create_response = None
pending_agents = []
attention = [{"id": "d1", "session": "mike", "kind": "decision"}]
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
        return self.headers.get("Authorization") in (f"Bearer {TOKEN}", f"Bearer {PAIRED_TOKEN}")

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
        record({"method": "GET", "path": url.path, "query": query,
                "authorization": self.headers.get("Authorization", "")})
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        if url.path == "/server-info":
            return self.reply(200, {"name": "Fake Host", "clarp_version": "9.9.9", "default_cwd": "/tmp"})
        if url.path == "/agents/snapshot":
            with state_lock:
                return self.reply(200, {"agents": agents, "personas": [{"id": "p", "name": "Paula"}]})
        if url.path == "/teams":
            with state_lock:
                return self.reply(200, {"teams": teams})
        if url.path == "/task-plan":
            return self.reply(200, {"plan": {"title": "Ship it", "items": [{"title": "Probe"}]}})
        if url.path == "/agent-heartbeat/status":
            return self.reply(200, {"enabled": True, "interval_minutes": 30})
        if url.path == "/identity/prompt-history":
            page = [{"turn_id": f"p{i}", "text": f"prompt {i}"} for i in range(1, 31)]
            before = query.get("before")
            start = next((i + 1 for i, p in enumerate(page) if p["turn_id"] == before), 0)
            chunk = page[start:start + int(query.get("limit", "20"))]
            more = start + len(chunk) < len(page)
            return self.reply(200, {"prompts": chunk, "page": {"has_more": more, "next_before": chunk[-1]["turn_id"] if more else ""}})
        if url.path == "/diagnostics/health":
            return self.reply(200, {"status": "ok"})
        if url.path == "/transcription-capabilities":
            return self.reply(200, {"providers": ["whisper"]})
        if url.path == "/tts/providers":
            return self.reply(200, {"provider": "elevenlabs", "fallback": "none"})
        if url.path == "/voices":
            return self.reply(200, {"bio": "Warm and clear", "voices": [{"id": "v1", "label": "Warm", "taken_by": "Mike"}, {"id": "v2"}]})
        if url.path == "/orchestrator/settings":
            return self.reply(200, {"settings": {"enabled": False}, "recent_decisions": [
                {"final_action": "route", "target_session": "rachel", "confidence": 0.87}]})
        if url.path.startswith("/teams/") and url.path.endswith("/messages"):
            team = url.path.split("/")[2]
            return self.reply(200, {"messages": team_messages.get(team, [])})
        if url.path == "/turn-queue":
            with state_lock:
                return self.reply(200, {"items": turn_queue.get(query.get("session", ""), []), "paused": False})
        if url.path == "/attention":
            return self.reply(200, {"items": attention})
        if url.path == "/background-jobs":
            with state_lock:
                return self.reply(200, {"jobs": jobs})
        if url.path == "/artifacts":
            return self.reply(200, {"artifacts": [{"artifact_id": "art1", "title": "Report"}]})
        if url.path == "/agent-model-options":
            return self.reply(200, {"backends": []})
        if url.path == "/past-sessions":
            if query.get("cwd") == "/broken":
                return self.reply(500, {"error": "history unreadable"})
            if query.get("cwd") == "/slow":
                # Lets an earlier request's reply land first.
                time.sleep(0.5)
            sessions = [{"session_id": "old-1", "title": "Earlier work", "cwd": query.get("cwd", "")}]
            if query.get("scope") == "all":
                sessions.append({"session_id": "old-2", "title": "Other project", "cwd": "/elsewhere"})
            return self.reply(200, {"sessions": sessions})
        if url.path == "/launch-directories":
            q = query.get("q", "")
            if q == "slow":
                time.sleep(0.4)
            return self.reply(200, {"home": "/home/fake", "matches": [
                {"path": f"/home/fake/{q or 'src'}", "label": q or "src"}]})
        if url.path == "/dirs":
            return self.reply(200, {"matches": [query.get("path", "") + "/one", query.get("path", "") + "/two"]})
        if url.path == "/favorite-paths":
            return self.reply(200, {"paths": ["/home/fake/src", "/home/fake/notes"][: int(query.get("limit", "5"))]})
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
        raw = self.rfile.read(length)
        if url.path == "/upload":
            # Raw file bytes; metadata travels in headers.
            if self.in_outage():
                return
            name = unquote(self.headers.get("X-File-Name", "file"))
            record({"method": "POST", "path": url.path, "body": {
                "name": name, "size": len(raw),
                "content_type": self.headers.get("Content-Type", ""),
                "session": self.headers.get("X-Session", ""),
                "upload_id": self.headers.get("X-Upload-ID", "")}})
            if name.startswith("fail"):
                return self.reply(500, {"error": "upload refused"})
            return self.reply(200, {"path": f"/srv/uploads/{name}", "name": name})
        body = json.loads(raw or b"{}")
        if url.path == "/__control/outage":
            # Test control, outside the protocol: drop every stream and refuse
            # requests for `seconds`.
            outage_until = time.time() + float(body.get("seconds", 1))
            with state_lock:
                for subscriber in list(subscribers):
                    subscriber.put(CLOSE)
            return self.reply(200, {"ok": True})
        global create_mode, next_create_response, pending_agents
        if url.path == "/__control/create":
            create_mode = body.get("mode", create_mode)
            next_create_response = body.get("respond")
            return self.reply(200, {"ok": True})
        if url.path == "/__control/publish-pending":
            with state_lock:
                agents.extend(pending_agents)
                pending_agents = []
            return self.reply(200, {"ok": True})
        if url.path == "/__control/jobs":
            # Test control: replace the job list, then push an optional event.
            global jobs
            with state_lock:
                jobs = body.get("jobs", [])
            if body.get("event"):
                broadcast(body["event"])
            return self.reply(200, {"ok": True})
        if self.in_outage():
            return
        record({"method": "POST", "path": url.path, "body": body,
                "authorization": self.headers.get("Authorization", "")})
        if url.path == "/pairing/exchange":
            # Unauthenticated by design: the one-time code is the credential.
            code = body.get("code")
            if code == "123456":
                return self.reply(200, {"device": {"id": "d1", "token": PAIRED_TOKEN}})
            if code == "000000":
                return self.reply(200, {"device": {"id": "d1"}})
            return self.reply(403, {"error": "pairing code expired"})
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        if url.path == "/tool-explanations":
            # First poll of an item is pending, the next is ready.
            rows = []
            for item in body.get("items", []):
                seen = explanation_polls.get(item["id"], 0)
                explanation_polls[item["id"]] = seen + 1
                summary = item.get("activity", {}).get("summary", "")
                rows.append({"id": item["id"], "status": "ready" if seen else "pending",
                             "text": "Explained: " + summary if seen else ""})
            return self.reply(200, {"items": rows})
        if url.path == "/tts/providers":
            return self.reply(200, {"provider": body["provider"], "fallback": body["fallback"]})
        if url.path == "/orchestrator/settings":
            return self.reply(200, {"settings": {"enabled": body["enabled"], "provider": body["provider"],
                                                 "timeout_ms": body["timeout_ms"]}, "recent_decisions": []})
        if url.path == "/teams":
            with state_lock:
                team_id = "t%d" % (len(teams) + 1)
                teams.append({"team_id": team_id, "name": body["name"], "color": body.get("color", ""), "member_agent_ids": []})
            return self.reply(201, {"team_id": team_id})
        if url.path.startswith("/teams/") and url.path.endswith("/members"):
            team = url.path.split("/")[2]
            with state_lock:
                for t in teams:
                    if t["team_id"] == team:
                        t["member_agent_ids"].append(body["agent_id"])
            return self.reply(200, {"ok": True})
        if url.path.startswith("/teams/"):
            team = url.path.split("/")[2]
            with state_lock:
                for t in teams:
                    if t["team_id"] == team:
                        t.update({"name": body.get("name", t["name"]), "leader": body.get("leader", "")})
            return self.reply(200, {"ok": True})
        if url.path.startswith("/turn-queue/") and url.path.endswith("/send"):
            item = url.path.split("/")[2]
            with state_lock:
                for session, items in turn_queue.items():
                    turn_queue[session] = [i for i in items if i["queue_id"] != item]
            return self.reply(200, {"ok": True})
        if url.path == "/agent-assign":
            if body.get("mode") == "options":
                return self.reply(200, {"contacts": [{"name": "Paula", "available": True}]})
            if body.get("name") == "Nobody":
                return self.reply(409, {"error": "contact is busy"})
            return self.reply(200, {"ok": True, "session": body.get("session")})
        if url.path in ("/select", "/stop", "/compact", "/agent-schedules/toggle", "/team-nudging") or url.path.startswith("/agent-"):
            return self.reply(200, {"ok": True})
        if url.path == "/agents":
            if next_create_response is not None:
                scripted, next_create_response = next_create_response, None
                return self.reply(scripted["status"], scripted["body"])
            name = body.get("name") or ("Pool" if body.get("auto_contact") else "Anon")
            session = name.lower() + "-new"
            row = {"agent_id": session + "-id", "session": session, "persona": name, "backend": body.get("backend", ""),
                   "cwd": body.get("cwd", ""), "latest_state": "idle", "alive": True, "last_activity": 9000,
                   "conversation_id": "c-" + session, "head_revision": 0}
            with state_lock:
                turns[session] = []
                if create_mode == "modern":
                    agents.append(row)
                else:
                    pending_agents.append(row)
            if create_mode == "modern":
                self.reply(201, {"session": session, "agent": row})
            else:
                self.reply(201, {"session": session})
            broadcast({"type": "agent-roster", "session": session, "kind": "created"})
            return
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


def do_DELETE(self):
    url = urlparse(self.path)
    record({"method": "DELETE", "path": url.path})
    if not self.authorized():
        return self.reply(401, {"error": "unauthorized"})
    if url.path.startswith("/agents/"):
        session = url.path[len("/agents/"):]
        with state_lock:
            agents[:] = [a for a in agents if a["session"] != session]
        self.reply(200, {"ok": True})
        broadcast({"type": "agent-roster", "session": session, "kind": "deleted"})
        return
    if url.path.startswith("/background-jobs/"):
        return self.reply(200, {"ok": True})
    if url.path.startswith("/teams/") and "/members/" in url.path:
        _, _, team, _, agent = url.path.split("/")
        with state_lock:
            for t in teams:
                if t["team_id"] == team:
                    t["member_agent_ids"] = [a for a in t["member_agent_ids"] if a != agent]
        return self.reply(200, {"ok": True})
    if url.path.startswith("/teams/"):
        team = url.path.split("/")[2]
        with state_lock:
            teams[:] = [t for t in teams if t["team_id"] != team]
        return self.reply(200, {"ok": True})
    if url.path.startswith("/turn-queue/"):
        item = url.path.split("/")[2]
        with state_lock:
            for session, items in turn_queue.items():
                turn_queue[session] = [i for i in items if i["queue_id"] != item]
        return self.reply(200, {"ok": True})
    return self.reply(404, {"error": "not found"})


def do_PUT(self):
    url = urlparse(self.path)
    length = int(self.headers.get("Content-Length", "0"))
    body = json.loads(self.rfile.read(length) or b"{}")
    record({"method": "PUT", "path": url.path, "body": body})
    if not self.authorized():
        return self.reply(401, {"error": "unauthorized"})
    if url.path.startswith("/turn-queue/"):
        item = url.path.split("/")[2]
        with state_lock:
            for items in turn_queue.values():
                for i in items:
                    if i["queue_id"] == item:
                        i["text"] = body["text"]
        return self.reply(200, {"ok": True})
    return self.reply(404, {"error": "not found"})


Handler.do_DELETE = do_DELETE
Handler.do_PUT = do_PUT


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # One tagged line per failure, so the runner output shows the cause.
        import sys
        import traceback
        kind, error, _ = sys.exc_info()
        if kind in (ConnectionResetError, BrokenPipeError):
            # A client closing a kept-alive or streaming connection on exit.
            sys.stderr.write(f"fake host: client {client_address[1]} went away ({kind.__name__})\n")
            return
        sys.stderr.write(f"FAKE_HOST_ERROR {client_address[1]} {kind.__name__}: {error} | "
                         + " / ".join(traceback.format_exc().strip().splitlines()[-3:]) + "\n")


def main():
    global log_path
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    log_path = args.log
    server = Server(("127.0.0.1", 0), Handler)
    with open(args.port_file, "w") as handle:
        handle.write(str(server.server_address[1]))
    server.serve_forever()


if __name__ == "__main__":
    main()
