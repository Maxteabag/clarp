"""Normalize agent tool calls into (actor, verb, target) for the fleet map.

`state_log` records every tool call an agent makes, in two shapes depending on
backend: Claude writes a structured `input`, codex writes the command line into
`tool` (clamped to 80 chars, so its tail may be missing). Both collapse here
into one event shape the client can draw.

The rule tables below are plain lookups -- there is deliberately no model call
in this path. It runs once per event, tens of thousands of times per replay,
and a non-deterministic classifier would move a node between renders, which
destroys the one thing a map offers over a log: stable position. An LLM's job
is to *append rules* (see `unmatched_clusters`), never to classify an event.
"""
from __future__ import annotations

import functools
import json
import os
import re
from typing import Any, Iterable

from . import viz_archetypes
from .protocol import AgentState

# --- rules ----------------------------------------------------------------
# (verb, fallback target kind) keyed on executable basename.
EXE_RULES: dict[str, tuple[str, str]] = {
    "git": ("vcs", "repo"), "gh": ("github", "service:github"),
    "npm": ("build", "toolchain"), "yarn": ("build", "toolchain"),
    "pnpm": ("build", "toolchain"), "make": ("build", "toolchain"),
    "cargo": ("build", "toolchain"), "uv": ("build", "toolchain"),
    "pip": ("build", "toolchain"), "vite": ("build", "toolchain"),
    "tsc": ("build", "toolchain"), "cmake": ("build", "toolchain"),
    "brew": ("build", "toolchain"), "npx": ("build", "toolchain"),
    "dotnet": ("build", "toolchain"), "mise": ("build", "toolchain"),
    "xcodebuild": ("build", "toolchain"), "xtool": ("build", "toolchain"),
    "swift": ("build", "toolchain"),
    "pytest": ("test", "toolchain"), "vitest": ("test", "toolchain"),
    "playwright": ("test", "toolchain"),
    "sqlite3": ("query", "database"),
    "cat": ("read", "file"), "head": ("read", "file"), "tail": ("read", "file"),
    "sed": ("read", "file"), "nl": ("read", "file"), "bat": ("read", "file"),
    "less": ("read", "file"), "wc": ("read", "file"), "jq": ("read", "file"),
    "grep": ("search", "path"), "rg": ("search", "path"), "ag": ("search", "path"),
    "find": ("search", "path"), "fd": ("search", "path"), "ls": ("search", "path"),
    "tree": ("search", "path"), "firecrawl": ("search", "service:web"),
    "python": ("execute", "script"), "python3": ("execute", "script"),
    "node": ("execute", "script"), "uvx": ("execute", "script"),
    "curl": ("network", "service:web"), "wget": ("network", "service:web"),
    "docker": ("ops", "container"), "docker-compose": ("ops", "container"),
    "systemctl": ("ops", "service"), "journalctl": ("ops", "service"),
    "launchctl": ("ops", "service"),
    "ssh": ("remote", "host"), "scp": ("remote", "host"),
    "tailscale": ("remote", "host"), "rsync": ("remote", "host"),
    "mkdir": ("write", "path"), "rm": ("write", "path"), "cp": ("write", "path"),
    "mv": ("write", "path"), "touch": ("write", "path"), "chmod": ("write", "path"),
    "tee": ("write", "file"), "ln": ("write", "path"),
    "date": ("util", "host"), "sleep": ("util", "host"), "which": ("util", "host"),
    "readlink": ("util", "host"), "df": ("util", "host"), "ps": ("util", "host"),
    "kill": ("ops", "host"), "pkill": ("ops", "host"), "tmux": ("ops", "host"),
    "autoreview": ("review", "toolchain"),
    "clarp-admin": ("clarp", "service:clarp"),
    "clarp-agent-bg": ("clarp", "service:clarp"),
    "clarp-agent-tasks": ("clarp", "service:clarp"),
    "wacli": ("message", "service:whatsapp"),
    "himalaya": ("message", "service:email"),
    "az": ("cloud", "service:azure"),
}
_GIT_REMOTE = {"push", "pull", "fetch", "clone", "remote"}
_GH_TARGET = {
    "pr": "service:github", "issue": "service:github",
    "release": "service:github", "api": "service:github",
    "repo": "service:github", "run": "service:github-actions",
    "workflow": "service:github-actions",
}
NATIVE_RULES: dict[str, tuple[str, str]] = {
    "Read": ("read", "file"), "Edit": ("write", "file"), "Write": ("write", "file"),
    "NotebookEdit": ("write", "file"), "file_change": ("write", "file"),
    "Grep": ("search", "path"), "Glob": ("search", "path"), "LS": ("search", "path"),
    "WebSearch": ("search", "service:web"), "web_search": ("search", "service:web"),
    "web_search_call": ("search", "service:web"),
    "WebFetch": ("network", "service:web"),
    "Agent": ("spawn", "agent"), "Task": ("spawn", "agent"),
    "collab_agent_tool_call": ("message", "agent"),
    "SendMessage": ("message", "agent"),
    "image_view": ("media", "asset"), "image_generation": ("media", "asset"),
    "Skill": ("skill", "toolchain"), "mcp_tool_call": ("skill", "toolchain"),
    "TodoWrite": ("plan", "self"), "Monitor": ("ops", "host"),
    "LSP": ("read", "file"),
    # codex spells several of these lowercase
    "read": ("read", "file"), "edit": ("write", "file"),
    "write": ("write", "file"), "find_by_name": ("search", "path"),
}

