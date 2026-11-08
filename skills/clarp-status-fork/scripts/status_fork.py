#!/usr/bin/env python3
"""Ask a Clarp agent for its status through a throwaway fork of its conversation.

The fork reuses Clarp's exact launch argv so it hits the agent's own prompt
cache, may only read (a PreToolUse guard denies every other tool, whatever the
inherited permission rules allow), and is not persisted, so the agent itself is
never interrupted, queued behind, or changed. Claude-backend agents only for now.

  clarp-status-fork pebble [hobbles ...] [--question TEXT] [--json]
"""
import argparse
import concurrent.futures
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

SHARE = Path(os.environ.get("CLARP_SHARE_DIR", Path.home() / ".local/share/clarp"))
DB = Path(os.environ.get("CLAUDE_PWA_DB", SHARE / "state.sqlite"))
CODE_ROOT = Path(os.environ.get("CLARP_CODE_ROOT", SHARE / "current"))
GUARD = Path(__file__).resolve().with_name("read_only_guard.py")
DEFAULT_QUESTION = (
    "STATUS SNAPSHOT REQUEST from the user, answered by a read-only fork of you. "
    "Do not continue any work and do not call tools. In max 8 short lines: what you "
    "are working on right now, how far along (done / remaining), what is blocking, "
    "anything waiting on the user's decision, and ETA. Use times from the conversation. "
    "If unsure, say so.")


def _literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def query(sql, *params):
    """Rows as dicts, read through the sqlite3 CLI in -readonly mode.

    The live database is only ever read through `sqlite3 -readonly` (the Host's
    standing rule), so values are inlined as quoted SQL literals; `?` must not
    appear in the SQL text except as a placeholder.
    """
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise ValueError(f"{len(parts) - 1} placeholders for {len(params)} values")
    text = parts[0] + "".join(_literal(p) + rest for p, rest in zip(params, parts[1:]))
    proc = subprocess.run(["sqlite3", "-readonly", "-json", str(DB), text],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"sqlite3 -readonly failed ({proc.returncode}): "
                           f"{proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else []


def _clock(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "-"


def live_facts(agent):
    """Facts that go stale in the agent's own memory, read fresh from Clarp.

    Appended after the question, so the cached conversation prefix is untouched.
    """
    aid, now = agent["agent_id"], int(time.time() * 1000)
    since = now - 12 * 3600 * 1000
    lines = [f"Now: {_clock(now)} {time.strftime('%Z')} (local time)."]
    last = query("SELECT max(timestamp) AS t FROM messages WHERE agent_id=?", aid)
    running = query("SELECT started_at FROM provider_turns WHERE agent_id=? AND exited_at IS NULL "
                    "ORDER BY started_at DESC LIMIT 1", aid)
    queued = query("SELECT count(*) AS n, min(enqueued_at) AS oldest FROM queued_turns "
                   "WHERE agent_id=? AND status='queued'", aid)[0]
    lines.append(
        f"You: last message {(last and last[0]['t']) or '-'} (UTC); "
        + (f"turn running since {_clock(running[0]['started_at'])}" if running
           else "idle, no turn running")
        + (f"; {queued['n']} queued message(s), oldest {_clock(queued['oldest'])}"
           if queued["n"] else ""))
    helpers = query("SELECT agent_id, session, helper_state, helper_completed_at, created_at "
                    "FROM agents WHERE parent_agent_id=? AND deleted_at IS NULL", aid)
    owners = {aid: "you", **{h["agent_id"]: h["session"] for h in helpers}}

    def moment(h):
        return h["helper_completed_at"] or h["created_at"] or 0

    recent = [h for h in helpers if h["helper_state"] == "running" or moment(h) > since]
    if recent:
        lines.append("Your helper agents (running, or finished in the last 12 h):")
        for h in sorted(recent, key=moment, reverse=True)[:15]:
            when = (f"finished {_clock(h['helper_completed_at'])}" if h["helper_completed_at"]
                    else f"created {_clock(h['created_at'])}")
            lines.append(f"- {h['session']}: {h['helper_state'] or 'unknown'}, {when}")
    marks = ",".join("?" * len(owners))
    jobs = query(
        f"""SELECT agent_id, title, status, started_at, terminal_at, terminal_reason, progress_text
            FROM background_jobs WHERE agent_id IN ({marks})
              AND (terminal_at IS NULL OR terminal_at > ?) AND updated_at > ?
            ORDER BY coalesce(terminal_at, updated_at) DESC LIMIT 25""",
        *owners, since, since)
    if jobs:
        lines.append("Background jobs of you and your helpers (last 12 h):")
        for j in jobs:
            when = (f"ended {_clock(j['terminal_at'])}" if j["terminal_at"]
                    else f"started {_clock(j['started_at'])}")
            extra = (f"; {j['terminal_reason']}" if j["terminal_reason"]
                     else (f"; {j['progress_text'][:80]}" if j["progress_text"] else ""))
            lines.append(f"- [{owners[j['agent_id']]}] {j['title'][:70]}: {j['status']}, {when}{extra}")
    cards = query(
        """SELECT d.question, d.status, d.resolved_choice, d.resolved_by, d.resolved_at, a.created_at
           FROM artifact_decisions d JOIN artifacts a ON a.artifact_id=d.artifact_id
           WHERE a.agent_id=? AND (d.status='pending' OR d.resolved_at > ?)
           ORDER BY d.status='pending' DESC, coalesce(d.resolved_at, a.created_at) DESC
           LIMIT 10""", aid, since)
    if cards:
        lines.append("Your cards for the user (pending, or resolved in the last 12 h):")
        for c in cards:
            state = (f"PENDING since {_clock(c['created_at'])}" if c["status"] == "pending"
                     else f"{c['status']} ({c['resolved_choice']} by {c['resolved_by']}) "
                          f"at {_clock(c['resolved_at'])}")
            lines.append(f"- {c['question'][:90]}: {state}")
    return "\n".join(lines)


def with_facts(question, facts):
    return (f"{question}\n\nLIVE FACTS read from Clarp just now. They override what you "
            f"remember; where they differ, say so briefly:\n{facts}")


def resolve(name):
    rows = query("""SELECT agent_id, session, persona, backend, cwd, model, effort FROM agents
                    WHERE lower(session)=lower(?) OR lower(persona)=lower(?)
                       OR lower(session) LIKE lower(?) || '-%'""", name, name, name)
    if not rows:
        raise SystemExit(f"no Clarp agent matches {name!r}")
    if len(rows) > 1:
        exact = [r for r in rows if r["session"].lower() == name.lower()]
        if len(exact) != 1:
            # Prefer the most recently active match rather than guessing silently.
            ids = [r["agent_id"] for r in rows]
            latest = query(f"""SELECT agent_id FROM prompt_admissions
                               WHERE agent_id IN ({",".join("?" * len(ids))})
                               ORDER BY observed_at DESC LIMIT 1""", *ids)
            exact = [r for r in rows if latest and r["agent_id"] == latest[0]["agent_id"]] or exact
        if len(exact) != 1:
            raise SystemExit(f"{name!r} is ambiguous: " + ", ".join(r["session"] for r in rows))
        rows = exact
    agent = rows[0]
    head = query("""SELECT backend_session_id FROM conversation_heads WHERE agent_id=?
                    ORDER BY revision DESC LIMIT 1""", agent["agent_id"])
    if not head:
        raise SystemExit(f"{agent['session']}: no native conversation recorded yet")
    return {**agent, "native_id": head[0]["backend_session_id"]}


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


def guard_settings():
    """Settings that deny every non-read tool call at call time.

    Not deny rules: a bare-tool or `Tool(*)` deny removes the tool from the
    model's context, which changes the request and loses the agent's prompt
    cache (measured: 214k of 424k tokens billed anew). A PreToolUse hook leaves
    the tool list alone and its deny wins over inherited allow rules.
    """
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(GUARD))}"
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}})


