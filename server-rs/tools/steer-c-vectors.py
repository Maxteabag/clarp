#!/usr/bin/env python3
"""Record the reference's Claude input fence and replay echoes for issue 491."""

from __future__ import annotations

import io
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/steer-c-vectors.json"


def block(kind, tool_id):
    return {"type": kind, "id": tool_id}


def partial(part, parent=None):
    return {"type": "stream_event", "parent_tool_use_id": parent,
            "event": {"type": "content_block_start", "content_block": part}}


def assistant(*parts):
    return {"type": "assistant", "message": {"content": list(parts)}}


def returned(tool_id, parent=None):
    return {"type": "user", "parent_tool_use_id": parent,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]}}


def echo(uid):
    return {"type": "user", "isReplay": True, "uuid": uid}


def result(*uuids, failed=False):
    return {"type": "result", "is_error": failed, "user_message_uuids": list(uuids)}


STREAMS = {
    "ordinary answer": [echo("one"), partial({"type": "text"}), result("one")],
    "between tools and a nested result": [
        partial(block("tool_use", "first")), returned("first"),
        {"type": "result", "parent_tool_use_id": "task"},
        partial(block("tool_use", "next")), returned("next"), echo("two"), result("one", "two"),
    ],
    "authoritative assistant repeats a partial call": [
        partial(block("tool_use", "bash")), assistant(block("tool_use", "bash")),
        returned("missing"), returned("bash"), result("one"),
    ],
    "parallel calls finish separately": [
        assistant(block("tool_use", "bash"), block("server_tool_use", "search")),
        returned("search"), returned("bash"), result("one"),
    ],
    "foreground Task outlives its child": [
        partial(block("tool_use", "task")), partial(block("tool_use", "child"), "task"),
        returned("child", "task"), returned("task"), echo("two"), result("one", "two"),
    ],
    "two follow-ups are echoed in arrival order": [
        partial(block("tool_use", "bash")), returned("bash"),
        echo("two"), echo("three"), result("one", "two", "three"),
    ],
    "the result crosses the follow-up echo": [
        partial(block("tool_use", "bash")), result("one"),
        echo("two"), result("two", failed=True),
    ],
}


@scratch.case("steer-c-reference-")
def generate(root):
    replay = runpy.run_path(str(ROOT / "tests/replay/host.py"))
    replay["isolate"](root)
    replay["loopback_only"]()
    sys.path.insert(0, str(ROOT / "server"))
    from lib.backend import claude

    streams = []
    for name, script in STREAMS.items():
        state = claude.SteerState(io.StringIO())
        handle = SimpleNamespace(steer_state=state)
        steps = []
        for event in script:
            echo_seen = claude.note_steer(handle, event)
            steps.append({"event": event, "can_offer": state.open,
                          "echo": event.get("uuid") if echo_seen else None})
        streams.append({"name": name, "steps": steps})
    return {"streams": streams}


if __name__ == "__main__":
    OUT.write_text(json.dumps(generate(), indent=2, ensure_ascii=True) + "\n", encoding="ascii")
