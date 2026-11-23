#!/usr/bin/env python3
# A stand-in for `agy acp`: an Agent Client Protocol (v1) agent on standard input and output,
# scripted by the file `scenario` beside it (one word; `turn` when there is none). Every
# message it is sent is written to `acp.log` beside it, one per line, for the test to read.
# Started with anything but `acp` first (`agy models`, `agy --version`), it says nothing.
import json, os, sys

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


def ask(sid, options, title):
    send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": {"toolCallId": "t-2", "title": title}, "options": options}})
    while True:
        answer = read()
        if answer.get("id") == "perm-1" and "method" not in answer:
            return answer


def prompt(mid, sid):
    if scenario == "quota":
        return error(mid, -32603, "Error 429, Message: Resource has been exhausted, Status: RESOURCE_EXHAUSTED "
                     "[reason:QUOTA_EXHAUSTED quotaResetTimeStamp:2026-11-23T18:00:00Z]")
    if scenario == "signature":
        return error(mid, -32603, "messages.3.content.0: Invalid `signature` in `thinking` block")
    text(sid, "agent_thought_chunk", "Considering")
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
                                           "promptCapabilities": {"image": True, "audio": True, "embeddedContext": True},
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
        prompt(mid, params.get("sessionId"))
    elif mid is not None:
        error(mid, -32601, "Method not found")
