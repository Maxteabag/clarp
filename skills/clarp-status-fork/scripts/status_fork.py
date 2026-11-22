#!/usr/bin/env python3
"""Ask a Clarp agent for its status through a throwaway fork of its conversation.

A Claude fork uses a read-only PreToolUse guard. Rust Host Codex forks use
Codex's read-only sandbox with shell, plugins and configured MCP tools disabled.
Both fork the saved native conversation for one unsaved answer.

  clarp-status-fork pebble [hobbles ...] [--question TEXT] [--json]
"""
import argparse
import concurrent.futures
import http.client
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlencode

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


class HostError(RuntimeError):
    pass


def host_running(unit):
    return subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", unit],
        capture_output=True, timeout=5,
    ).returncode == 0


class RustHost:
    def __init__(self, config):
        try:
            server = tomllib.loads(config.read_text())["server"]
            addr = server.get("bind_addr", "127.0.0.1")
            self.address = ipaddress.ip_address("127.0.0.1" if addr == "localhost" else addr)
            self.port = server["port"]
            self._token = server["auth_token"]
        except (OSError, KeyError, TypeError, ValueError):
            raise HostError("cannot read the local Rust Host's server configuration") from None
        if not self.address.is_loopback:
            raise HostError("the Rust Host's administrator API must be on loopback")
        if (type(self.port) is not int or not 0 < self.port < 65536
                or not isinstance(self._token, str) or not self._token.isascii()
                or any(not 33 <= ord(c) <= 126 for c in self._token) or not self._token):
            raise HostError("invalid local Rust Host port or administrator credential")

    def get(self, route, **query):
        path = route + ("?" + urlencode(query) if query else "")
        # Direct HTTP to the validated numeric address, with no proxy or redirect.
        conn = http.client.HTTPConnection(str(self.address), self.port, timeout=10)
        try:
            conn.request("GET", path, headers={"Authorization": "Bearer " + self._token})
            response = conn.getresponse()
            if response.status != 200:
                raise HostError(f"Rust Host GET {route} returned HTTP {response.status}")
            return json.load(response)
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            raise HostError(f"could not read Rust Host GET {route}") from None
        finally:
            conn.close()

    @classmethod
    def discover(cls):
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        config = config / "clarp-host" / "config.toml"
        if not config.exists():
            return None, []
        try:
            host = cls(config)
            return host, host.get("/agents/snapshot")["agents"]
        except (HostError, KeyError):
            if not host_running("clarp-host.service") and host_running("clarp.service"):
                return None, []
            raise

    def inbox(self, agent_id):
        cursor = ""
        seen = set()
        while cursor not in seen:
            seen.add(cursor)
            page = self.get("/attention/inbox", limit=200, cursor=cursor, representation="flat-v1")
            yield from (row for row in page["artifacts"] if row.get("agent_id") == agent_id)
            cursor = page.get("next_cursor")
            if not cursor:
                return
        raise HostError("Rust Host attention inbox repeated a cursor")


def resolve_rust(name, rows):
    folded = name.casefold()
    rows = [row for row in rows if not row.get("moved")]
    exact = [row for row in rows if row["session"].casefold() == folded]
    found = exact or [row for row in rows
                      if row["session"].casefold().startswith(folded + "-")
                      or row["persona"].casefold() == folded]
    if not found:
        return None
    found.sort(key=lambda row: row.get("last_activity") or 0, reverse=True)
    if len(found) > 1 and found[0].get("last_activity") == found[1].get("last_activity"):
        raise HostError(f"{name!r} is ambiguous: " + ", ".join(row["session"] for row in found))
    row = found[0]
    agent = {key: row.get(key) for key in ("agent_id", "session", "persona", "backend",
                                          "cwd", "model", "effort")}
    return {**agent, "native_id": row.get("backend_session_id"), "source": "rust"}


