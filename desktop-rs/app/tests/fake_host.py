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
import pathlib
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

TOKEN = "probe-token"


def solid_png(width, height, rgba):
    """A tiny valid PNG, built with the standard library."""
    import struct
    import zlib

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


AVATAR_PNG = solid_png(64, 48, (30, 120, 200, 255))
MEDIA_PNG = solid_png(8, 8, (250, 200, 0, 255))
# Pairing mints this device token; it is accepted like TOKEN afterwards.
PAIRED_TOKEN = "cld_probe_paired_device"
state_lock = threading.Lock()
revision = 2
pair_revision = 3
rooms_gone = False  # an older Host without /agent-conversations
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
    "pair:a1:a2": [{"id": "p1", "role": "assistant", "text": "Rachel to Mike: ready?", "revision": 3,
                    "timestamp": "2026-09-29T09:30:00Z", "sender_name": "Rachel"}],
    "mike": [{"id": "m1", "role": "assistant", "text": "Mike here", "revision": 1, "timestamp": "2026-09-29T09:00:00Z"},
             {"id": "m2", "role": "assistant", "revision": 2, "timestamp": "2026-09-29T09:01:00Z", "text":
              "## Build plan\n\nRun `cargo test` first, then read the [guide](https://example.com).\n\n"
              "```rust\nfn main() {\n    println!(\"hello\");\n}\n```\n\n> Keep the main checkout untouched.\n\n"
              "- core models\n- **Qt bridges**\n\n| step | state |\n|---|---|\n| port | done |\n| verify | running |"}],
}
jobs = []
# /artifacts; /__control/artifacts replaces it.
artifacts = [
    {"artifact_id": "art1", "title": "Report"},
    {"artifact_id": "doc1", "title": "Findings", "summary": "What we found", "type": "document", "session": "rachel",
     "content": "# Findings\n\nAll *good*."},
    {"artifact_id": "html1", "title": "Page", "type": "research", "session": "rachel",
     "content": "<!doctype html><html><body><h1>Page</h1><img src=\"https://tracker.example/p.gif\"></body></html>"},
    {"artifact_id": "link1", "title": "Link", "type": "link", "session": "mike", "content": "https://example.com"}]
# /agents behaviour: "modern" returns the created agent row; "session-only"
# returns just the id and shows the row only after /__control/publish-pending.
explanation_polls = {}
teams = [{"team_id": "t1", "name": "Core", "color": "#fff", "member_agent_ids": ["a1"], "leader": "a1"}]
team_messages = {"t1": [{"id": "tm1", "text": "Standup at 9"}]}
turn_queue = {"rachel": [{"queue_id": "q1", "text": "later please"}, {"queue_id": "q2", "text": "and this"}]}
create_mode = "modern"
# The snapshot's personas (idle contacts); /__control/personas replaces them.
personas = [{"id": "p", "name": "Paula"}]
# /agent-model-options; /__control/catalog replaces it.
model_options = {"backends": []}
next_create_response = None
pending_agents = []
attention = [{"id": "d1", "session": "mike", "kind": "decision"}]
subscribers = []
event_id = 0
log_path = None
outage_until = 0.0
# Seconds an older page (/log?before=) takes; /__control/older-delay sets it.
older_delay = 0.0
CLOSE = object()


LOREM = ("The build runs the unit tests first, then the integration suite against a scratch "
         "database, and only then packages the desktop app. ")


