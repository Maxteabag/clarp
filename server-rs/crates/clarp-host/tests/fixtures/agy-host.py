#!/usr/bin/env python3
"""Host scenarios using the same ACP peer as the shim integration tests."""
import json
import os
from pathlib import Path
import sys
import uuid

import fake_acp as peer

root = Path(__file__).resolve().parent
scenario = peer.scenario
for model in ["gemini-3-flash", "gemini-4-hydrogen-low", "gemini-4-carbon-high"]:
    peer.OPTIONS[0]["options"].append({"value": model, "name": model})


def record(name, value):
    with (root / name).open("a") as out:
        print(json.dumps(value), file=out)


def selected(option):
    return next(row["currentValue"] for row in peer.OPTIONS if row["id"] == option)


def keeper(mid, sid, words):
    read, schema_text = words.rsplit("Return only a JSON answer matching this schema: ", 1)
    schema = json.loads(schema_text)
    work, home = Path.cwd(), Path.home()
    login = home / ".gemini/oauth_creds.json"
    settings = home / ".gemini/antigravity-cli/settings.json"
    record("kept", {"args": sys.argv[1:], "model": selected("model"), "read": read, "schema": schema,
                    "works_in": work.name, "in_work": sorted(os.listdir(work)),
                    "beside": sorted(os.listdir(work.parent)), "home": os.path.relpath(home, work.parent),
                    "login_linked": login.is_symlink(), "login": login.read_text() if login.exists() else None,
                    "settings_copied": settings.is_file() and not settings.is_symlink(),
                    "env": sorted(k for k in os.environ if (k.startswith("CLARP_") and os.environ[k]) or k in ("HOME", "PATH"))})
    if (root / "keeper-fails").exists():
        return peer.error(mid, -32603, "the model is not logged in: " + read)
    steps = {s: {"status": "pending", "evidence": "", "note": ""}
             for s in schema["properties"]["steps"]["required"]}
    if "probe" in steps:
        steps["probe"].update(status="completed", evidence="the owner said: it behaves")
    if "build" in steps:
        steps["build"].update(status="completed", note="said so")
    answer = {"steps": steps, "checkpoint": {"progress": "Probe done; the build is said to be done.",
              "next_work": "Show the build."}, "changed": True}
    finish(mid, sid, json.dumps(answer))


def finish(mid, sid, words):
    peer.text(sid, "agent_message_chunk", words)
    peer.result(mid, {"stopReason": "end_turn"})


def prompt(mid, sid, words):
    if scenario == "keeper":
        if "--sandbox" in sys.argv:
            return keeper(mid, sid, words)
        record("tokens", [sid, os.environ["CLARP_PROVIDER_TURN"]])
        return finish(mid, sid, "done")
    if scenario == "router":
        packet = json.loads(words.split("INPUT:\n", 1)[1])
        record("asked", {"utterance": packet["utterance"], "args": sys.argv[1:], "model": selected("model"),
                         "effort": selected("thought_level"), "home": str(Path.home()),
                         "login_linked": (Path.home() / ".gemini/oauth_creds.json").is_symlink()})
        if "broken" in packet["utterance"]:
            print("workspace discovery warning", file=sys.stderr, flush=True)
            return peer.error(mid, -32603, "out of quota")
        return finish(mid, sid, json.dumps({"kind": "agent_message", "target_session": "anna",
                                           "confidence": 0.95, "addressing": True}))
    if scenario == "speech":
        if "voice-fixture-next" in words:
            return finish(mid, sid, "<speak>Next turn only.</speak>")
        peer.text(sid, "agent_message_chunk", "<speak>First line. " +
                  "The longer opening stays ahead of the short answer. " * 8 + "</speak>")
        peer.text(sid, "agent_thought_chunk", "Preparing the summary")
        return finish(mid, sid, "<speak>First summary.</speak>")
    if scenario == "counted":
        peer.text(sid, "user_message_chunk", words)
    if scenario in ("counted", "lost"):
        record("started", {"session": sid, "prompt": words})
        number = len((root / "started").read_text().splitlines())
        answer = f"agy answer {number}" if scenario == "counted" else f"Answer {number}."
        return finish(mid, sid, answer)
    if scenario == "tool-turn":
        if "fail now" in words:
            return peer.error(mid, -32603, "quota exhausted")
        if "list what is here" in words:
            peer.update(sid, {"sessionUpdate": "tool_call", "toolCallId": "ls", "title": "run_command",
                              "kind": "execute", "status": "in_progress", "rawInput": {"CommandLine": "ls -la"}})
            peer.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "ls", "status": "completed",
                              "rawOutput": {"combinedOutput": "Output:\na.txt"}})
        peer.text(sid, "agent_message_chunk", "Seen: ")
        peer.text(sid, "agent_message_chunk", "one file.")
        return peer.result(mid, {"stopReason": "end_turn", "usage": {"inputTokens": 140, "outputTokens": 9,
                                                                  "thoughtTokens": 3}})
    return peer.prompt(mid, sid, words)


def new_id():
    if scenario == "keeper":
        return str(uuid.uuid4())
    if scenario == "lost":
        if os.environ.get("CLARP_AGY_MANAGED_TURN") != "1":
            return "conv-probe"
        path = root / "begun"
        number = int(path.read_text()) + 1 if path.exists() else 1
        path.write_text(str(number))
        return f"conv-{number}"
    return "conv-new"


peer.serve(prompt, new_id)
