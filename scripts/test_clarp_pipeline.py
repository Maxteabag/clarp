#!/usr/bin/env python3
"""End-to-end test harness for the clarp dispatch path.

Drives /send via HTTP against the live server, watches the SSE stream
in parallel, polls the DB + filesystem for downstream effects, and
prints a stage-by-stage report so we can spot exactly where the
pipeline fails for any agent.

Usage:
    scripts/test_clarp_pipeline.py [--session <session>] [--prompt <text>] [--timeout <sec>]

Stages checked:
    1. /send returns 200 with `dispatch: clarp`
    2. clarp subprocess spawned (server log has clarpSpawn)
    3. clarp produced a system.init event (server log has clarpSessionInit)
    4. UserPromptSubmit hook ran → new turn row with source='pwa'
    5. agent state transitioned to 'thinking' (SSE agent-state event)
    6. JSONL transcript grew (file size increased)
    7. transcript_streamer broadcast transcript-updated (SSE)
    8. tts_queue gained a row carrying the assistant text
    9. agent state transitioned to 'idle' (SSE agent-state event)
   10. tts_queue row reached status='done' OR an audio SSE event arrived

The harness FAILS LOUDLY on each missing stage with the exact server
log lines that would explain it.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field

BASE = "http://127.0.0.1:7682"
CONFIG = pathlib.Path.home() / ".config/clarp/config.toml"
DB     = pathlib.Path.home() / ".local/share/clarp/state.sqlite"


def auth_header() -> dict:
    try:
        for line in CONFIG.read_text().splitlines():
            if line.startswith("auth_token"):
                tok = line.split("=", 1)[1].strip().strip('"')
                if tok:
                    return {"Authorization": f"Bearer {tok}"}
    except Exception:
        pass
    return {}


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def get_agent(session: str) -> dict | None:
    with db() as c:
        row = c.execute(
            "SELECT a.agent_id, a.persona, a.cwd, a.session, r.backend_session_id "
            "FROM agents a LEFT JOIN runtimes r USING (agent_id) "
            "WHERE a.session = ? AND a.deleted_at IS NULL "
            "AND (r.ended_at IS NULL OR r.ended_at IS NULL)",
            (session,),
        ).fetchone()
    return dict(row) if row else None


def find_jsonl(backend_session_id: str, cwd: str) -> pathlib.Path | None:
    """Mirror lib.transcript_log.find_latest_jsonl's lookup rule."""
    projects = pathlib.Path.home() / ".claude/projects"
    # Project dir name: cwd with '/' → '-' (Claude Code's encoding)
    candidates = [
        projects / ("-" + cwd.lstrip("/").replace("/", "-")) / f"{backend_session_id}.jsonl",
    ]
    for proj in projects.glob("*"):
        if proj.is_dir():
            candidates.append(proj / f"{backend_session_id}.jsonl")
    for c in candidates:
        if c.is_file():
            return c
    return None


@dataclass
class SSECapture:
    """Background thread that captures SSE events for analysis."""
    events: list[dict] = field(default_factory=list)
    stop_flag: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            req = urllib.request.Request(BASE + "/events", headers=auth_header())
            with urllib.request.urlopen(req, timeout=60) as r:
                for raw in r:
                    if self.stop_flag.is_set():
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    ev["_ts"] = time.monotonic()
                    self.events.append(ev)
        except Exception as e:
            self.events.append({"_error": str(e), "_ts": time.monotonic()})

    def stop(self) -> None:
        self.stop_flag.set()

    def filter(self, typ: str, *, agent_id: str | None = None) -> list[dict]:
        out = []
        for ev in self.events:
            if ev.get("type") != typ:
                continue
            if agent_id and ev.get("agent_id") != agent_id:
                continue
            out.append(ev)
        return out


def post_send(text: str, session: str) -> tuple[int, dict]:
    body = json.dumps({"text": text, "session": session}).encode()
    headers = {"Content-Type": "application/json", **auth_header()}
    req = urllib.request.Request(BASE + "/send", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()}


def tail_service_log(since_ts: float) -> list[str]:
    """journalctl since the given monotonic-clock timestamp (translated
    to absolute via wall-clock delta)."""
    # journalctl needs --since in absolute time. We approximate: use
    # `--since "5 seconds ago"` which slightly overshoots but is fine
    # for diagnostics.
    secs_ago = max(1, int(time.monotonic() - since_ts) + 2)
    p = subprocess.run(
        ["journalctl", "--user", "-u", "clarp.service",
         "--since", f"{secs_ago} seconds ago", "--no-pager"],
        capture_output=True, text=True, timeout=5,
    )
    return p.stdout.splitlines()