def fork_argv(cmd, question):
    """Turn an agent's turn argv into a one-shot, read-only, unsaved fork.

    The working directory stays the agent's: `--resume` finds the conversation
    by it, and it is part of the cached system prompt. The guard, not the
    directory, is what keeps the fork from changing anything.
    """
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
        if arg == "--dangerously-skip-permissions":
            out += ["--permission-mode", "dontAsk"]
            continue
        out.append(arg)
    return out + ["--settings", guard_settings(), "--fork-session",
                  "--no-session-persistence", "--output-format", "json", question]


def fork_status(agent, question):
    if agent["backend"] != "claude":
        return {**agent, "ok": False,
                "error": f"backend {agent['backend']} not supported yet (Claude only)"}
    started = time.monotonic()
    env = {k: v for k, v in os.environ.items() if k not in (
        "CLAUDE_PWA_SESSION", "CLARP_SESSION", "CLAUDE_CODE_SESSION_ID",
        "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION")}
    try:
        proc = subprocess.run(
            clarp_fork_argv(agent, question),
            cwd=agent["cwd"] if os.path.isdir(agent["cwd"]) else None,
            env=env, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {**agent, "ok": False, "seconds": round(time.monotonic() - started, 1),
                "error": "the fork did not answer within 300 s"}
    return {**agent, **fork_result(proc), "seconds": round(time.monotonic() - started, 1)}


def fork_result(proc):
    """ok only for exit 0 with a non-error JSON result; every failure carries 'error'."""
    detail = (proc.stderr or proc.stdout or "").strip()[-500:]
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error": f"exit {proc.returncode}: {detail or 'no output'}"}
    usage = data.get("usage") or {}
    result = data.get("result") or ""
    tokens = {"reused_tokens": usage.get("cache_read_input_tokens", 0),
              "new_tokens": usage.get("input_tokens", 0)
              + usage.get("cache_creation_input_tokens", 0)}
    if proc.returncode != 0 or data.get("is_error"):
        reason = result or data.get("subtype") or detail or "unknown error"
        return {"ok": False, "error": f"exit {proc.returncode}: {reason}", **tokens}
    return {"ok": True, "status": result, **tokens}


def report(results):
    lines = []
    for r in results:
        head = f"## {r['persona']} ({r['session']})"
        if r["ok"]:
            lines.append(f"{head} · {r['seconds']}s · {r['reused_tokens'] // 1000}k reused,"
                         f" {r['new_tokens'] // 1000}k new\n{r['status']}\n")
        else:
            lines.append(f"{head}\nERROR: {r.get('error', 'unknown error')}\n")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="clarp-status-fork", description=__doc__.splitlines()[0])
    ap.add_argument("agents", nargs="+", help="Clarp session slug or persona name")
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-facts", action="store_true", help="skip the live fact sheet")
    ap.add_argument("--show-facts", action="store_true", help="print the fact sheet only")
    args = ap.parse_args(argv)
    agents = [resolve(a) for a in args.agents]
    for a in agents:
        a["facts"] = "" if args.no_facts else live_facts(a)
    if args.show_facts:
        for a in agents:
            print(f"## {a['persona']}\n{a['facts']}\n")
        return 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
        results = list(pool.map(
            lambda a: fork_status(a, with_facts(args.question, a["facts"]) if a["facts"]
                                  else args.question), agents))
    print(json.dumps(results, ensure_ascii=False, indent=2) if args.json else report(results))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
