#!/usr/bin/env python3
"""Ask a Clarp agent for its status through a throwaway fork of its conversation.

The fork reuses Clarp's exact launch argv so it hits the agent's own prompt
cache, runs with --permission-mode dontAsk (only allowlisted tools execute) and
is not persisted, so the agent itself is never interrupted, queued behind, or
changed. Claude-backend agents only for now.

  clarp-status-fork pebble [hobbles ...] [--question TEXT] [--json]
"""
import argparse
import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SHARE = Path(os.environ.get("CLARP_SHARE_DIR", Path.home() / ".local/share/clarp"))
DB = Path(os.environ.get("CLAUDE_PWA_DB", SHARE / "state.sqlite"))
CODE_ROOT = Path(os.environ.get("CLARP_CODE_ROOT", SHARE / "current"))
DEFAULT_QUESTION = (
    "STATUS SNAPSHOT REQUEST from the user, answered by a read-only fork of you. "
    "Do not continue any work and do not call tools. In max 8 short lines: what you "
    "are working on right now, how far along (done / remaining), what is blocking, "
    "anything waiting on the user's decision, and ETA. Use times from the conversation. "
    "If unsure, say so.")


def _clock(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "-"


def live_facts(con, agent):
    """Facts that go stale in the agent's own memory, read fresh from Clarp.

    Appended after the question, so the cached conversation prefix is untouched.
    """
    aid, now = agent["agent_id"], int(time.time() * 1000)
    since = now - 12 * 3600 * 1000
    lines = [f"Now: {_clock(now)} {time.strftime('%Z')} (local time)."]
    last = con.execute(
        "SELECT max(timestamp) FROM messages WHERE agent_id=?", (aid,)).fetchone()[0]
    running = con.execute(
        "SELECT started_at FROM provider_turns WHERE agent_id=? AND exited_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1", (aid,)).fetchone()
    queued = con.execute(
        "SELECT count(*), min(enqueued_at) FROM queued_turns WHERE agent_id=? AND status='queued'",
        (aid,)).fetchone()
    lines.append(
        f"You: last message {last or '-'} (UTC); "
        + (f"turn running since {_clock(running[0])}" if running else "idle, no turn running")
        + (f"; {queued[0]} queued message(s), oldest {_clock(queued[1])}" if queued[0] else ""))
    helpers = con.execute(
        "SELECT agent_id, session, helper_state, helper_completed_at, created_at FROM agents "
        "WHERE parent_agent_id=? AND deleted_at IS NULL", (aid,)).fetchall()
    owners = {aid: "you", **{h[0]: h[1] for h in helpers}}
    recent = [h for h in helpers if h[2] == "running" or (h[3] or h[4] or 0) > since]
    if recent:
        lines.append("Your helper agents (running, or finished in the last 12 h):")
        for _, session, state, completed, created in sorted(
                recent, key=lambda h: h[3] or h[4] or 0, reverse=True)[:15]:
            when = f"finished {_clock(completed)}" if completed else f"created {_clock(created)}"
            lines.append(f"- {session}: {state or 'unknown'}, {when}")
    q = ",".join("?" * len(owners))
    jobs = con.execute(
        f"""SELECT agent_id, title, status, started_at, terminal_at, terminal_reason, progress_text
            FROM background_jobs WHERE agent_id IN ({q})
              AND (terminal_at IS NULL OR terminal_at > ?) AND updated_at > ?
            ORDER BY coalesce(terminal_at, updated_at) DESC LIMIT 25""",
        (*owners, since, since)).fetchall()
    if jobs:
        lines.append("Background jobs of you and your helpers (last 12 h):")
        for owner, title, status, started, ended, reason, progress in jobs:
            when = f"ended {_clock(ended)}" if ended else f"started {_clock(started)}"
            extra = f"; {reason}" if reason else (f"; {progress[:80]}" if progress else "")
            lines.append(f"- [{owners[owner]}] {title[:70]}: {status}, {when}{extra}")
    cards = con.execute(
        """SELECT d.question, d.status, d.resolved_choice, d.resolved_by, d.resolved_at, a.created_at
           FROM artifact_decisions d JOIN artifacts a ON a.artifact_id=d.artifact_id
           WHERE a.agent_id=? AND (d.status='pending' OR d.resolved_at > ?)
           ORDER BY d.status='pending' DESC, coalesce(d.resolved_at, a.created_at) DESC
           LIMIT 10""", (aid, since)).fetchall()
    if cards:
        lines.append("Your cards for the user (pending, or resolved in the last 12 h):")
        for question, status, choice, by, resolved, created in cards:
            state = (f"PENDING since {_clock(created)}" if status == "pending"
                     else f"{status} ({choice} by {by}) at {_clock(resolved)}")
            lines.append(f"- {question[:90]}: {state}")
    return "\n".join(lines)


def with_facts(question, facts):
    return (f"{question}\n\nLIVE FACTS read from Clarp just now. They override what you "
            f"remember; where they differ, say so briefly:\n{facts}")


def resolve(con, name):
    rows = con.execute(
        """SELECT agent_id, session, persona, backend, cwd, model, effort FROM agents
           WHERE lower(session)=lower(?) OR lower(persona)=lower(?)
              OR lower(session) LIKE lower(?) || '-%'""",
        (name, name, name)).fetchall()
    if not rows:
        raise SystemExit(f"no Clarp agent matches {name!r}")
    if len(rows) > 1:
        exact = [r for r in rows if r[1].lower() == name.lower()]
        if len(exact) != 1:
            # Prefer the most recently active match rather than guessing silently.
            ids = [r[0] for r in rows]
            q = ",".join("?" * len(ids))
            latest = con.execute(
                f"""SELECT agent_id FROM prompt_admissions WHERE agent_id IN ({q})
                    ORDER BY observed_at DESC LIMIT 1""", ids).fetchone()
            exact = [r for r in rows if latest and r[0] == latest[0]] or exact
        if len(exact) != 1:
            raise SystemExit(f"{name!r} is ambiguous: " + ", ".join(r[1] for r in rows))
        rows = exact
    agent_id, session, persona, backend, cwd, model, effort = rows[0]
    head = con.execute(
        """SELECT backend_session_id FROM conversation_heads WHERE agent_id=?
           ORDER BY revision DESC LIMIT 1""", (agent_id,)).fetchone()
    if not head:
        raise SystemExit(f"{session}: no native conversation recorded yet")
    return {"agent_id": agent_id, "session": session, "persona": persona, "backend": backend,
            "cwd": cwd, "model": model, "effort": effort, "native_id": head[0]}


# Turn-streaming flags that do not shape the model request; everything else in
# Clarp's argv (model, effort, thinking display, plugin, MCP scope, persona
# prompt) must match the agent's own turns, or the prompt cache is not reused
# and every fork reloads the whole context.
_STREAM_FLAGS = {"--include-partial-messages", "--verbose"}
_STREAM_OPTS = {"--input-format", "--output-format"}


def _claude_backend():
    # A release keeps lib/ at its root; a source checkout keeps it under server/.
    for root in (CODE_ROOT, CODE_ROOT / "server"):
        if (root / "lib" / "backend" / "claude.py").is_file():
            sys.path.insert(0, str(root))
            break
    from lib.backend.claude import ClaudeBackend
    return ClaudeBackend()


def clarp_fork_argv(agent, question):
    """Clarp's own Claude argv for this agent, turned into a read-only fork."""
    cmd = _claude_backend().build_cmd(
        agent["native_id"], model=agent["model"] or "", effort=agent["effort"] or "",
        persona=agent["persona"] or "", session=agent["session"])
    return fork_argv(cmd, question)


def fork_argv(cmd, question):
    """Turn an agent's turn argv into a one-shot, read-only, unsaved fork."""
    out, skip = [], False
    for arg in cmd:
        if skip:
            skip = False
            continue
        if arg in _STREAM_OPTS:
            skip = True
            continue
        if arg in _STREAM_FLAGS:
            continue
        # Same tool list (keeps the cache), but nothing runs without an allow rule.
        if arg == "--dangerously-skip-permissions":
            out += ["--permission-mode", "dontAsk"]
            continue
        out.append(arg)
    return out + ["--fork-session", "--no-session-persistence",
                  "--output-format", "json", question]


def fork_status(agent, question):
    if agent["backend"] != "claude":
        return {**agent, "ok": False,
                "error": f"backend {agent['backend']} not supported yet (Claude only)"}
    started = time.monotonic()
    env = {k: v for k, v in os.environ.items() if k not in (
        "CLAUDE_PWA_SESSION", "CLARP_SESSION", "CLAUDE_CODE_SESSION_ID",
        "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION")}
    proc = subprocess.run(
        clarp_fork_argv(agent, question),
        cwd=agent["cwd"] if os.path.isdir(agent["cwd"]) else None,
        env=env, capture_output=True, text=True, timeout=300)
    seconds = round(time.monotonic() - started, 1)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {**agent, "ok": False, "seconds": seconds,
                "error": (proc.stderr or proc.stdout).strip()[-500:]}
    usage = data.get("usage") or {}
    return {**agent, "ok": not data.get("is_error"), "seconds": seconds,
            "status": data.get("result", ""),
            "reused_tokens": usage.get("cache_read_input_tokens", 0),
            "new_tokens": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)}


def main():
    ap = argparse.ArgumentParser(prog="clarp-status-fork", description=__doc__.splitlines()[0])
    ap.add_argument("agents", nargs="+", help="Clarp session slug or persona name")
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-facts", action="store_true", help="skip the live fact sheet")
    ap.add_argument("--show-facts", action="store_true", help="print the fact sheet only")
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    agents = [resolve(con, a) for a in args.agents]
    for a in agents:
        a["facts"] = "" if args.no_facts else live_facts(con, a)
    if args.show_facts:
        for a in agents:
            print(f"## {a['persona']}\n{a['facts']}\n")
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
        results = list(pool.map(
            lambda a: fork_status(a, with_facts(args.question, a["facts"]) if a["facts"]
                                  else args.question), agents))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    for r in results:
        print(f"## {r['persona']} ({r['session']})", end="")
        if r["ok"]:
            print(f" · {r['seconds']}s · {r['reused_tokens']//1000}k reused,"
                  f" {r['new_tokens']//1000}k new\n{r['status']}\n")
        else:
            print(f"\nERROR: {r['error']}\n", file=sys.stdout)


if __name__ == "__main__":
    main()
