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

import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from . import tool_explanation_shapes as shapes
from . import tool_explanation_templates as templates
from .log import log

PER_AGENT = 256
PER_AGENT_BYTES = 1024 * 1024
IDLE_SECONDS = 30 * 60
AGENTS = 128
MAX_COMMAND = 65536
# A label the runtime could not complete is not asked about again for this
# long: clients poll a pending row every ~600ms.
MISS_SECONDS = 10.0
MISSES = 1024
REPORT_SECONDS = 60.0

_lock = threading.Lock()
@dataclass(slots=True)
class _Commands:
    # Charge the actual Python str object, including wide Unicode, without
    # retaining an encoded copy. OrderedDict nodes and these three scalar
    # fields are separately bounded by PER_AGENT and AGENTS.
    entries: OrderedDict[str, None] = field(default_factory=OrderedDict)
    nbytes: int = 0
    last_used: float = 0.0


_recent: OrderedDict[str, _Commands] = OrderedDict()
_peer = None
_misses: OrderedDict[tuple, float] = OrderedDict()
_counts = {"resolved": 0, "missed": 0, "failed": 0}
_reported = [0.0]


def _expire(now):
    # Deliberately lazy: any cache access sweeps all idle agents. Without
    # an operation there is no promise of physical release at the TTL.
    for agent_id in list(_recent):
        if now - _recent[agent_id].last_used >= IDLE_SECONDS:
            del _recent[agent_id]


def remember(agent_id, command):
    """Keep recent commands within both the count and Python-byte budgets."""
    valid = (agent_id and isinstance(command, str)
             and templates.LABEL_CLIP <= len(command) <= MAX_COMMAND)
    size = sys.getsizeof(command) if valid else 0
    with _lock:
        now = time.monotonic()
        _expire(now)
        # Reject before allocating/touching an agent or evicting hot entries.
        if not valid or size > PER_AGENT_BYTES:
            return
        commands = _recent.get(agent_id)
        if commands is None:
            commands = _Commands()
            _recent[agent_id] = commands
        if command not in commands.entries:
            commands.entries[command] = None
            commands.nbytes += size
        commands.entries.move_to_end(command)
        while (commands.nbytes > PER_AGENT_BYTES
               or len(commands.entries) > PER_AGENT):
            old, _ = commands.entries.popitem(last=False)
            commands.nbytes -= sys.getsizeof(old)
        commands.last_used = now
        _recent.move_to_end(agent_id)
        while len(_recent) > AGENTS:
            _recent.popitem(last=False)


def resolve(agent_id, shown):
    """Resolve a prefix; only successful reads touch entry and agent LRU/TTL.

    Heredoc-equivalent matches all count as used; ambiguous or missing
    prefixes never extend an agent's lifetime. answer/expand share this path.
    """
    shown = shown.strip() if isinstance(shown, str) else ""
    with _lock:
        now = time.monotonic()
        _expire(now)
        if not agent_id or not shown:
            return ""
        commands = _recent.get(agent_id)
        if commands is None:
            return ""
        matches = [command for command in commands.entries
                   if len(command) > len(shown) and command.startswith(shown)]
        choices = set(matches)
        if len(choices) > 1:
            choices = {shapes.redact(command) for command in choices}
        if len(choices) != 1:
            return ""
        result = choices.pop()
        if result:
            for command in matches:
                commands.entries.move_to_end(command)
            commands.last_used = now
            _recent.move_to_end(agent_id)
        return result


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