def assert_stage(label: str, cond: bool, *, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    line = f"  {mark} {label}"
    if detail:
        line += f"  — {detail}"
    print(line, flush=True)
    return cond


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="domi",
                    help="session name (=agent picker) to target")
    ap.add_argument("--prompt", default="Reply with exactly one short sentence about pencils.",
                    help="prompt text to send")
    ap.add_argument("--timeout", type=float, default=45.0,
                    help="max seconds to wait for pipeline completion")
    args = ap.parse_args()

    print(f"== clarp pipeline test ==")
    print(f"   session: {args.session}")
    print(f"   prompt:  {args.prompt!r}")
    print()

    # --- 0. preflight + pre-state -----------------------------------
    agent = get_agent(args.session)
    if not agent:
        print(f"!! no agent with session={args.session!r}; aborting")
        return 2
    agent_id = agent["agent_id"]
    persona  = agent["persona"]
    pre_backend_session_id = agent["backend_session_id"] or ""
    pre_jsonl = find_jsonl(pre_backend_session_id, agent["cwd"]) if pre_backend_session_id else None
    pre_jsonl_size = pre_jsonl.stat().st_size if pre_jsonl and pre_jsonl.is_file() else 0
    with db() as c:
        last_turn_id = c.execute(
            "SELECT MAX(turn_id) AS t FROM turns WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()["t"] or 0
        last_queue_id = c.execute(
            "SELECT MAX(queue_id) AS q FROM tts_queue WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()["q"] or 0

    print(f"-- preflight --")
    print(f"   agent_id          = {agent_id}")
    print(f"   persona           = {persona}")
    print(f"   cwd               = {agent['cwd']}")
    print(f"   backend_session_id    = {pre_backend_session_id or '∅'}")
    print(f"   transcript JSONL  = {pre_jsonl or '∅'}")
    print(f"   pre JSONL size    = {pre_jsonl_size}")
    print(f"   last turn_id      = {last_turn_id}")
    print(f"   last queue_id     = {last_queue_id}")
    print()

    # Agent-id collision check.
    with db() as c:
        if pre_backend_session_id:
            collisions = c.execute(
                "SELECT a.persona FROM agents a JOIN runtimes r USING (agent_id) "
                "WHERE r.backend_session_id = ? AND r.ended_at IS NULL "
                "AND a.deleted_at IS NULL AND a.persona != ?",
                (pre_backend_session_id, persona),
            ).fetchall()
        else:
            collisions = []
    if collisions:
        print(f"!! WARNING: backend_session_id {pre_backend_session_id} is also bound to: "
              f"{[r['persona'] for r in collisions]}.")
        print(f"   the hook's get_by_backend_session() will resolve to ONE of them — "
              f"likely not {persona}.")
        print()

    # --- 1. start SSE listener --------------------------------------
    sse = SSECapture()
    sse.start()
    time.sleep(0.3)   # let it connect

    t_start = time.monotonic()

    # --- 2. POST /send ---------------------------------------------
    print(f"-- POST /send --")
    status, body = post_send(args.prompt, args.session)
    ok_post = assert_stage(f"HTTP 200 from /send (got {status})", status == 200,
                            detail=json.dumps(body))
    ok_dispatch = assert_stage("dispatch == 'clarp'",
                                body.get("dispatch") == "clarp")
    if not ok_post:
        sse.stop()
        return 1
    print()

    # --- 3. wait + poll downstream effects --------------------------
    deadline = t_start + args.timeout
    saw = {
        "clarpSpawn": False, "clarpSessionInit": False,
        "agent_thinking": False, "agent_idle": False,
        "transcript_updated": 0, "audio": 0,
        "new_turn_id": 0, "new_turn_source": "",
        "new_queue_id": 0, "queue_text": "", "queue_status": "",
        "jsonl_grew": False, "jsonl_size_now": pre_jsonl_size,
    }
    while time.monotonic() < deadline:
        time.sleep(0.4)
        # SSE-driven signals
        if not saw["agent_thinking"]:
            for ev in sse.filter("agent-state", agent_id=agent_id):
                if ev.get("kind") == "thinking":
                    saw["agent_thinking"] = True; break
        if not saw["agent_idle"]:
            for ev in sse.filter("agent-state", agent_id=agent_id):
                if ev.get("kind") == "idle":
                    saw["agent_idle"] = True; break
        saw["transcript_updated"] = len(sse.filter("transcript-updated", agent_id=agent_id))
        saw["audio"] = len([e for e in sse.events
                            if e.get("type") == "audio"
                            and e.get("session") == args.session])
        # DB signals
        with db() as c:
            r = c.execute(
                "SELECT turn_id, source FROM turns "
                "WHERE agent_id = ? AND turn_id > ? "
                "ORDER BY turn_id DESC LIMIT 1",
                (agent_id, last_turn_id),
            ).fetchone()
            if r:
                saw["new_turn_id"] = r["turn_id"]; saw["new_turn_source"] = r["source"]
            r = c.execute(
                "SELECT queue_id, substr(text, 1, 80) AS text, status "
                "FROM tts_queue WHERE agent_id = ? AND queue_id > ? "
                "ORDER BY queue_id DESC LIMIT 1",
                (agent_id, last_queue_id),
            ).fetchone()
            if r:
                saw["new_queue_id"] = r["queue_id"]; saw["queue_text"] = r["text"]
                saw["queue_status"] = r["status"]
        # JSONL growth (re-resolve in case backend conversation changed).
        with db() as c:
            r = c.execute(
                "SELECT backend_session_id FROM runtimes WHERE agent_id = ? "
                "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        cs_now = (r and r["backend_session_id"]) or ""
        if cs_now:
            j = find_jsonl(cs_now, agent["cwd"])
            if j and j.is_file():
                saw["jsonl_size_now"] = j.stat().st_size
                if saw["jsonl_size_now"] > pre_jsonl_size:
                    saw["jsonl_grew"] = True

        # Done condition: turn ended (audio in done state OR result fired)
        if saw["agent_idle"] and saw["queue_status"] in ("done", "failed"):
            break

    sse.stop()

    # --- 4. server log evidence ------------------------------------
    log_lines = tail_service_log(t_start)
    for needle in ("clarpSpawn", "clarpSessionInit"):
        saw[needle] = any(needle in l for l in log_lines)

    # --- 5. report -------------------------------------------------
    print(f"-- pipeline stages --")
    s1 = assert_stage("clarp subprocess spawned (log: clarpSpawn)", saw["clarpSpawn"])
    s2 = assert_stage("clarp emitted system.init (log: clarpSessionInit)", saw["clarpSessionInit"])
    s3 = assert_stage(
        f"hook opened new PWA turn (got source={saw['new_turn_source']!r})",
        saw["new_turn_source"] == "pwa",
        detail=f"turn_id={saw['new_turn_id']}" if saw["new_turn_id"] else "no new turn row",
    )
    s4 = assert_stage("agent-state thinking (SSE)", saw["agent_thinking"])
    s5 = assert_stage("JSONL transcript grew",
                       saw["jsonl_grew"],
                       detail=f"{pre_jsonl_size} → {saw['jsonl_size_now']} bytes")
    s6 = assert_stage(f"transcript-updated SSE broadcast ({saw['transcript_updated']} events)",
                       saw["transcript_updated"] > 0)
    s7 = assert_stage(
        f"tts_queue row enqueued (queue_id={saw['new_queue_id']})",
        saw["new_queue_id"] > 0,
        detail=f"text={saw['queue_text']!r}" if saw["queue_text"] else "no row enqueued",
    )
    s8 = assert_stage("agent-state idle (SSE)", saw["agent_idle"])
    s9 = assert_stage(
        f"tts_queue row reached terminal status (got {saw['queue_status']!r})",
        saw["queue_status"] in ("done", "failed"),
    )
    s10 = assert_stage(
        f"audio SSE event for {args.session}",
        saw["audio"] > 0,
        detail=f"{saw['audio']} audio events" if saw["audio"] else "no audio SSE seen",
    )

    print()
    all_ok = all([s1, s2, s3, s4, s5, s6, s7, s8, s9, s10])
    print(f"== {'PASS' if all_ok else 'FAIL'} ==  ({len(sse.events)} total SSE events)")

    if not all_ok:
        print()
        print("-- last 30 service log lines for context --")
        for l in log_lines[-30:]:
            print(f"   {l}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