# Shell words that are never the command being run. `set -euo pipefail`
# preambles and `for f in ...` loops otherwise surface as fake executables.
_NOISE = {
    "cd", "eval", "export", "timeout", "if", "then", "else", "fi", "[", "test",
    "printf", "echo", "true", "set", "source", ".", "while", "do", "done",
    "env", "sudo", "command", "exec", "nohup", "for", "in", "elif", "case",
    "esac", "function", "return", "local", "declare", "read", "shift", "trap",
    "pipefail", "euo", "errexit", "nounset", "shopt", "ulimit", "shellenv",
    "unset", "wait", "type", "hash", "alias", "bash", "sh", "zsh", "dash",
}
# (exe, subcommand) pairs that only ever set up an environment. Without this
# `eval "$(brew shellenv)" && git commit` scores as a build, because the
# preamble's executable is reached before the command the agent actually ran.
_PREAMBLE = {("brew", "shellenv")}

_TOKEN = re.compile(r"[A-Za-z0-9_./~+-]+")
_REPO_RE = re.compile(r"/home/[^/]+/(?:GIT|git|dev|src)/([A-Za-z0-9._-]+)")

# The codex dispatch shape clamps its command to this width; a value at the
# limit has lost its tail, so a missing target is expected rather than a bug.
CODEX_TOOL_CLAMP = 80


@functools.lru_cache(maxsize=2048)
def _is_checkout(path: str) -> bool:
    return os.path.isdir(path)


def repo_of(text: str) -> str | None:
    """Repo node for a path, only if that checkout actually exists.

    Without the existence check any path-shaped text mints a node: live data
    produced `repo:null` from a stray path and `repo:dotfiles-video-` from a
    name the 80-char clamp cut in half. A node that never existed is worse
    than a generic bucket, because it looks like somewhere real.
    """
    for m in _REPO_RE.finditer(text or ""):
        if _is_checkout(m.group(0)):
            return f"repo:{m.group(1)}"
    return None


def first_known_executable(cmd: str) -> tuple[str, str]:
    """First token that names a known executable, plus its subcommand.

    Scanning every token beats parsing the leading pipeline segment: real
    commands look like `if [ -f x ]; then ...` or `eval "$(brew shellenv)" &&
    git push`, where the leading token is a shell keyword. Measured against
    57k live events, segment-walking classified 33% and this reaches 96%.
    """
    toks = _TOKEN.findall(cmd or "")
    for i, tok in enumerate(toks):
        base = os.path.basename(tok)
        if base in EXE_RULES:
            sub = next((t for t in toks[i + 1:]
                        if not t.startswith("-") and t not in _NOISE), "")
            # Check the immediately following token, not `sub`: the preamble
            # marker itself lives in _NOISE, so `sub` skips past it.
            nxt = os.path.basename(toks[i + 1]) if i + 1 < len(toks) else ""
            if (base, nxt) in _PREAMBLE:
                continue                  # environment setup, keep looking
            return base, sub
    for tok in toks:
        base = os.path.basename(tok)
        if (len(base) > 2 and base not in _NOISE and not base.startswith("-")
                and "=" not in base and not base[0].isdigit()
                and not base.endswith((".md", ".json", ".txt", ".env", ".log"))):
            return base, ""
    return "", ""


def classify(tool: str, inp: Any, file_path: str = "") -> tuple[str, str] | None:
    """-> (verb, target) or None when no rule matched."""
    if tool in NATIVE_RULES:
        verb, kind = NATIVE_RULES[tool]
        if kind == "file":
            path = file_path or (
                inp.get("file_path", "") if isinstance(inp, dict) else "")
            return verb, repo_of(path) or kind
        if kind == "path":
            return verb, repo_of(json.dumps(inp) if inp else "") or kind
        return verb, kind
    if tool == "Bash" or tool.startswith(("/usr/bin/", "/bin/", "bash ", "sh ")):
        cmd = inp.get("command", "") if isinstance(inp, dict) else ""
        cmd = cmd or tool
        exe, sub = first_known_executable(cmd)
        if not exe:
            return None
        if exe == "git":
            verb = "push" if sub == "push" else "vcs"
            kind = "service:github" if sub in _GIT_REMOTE else "repo"
            return verb, repo_of(cmd) or kind
        if exe == "gh":
            return "github", _GH_TARGET.get(sub, "service:github")
        if exe in EXE_RULES:
            verb, kind = EXE_RULES[exe]
            return verb, repo_of(cmd) or kind
    return None


