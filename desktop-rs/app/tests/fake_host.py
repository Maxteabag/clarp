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


def iso_at(ms, offset_hours=0):
    """An ISO 8601 stamp with an explicit offset, as the Host requires."""
    import datetime
    zone = datetime.timezone(datetime.timedelta(hours=offset_hours))
    return datetime.datetime.fromtimestamp(ms / 1000, zone).isoformat(timespec="seconds")


def artifact_fixtures(kind, session, now_ms):
    """Realistic artifacts of one type, as the Host's flat-v1 list returns
    them: long titles, failed and expired states, missing parts."""
    s = session
    day = 86_400_000
    if kind == "countdown":
        return [
            {"artifact_id": "cd-launch", "type": "countdown", "status": "active", "session": s,
             "title": "Launch window for the Nordics production rollout of the new billing pipeline (phase two of three)",
             "summary": "Freeze starts an hour before.", "content": "Remember to **page** the on-call.",
             "target_at": iso_at(now_ms + day + 7_384_000 + 600, 2), "time_zone": "Europe/Oslo"},
            {"artifact_id": "cd-reached", "type": "countdown", "status": "active", "session": s, "title": "Stand-up",
             "target_at": iso_at(now_ms - 20_000), "time_zone": "UTC"},
            {"artifact_id": "cd-past", "type": "countdown", "status": "completed", "session": s, "title": "Deploy finished",
             "target_at": iso_at(now_ms - 7_500_000, -4), "time_zone": "America/New_York"},
            {"artifact_id": "cd-cancelled", "type": "countdown", "status": "cancelled", "session": s, "title": "Offsite (called off)",
             "target_at": iso_at(now_ms + 3 * day, 2), "time_zone": "Europe/Oslo"},
            {"artifact_id": "cd-broken", "type": "countdown", "status": "active", "session": s, "title": "Someday",
             "target_at": "next tuesday", "time_zone": "UTC"},
        ]
    if kind == "html_form":
        form = ("<h1>Trip planner</h1><form><label>Destination <input name=\"destination\" value=\"Bergen\"></label>"
                "<label>Nights <input name=\"nights\" type=\"number\" value=\"2\"></label>"
                "<label><input name=\"train\" type=\"checkbox\"> By train</label><button>Send</button></form>")
        schema = {"type": "object", "properties": {"destination": {"type": "string"}, "nights": {"type": "number"},
                                                   "train": {"type": "boolean"}}}
        return [
            {"artifact_id": "form-trip", "type": "html_form", "status": "active", "session": s, "version": 3,
             "title": "Plan the team offsite: destination, nights, and whether we take the night train from Oslo",
             "summary": "Pick where we go and for how long.", "content": form, "answer_schema": schema},
            {"artifact_id": "form-report", "type": "html_form", "status": "ready", "session": s, "version": 1, "read_only": True,
             "title": "Quarterly cost report", "summary": "Spend by team, read only.",
             "content": "<h1>Costs</h1><p>Compute is <b>up 12%</b>.</p><ul><li>Core: 40k</li><li>Web: 12k</li></ul>",
             "answer_schema": {"type": "object", "properties": {}}},
            {"artifact_id": "form-stale", "type": "html_form", "status": "active", "session": s, "version": 1,
             "title": "Lunch order", "summary": "", "content": "<form><input name=\"dish\"></form>",
             "answer_schema": {"type": "object", "properties": {"dish": {"type": "string"}}}},
        ]
    if kind == "decision":
        def decision(did, status, revision=4, **more):
            return {"decision_id": did, "question": "", "context": "", "status": status, "revision": revision,
                    "response_type": "approval", "response_effort": "quick", "urgency": "normal", **more}
        return [
            {"artifact_id": "dec-deploy", "type": "decision", "status": "active", "session": s,
             "title": "Deploy the billing migration to production tonight, during the low-traffic window after 22:00",
             "decision": decision("d-deploy", "pending", question="Deploy the billing migration tonight?",
                                  context="All checks are green on staging. Rolling back takes about ten minutes.",
                                  yes_label="Ship it", no_label="Hold", urgency="time_sensitive", response_effort="review",
                                  priority_reason="Blocks the release train")},
            {"artifact_id": "dec-refused", "type": "decision", "status": "active", "session": s, "title": "Rotate the API keys",
             "decision": decision("d-refused", "pending", revision=2, question="Rotate the keys now?",
                                  _host_revision=3)},
            {"artifact_id": "dec-discard", "type": "decision", "status": "active", "session": s, "title": "Archive old branches",
             "decision": decision("d-discard", "pending", question="Archive the 40 merged branches?",
                                  deadline_at=iso_at(now_ms + 2 * day))},
            {"artifact_id": "dec-approved", "type": "decision", "status": "completed", "session": s, "title": "Upgrade Postgres",
             "decision": decision("d-approved", "accepted", question="Upgrade to Postgres 17?", resolved_choice="accepted")},
            {"artifact_id": "dec-declined", "type": "decision", "status": "completed", "session": s, "title": "Delete the staging data",
             "decision": decision("d-declined", "rejected", question="Delete staging data?", resolved_choice="rejected")},
            {"artifact_id": "dec-expired", "type": "decision", "status": "expired", "session": s, "title": "Book the venue",
             "decision": decision("d-expired", "expired", question="Book it before Friday?")},
            {"artifact_id": "dec-waiting", "type": "decision", "status": "completed", "session": s, "title": "Merge the docs PR",
             "decision": decision("d-waiting", "accepted", question="Merge it?", delivery_pending=True)},
            {"artifact_id": "dec-missing", "type": "decision", "status": "active", "session": s, "title": "A decision without its question"},
            {"artifact_id": "dec-click", "type": "decision", "status": "active", "session": s, "title": "Turn on the nightly backups",
             "decision": decision("d-click", "pending", question="Back up every night at 03:00?", yes_label="Turn on", no_label="Not now")},
        ]
    if kind == "question":
        def question(did, status, options, revision=7, **more):
            return {"decision_id": did, "status": status, "revision": revision, "response_type": "single_choice",
                    "response_effort": "quick", "urgency": "normal", "options": options, **more}
        trip = [{"id": "bergen", "label": "Bergen", "description": "Fjords and rain; seven hours by train from Oslo."},
                {"id": "tromso", "label": "Tromsø", "description": "Northern lights season starts in October."},
                {"id": "stay", "label": "Stay in Oslo (recommended)", "description": ""}]
        days = [{"id": "thu", "label": "Thursday"}, {"id": "fri", "label": "Friday"}]
        return [
            {"artifact_id": "q-trip", "type": "question", "status": "active", "session": s,
             "title": "Where should the team offsite go this autumn, given the budget we agreed in August?",
             "decision": question("q-trip", "pending", trip, question="Where should we go?",
                                  context="Budget is 40k for twelve people, travel included.", recommended_option_id="tromso",
                                  allow_custom_text=True, response_effort="short")},
            {"artifact_id": "q-custom", "type": "question", "status": "active", "session": s, "title": "Pick the demo day",
             "decision": question("q-custom", "pending", days, question="Which day suits the demo?", allow_custom_text=True)},
            {"artifact_id": "q-picked", "type": "question", "status": "completed", "session": s, "title": "Database for the prototype",
             "decision": question("q-picked", "answered", [{"id": "pg", "label": "Postgres"}, {"id": "lite", "label": "SQLite"}],
                                  question="Which database?", answer={"option_id": "lite"})},
            {"artifact_id": "q-written", "type": "question", "status": "completed", "session": s, "title": "Name the release",
             "decision": question("q-written", "answered", [{"id": "a", "label": "Aurora"}], question="Which name?",
                                  allow_custom_text=True, answer={"text": "Let's call it Fjord instead"})},
            {"artifact_id": "q-ranking", "type": "question", "status": "active", "session": s, "title": "Rank the features",
             "decision": {**question("q-ranking", "pending", []), "response_type": "ranking", "question": "Rank them"}},
            {"artifact_id": "q-escape", "type": "question", "status": "active", "session": s, "title": "Venue for the dinner",
             "decision": question("q-escape", "pending", [{"id": "inn", "label": "The inn"}, {"id": "boat", "label": "The boat"}],
                                  question="Where do we eat?", allow_custom_text=True)},
            {"artifact_id": "q-click", "type": "question", "status": "active", "session": s, "title": "Coffee or tea at the offsite",
             "decision": question("q-click", "pending", [{"id": "coffee", "label": "Coffee"}, {"id": "tea", "label": "Tea"}],
                                  question="Coffee or tea?")},
        ]
    if kind == "plan":
        def item(title, status, subtasks=(), detail=""):
            return {"item_id": title[:8], "title": title, "status": status, "detail": detail, "subtasks": list(subtasks)}
        ship = [item("Freeze the API", "completed"),
                item("Port the transcript to Slint without changing how it scrolls for a reader who is up", "in_progress", [
                    item("Embed the cards", "completed"), item("Measure the row heights before and after", "in_progress")]),
                item("Write the release notes", "pending", detail="Short, for the changelog")]
        return [
            {"artifact_id": "plan-ship", "type": "plan", "status": "active", "session": s,
             "title": "Ship the Slint desktop client to the beta group before the end of the quarter",
             "summary": "Three steps, the second under way.",
             "plan": {"plan_id": "p1", "title": "Ship 2.0", "status": "active", "goal": "Beta testers on the Slint app",
                      "items": ship, "completed_count": 2, "total_count": 5}},
            {"artifact_id": "plan-done", "type": "plan", "status": "completed", "session": s, "title": "Tidy the repo",
             "plan": {"plan_id": "p2", "title": "Tidy", "status": "completed", "items": [item("Prune branches", "completed")],
                      "completed_count": 1, "total_count": 1}},
            {"artifact_id": "plan-blocked", "type": "plan", "status": "failed", "session": s, "title": "Migrate the database",
             "plan": {"plan_id": "p3", "title": "Migrate", "status": "blocked",
                      "items": [item("Back up", "completed"), item("Wait for the DBA", "blocked")], "completed_count": 1, "total_count": 2}},
            {"artifact_id": "plan-missing", "type": "plan", "status": "active", "session": s, "title": "A plan whose details went missing"},
            {"artifact_id": "plan-last", "type": "plan", "status": "active", "session": s, "title": "Rotate the certificates",
             "plan": {"plan_id": "p4", "title": "Certificates", "status": "active",
                      "items": [item("Renew", "completed"), item("Deploy the new certificates to the edge", "in_progress")],
                      "completed_count": 1, "total_count": 2}},
        ]
    if kind == "document":
        spec = ("# Slint client: design notes\n\nThe **transcript** keeps its offset while a reader is up; cards keep their height.\n\n"
                "## Keyboard\n\nK and J move over the cards on screen. Enter acts on the selected one.\n\n"
                "- one\n- two\n\n```rust\nfn main() {}\n```")
        huge = "\n\n".join(f"## Section {i}\n\nParagraph {i} of a long report, with enough words to wrap across the card's width at least once." for i in range(120))
        return [
            {"artifact_id": "doc-spec", "type": "document", "status": "ready", "session": s,
             "title": "Design notes for the Slint desktop client's transcript, cards and keyboard", "content": spec},
            {"artifact_id": "doc-summary", "type": "document", "status": "draft", "session": s, "title": "Outline only",
             "summary": "Only a summary so far; the body comes later."},
            {"artifact_id": "doc-huge", "type": "document", "status": "ready", "session": s, "title": "A very long report", "content": huge},
        ]
    if kind == "research":
        return [
            {"artifact_id": "res-market", "type": "research", "status": "ready", "session": s,
             "title": "Desktop agent clients in 2026: who ships what, on which toolkit, and why Slint",
             "summary": "Four vendors compared.",
             "content": "## Findings\n\nMost clients ship **Electron**; two ship native toolkits.\n\n## Method\n\nWe read the release notes.",
             "sources": [{"title": "Gartner forecast", "url": "https://gartner.example/r"},
                         {"title": "Vendor blog", "url": "https://vendor.example/b"},
                         {"title": "Old mirror", "url": "http://insecure.example/old"},
                         {"title": "Unlinked note"}]},
            {"artifact_id": "res-html", "type": "research", "status": "ready", "session": s, "title": "Market page",
             "content": "<!doctype html><html><body><h1>Market</h1><p>Growth is <b>12%</b> a year.</p><script>steal()</script></body></html>"},
            {"artifact_id": "res-plain", "type": "research", "status": "ready", "session": s, "title": "Notes without sources",
             "content": "Short notes."},
        ]
    if kind == "code_change":
        diff = ("diff --git a/src/main.rs b/src/main.rs\n--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1,3 +1,4 @@\n"
                " use std::io;\n-fn main() {}\n+fn main() {\n+    println!(\"cards\");\n+}\n")
        return [
            {"artifact_id": "cc-big", "type": "code_change", "status": "ready", "session": s,
             "title": "Embed every artifact type in the Slint chat, test first, with stable card heights",
             "repository": "clarp", "branch": "slint-artifacts", "commit": "4560a8ed", "files_changed": 12,
             "additions": 840, "deletions": 132, "diff": diff, "source_url": "https://github.com/example/clarp/commit/4560a8ed"},
            {"artifact_id": "cc-bare", "type": "code_change", "status": "draft", "session": s, "title": "A change without details",
             "repository": ""},
            {"artifact_id": "cc-failed", "type": "code_change", "status": "failed", "session": s, "title": "Rebase onto main",
             "repository": "clarp", "branch": "rebase", "files_changed": 1, "additions": 0, "deletions": 3},
        ]
    if kind == "data":
        regions = ["Nordics", "Benelux", "Iberia", "Baltics"]
        sales = [[f"{regions[i % 4]} {i}", 100 + i, 120 + i, 90 + 2 * i, 150 + 3 * i, i % 3 == 0, None if i % 5 == 0 else "ok"]
                 for i in range(120)]
        wide = [[f"r{r}c{c}" for c in range(30)] for r in range(3)]
        return [
            {"artifact_id": "data-sales", "type": "data", "status": "ready", "session": s,
             "title": "Quarterly sales by region, every store, with the audit flags the finance team asked for",
             "columns": ["Region", "Q1", "Q2", "Q3", "Q4", "Audited", "Note"], "rows": sales,
             "chart": {"kind": "bar", "category_column": "Region", "value_column": "Q4"}},
            {"artifact_id": "data-wide", "type": "data", "status": "ready", "session": s, "title": "Thirty columns",
             "columns": [f"Column {c}" for c in range(30)], "rows": wide},
            {"artifact_id": "data-empty", "type": "data", "status": "ready", "session": s, "title": "No rows yet",
             "columns": ["Name", "Value"], "rows": []},
            {"artifact_id": "data-broken", "type": "data", "status": "failed", "session": s, "title": "Export that failed"},
        ]
    if kind == "audio":
        return [
            {"artifact_id": "aud-brief", "type": "audio", "status": "ready", "session": s,
             "title": "This morning's stand-up, summarised for the people who could not make it to the call",
             "url": "/media/aud1", "mime_type": "audio/mpeg", "file_name": "standup-summary.mp3", "duration_ms": 72000},
            {"artifact_id": "aud-gone", "type": "audio", "status": "ready", "session": s, "title": "An expired recording",
             "url": "/media/gone", "mime_type": "audio/mpeg", "file_name": "old.mp3", "duration_ms": 5000},
            {"artifact_id": "aud-elsewhere", "type": "audio", "status": "ready", "session": s, "title": "A clip on another site",
             "url": "http://evil.example/x.mp3", "mime_type": "audio/mpeg", "file_name": "x.mp3"},
        ]
    if kind == "video":
        return [
            {"artifact_id": "vid-demo", "type": "video", "status": "ready", "session": s,
             "title": "Screen recording of the new artifact cards, keyboard first, with every type in one chat",
             "url": "/media/vid1", "mime_type": "video/mp4", "file_name": "cards-demo.mp4", "duration_ms": 95000,
             "thumbnail_url": "/media/thumb-slow"},
            {"artifact_id": "vid-plain", "type": "video", "status": "ready", "session": s, "title": "No poster",
             "url": "/media/vid1", "mime_type": "video/mp4", "file_name": "plain.mp4"},
            {"artifact_id": "vid-gone", "type": "video", "status": "ready", "session": s, "title": "Expired video",
             "url": "/media/gone", "mime_type": "video/mp4", "file_name": "gone.mp4"},
            {"artifact_id": "vid-none", "type": "video", "status": "failed", "session": s, "title": "A video that never rendered"},
        ]
    if kind == "file":
        return [
            {"artifact_id": "file-pdf", "type": "file", "status": "ready", "session": s, "title": "The signed contract",
             "url": "/media/pdf1", "mime_type": "application/pdf", "file_name": "contract-2026-signed.pdf", "size_bytes": 1258291},
            {"artifact_id": "file-csv", "type": "file", "status": "ready", "session": s, "title": "Numbers",
             "url": "/media/csv1", "mime_type": "text/csv",
             "file_name": "third-quarter-numbers-for-every-region-and-store-with-the-audit-flags.csv", "size_bytes": 2048},
            {"artifact_id": "file-gone", "type": "file", "status": "ready", "session": s, "title": "Expired",
             "url": "/media/gone", "mime_type": "application/zip", "file_name": "logs.zip", "size_bytes": 900},
            {"artifact_id": "file-elsewhere", "type": "file", "status": "ready", "session": s, "title": "Elsewhere",
             "url": "https://files.example/x.bin", "mime_type": "application/octet-stream", "file_name": "x.bin"},
        ]
    if kind == "release":
        return [
            {"artifact_id": "rel-ready", "type": "release", "status": "ready", "session": s,
             "title": "Clarp 2.4: artifact cards in the Slint desktop client, and the keyboard to reach them",
             "version": "2.4.0", "build": "1234", "commit": "4560a8ed", "repository": "clarp",
             "content": "## What's new\n\n- Every artifact type embedded\n- **J/K** reach the cards", "source_url": "https://github.com/example/clarp/releases/2.4.0"},
            {"artifact_id": "rel-failed", "type": "release", "status": "failed", "session": s, "title": "Nightly", "commit": "deadbeef"},
            {"artifact_id": "rel-active", "type": "release", "status": "active", "session": s, "title": "Rolling out"},
        ]
    if kind == "directory":
        return [
            {"artifact_id": "dir-out", "type": "directory", "status": "ready", "session": s, "title": "The build's output",
             "root": "workspace", "relative_path": "build/out"},
            {"artifact_id": "dir-home", "type": "directory", "status": "ready", "session": s, "title": "Downloads", "root": "home",
             "relative_path": "Downloads"},
            {"artifact_id": "dir-escape", "type": "directory", "status": "ready", "session": s, "title": "Somewhere it should not go",
             "root": "workspace", "relative_path": "../../etc"},
        ]
    if kind == "workflow_run":
        run = lambda n: f"https://github.com/example/clarp/actions/runs/{n}"
        return [
            {"artifact_id": "wf-ci", "type": "workflow_run", "status": "active", "session": s,
             "title": "CI for the artifact cards branch: build, unit tests, every headless check, and the release bundle",
             "provider": "github", "run_id": "901", "run_url": run(901), "workflow_name": "CI",
             "current_step": "Run the headless checks (artifacts, transcript, composer, panes)", "total_steps": 8, "completed_steps": 3},
            {"artifact_id": "wf-queued", "type": "workflow_run", "status": "active", "session": s, "title": "Nightly",
             "provider": "github", "run_id": "902", "run_url": run(902), "workflow_name": "Nightly"},
            {"artifact_id": "wf-done", "type": "workflow_run", "status": "completed", "conclusion": "success", "session": s,
             "title": "Lint", "provider": "github", "run_id": "903", "run_url": run(903), "workflow_name": "Lint",
             "total_steps": 4, "completed_steps": 4},
            {"artifact_id": "wf-failed", "type": "workflow_run", "status": "completed", "conclusion": "failure", "session": s,
             "title": "Deploy", "provider": "github", "run_id": "904", "run_url": run(904), "workflow_name": "Deploy",
             "current_step": "Push the image", "total_steps": 5, "completed_steps": 2},
        ]
    raise KeyError(kind)