def rust_facts(host, rows, agent):
    aid = agent["agent_id"]
    row = next(row for row in rows if row["agent_id"] == aid)
    now = int(time.time() * 1000)
    since = now - 12 * 3600 * 1000

    def clock(ms):
        return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ms / 1000)) if ms else "unknown"

    queue = host.get("/turn-queue", session=agent["session"])
    waiting = queue["items"]
    oldest = min((item["enqueued_at"] for item in waiting if item.get("enqueued_at")), default=0)
    turn = ("turn running since " + clock(row.get("turn_started_at")) if row.get("busy")
            else "idle, no turn running")
    lines = [f"Now: {clock(now)} (local time).",
             f"You: {turn}, state {row.get('latest_state', 'unknown')}; "
             f"last activity {clock(row.get('last_activity'))}; {len(waiting)} queued message(s)"
             + (f", oldest {clock(oldest)}" if oldest else "")
             + ("; queue paused" if queue.get("paused") else "")]
    children = [item for item in rows if item.get("parent_agent_id") == aid and not item.get("moved")]
    owners = {aid: "you", **{item["agent_id"]: item["session"] for item in children}}
    recent = [item for item in children
              if item.get("helper_state") == "running" or (item.get("last_activity") or 0) > since]
    if recent:
        lines.append("Your helper agents (running or active in the last 12 h):")
        for item in sorted(recent, key=lambda row: row.get("last_activity") or 0, reverse=True)[:15]:
            lines.append(f"- {item['session']}: {item.get('helper_state') or 'unknown'}, "
                         f"last activity {clock(item.get('last_activity'))}")
    shown_jobs = []
    for job in host.get("/background-jobs")["jobs"]:
        if job.get("agent_id") not in owners or (job.get("terminal_at") or now) <= since:
            continue
        extra = (job.get("terminal_reason") or job.get("progress_text") or "")[:80]
        when = job.get("terminal_at") or job.get("started_at")
        shown_jobs.append(f"- [{owners[job['agent_id']]}] {job.get('title', '')[:70]}: "
                          f"{job.get('status', 'unknown')}, {clock(when)}"
                          + (f"; {extra}" if extra else ""))
    if shown_jobs:
        lines += ["Background jobs of you and your helpers (active or ended in the last 12 h):",
                  *shown_jobs[:25]]
    cards = []
    for artifact in host.inbox(aid):
        decision = artifact.get("decision")
        if not isinstance(decision, dict):
            continue
        pending = decision.get("status") == "pending"
        if pending and (decision.get("expires_at") or now + 1) <= now:
            continue
        when = artifact.get("created_at") if pending else decision.get("resolved_at")
        if pending or (when or 0) > since:
            question = decision.get("question") or artifact.get("title") or ""
            state = "PENDING" if pending else decision.get("status", "unknown")
            cards.append((pending, when or 0, f"- {question[:90]}: {state} at {clock(when)}"))
    if cards:
        lines += ["Your cards for the user (pending or resolved in the last 12 h):",
                  *(text for _, _, text in sorted(cards, reverse=True)[:10])]
    return "\n".join(lines)


