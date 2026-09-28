"""Full text of recent tool commands whose display label is clipped.

A Codex command row reaches the apps as the agent's current tool, a
`/usr/bin/bash -lc "..."` label clipped to 80 characters, and the apps ask
for an explanation of that label. The tail (the path, the subcommand, the
rest of a heredoc) is lost, so the call cannot be shaped or learned. The
backend that saw the whole command remembers it here, and the explainer
swaps the clipped label for it (`expand`). What the apps display and send
is unchanged.

Codex items are handled in the agent runtime process and explanations in
the HTTP server, so the commands are remembered where the runtime runs and
the server asks for one over the runtime bridge (`configure`, `answer`)
when its own memory has none. Nothing is persisted, and a heredoc body a
shaped call withholds never leaves the runtime.

In memory only and bounded: after a runtime restart, or when two recent
commands of one agent share the clipped text and differ in more than a
withheld heredoc body, the label stays clipped and is explained as today
(`truncated`).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

from . import tool_explanation_shapes as shapes
from . import tool_explanation_templates as templates
from .log import log

PER_AGENT = 256
AGENTS = 128
MAX_COMMAND = 65536
# A label the runtime could not complete is not asked about again for this
# long: clients poll a pending row every ~600ms.
MISS_SECONDS = 10.0
MISSES = 1024
REPORT_SECONDS = 60.0

_lock = threading.Lock()
_recent: OrderedDict[str, deque] = OrderedDict()
_peer = None
_misses: OrderedDict[tuple, float] = OrderedDict()
_counts = {"resolved": 0, "missed": 0, "failed": 0}
_reported = [0.0]


def remember(agent_id, command):
    """Keep a tool command longer than the label clip, for this agent."""
    if not agent_id or not isinstance(command, str) or not templates.LABEL_CLIP <= len(command) <= MAX_COMMAND:
        return
    with _lock:
        commands = _recent.pop(agent_id, None) or deque(maxlen=PER_AGENT)
        if command in commands:
            commands.remove(command)
        commands.append(command)
        _recent[agent_id] = commands
        while len(_recent) > AGENTS:
            _recent.popitem(last=False)


def resolve(agent_id, shown):
    """The one remembered command `shown` is a clipped prefix of, or "".

    Several commands that read the same once their heredoc bodies are
    withheld (`shapes.redact`) explain the same, so that text is returned.
    """
    shown = shown.strip() if isinstance(shown, str) else ""
    if not agent_id or not shown:
        return ""
    with _lock:
        matches = {command for command in _recent.get(agent_id, ()) if len(command) > len(shown)
                   and command.startswith(shown)}
    if len(matches) > 1:
        matches = {shapes.redact(command) for command in matches}
    return matches.pop() if len(matches) == 1 else ""


def answer(agent_id, shown):
    """What the runtime tells the server a clipped label stands for: the
    command with any withheld heredoc body already taken out."""
    command = resolve(agent_id, shown)
    return shapes.redact(command) if command else ""


def configure(peer):
    """`peer(agent_id, label) -> str` asks the process that remembers the
    commands (the runtime bridge); None when this process is that one."""
    global _peer
    with _lock:
        _peer = peer
        _misses.clear()


def _ask(agent_id, shown):
    peer = _peer
    if peer is None or not agent_id:
        return ""
    key, now = (agent_id, shown.strip()), time.monotonic()
    with _lock:
        if now - _misses.get(key, -MISS_SECONDS) < MISS_SECONDS:
            return ""
    try:
        command = peer(agent_id, shown) or ""
        outcome = "resolved" if command else "missed"
    except Exception:  # noqa: BLE001 - an old or stopped runtime: explain the label as is
        command, outcome = "", "failed"
    with _lock:
        _counts[outcome] += 1
        if not command:
            _misses[key] = now
            _misses.move_to_end(key)
            while len(_misses) > MISSES:
                _misses.popitem(last=False)
        if now - _reported[0] >= REPORT_SECONDS:
            counts = dict(_counts)
            _counts.update(resolved=0, missed=0, failed=0)
            _reported[0] = now
        else:
            counts = None
    if counts:
        # Counts only: never a label or a command.
        log("toolExplanationCommands", " ".join(f"{k}={v}" for k, v in counts.items()))
    return command


def expand(activity, agent_id):
    """The activity with a clipped shell label replaced by the whole command.

    Anything else, and a label this Host cannot complete, is returned as is.
    """
    if not isinstance(activity, dict):
        return activity
    label = templates.shell_label(activity)
    if not label or len(label) < templates.LABEL_CLIP:
        return activity
    command = resolve(agent_id, label) or _ask(agent_id, label)
    return {"name": "Bash", "command": command} if command else activity


def clear():
    with _lock:
        _recent.clear()
        _misses.clear()
