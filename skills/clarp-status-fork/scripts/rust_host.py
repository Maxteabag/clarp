"""Read status-fork facts from the local Rust Host, as an agent's tool reads them.

It presents the token of the agents' tools, never the administrator's (#795): what its turn
was given (CLARP_TOOL_TOKEN), else what the installer wrote for the tools (tools.toml beside
the Host's config.toml), else the same token made from config.toml as the Host makes it. The
Host answers it through bound privacy, with the reader of the turn it presents.
"""

import hashlib
import http.client
import ipaddress
import json
import os
import shlex
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from urllib.parse import urlencode


class HostError(RuntimeError):
    pass


def running(unit):
    return subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", unit],
        capture_output=True, timeout=5,
    ).returncode == 0


# What the Host's token of the agents' tools is made from (clarp_host::tool_token).
TOOLS_TOKEN_PREFIX = b"clarp-host: the token of an agent's own tools\0"


def tools_token_of(administrators):
    """The token of the agents' tools on a Host whose administrator has this token."""
    if not administrators:
        return ""
    return hashlib.sha256(TOOLS_TOKEN_PREFIX + administrators.encode()).hexdigest()


def tools_token(config, server, environ=os.environ):
    """The token this script presents to the Host `config` names (its `[server]` table):
    the one its turn was given for this Host, the tools' own file's, or the one made from
    config.toml. Never the administrator's own."""
    told = environ.get("CLARP_TOOL_TOKEN", "").strip()
    here = f"{server.get('bind_addr', '127.0.0.1')}:{server.get('port')}"
    if told and environ.get("CLARP_HOST_ADDR", "").strip() == here:
        return told
    try:
        tools = tomllib.loads((config.parent / "tools.toml").read_text())["server"]
        if (tools.get("kind") == "clarp-host" and tools.get("tool_token")
                and f"{tools.get('bind_addr', '127.0.0.1')}:{tools.get('port')}" == here):
            return str(tools["tool_token"])
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return tools_token_of(str(server.get("auth_token") or ""))


class RustHost:
    def __init__(self, config, environ=os.environ):
        try:
            server = tomllib.loads(config.read_text())["server"]
            addr = server.get("bind_addr", "127.0.0.1")
            self.address = ipaddress.ip_address("127.0.0.1" if addr == "localhost" else addr)
            self.port = server["port"]
            self._token = tools_token(config, server, environ)
        except (OSError, KeyError, TypeError, ValueError):
            raise HostError("cannot read the local Rust Host's server configuration") from None
        if not self.address.is_loopback:
            raise HostError("the Rust Host must be on loopback")
        if (type(self.port) is not int or not 0 < self.port < 65536
                or not isinstance(self._token, str) or not self._token.isascii()
                or any(not 33 <= ord(c) <= 126 for c in self._token) or not self._token):
            raise HostError("invalid local Rust Host port or tools credential")

    def get(self, route, **query):
        path = route + ("?" + urlencode(query) if query else "")
        headers = {"Authorization": "Bearer " + self._token}
        # Run from an agent's turn, the Host is told whose: an agent bound to a model provider
        # of its own is then answered without its words (feature bound_privacy).
        if turn := turn_token():
            headers["X-Clarp-Turn"] = turn
        # Direct HTTP to the validated numeric address, with no proxy or redirect.
        conn = http.client.HTTPConnection(str(self.address), self.port, timeout=10)
        try:
            conn.request("GET", path, headers=headers)
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
            if not running("clarp-host.service") and running("clarp.service"):
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


def turn_token():
    """The token of the agent's turn this runs in, where it runs in one."""
    token = os.environ.get("CLARP_PROVIDER_TURN", "").strip()
    if not token and (path := os.environ.get("CLARP_PROVIDER_TURN_FILE")):
        try:
            token = Path(path).read_text()[:4096].strip()
        except OSError:
            token = ""
    return token if token and not any(c.isspace() for c in token) else ""


def resolve(name, rows):
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
                                          "cwd", "model", "effort", "codex_home",
                                          "codex_provider", "words_withheld")}
    return {**agent, "native_id": row.get("backend_session_id"), "source": "rust"}


def live_facts(host, rows, agent):
    aid = agent["agent_id"]
    row = next(row for row in rows if row["agent_id"] == aid)
    now = int(time.time() * 1000)
    since = now - 12 * 3600 * 1000

    def clock(ms):
        return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ms / 1000)) if ms else "unknown"

    queue = host.get("/turn-queue", session=agent["session"])
    waiting = queue["items"]
    oldest = min((item["enqueued_at"] for item in waiting if item.get("enqueued_at")), default=0)
    # Rust Host contract 169: the user's messages a running turn cannot take go to the agent
    # together, as one turn, when it ends. Older Hosts do not send the field.
    together = sum(1 for item in waiting if item.get("read_together"))
    turn = ("turn running since " + clock(row.get("turn_started_at")) if row.get("busy")
            else "idle, no turn running")
    lines = [f"Now: {clock(now)} (local time).",
             f"You: {turn}, state {row.get('latest_state', 'unknown')}; "
             # latest_state_ts is the time of the turn's last change; last_activity only sorts the chat list
             f"last change {clock(row.get('latest_state_ts'))}"
             + (f", STALLED since {clock((row.get('activity') or {}).get('stalled_since'))}"
                if (row.get('activity') or {}).get('stalled_since') else "")
             + f"; {len(waiting)} queued message(s)"
             + (f", oldest {clock(oldest)}" if oldest else "")
             + (f", {together} of the user's to be read together when the turn ends"
                if together else "")
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


def program_for(backend):
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