def rust_program(backend):
    variable = {"claude": "CLARP_CLAUDE_PROGRAM", "codex": "CLARP_CODEX_PROGRAM"}[backend]
    try:
        raw = subprocess.check_output(
            ["systemctl", "--user", "show", "clarp-host.service", "-p", "Environment", "--value"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        unit = dict(word.split("=", 1) for word in shlex.split(raw) if "=" in word)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise HostError("cannot read the Rust Host's turn program configuration") from None
    program = unit.get(variable) or backend
    program = shutil.which(program, path=unit.get("PATH", os.defpath))
    if not program or not Path(program).is_absolute():
        raise HostError(f"could not locate the Rust Host's {backend} program")
    environment = {key: unit[key] for key in ("PATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR") if key in unit}
    return program, environment


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


def launch_dir(saved):
    """The one directory both the settings check and the launch use.

    The agent's saved cwd must be an absolute path to an existing directory; it is
    resolved through symlinks. There is no fallback: launching anywhere else would
    inherit project settings that preflight() never examined.
    """
    path = Path(saved or "").expanduser()
    if not saved or not path.is_absolute():
        raise Refused(f"the agent's working directory {saved!r} is not an absolute path")
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Refused(f"the agent's working directory {saved} does not exist ({exc})")
    if not real.is_dir():
        raise Refused(f"the agent's working directory {saved} is not a directory")
    return path, real


def clarp_fork_argv(agent, question):
    """Clarp's own Claude argv for this agent as a read-only fork, and where to run it.

    Raises Refused, before anything is launched, when the guard cannot be guaranteed.
    """
    saved, real = launch_dir(agent["cwd"])
    cmd = _claude_backend().build_cmd(
        agent["native_id"], model=agent["model"] or "", effort=agent["effort"] or "",
        persona=agent["persona"] or "", session=agent["session"])
    version = preflight(cmd[0], real, alias=saved)
    return fork_argv(cmd, question, version), real


# Verified with this skill: PreToolUse JSON deny, exit-2 blocking, --settings precedence.
MIN_CLAUDE = (2, 1, 285)
# `onFailure: "block"` (a hook that fails or times out blocks the call) starts here.
ON_FAILURE_BLOCK = (2, 1, 295)
GUARD_TIMEOUT_S = 10
READ_TOOLS = ("Read", "Grep", "Glob", "LS", "NotebookRead")


class Refused(Exception):
    """The fork cannot be guaranteed read-only here; nothing was launched."""


def claude_version(binary, env=None):
    proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30, env=env)
    parts = proc.stdout.split()[0].split(".") if proc.stdout.split() else []
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        raise Refused(f"cannot read the Claude Code version from {proc.stdout.strip()!r}")


def guard_settings(version):
    """Settings that deny every non-read tool call at call time, failing closed.

    Not deny rules: a bare-tool or `Tool(*)` deny removes the tool from the
    model's context, which changes the request and loses the agent's prompt
    cache (measured: 214k of 424k tokens billed anew). A PreToolUse hook leaves
    the tool list alone and its deny wins over inherited allow rules.

    - `disableAllHooks: false` here outranks a `true` in user, project or local
      settings (--settings precedence); managed policy is refused in preflight().
    - Shell form `... || exit 2`: a guard that cannot start or crashes exits 2,
      which blocks a PreToolUse call on every supported version.
    - From ON_FAILURE_BLOCK, `onFailure: block` also blocks on a hook timeout.
      Before it, a timeout falls back to dontAsk, so preflight() refuses when
      any inherited rule pre-approves a non-read tool.
    """
    hook = {"type": "command", "timeout": GUARD_TIMEOUT_S,
            "command": f"{shlex.quote(sys.executable)} {shlex.quote(str(GUARD))} || exit 2"}
    if version >= ON_FAILURE_BLOCK:
        hook["onFailure"] = "block"
    return json.dumps({"disableAllHooks": False,
                       "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}})


def _settings_files(cwd, alias=None, env=None):
    """Settings files the fork inherits, by layer: managed first.

    `cwd` is the resolved launch directory. Project settings are taken from every
    ancestor of it, and also of the unresolved `alias` path when it differs, so a
    symlinked path cannot hide settings on either side.
    """
    env = os.environ if env is None else env
    managed = Path(env.get("CLARP_STATUS_FORK_MANAGED_DIR", "/etc/claude-code"))
    files = [("managed", managed / "managed-settings.json"),
             *(("managed", p) for p in sorted((managed / "managed-settings.d").glob("*.json")))]
    user = Path(env.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    files += [("user", user / "settings.json"), ("user", user / "settings.local.json")]
    folders = []
    for start in (Path(cwd), Path(alias) if alias else None):
        if start is None:
            continue
        for folder in (start, *start.parents):
            for candidate in (folder, folder.resolve()):
                if candidate not in folders:
                    folders.append(candidate)
    for folder in folders:
        files += [("project", folder / ".claude/settings.json"),
                  ("project", folder / ".claude/settings.local.json")]
    return files


def preflight(binary, cwd, alias=None, env=None):
    """Refuse (raise Refused) unless the read-only guard is guaranteed to hold."""
    if not os.access(GUARD, os.R_OK):
        raise Refused(f"the read-only guard {GUARD} is missing or unreadable")
    version = claude_version(binary) if env is None else claude_version(binary, env)
    if version < MIN_CLAUDE:
        raise Refused(f"Claude Code {'.'.join(map(str, version))} is older than "
                      f"{'.'.join(map(str, MIN_CLAUDE))}, the oldest verified with the guard")
    for verdict, tool in (("deny", "Bash"), (None, "Read")):
        probe = subprocess.run([sys.executable, str(GUARD)], input=json.dumps({"tool_name": tool}),
                               capture_output=True, text=True, timeout=GUARD_TIMEOUT_S)
        out = probe.stdout.strip()
        got = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None
        if probe.returncode != 0 or got != verdict:
            raise Refused(f"the read-only guard failed its self-test on {tool}")
    for layer, path in _settings_files(cwd, alias, env):
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            raise Refused(f"cannot read {layer} settings {path}: {exc}")
        if not isinstance(data, dict):
            raise Refused(f"{layer} settings {path} is not a JSON object")
        if layer == "managed" and (data.get("disableAllHooks") or data.get("allowManagedHooksOnly")):
            raise Refused(f"managed policy {path} disables or restricts hooks; the guard "
                          "would not run and managed settings cannot be overridden")
        allowed = (data.get("permissions") or {}).get("allow") or []
        writable = [r for r in allowed if not str(r).split("(")[0] in READ_TOOLS]
        if writable and version < ON_FAILURE_BLOCK:
            raise Refused(f"{layer} settings {path} pre-approve {writable[:3]}; before Claude "
                          f"Code {'.'.join(map(str, ON_FAILURE_BLOCK))} a timed-out guard "
                          "would let them run")
    return version


def fork_argv(cmd, question, version):
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
    return out + ["--settings", guard_settings(version), "--fork-session",
                  "--no-session-persistence", "--output-format", "json", question]


def rust_fork_argv(agent, question, binary, env):
    saved, cwd = launch_dir(agent["cwd"])
    native = agent.get("native_id") or ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", native):
        raise Refused("the agent has no usable native conversation ID")
    model, effort = agent.get("model"), agent.get("effort")
    if agent["backend"] == "claude":
        cmd = [binary, "-p", "--dangerously-skip-permissions", "--strict-mcp-config"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if persona := (agent.get("persona") or "").strip():
            cmd += ["--append-system-prompt", f"You are {persona}."]
        cmd += ["--resume", native]
        version = preflight(binary, cwd, alias=saved, env=env)
        return fork_argv(cmd, question, version), cwd

    # Codex has a native ephemeral fork. Keep the filesystem sandbox read-only
    # and remove shell, plugins and configured MCP tools at the request boundary.
    cmd = [binary, "-a", "never", "exec", "--sandbox", "read-only", "fork",
           "--ephemeral", "--ignore-user-config", "--ignore-rules",
           "--disable", "shell_tool", "--disable", "plugins",
           "-c", "mcp_servers={}", "-c", 'web_search="disabled"',
           "--skip-git-repo-check", "--json"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    return cmd + [native, "-"], cwd


def fork_status(agent, question):
    rust = agent.get("source") == "rust"
    if agent["backend"] not in ({"claude", "codex"} if rust else {"claude"}):
        return {**agent, "ok": False, "error": f"unsupported fork backend {agent['backend']}"}
    started = time.monotonic()
    env = {k: v for k, v in os.environ.items() if k not in (
        "CLAUDE_PWA_SESSION", "CLARP_SESSION", "CLAUDE_CODE_SESSION_ID",
        "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLARP_PROVIDER_TURN",
        "CLARP_HOST_TOKEN", "CLARP_PUSH_CREDENTIAL", "CLARP_SOURCE_TOKEN", "CLARP_PEER_TOKEN")}
    codex = agent["backend"] == "codex"
    try:
        if rust:
            binary, turn_environment = rust_program(agent["backend"])
            env.update(turn_environment)
            argv, cwd = rust_fork_argv(agent, question, binary, env)
        else:
            argv, cwd = clarp_fork_argv(agent, question)
    except (Refused, HostError, OSError, subprocess.SubprocessError) as exc:
        return {**agent, "ok": False, "seconds": 0.0, "error": f"refused, nothing launched: {exc}"}
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                              input=question if codex else None, timeout=300)
    except subprocess.TimeoutExpired:
        return {**agent, "ok": False, "seconds": round(time.monotonic() - started, 1),
                "error": "the fork did not answer within 300 s"}
    except OSError as exc:
        return {**agent, "ok": False, "error": f"could not start the fork: {exc}"}
    result = codex_result(proc) if codex else fork_result(proc)
    return {**agent, **result, "seconds": round(time.monotonic() - started, 1)}


def codex_result(proc):
    answer, usage = "", None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = item.get("text", "")
        elif event.get("type") == "turn.completed":
            usage = event.get("usage", {})
        elif event.get("type") == "turn.failed":
            usage = None
    if proc.returncode != 0 or usage is None or not answer.strip():
        return {"ok": False, "error": f"Codex fork ended without an answer (exit {proc.returncode})"}
    reused = usage.get("cached_input_tokens", 0)
    return {"ok": True, "status": answer, "reused_tokens": reused,
            "new_tokens": max(0, usage.get("input_tokens", 0) - reused)}


def fork_result(proc):
    """ok only for exit 0 with a non-error, non-empty JSON result; failures carry 'error'."""
    detail = (proc.stderr or proc.stdout or "").strip()[-500:]
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error": f"exit {proc.returncode}: {detail or 'no output'}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": f"exit {proc.returncode}: unexpected output {detail[:200]}"}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    result = data.get("result") if isinstance(data.get("result"), str) else ""
    tokens = {"reused_tokens": usage.get("cache_read_input_tokens", 0),
              "new_tokens": usage.get("input_tokens", 0)
              + usage.get("cache_creation_input_tokens", 0)}
    if proc.returncode != 0 or data.get("is_error") or not result.strip():
        reason = result or data.get("subtype") or detail or "no answer"
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
    try:
        host, roster = RustHost.discover()
        agents = []
        for name in args.agents:
            agent = resolve_rust(name, roster)
            if agent is None:
                if host is not None and not host_running("clarp.service"):
                    raise HostError(f"no local Rust Host agent matches {name!r}")
                agent = resolve(name)
            if args.show_facts or not args.no_facts:
                agent["facts"] = rust_facts(host, roster, agent) if agent.get("source") == "rust" else live_facts(agent)
            else:
                agent["facts"] = ""
            agents.append(agent)
    except (HostError, KeyError, OSError, subprocess.SubprocessError) as exc:
        ap.exit(2, f"clarp-status-fork: {exc}\n")
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