def rich_turn(i):
    """Row `i` of a realistic chat: its kind cycles, its size varies."""
    size = 1 + (i * 7) % 5
    kind = i % 10
    if kind == 0:
        return {"role": "user", "text": f"Question {i}: can you check the build?"}
    if kind == 1:
        return {"role": "assistant", "text": f"Answer {i}. " + LOREM * (2 + size * 2)}
    if kind == 2:
        code = "\n".join(f"    let value_{n} = compute({n}, \"step\");" for n in range(3 + size * 3))
        return {"role": "assistant", "text": f"Code {i}:\n\n```rust\nfn main() {{\n{code}\n}}\n```"}
    if kind == 3:
        return {"role": "user", "text": f"Long request {i}. " + LOREM * (1 + size)}
    if kind == 4:
        table = "\n".join(f"| step {n} | {'done' if n % 2 else 'running'} | {n * 3}s |" for n in range(2 + size))
        return {"role": "assistant", "text": f"Table {i}:\n\n| step | state | time |\n|---|---|---|\n{table}"}
    if kind == 5:
        items = "\n".join(f"- item {n}: " + LOREM[: 40 + n * 9] for n in range(3 + size))
        return {"role": "assistant", "text": f"## List {i}\n\n{items}"}
    if kind == 6:
        tools = [{"name": "Bash", "command": f"cargo test -p part{n}", "status": "completed",
                  "result": "\n".join(f"test case_{m} ... ok" for m in range(4 + n))} for n in range(1 + size % 3)]
        return {"role": "assistant", "text": f"Ran the tests for {i}.", "tools": tools}
    if kind == 7:
        return {"role": "assistant", "text": f"Done {i}."}
    if kind == 8:
        return {"role": "assistant", "text": f"### Mixed {i}\n\n" + LOREM * size
                + "\n\n> Keep the main checkout untouched.\n\n```sh\ncargo build\ncargo test\n```"}
    return {"role": "user", "text": f"Follow-up {i}: " + LOREM[: 30 + size * 20]}


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

    def reply_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
                "authorization": self.headers.get("Authorization", ""),
                "last_event_id": self.headers.get("Last-Event-ID", "")})
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        if url.path == "/server-info":
            return self.reply(200, {"name": "Fake Host", "clarp_version": "9.9.9", "default_cwd": "/tmp"})
        if url.path == "/agents/snapshot":
            with state_lock:
                return self.reply(200, {"agents": agents, "personas": personas,
                                        "available_mcp_servers": [{"name": "github", "description": "GitHub"}]})
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
            with state_lock:
                return self.reply(200, {"artifacts": artifacts})
        if url.path == "/message-tool-details":
            return self.reply(200, {"tools": [{"name": "Bash", "input": {"command": "ls"}}], "display_cells": []})
        if url.path == "/agent-model-options":
            return self.reply(200, model_options)
        if url.path == "/static/avatars/rachel.png":
            return self.reply_bytes(200, AVATAR_PNG, "image/png")
        if url.path == "/fixtures/tone.pcm":
            import math
            import struct
            samples = [int(8000 * math.sin(i / 8)) for i in range(4800)]
            return self.reply_bytes(200, struct.pack("<%dh" % len(samples), *samples), "application/octet-stream")
        if url.path in ("/fixtures/clip.mp3", "/fixtures/clip.m4a"):
            name = url.path.rsplit("/", 1)[1]
            data = (pathlib.Path(__file__).parent / "fixtures" / name).read_bytes()
            return self.reply_bytes(200, data, "audio/mpeg" if name.endswith(".mp3") else "audio/mp4")
        if url.path.startswith("/clips/") and url.path.endswith("/complete.mp3"):
            if "/404/" in url.path:
                return self.reply(404, {"error": "clip expired"})
            return self.reply_bytes(200, b"ID3fixture-audio", "audio/mpeg")
        if url.path == "/clips/recoverable":
            events = [{"type": "audio", "clip_id": 50, "session": query.get("session"), "url": "/clips/50/complete.mp3",
                       "complete_url": "/clips/50/complete.mp3", "trace_id": "recovered"}] if query.get("session") == "mike" else []
            return self.reply(200, {"events": events})
        if url.path == "/media/files/m1":
            return self.reply_bytes(200, MEDIA_PNG, "image/png")
        if url.path == "/media":
            return self.reply(200, {"assets": [
                {"asset_id": "m1", "mime_type": "image/png", "url": "/media/files/m1", "session": query.get("session")},
                {"asset_id": "doc1", "mime_type": "application/pdf", "url": "/media/files/doc1"}]})
        if url.path == "/agent-conversations":
            if rooms_gone:
                return self.reply(404, {"error": "not found"})
            return self.reply(200, {"conversations": [
                {"conversation_id": "pair:a1:a2", "title": "Rachel & Mike", "latest_revision": pair_revision},
                {"conversation_id": "team-standup", "title": "not a pair", "latest_revision": 9}]})
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
            if query.get("before") and older_delay:
                time.sleep(older_delay)
            with state_lock:
                rows = turns.get(session, [])
                after = int(query.get("after_revision", "-1"))
                chosen = [t for t in rows if t["revision"] > after] if after >= 0 else rows
                latest = max([t["revision"] for t in rows] or [0])
                cid = next((a["conversation_id"] for a in agents if a["session"] == session), "")
                # Pages of `limit` rows, newest first; `before` is the oldest
                # row the client holds.
                more = False
                if after < 0:
                    before = query.get("before")
                    if before:
                        end = next((i for i, t in enumerate(rows) if t["id"] == before), len(rows))
                        chosen = rows[:end]
                    limit = int(query.get("limit", "0") or 0)
                    if limit and len(chosen) > limit:
                        chosen, more = chosen[-limit:], True
            return self.reply(200, {"conversation_id": cid, "turns": chosen, "latest_revision": latest,
                                    "has_more": more, "missing": False})
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
        if url.path == "/transcribe":
            # Raw WAV; the id travels in a header and comes back.
            if self.in_outage():
                return
            record({"method": "POST", "path": url.path, "body": {
                "size": len(raw), "content_type": self.headers.get("Content-Type", ""),
                "transcription_id": self.headers.get("X-Transcription-ID", ""),
                "hands_free": self.headers.get("X-Hands-Free", ""), "riff": raw[:4].decode("latin1")}})
            return self.reply(200, {"text": "  dictated words  ", "trace_id": "trace-dictation",
                                    "transcription_id": self.headers.get("X-Transcription-ID", ""), "hands_free": False})
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
        if url.path == "/__control/catalog":
            # Test control: the model catalog /agent-model-options answers.
            global model_options
            model_options = body
            return self.reply(200, {"ok": True})
        if url.path == "/__control/personas":
            # Test control: the personas the snapshot lists, then a roster event.
            global personas
            with state_lock:
                personas = body.get("personas", [])
            broadcast({"type": "agent-roster", "session": "", "kind": "updated"})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/add-agent":
            # Test control: one more idle agent in the roster, announced.
            session = body["session"]
            row = {"agent_id": session + "-id", "session": session, "persona": body.get("persona", session),
                   "backend": "claude", "latest_state": "idle", "alive": True, "last_activity": 500,
                   "conversation_id": "c-" + session, "head_revision": 0}
            with state_lock:
                turns.setdefault(session, [])
                agents.append(row)
            broadcast({"type": "agent-roster", "session": session, "kind": "created"})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/fill":
            # Test control: replace a chat's history with `count` rows, under
            # `conversation_id` (default: unchanged), and announce it.
            session, count = body["session"], int(body["count"])
            global revision
            with state_lock:
                rows = []
                for i in range(count):
                    revision += 1
                    rows.append({"id": f"{session}-{i}", "role": "user" if i % 2 == 0 else "assistant",
                                 "text": f"{body.get('prefix', 'Row')} {i}", "revision": revision,
                                 "timestamp": "2026-09-15T08:00:00Z"})
                turns[session] = rows
                for agent in agents:
                    if agent["session"] == session:
                        agent["head_revision"] = revision
                        if body.get("conversation_id"):
                            agent["conversation_id"] = body["conversation_id"]
            broadcast({"type": "transcript-updated", "session": session})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/turns":
            # Test control: replace a chat's history with `turns` as given
            # (tool calls, activity rows), and announce it.
            session = body["session"]
            with state_lock:
                rows = []
                for turn in body["turns"]:
                    revision += 1
                    rows.append({"revision": revision, "timestamp": "2026-09-29T10:00:00Z", **turn})
                turns[session] = rows
                for agent in agents:
                    if agent["session"] == session:
                        agent["head_revision"] = revision
            broadcast({"type": "transcript-updated", "session": session})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/rich":
            # Test control: replace a chat's history with `count` rows of
            # varied height (wrapped prose, code, tables, lists, tool calls,
            # user bubbles), deterministic by index, and announce it.
            session, count = body["session"], int(body["count"])
            with state_lock:
                rows = []
                for i in range(count):
                    revision += 1
                    rows.append({"id": f"{session}-{i}", "revision": revision,
                                 "timestamp": "2026-09-15T08:00:00Z", **rich_turn(i)})
                turns[session] = rows
                for agent in agents:
                    if agent["session"] == session:
                        agent["head_revision"] = revision
            broadcast({"type": "transcript-updated", "session": session})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/older-delay":
            # Test control: older pages arrive after `seconds`.
            global older_delay
            older_delay = float(body.get("seconds", 0))
            return self.reply(200, {"ok": True})
        if url.path == "/__control/upsert":
            # Test control: update turns by id (a streaming reply growing) or
            # append them, each with a new revision, and announce it.
            session = body["session"]
            with state_lock:
                rows = turns.setdefault(session, [])
                for turn in body["turns"]:
                    revision += 1
                    turn = {"timestamp": "2026-09-29T10:00:00Z", **turn, "revision": revision}
                    existing = next((t for t in rows if t["id"] == turn["id"]), None)
                    if existing is None:
                        rows.append(turn)
                    else:
                        existing.update(turn)
                for agent in agents:
                    if agent["session"] == session:
                        agent["head_revision"] = revision
            broadcast({"type": "transcript-updated", "session": session})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/publish-pending":
            with state_lock:
                agents.extend(pending_agents)
                pending_agents = []
            return self.reply(200, {"ok": True})
        if url.path == "/__control/agent":
            # Test control: change an agent's fields, then announce it.
            with state_lock:
                for agent in agents:
                    if agent["session"] == body.get("session"):
                        agent.update(body.get("set", {}))
            broadcast({"type": "agent-roster", "session": body.get("session"), "kind": "updated"})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/rooms-gone":
            global rooms_gone
            rooms_gone = bool(body.get("gone", True))
            return self.reply(200, {"ok": True})
        if url.path == "/__control/pair-message":
            # Test control: the pair room gains a message.
            global pair_revision
            with state_lock:
                pair_revision += 1
                turns["pair:a1:a2"].append({"id": f"p{pair_revision}", "role": "assistant", "text": body.get("text", "more"),
                                            "revision": pair_revision, "timestamp": "2026-09-29T09:31:00Z"})
            broadcast({"type": "transcript-updated", "session": "rachel"})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/event":
            # Test control: push one SSE event to every stream.
            broadcast(body)
            return self.reply(200, {"ok": True})
        if url.path == "/__control/attention":
            # Test control: replace the attention items and say so.
            with state_lock:
                attention[:] = body.get("items", [])
            broadcast({"type": "attention-updated"})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/artifacts":
            # Test control: replace the artifact list and announce it.
            global artifacts
            with state_lock:
                artifacts = body.get("artifacts", [])
            broadcast({"type": "artifact-updated", "session": body.get("session", "")})
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
        if url.path in ("/desktop-presence", "/application-activity"):
            return self.reply(200, {"ok": True})
        if url.path == "/preview":
            return self.reply(200, {"ok": True})
        if url.path == "/clips/ack":
            return self.reply(200, {"ok": True})
        if url.path == "/agent-assign":
            if body.get("mode") == "options":
                return self.reply(200, {"contacts": [{"name": "Paula", "available": True}]})
            if body.get("name") == "Nobody":
                return self.reply(409, {"error": "contact is busy"})
            return self.reply(200, {"ok": True, "session": body.get("session")})
        if url.path.startswith("/decisions/") and url.path.endswith("/resolve"):
            # A resolved decision leaves the attention list.
            decision = url.path.split("/")[2]
            with state_lock:
                attention[:] = [a for a in attention if a.get("id") != decision]
            return self.reply(200, {"ok": True, "decision_id": decision, "status": body.get("choice")})
        if url.path == "/agent-archive":
            # Archive or restore: the agent's row moves and the roster says so.
            with state_lock:
                for agent in agents:
                    if agent["session"] == body.get("session"):
                        agent["archived_at"] = int(time.time()) if body.get("archived") else None
            broadcast({"type": "agent-roster", "session": body.get("session"), "kind": "updated"})
            return self.reply(200, {"ok": True})
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