def normalize(rows: Iterable[Any], names: dict[str, str]) -> list[dict]:
    """Project `state_log` tool rows into drawable events, in time order."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for row in rows:
        agent_id, ts, detail = row["agent_id"], row["ts"], row["detail"]
        try:
            data = json.loads(detail)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("phase") == "tool_finished":
            continue                      # started/finished is one event
        tool = str(data.get("tool") or "")
        key = (agent_id, ts, tool, data.get("trace_id"))
        if key in seen:
            continue                      # codex emits duplicate rows
        seen.add(key)
        hit = classify(tool, data.get("input") or {}, data.get("file_path") or "")
        if not hit:
            continue
        verb, target = hit
        out.append({
            "ts": ts,
            "agent": names.get(agent_id, agent_id[:8]),
            "agent_id": agent_id,
            "verb": verb,
            "archetype": viz_archetypes.archetype_for(verb),
            "target": target,
            "specific": ":" in target,
            "clamped": bool(data.get("dispatch")) and len(tool) >= CODEX_TOOL_CLAMP,
        })
    return out


def unmatched_clusters(rows: Iterable[Any], limit: int = 40) -> list[dict]:
    """Tools no rule matched, clustered. This is the rule-writer's queue.

    A model reads this and appends to EXE_RULES; it never sees a single event.
    `clamped` marks entries whose command was truncated before storage, so a
    rule cannot recover them and proposing one is wasted effort.
    """
    import collections
    counts: collections.Counter = collections.Counter()
    examples: dict[str, str] = {}
    clamped: collections.Counter = collections.Counter()
    for row in rows:
        try:
            data = json.loads(row["detail"])
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("phase") == "tool_finished":
            continue
        tool = str(data.get("tool") or "")
        if classify(tool, data.get("input") or {}, data.get("file_path") or ""):
            continue
        cmd = ""
        if isinstance(data.get("input"), dict):
            cmd = data["input"].get("command", "")
        exe, _ = first_known_executable(cmd or tool)
        hint = exe or (tool[:40] or "?")
        counts[hint] += 1
        examples.setdefault(hint, (cmd or tool)[:160])
        if bool(data.get("dispatch")) and len(tool) >= CODEX_TOOL_CLAMP:
            clamped[hint] += 1
    return [
        {"hint": h, "count": n, "clamped": clamped[h], "example": examples[h]}
        for h, n in counts.most_common(limit)
    ]


def build_fleet_map(since_ms: int, until_ms: int | None = None,
                    limit: int = 4000) -> dict[str, Any]:
    """Read model for the fleet map: events plus the nodes they imply.

    Nodes are derived, never declared -- a target exists on the map only
    because some agent touched it in this window. That is what keeps the
    canvas procedural: install a new tool and its node appears unprompted.
    """
    from . import db

    until = until_ms if until_ms is not None else (1 << 62)
    con = db.conn()
    names = {
        r["agent_id"]: (r["persona"] or r["session"])
        for r in con.execute(
            "SELECT agent_id, persona, session FROM agents")
    }
    rows = con.execute(
        "SELECT agent_id, ts, detail FROM state_log "
        "WHERE kind=? AND json_valid(detail) AND ts >= ? AND ts <= ? "
        "ORDER BY ts",
        (AgentState.TOOL, since_ms, until),
    ).fetchall()

    events = normalize(rows, names)
    if len(events) > limit:                       # keep the newest slice
        events = events[-limit:]

    actors: dict[str, dict] = {}
    nodes: dict[str, dict] = {}
    for ev in events:
        a = actors.setdefault(
            ev["agent"], {"id": ev["agent"], "kind": "agent", "events": 0})
        a["events"] += 1
        n = nodes.setdefault(ev["target"], {
            "id": ev["target"],
            "kind": ev["target"].split(":", 1)[0] if ":" in ev["target"]
                    else ev["target"],
            "specific": ":" in ev["target"],
            "events": 0,
            "agents": set(),
        })
        n["events"] += 1
        n["agents"].add(ev["agent"])
    for n in nodes.values():
        n["agents"] = sorted(n["agents"])         # JSON-serializable

    specific = sum(1 for e in events if e["specific"])
    return {
        "archetypes": viz_archetypes.specs(),
        "since": since_ms,
        "until": until if until < (1 << 61) else None,
        "events": events,
        "actors": sorted(actors.values(), key=lambda a: -a["events"]),
        "nodes": sorted(nodes.values(), key=lambda n: -n["events"]),
        "coverage": {
            "events": len(events),
            "specific_targets": specific,
            "specific_pct": round(100 * specific / max(len(events), 1), 1),
        },
    }
