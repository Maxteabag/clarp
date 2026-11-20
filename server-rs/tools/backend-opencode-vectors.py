#!/usr/bin/env python3
"""Record the command, model pair, permissions, session and text rules of both tools."""
import importlib.util
import itertools
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/backend-opencode-vectors.json"


def run_streams(adapter):
    """Use the real drain, replacing process, database, live hub and speech effects."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from lib.backend import opencode

    def story(events, code=0, stderr=""):
        calls, sessions, results, errors = [], [], [], []
        lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
        proc = SimpleNamespace(stdout=iter(lines), wait=lambda: code)
        def transition(agent, event, detail):
            if "tool" in detail:
                calls.append([detail["tool"], detail["input"], detail["status"]])
        with (patch.object(adapter, "_transition", transition),
              patch.object(adapter, "_speak"), patch.object(adapter, "_broadcast"),
              patch.object(opencode, "stderr_text", return_value=stderr),
              patch.object(opencode.tool_explanation_commands, "remember")):
            adapter._drain_stdout(proc=proc, agent_id="", session="", trace_id="", handle=None,
                                  backend_session_id="", on_session_init=sessions.append,
                                  on_result=results.append, on_error=errors.append, stream=None, enqueue=None)
        return dict(lines=lines, code=code, stderr=stderr, sessions=sessions, calls=calls,
                    result=next(iter(results), None), error=next(iter(errors), None))

    streams = [story(["invalid", "", [], None]), story([], 3), story([], 7, "explicit failure")]
    for kind in ("tool_use", "tool_start", "tool.running"):
        for state in ("running", "completed", "error"):
            args = dict(filePath="file", oldString="old", newString="new", replaceAll=False,
                        none=None, nested={"a": 1}, flag=True, number=2,
                        rows=[{"word": "b"*201, "child": {}, "flag": False}, None, ["omit"]],
                        command="make check")
            for name in ("bash", "read", "edit", "write", "glob", "grep", "webfetch",
                         "websearch", "todowrite", "skill", "custom"):
                part = dict(tool=name, state={"input": args, "status": state}, callID="c1")
                streams.append(story([dict(type="session", properties=dict(sessionId=" ses_one ")),
                                      dict(event=kind, part=part), dict(type="text", text="worked")]))
    for n in (199, 200, 201, 399, 400, 401, 49, 50, 51):
        streams.append(story([dict(type="tool_use", part=dict(id="i",name="read",state={
            "input": {"filePath":"a"*n, "rows":[{"key":"b"*n}]*min(n,51)}, "status":"completed"}))]))
    for e in (None, "", {"data":{"message":" nested "}}, {"message":" direct "},
              {"name":"APIError"}, {"other": 2}, "provider failed"):
        streams.append(story([dict(type="error",error=e,message="fallback"),dict(type="text",text="too late")]))
    for toks in ({}, {"input":0,"output":0}, {"input":12.5,"output":2}, {"input":"12","output":4}):
        streams.append(story([dict(type="step_finish",part={"tokens":toks,"cost":0.1})]*2))
    for kind in ("text","message","assistant","output"):
        streams.append(story([dict(type="step_start"),dict(type="text",text="comment"),
                              dict(type="step_start"),dict(type=kind,role="system",text="ignore"),
                              dict(type=kind,part={"text":"answer"}),dict(type=kind,delta="more")]))
    streams.append(story([{"type": "error"}]))
    return streams


def generate():
    spec = importlib.util.spec_from_file_location("replay", ROOT / "tests/replay/host.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    with tool_scratch.directory() as scratch:
        root = pathlib.Path(scratch)
        (root / "scenario-config.toml").write_text("")
        replay.isolate(root)
        replay.loopback_only()
        sys.path.insert(0, str(ROOT / "server"))
        from lib.backend.opencode import _session_id_from
        from lib.backend.opencode_serve import split_model
        from lib.backend.registry import by_id
        from lib import provider_capabilities as caps
        listings = ["", "words about a model", "DeepSeek", "deepseek", "-option",
                    "huggingface/org/DeepSeek\nother/one\nhuggingface/org/DeepSeek",
                    "x/one\n{\ninvalid\n}", "x/one\n{",
                    "x/one\n{\n}\nx/one\n{\n}",
                    "fireworks-ai/org/deepseek-v4\n{\n  \"name\": \"Example\",\n  \"variants\": {\"high\": {}, \"unknown\": {}}\n}",
                    "deepseek/d\nopencode/deepseek\nopenrouter/DeepSeek\nmyhost/deepseek/",
                    "x/deepseek\n{\n  \"variants\": {}\n}"]
        listings = [[s, caps.parse_opencode_models(s, observed_at="at"),
                     caps.parse_deepseek_models(s, observed_at="at")] for s in listings]
        rows = []
        for name in ("deepseek", "opencode"):
            adapter = by_id(name)
            for sid, fresh, model, effort, think in itertools.product(
                    ("", "ses_one"), (True, False), ("", "provider/model"),
                    ("", "high"), (True, False)):
                argv = adapter.build_cmd(sid, is_new_session=fresh, model=model,
                                         effort=effort, thinking=think)
                rows.append([sid if not fresh else "", model, effort, think, argv])
        perms = []
        for raw in ("", "broken", "null", "[]", "true", "{}", '{"edit":"ask"}',
                    '{"question":"allow"}', '{"question":null}'):
            os.environ["OPENCODE_PERMISSION"] = raw
            perms.append([raw, json.loads(adapter.turn_env()["OPENCODE_PERMISSION"])])
        events = [{"type": kind, "role": role, "part": part}
                  for kind, role, part in itertools.product(
                      ("assistant", "output", "message", "text", "reasoning"),
                      ("assistant", "user", "system"),
                      (None, "word", {"content": [{"delta": "a"}, {"text": "b"}]}))]
        events += [{"event": "text", field: value} for field in ("part", "text", "content", "delta")
                   for value in ({"part": {"message": "inner"}}, "", 5, ["a", None, "b"])]
        sessions = [{key: {other: " nested "}} for key in ("session", "data", "part", "properties")
                    for other in ("sessionID", "sessionId", "session_id")]
        sessions += [{"sessionID": val, "sessionId": " second "} for val in (5, [], "", " first ")]
        from lib import opencode_transcript as transcript
        recorded = []
        original = transcript._turns_from_messages

        def capture(rows, parts=None, ids=None):
            result = original(rows, parts, ids)
            messages = [[int(t), json.loads(raw), (parts or {}).get(ids[i], []) if ids else []]
                        for i, (t, raw) in enumerate(rows)]
            recorded.append([messages, result])
            return result

        transcript._turns_from_messages = capture
        suite = importlib.util.spec_from_file_location("transcript_suite", ROOT / "tests/unit/test_opencode_transcript.py")
        examples = importlib.util.module_from_spec(suite)
        suite.loader.exec_module(examples)
        for key, fn in vars(examples).items():
            if key.startswith("test_") and fn.__code__.co_argcount == 0:
                fn()
        for kind in ("edit", "write", "webfetch", "websearch", "skill", "custom", "glob", "read",
                     "grep", "task", "question", "todowrite", "bash"):
            for status in ("completed", "error", "running", "pending"):
                examples._turns([("human", None, [examples._text("hello")]), ("ai", "stop", [
                    examples._tool(kind, status=status, input={"filePath": "a", "oldString": "old",
                        "newString": "new", "replaceAll": True}, output="output", error="Error: example"),
                    examples._text("done")])])
        # Legacy inline text and parts, type aliases, ignored and synthetic parts,
        # and both sides of the seconds-to-milliseconds switch.
        for created, role in itertools.product((9999999999, 10000000000, 10000000001),
                                                ("human", "AI", "model", "unknown")):
            capture([(str(created), json.dumps({"type":role, "parts":[{"text":"one"},
                {"type":"text","text":"ignored","ignored":True}, {"type":"text","text":"synthetic","synthetic":True},
                {"type":"text","content":[{"delta":"two"}, {"text":"three"}]}]}))])
        for inline in ("inline", {"content":["a","b"]}, False, 0, None):
            capture([("123", json.dumps({"role":"assistant","content":inline,"body":"body","parts":[{"text":"parts"}]}))])
        for n in (39,40,41):
            examples._turns([("assistant","stop",[examples._text("patch"),examples._tool("edit",
                input={"oldString":"\n".join(["old"]*n),"newString":"\n".join(["new"]*n)})])])
        return dict(streams=run_streams(adapter), history=recorded, listings=listings, commands=rows, permissions=perms,
                    models=[[s, split_model(s)] for s in ("", "/", "/id", "p/", "p/m", "p/a/b")],
                    texts=[[e, adapter.routing_text(json.dumps(e))] for e in events],
                    sessions=[[e, _session_id_from(e)] for e in sessions])


if __name__ == "__main__":
    if "--inside" in sys.argv:
        print(json.dumps(generate(), ensure_ascii=True))
    else:
        ran = subprocess.run([sys.executable, "-I", __file__, "--inside"], check=True,
                             env=tool_scratch.child_env({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}),
                             capture_output=True, text=True)
        OUT.write_text(json.dumps(json.loads(ran.stdout.splitlines()[-1]), indent=1) + "\n")
