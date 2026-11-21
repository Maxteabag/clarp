#!/usr/bin/env python3
"""Issue 491 B: the reference's provider stream, transcript and input rules."""
from contextlib import ExitStack
from io import StringIO
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import scratch

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
OUT = TOOLS.parent / "crates/clarp-kernel/tests/fixtures/takeover-vectors.json"


class Received(StringIO):
    def close(self):
        # The reference closes the input after one line. Retain it for inspection.
        self.closed_by_runner = True


@scratch.case("clarp-takeover-vectors-")
def capture(place):
    spec = importlib.util.spec_from_file_location("replay_host", ROOT / "tests/replay/host.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    replay.isolate(place)
    replay.loopback_only()
    sys.path.insert(0, str(ROOT / "server"))
    from lib import takeover
    from lib.backend import agy, claude

    def claude_block(kind, where):
        return {"type": "stream_event", "event": {where: {"type": kind}}}

    def agy_step(kind, delta):
        return {"event": "step_update", "step_update": {"step_type": kind, "text_delta": delta}}

    streams = []
    for provider, observe, events in (
        ("claude", claude.note_output, [
            {},
            {"type": "system", "subtype": "init"},
            claude_block("thinking", "content_block"),
            claude_block("thinking_delta", "delta"),
            claude_block("text", "content_block"),
            claude_block("text_delta", "delta"),
            claude_block("tool_use", "content_block"),
            claude_block("input_json_delta", "delta"),
            claude_block("server_tool_use", "content_block"),
            {"type": "assistant", "message": {"content": [None, {"type": "thinking"}]}},
            {"type": "assistant", "message": {"content": [{"type": "thinking"}, {"type": "text"}]}},
            {"type": "assistant", "message": {"content": [{"type": "server_tool_use"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_result"}]}},
            {"type": "result", "subtype": "success"},
        ]),
        ("agy", agy.note_output, [
            {}, {"event": "init"}, {"event": "step_update", "step_update": []},
            agy_step("user_input", "already sent"), agy_step("checkpoint", ""),
            agy_step("agent_response", ""), agy_step("agent_response", "\t "),
            agy_step("agent_response", "first word"), agy_step("tool", ""),
            agy_step("agent_response", True), agy_step("agent_response", False),
        ]),
    ):
        for event in events:
            handle = SimpleNamespace(lines_read=0, output_seen=False)
            observe(handle, event)
            streams.append(dict(provider=provider, event=event, output=handle.output_seen))

    traces = ["trace-one", "trace-two", " trace-one\n", chr(0x2603) + "-trace", "\x1ftrace-one\x1c"]
    uuids = [dict(trace=t, uuid=claude.prompt_uuid(t)) for t in traces]

    def launch(trace, first, second):
        stdin = Received()
        cmd_seen = []

        def stand_in(cmd, **kwargs):
            cmd_seen.extend(cmd)
            return (SimpleNamespace(stdin=stdin), SimpleNamespace(process_group=123))

        backend = claude.ClaudeBackend()
        plan = None if first is None else dict(first=first, resume_at="previous", drops_turn="original")
        with ExitStack() as stack:
            stack.enter_context(patch.object(backend, "build_cmd", return_value=["claude", "--resume", "s"]))
            stack.enter_context(patch.object(backend, "register_handle"))
            stack.enter_context(patch.object(claude, "launch", side_effect=stand_in))
            stack.enter_context(patch.object(claude, "start_drain"))
            stack.enter_context(patch.object(claude, "log"))
            stack.enter_context(patch.object(claude.shutil, "which", return_value="/stand-in/claude"))
            backend.start_turn(text=second, cwd=place, trace_id=trace, takeover=plan, isolated=True)
        lines = stdin.getvalue().splitlines()
        assert len(lines) == 1 and stdin.closed_by_runner
        return dict(trace=trace, first=first, second=second, line=json.loads(lines[0]), args=cmd_seen)

    launches = [
        launch(traces[0], None, "plain"),
        launch(traces[1], "first line\nmore", 'second with "quotes"'),
        launch(traces[3], chr(0x2603), "two"),
    ]

    def entry(kind, uuid):
        return json.dumps(dict(type=kind, uuid=uuid))

    transcripts = []
    path = place / "transcript.jsonl"
    for lines in (
        None, [], [entry("user", "this")],
        [entry("assistant", "before"), entry("user", "this")],
        [entry("user", "earlier"), entry("summary", "ignored"), entry("assistant", "last")],
        [entry("user", "earlier"), entry("user", "this"), "{half a line"],
        [entry("user", "earlier"), entry("user", "this"), entry("assistant", "answer")],
        ["1", "[]", entry("assistant", "before"), entry("user", "this"), entry("system", "no")],
    ):
        text = None if lines is None else "\n".join(lines)
        if text is not None:
            path.write_text(text)
        facts = takeover.session_facts(None if text is None else path, "this")
        transcripts.append(dict(transcript=text, prompt="this", facts=facts))

    agy_guards = []
    for turns, first in [
        (None, "first"), ([], "first"), ([], "  "),
        ([{"role": "assistant", "text": "first"}], "first"),
        ([{"role": "user", "text": "other"}], "first"),
        ([{"role": "user", "text": "prefix first suffix"}], "\tfirst "),
        ([{"role": "user", "text": "First"}], "first"),
        ([{"role": "user", "text": None}], "first"),
    ]:
        agy_guards.append(dict(turns=turns, first=first,
                               allowed=takeover.agy_plan(turns, first) is not None))
    versions = []
    executable = place / "fake-tool"
    executable.write_text("only the stat is used")
    for provider, ready, outputs in (
        ("claude", takeover.claude_version_ok,
         ["none", "2.1.222", "2.1.223", "2.1.321 (Claude Code)", "2.2.0", "1.9.999", "2.1"]),
        ("agy", takeover.agy_version_ok,
         ["unknown", "agy 1.5.12", "agy 1.5.13", "1.5.14", "1.6.0", "1.4.99", "2.0.0"]),
    ):
        for output in outputs:
            takeover._cli_version.cache_clear()
            with patch.object(takeover.shutil, "which", return_value=str(executable)), \
                    patch.object(takeover.subprocess, "run", return_value=SimpleNamespace(stdout=output)):
                allowed = ready("fake-tool")
            versions.append(dict(provider=provider, output=output, allowed=allowed))

    return dict(uuids=uuids, launches=launches, streams=streams, transcripts=transcripts,
                agy_guards=agy_guards, versions=versions)


if __name__ == "__main__":
    data = capture()
    OUT.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="ascii")
    print(f"{OUT.name}: {sum(map(len, data.values()))} cases")