def artifact_chat(session, kinds, now_ms):
    """A chat where each artifact is made while its own reply is written:
    an ask, then a reply 30 s later, the artifact 10 s into it. The last
    reply carries the last artifact, so the chat ends on a card."""
    made = [a for kind in kinds for a in artifact_fixtures(kind, session, now_ms)]
    base = now_ms - 3 * 3_600_000
    rows = []
    for index, artifact in enumerate(made):
        at = base + index * 60_000
        artifact.setdefault("created_at", at + 10_000)
        artifact.setdefault("updated_at", at + 10_000)
        rows.append({"id": f"ask-{index}", "role": "user", "text": f"Make the {artifact['type'].replace('_', ' ')} please",
                     "timestamp": iso_at(at)})
        rows.append({"id": f"made-{artifact['artifact_id']}", "role": "assistant",
                     "text": f"Here it is: **{artifact.get('title') or artifact.get('file_name', '')}**.",
                     "timestamp": iso_at(at + 30_000)})
    return rows, made


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
        if url.path == "/media/pdf1":
            return self.reply_bytes(200, b"%PDF-1.4 fixture contract", "application/pdf")
        if url.path == "/media/csv1":
            return self.reply_bytes(200, b"region,q3\nNordics,150\n", "text/csv")
        if url.path in ("/media/img-chart", "/media/img-a", "/media/img-b", "/media/img-c"):
            colours = {"/media/img-chart": (200, 80, 40, 255), "/media/img-a": (40, 120, 200, 255),
                       "/media/img-b": (60, 180, 90, 255), "/media/img-c": (220, 190, 40, 255)}
            return self.reply_bytes(200, solid_png(400, 240, colours[url.path]), "image/png")
        if url.path == "/media/img-slow":
            # An image that takes its time: the message must not grow when it lands.
            time.sleep(1.5)
            return self.reply_bytes(200, solid_png(300, 300, (150, 60, 200, 255)), "image/png")
        if url.path == "/media/vid1":
            return self.reply_bytes(200, b"\x00\x00\x00\x18ftypmp42fixture-video", "video/mp4")
        if url.path == "/media/thumb-slow":
            # A poster that takes its time: the card must not grow when it lands.
            time.sleep(1.5)
            return self.reply_bytes(200, solid_png(320, 180, (40, 160, 90, 255)), "image/png")
        if url.path == "/media/aud1":
            data = (pathlib.Path(__file__).parent / "fixtures" / "clip.mp3").read_bytes()
            return self.reply_bytes(200, data, "audio/mpeg")
        if url.path == "/media/gone":
            return self.reply(404, {"error": "media expired"})
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
        if url.path == "/__control/artifact-chat":
            # Test control: `session` (a new agent unless it exists) gets a
            # chat of one reply per fixture artifact of `types`; its
            # artifacts replace that chat's, the other chats' stay.
            session = body.get("session", "rachel")
            rows, made = artifact_chat(session, body.get("types", []), int(time.time() * 1000))
            # `suffix` keeps one chat's artifact (and decision) ids its own.
            for artifact in made:
                artifact["artifact_id"] += body.get("suffix", "")
                if isinstance(artifact.get("decision"), dict):
                    artifact["decision"]["decision_id"] += body.get("suffix", "")
            created = False
            with state_lock:
                for row in rows:
                    revision += 1
                    row["revision"] = revision
                turns[session] = rows
                artifacts = [a for a in artifacts if a.get("session") != session] + made
                if not any(agent["session"] == session for agent in agents):
                    agents.append({"agent_id": session + "-id", "session": session, "persona": body.get("persona", session.title()),
                                   "cwd": body.get("cwd", ""),
                                   "backend": "claude", "latest_state": "idle", "alive": True, "last_activity": 3000,
                                   "conversation_id": "c-" + session, "head_revision": revision})
                    created = True
                for agent in agents:
                    if agent["session"] == session:
                        agent["head_revision"] = revision
            if created:
                broadcast({"type": "agent-roster", "session": session, "kind": "created"})
            broadcast({"type": "transcript-updated", "session": session})
            broadcast({"type": "artifact-updated", "session": session})
            return self.reply(200, {"ok": True, "artifacts": made})
        if url.path == "/__control/artifact-settle":
            # Test control: cards change height as the Host moves on: each
            # summary grows by `more`, each pending decision is approved.
            now_ms = int(time.time() * 1000)
            with state_lock:
                for artifact in artifacts:
                    if artifact.get("session") != body.get("session"):
                        continue
                    if artifact.get("summary"):
                        artifact["summary"] += " " + body.get("more", "")
                        artifact["updated_at"] = now_ms
                    plan = artifact.get("plan") or {}
                    if plan.get("status") == "active" and plan.get("items"):
                        # The step under way finishes (with its subtasks), the next begins.
                        flat = []
                        def walk(items):
                            for it in items:
                                flat.append(it)
                                walk(it.get("subtasks", []))
                        walk(plan["items"])
                        top = plan["items"]
                        current = next((it for it in top if it.get("status") == "in_progress"), None)
                        if current is not None:
                            current["status"] = "completed"
                            for sub in current.get("subtasks", []):
                                sub["status"] = "completed"
                            following = next((it for it in top if it.get("status") == "pending"), None)
                            if following is not None:
                                following["status"] = "in_progress"
                            else:
                                plan["status"] = "completed"
                                artifact["status"] = "completed"
                            plan["completed_count"] = sum(1 for it in flat if it.get("status") == "completed")
                            artifact["updated_at"] = now_ms
                    held = artifact.get("decision") or {}
                    if held.get("status") == "pending":
                        held.update({"status": "accepted", "resolved_choice": "accepted", "revision": held["revision"] + 1})
                        artifact["status"] = "completed"
                        artifact["updated_at"] = now_ms
            broadcast({"type": "artifact-updated", "session": body.get("session", "")})
            return self.reply(200, {"ok": True})
        if url.path == "/__control/artifact-update":
            # Test control: change one artifact's fields in place (a later
            # updated_at) and/or add `add` artifacts, then announce it.
            with state_lock:
                for artifact in artifacts:
                    if artifact.get("artifact_id") == body.get("id"):
                        artifact.update(body.get("set", {}))
                        artifact["updated_at"] = int(time.time() * 1000)
                for extra in body.get("add", []):
                    artifacts.append({"created_at": int(time.time() * 1000), "updated_at": int(time.time() * 1000), **extra})
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
        if url.path.startswith("/decisions/") and (url.path.endswith("/resolve") or url.path.endswith("/dismiss")):
            # A resolved decision leaves the attention list; an artifact's
            # decision takes the answer, unless the user saw an older one.
            decision = url.path.split("/")[2]
            owner = None
            with state_lock:
                attention[:] = [a for a in attention if a.get("id") != decision]
                for artifact in artifacts:
                    held = artifact.get("decision") or {}
                    if held.get("decision_id") == decision:
                        owner = artifact
                if owner is not None:
                    held = owner["decision"]
                    # `_host_revision`: the Host moved on since the card was listed.
                    if body.get("expected_revision") != held.get("_host_revision", held.get("revision")):
                        return self.reply(409, {"error": "the decision changed since you saw it"})
                    if url.path.endswith("/dismiss"):
                        held["status"] = "cancelled"
                    elif "answer" in body:
                        answer = body["answer"]
                        label = next((o["label"] for o in held.get("options", []) if o["id"] == answer.get("option_id")), "")
                        held["status"] = "answered"
                        held["answer"] = {**answer, "label": label}
                    else:
                        held["status"] = body.get("choice")
                        held["resolved_choice"] = body.get("choice")
                    held["revision"] += 1
                    owner["status"] = "completed"
                    owner["updated_at"] = int(time.time() * 1000)
            if owner is not None:
                broadcast({"type": "artifact-updated", "session": owner.get("session", "")})
            return self.reply(200, {"ok": True, "decision_id": decision, "status": body.get("choice")})
        if url.path.startswith("/artifacts/") and url.path.endswith("/submit"):
            # A form's answers: a receipt echoing the submission, or 409
            # when the form changed since it was opened.
            artifact = url.path.split("/")[2]
            if artifact == "form-stale":
                return self.reply(409, {"error": "form version changed"})
            return self.reply(200, {"accepted": True, "submission_id": body.get("submission_id"),
                                    "artifact_id": artifact, "version": body.get("version")})
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
