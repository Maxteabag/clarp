#!/usr/bin/env python3
# A stand-in for `agy acp`: an Agent Client Protocol (v1) agent on standard input and output,
# scripted by the file `scenario` beside it (one word; `turn` when there is none). Every
# message it is sent is written to `acp.log` beside it, one per line, for the test to read.
# Started with anything but `acp` first (`agy models`, `agy --version`), it says nothing.
#
# Scenario `steer` meets a second session/prompt as agy 1.5 does (seen on 23 Nov 2026): the
# running prompt is answered at once as ended and the work goes on under the new one. The
# first prompt's text says how (`case-tool`, `case-writing`, `case-refused`, `case-stop`,
# `case-after`); a prompt that names `add-...` and no case is a message run as a turn of its
# own, answered `separate-add-...`. It waits for files beside it (`taken-<mode>`: say a word
# under the new prompt; `release-<mode>`: answer) and leaves files for the test
# (`working-<mode>`, `received-<message>`).
import json, os, re, subprocess, sys, time

here = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:2] != ["acp"]:
    sys.exit(0)
try:
    with open(os.path.join(here, "scenario")) as said:
        scenario = said.read().strip() or "turn"
except FileNotFoundError:
    scenario = "turn"
log = open(os.path.join(here, "acp.log"), "a")
with open(os.path.join(here, "argv"), "a") as argv:
    argv.write(json.dumps({"argv": sys.argv[1:], "env": {k: os.environ.get(k) for k in (
        "GEMINI_API_KEY", "AGY_CLI_DISABLE_AUTO_UPDATE", "AGY_ACP_FORCE_FILE_STORAGE", "SSH_CONNECTION")}}) + "\n")

OPTIONS = [
    {"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": "gemini-a",
     "options": [{"value": "gemini-a", "name": "Gemini A"}, {"value": "claude-b", "name": "Claude B"}]},
    {"id": "thought_level", "name": "Thinking", "category": "thought_level", "type": "select",
     "currentValue": "low", "options": [{"value": "low", "name": "Low"}, {"value": "high", "name": "High"}]},
]


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    message = json.loads(line)
    log.write(json.dumps(message) + "\n")
    log.flush()
    return message


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(mid, value):
    send({"jsonrpc": "2.0", "id": mid, "result": value})


def error(mid, code, message):
    send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


def update(sid, value):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": value}})


def text(sid, kind, words):
    update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": words}})


def touch(name):
    open(os.path.join(here, name), "a").close()


def wait_file(name):
    end = time.monotonic() + 40
    while not os.path.exists(os.path.join(here, name)):
        if time.monotonic() >= end:
            sys.exit("stand-in release expired: " + name)
        time.sleep(0.02)


def said_in(message):
    return "".join(block.get("text", "") for block in (message.get("params") or {}).get("prompt", [])
                   if block.get("type") == "text")


def next_prompt():
    while True:
        message = read()
        if message.get("method") == "session/prompt":
            return message


def wait_cancel():
    while True:
        if read().get("method") == "session/cancel":
            return


def tool(sid, status):
    kind = "tool_call" if status == "in_progress" else "tool_call_update"
    update(sid, {"sessionUpdate": kind, "toolCallId": "t-s", "title": "run_command", "kind": "execute",
                 "status": status, "rawInput": {"CommandLine": "sleep 5"}})


def steering(mid, sid, words):
    found = re.search(r"case-([a-z]+)", words)
    mode = found.group(1) if found else ""
    if mode == "after":
        # Answered before anything else comes: what comes later is a prompt of its own.
        text(sid, "agent_message_chunk", "first-after")
        return result(mid, {"stopReason": "end_turn"})
    if mode == "writing":
        text(sid, "agent_message_chunk", "before-steer")
    else:
        tool(sid, "in_progress")
    touch("working-" + mode)
    second = next_prompt()
    touch("received-" + said_in(second))
    if mode == "refused":
        error(second["id"], -32603, "a prompt is already running")
        wait_file("release-" + mode)
        tool(sid, "completed")
        text(sid, "agent_message_chunk", "first-refused")
        return result(mid, {"stopReason": "end_turn"})
    # As agy 1.5 does: the running prompt is answered at once; the work goes on under the new one.
    result(mid, {"stopReason": "end_turn", "usage": {"inputTokens": 3, "outputTokens": 1}})
    # Its first word under the new prompt is what makes the message read: the test says when.
    wait_file("taken-" + mode)
    text(sid, "agent_thought_chunk", "Reading " + said_in(second))
    if mode == "stop":
        third = next_prompt()
        touch("received-" + said_in(third))
        result(second["id"], {"stopReason": "end_turn"})
        # Nothing is said of the third prompt: it is never taken. The Stop comes.
        wait_cancel()
        tool(sid, "failed")
        return result(third["id"], {"stopReason": "cancelled"})
    wait_file("release-" + mode)
    if mode != "writing":
        tool(sid, "completed")
    text(sid, "agent_message_chunk", "after-" + mode)
    result(second["id"], {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5}})


def ask(sid, options, title):
    send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": {"toolCallId": "t-2", "title": title}, "options": options}})
    while True:
        answer = read()
        if answer.get("id") == "perm-1" and "method" not in answer:
            return answer


def prompt(mid, sid, words=""):
    if scenario == "steer":
        alone = re.search(r"add-[a-z0-9-]+", words)
        if alone and "case-" not in words:
            text(sid, "agent_message_chunk", "separate-" + alone.group(0))
            return result(mid, {"stopReason": "end_turn"})
        return steering(mid, sid, words)
    if scenario == "quota":
        return error(mid, -32603, "Error 429, Message: Resource has been exhausted, Status: RESOURCE_EXHAUSTED "
                     "[reason:QUOTA_EXHAUSTED quotaResetTimeStamp:2026-11-23T18:00:00Z]")
    if scenario == "signature":
        return error(mid, -32603, "messages.3.content.0: Invalid `signature` in `thinking` block")
    text(sid, "agent_thought_chunk", "Considering")
    if scenario == "orphan":
        # A child that outlives agy and holds its standard error.
        subprocess.Popen(["sleep", "120"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    if scenario == "slow":
        time.sleep(1.5)
    if scenario == "slow-tool":
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t-9", "title": "run_command",
                     "kind": "execute", "status": "in_progress", "rawInput": {"CommandLine": "make"}})
        time.sleep(1.5)
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t-9", "status": "completed",
                     "rawOutput": {"combinedOutput": "Output:\nbuilt"}})
    if scenario == "big":
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t-big", "title": "run_command",
                     "kind": "execute", "status": "pending", "rawInput": {"CommandLine": "cat big"}})
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t-big", "status": "completed",
                     "content": [{"type": "content", "content": {"type": "image", "mimeType": "image/png",
                                                                 "data": "A" * (3 * 1024 * 1024)}}],
                     "rawOutput": {"combinedOutput": "Output:\n" + "line\n" * 1000000}})
    if scenario == "cancel-ask":
        text(sid, "agent_message_chunk", "working")
        while True:
            message = read()
            if message.get("method") == "session/cancel":
                break
        ask(sid, [{"optionId": "yes", "name": "Allow", "kind": "allow_once"}], "Run after stop?")
        return result(mid, {"stopReason": "cancelled"})
    if scenario == "tool":
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t-1", "title": "run_command",
                     "kind": "execute", "status": "pending", "rawInput": {"CommandLine": "ls", "Cwd": "/w"}})
        ask(sid, [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                  {"optionId": "no", "name": "Reject", "kind": "reject_once"}], "Run ls?")
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t-1", "status": "completed",
                     "rawOutput": {"combinedOutput": "Output:\na\nb"}})
    if scenario == "question":
        ask(sid, [{"optionId": "red", "name": "Red", "kind": "reject_once"}], "Which colour?")
    if scenario == "cancel":
        text(sid, "agent_message_chunk", "working")
        while True:
            message = read()
            if message.get("method") == "session/cancel":
                return result(mid, {"stopReason": "cancelled"})
    text(sid, "agent_message_chunk", "Hel")
    text(sid, "agent_message_chunk", "lo")
    update(sid, {"sessionUpdate": "usage_update", "used": 1234, "size": 1000000})
    result(mid, {"stopReason": "end_turn",
                 "usage": {"inputTokens": 10, "outputTokens": 5, "thoughtTokens": 2, "cachedReadTokens": 3}})


while True:
    message = read()
    method, mid = message.get("method"), message.get("id")
    if method is None:
        continue
    params = message.get("params") or {}
    if method == "initialize":
        result(mid, {"protocolVersion": 1, "agentInfo": {"name": "agy", "version": "1.5.15"},
                     "agentCapabilities": {"loadSession": True,
                                           # As agy 1.5 says it: no audio, unless the test asks for it.
                                           "promptCapabilities": {"image": True,
                                                                  "audio": os.path.exists(os.path.join(here, "audio")),
                                                                  "embeddedContext": True},
                                           "_meta": {"antigravity": {"systemPrompt": {"append": True}}}},
                     "authMethods": [{"id": "oauth-personal", "name": "Google"}]})
    elif method == "session/new":
        if scenario == "signed-out":
            error(mid, -32000, "Authentication required")
        else:
            result(mid, {"sessionId": "conv-new", "configOptions": OPTIONS})
    elif method == "session/resume":
        if scenario == "resume-lost":
            error(mid, -32002, "Session not found")
        else:
            result(mid, {"configOptions": OPTIONS})
    elif method == "session/set_config_option":
        for option in OPTIONS:
            if option["id"] == params.get("configId"):
                option["currentValue"] = params.get("value")
        result(mid, {"configOptions": OPTIONS})
    elif method == "session/prompt":
        prompt(mid, params.get("sessionId"), said_in(message))
    elif mid is not None:
        error(mid, -32601, "Method not found")
