#!/usr/bin/env python3
# A stand-in for `agy acp`: an Agent Client Protocol (v1) agent on standard input and output,
# scripted by the file `scenario` beside it (one word; `turn` when there is none). Every
# message it is sent is written to `acp.log` beside it, one per line, for the test to read.
# Model and version probes are separate from turns. Record any other non-ACP launch.
#
# Scenario `steer` meets a second session/prompt as agy 1.5 does (seen on 23 Nov 2026): the
# running prompt is answered at once as ended and the work goes on under the new one. The
# first prompt's text says how (`case-tool`, `case-writing`, `case-sentence`, `case-closing`, `case-background`, `case-refused`,
# `case-stop`, `case-after`, `case-hold`, the paragraph cases in `WRITTEN`, and the cut cases in `CUTS`); a prompt that names `add-...` and no case is a message run as a turn of its
# own, answered `separate-add-...`. It waits for files beside it (`taken-<mode>`: say a word
# under the new prompt; `release-<mode>`: answer) and leaves files for the test
# (`working-<mode>`, `received-<message>`).
import json, os, re, subprocess, sys, time

here = os.path.dirname(os.path.abspath(__file__))


def keep(conversation):
    # Where agy keeps a conversation, under its user's home: what the Host looks for.
    store = os.path.join(os.environ.get("HOME", here), ".gemini", "antigravity-cli", "conversations")
    os.makedirs(store, exist_ok=True)
    open(os.path.join(store, conversation + ".db"), "a").close()


MODELS = [("gemini-a", "Gemini A"), ("claude-b", "Claude B"), ("gemini-4-neon", "Gemini 4 Neon")]
EFFORTS = ["low", "medium", "high"]

if sys.argv[1:2] != ["acp"]:
    if sys.argv[1:] == ["models"]:
        for model, label in MODELS:
            for effort in EFFORTS:
                print(f"{model}-{effort}\t{label} ({effort.title()})")
    elif sys.argv[1:] != ["--version"]:
        with open(os.path.join(here, "print.log"), "a") as out:
            print(json.dumps(sys.argv[1:]), file=out)
        sys.exit("unexpected standalone invocation")
    sys.exit(0)
try:
    with open(os.path.join(here, "scenario")) as said:
        scenario = said.read().strip() or "turn"
except FileNotFoundError:
    scenario = "turn"
log = open(os.path.join(here, "acp.log"), "a")
with open(os.path.join(here, "argv"), "a") as argv:
    argv.write(json.dumps({"argv": sys.argv[1:], "env": {k: os.environ.get(k) for k in (
        "GEMINI_API_KEY", "CLARP_AGY_MANAGED_TURN", "AGY_CLI_DISABLE_AUTO_UPDATE", "AGY_ACP_FORCE_FILE_STORAGE", "SSH_CONNECTION")}}) + "\n")

OPTIONS = [
    {"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": "gemini-a",
     "options": [{"value": value, "name": name} for value, name in MODELS]},
    {"id": "thought_level", "name": "Thinking", "category": "thought_level", "type": "select",
     "currentValue": "low", "options": [{"value": level, "name": level.title()} for level in EFFORTS]},
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


# What agy writes before and after the hand-over when it answers the new prompt inside the
# text it was writing (seen live on 23 Nov 2026): the new prompt is then answered with nothing
# else said.
WRITTEN = {
    # A paragraph break inside a piece written after the hand-over.
    "paragraph": ("5 plus", [" 4 is 9.\n\n7 times", " 6 is 42."]),
    # A break whose first half ended the piece before the hand-over.
    "straddle": ("5 plus 4 is 9.\n", ["\n7 times 6 is 42."]),
    # Lines, but no paragraph break.
    "lines": ("one\ntwo", ["\nthree\n", "four"]),
    # A blank line inside a code block begun before the hand-over, then a break after it closes.
    "fence": ("Here:\n```sh\necho a\n", ["\necho b\n```\n\nThe answer", " is 42."]),
}


# A reply the driver cuts for a message (agy 1.5.16, 24 Nov 2026): what agy wrote before the
# message came, and what it writes once the test says `go-<mode>`. At the cancel, agy answers the
# prompt `cancelled` at once; a word it wrote before the cancel landed comes first.
CUTS = {
    # A list that goes on past a line end: the cut comes there.
    "list": ("1. Pho\n2. Ra", "men\n3. Udon"),
    # A code block is open: its lines are no place to cut; the end of the line that closes it is.
    "blockend": ("Here:\n```sh\necho a\n", "echo b\n```\nDone.\nMore"),
    # A code block that never closes: the cut comes at the driver's bound.
    "blockopen": ("```\nx\n", "y\n"),
    # A Stop while the message is held.
    "holdstop": ("1. Pho\n2. Ra", None),
    # The reply already ends a line when the message comes: the cut comes at once.
    "linestart": ("1. Pho\n2. Ramen\n", None),
    # A second message comes while the first is held: it goes after it.
    "twice": ("1. Pho\n2. Ra", "men\n3. Udon"),
}

# What agy does otherwise while a message is held (`go-<mode>`): it answers the cancel only after
# the driver's bound, with what it wrote before, and then takes the message (`slow`) or turns it
# away (`slowrefused`); it never answers the cancel and answers only the message (`skip`); it stops
# writing for a thought before any line end (`missed`); or it ends its reply by itself before any
# line end (`ends`).
HELD = {"slow", "slowrefused", "skip", "missed", "ends"}


def held(mid, sid, mode):
    text(sid, "agent_message_chunk", "1. Pho\n2. Ra")
    touch("working-" + mode)
    wait_file("go-" + mode)
    if mode == "missed":
        text(sid, "agent_thought_chunk", "Thinking it over")
    elif mode == "ends":
        text(sid, "agent_message_chunk", "men")
        result(mid, {"stopReason": "end_turn", "usage": {"inputTokens": 3, "outputTokens": 1}})
    else:
        text(sid, "agent_message_chunk", "men\n3. Udon")
        while read().get("method") != "session/cancel":
            pass
        touch("cancelled-" + mode)
    second = next_prompt()
    with open(os.path.join(here, "prompt-" + mode), "w") as kept:
        kept.write(said_in(second))
    if mode == "slowrefused":
        error(second["id"], -32603, "a prompt is already running")
        text(sid, "agent_message_chunk", "tail")
        return result(mid, {"stopReason": "cancelled", "usage": {"inputTokens": 3, "outputTokens": 1}})
    if mode == "slow":
        # Still the cancelled prompt's: written before its answer.
        text(sid, "agent_message_chunk", "tail")
    if mode not in ("ends", "skip"):
        stop = "cancelled" if mode == "slow" else "end_turn"
        result(mid, {"stopReason": stop, "usage": {"inputTokens": 3, "outputTokens": 1}})
    text(sid, "agent_message_chunk", "BANANA")
    result(second["id"], {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5}})


def cut(mid, sid, mode):
    before, after = CUTS[mode]
    text(sid, "agent_message_chunk", before)
    touch("working-" + mode)
    wait_file("go-" + mode)
    if after:
        text(sid, "agent_message_chunk", after)
    while read().get("method") != "session/cancel":
        pass
    touch("cancelled-" + mode)
    text(sid, "agent_message_chunk", "late")
    result(mid, {"stopReason": "cancelled", "usage": {"inputTokens": 3, "outputTokens": 1}})
    if mode == "holdstop":
        return
    second = next_prompt()
    with open(os.path.join(here, "prompt-" + mode), "w") as kept:
        kept.write(said_in(second))
    if mode == "twice":
        # The message after it comes at once: agy takes it over, as any prompt sent while one runs.
        third = next_prompt()
        with open(os.path.join(here, "prompt-" + mode + "-2"), "w") as kept:
            kept.write(said_in(third))
        result(second["id"], {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5}})
        text(sid, "agent_message_chunk", "BANANA")
        return result(third["id"], {"stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1}})
    text(sid, "agent_message_chunk", "BANANA")
    result(second["id"], {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5}})


def steering(mid, sid, words):
    found = re.search(r"case-([a-z]+)", words)
    mode = found.group(1) if found else ""
    if mode in CUTS:
        return cut(mid, sid, mode)
    if mode in HELD:
        return held(mid, sid, mode)
    if mode == "after":
        # Answered before anything else comes: what comes later is a prompt of its own.
        text(sid, "agent_message_chunk", "first-after")
        return result(mid, {"stopReason": "end_turn"})
    if mode == "hold":
        # It works until it is stopped, and takes none of the prompts it is sent meanwhile:
        # each is answered as cancelled once the Stop came, and then the one it worked on.
        tool(sid, "in_progress")
        sent = []
        while True:
            message = read()
            if message.get("method") == "session/prompt":
                sent.append(message["id"])
            elif message.get("method") == "session/cancel":
                break
        tool(sid, "failed")
        for other in sent:
            result(other, {"stopReason": "cancelled"})
        return result(mid, {"stopReason": "cancelled"})
    if mode == "writing":
        text(sid, "agent_message_chunk", "before-steer")
    elif mode in ("sentence", "closing"):
        text(sid, "agent_message_chunk", "5 plus")
    elif mode in WRITTEN:
        text(sid, "agent_message_chunk", WRITTEN[mode][0])
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
    # As agy does within milliseconds of every prompt, before it has touched the message.
    update(sid, {"sessionUpdate": "usage_update", "used": 0, "size": 1000000})
    if mode == "background":
        # The command begun under the first prompt ends by itself, before agy works on the new one.
        tool(sid, "completed")
        touch("ended-background")
    # Its first word under the new prompt is what makes the message read: the test says when.
    wait_file("taken-" + mode)
    if mode in ("sentence", "closing"):
        # As agy did live: the sentence it was writing goes on after the hand-over.
        text(sid, "agent_message_chunk", " 4 is 9")
    if mode == "closing":
        # And the new prompt is answered with nothing more said.
        return result(second["id"], {"stopReason": "end_turn"})
    if mode in WRITTEN:
        for piece in WRITTEN[mode][1]:
            text(sid, "agent_message_chunk", piece)
        return result(second["id"], {"stopReason": "end_turn"})
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
    if mode not in ("writing", "background", "sentence"):
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
            # As agy does on its own: what it offers, before it touches the message.
            update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": []})
            if "meta" in alone.group(0):
                return error(mid, -32603, "a prompt it would not take")
            text(sid, "agent_message_chunk", "separate-" + alone.group(0))
            return result(mid, {"stopReason": "end_turn"})
        return steering(mid, sid, words)
    if scenario == "quota":
        return error(mid, -32603, "Error 429, Message: Resource has been exhausted, Status: RESOURCE_EXHAUSTED "
                     "[reason:QUOTA_EXHAUSTED quotaResetTimeStamp:2026-11-23T18:00:00Z]")
    if scenario == "signature":
        return error(mid, -32603, "messages.3.content.0: Invalid `signature` in `thinking` block")
    text(sid, "agent_thought_chunk", "Considering")
    if scenario == "detached":
        # What an agent leaves running on purpose: one in a session of its own, one `nohup`-style
        # in agy's group, both writing elsewhere than agy's pipes.
        log = open(os.path.join(here, "server.log"), "a")
        own = subprocess.Popen(["sleep", "120"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
        kept = subprocess.Popen(["sleep", "120"], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        with open(os.path.join(here, "detached.pids"), "w") as pids:
            pids.write("%d %d" % (own.pid, kept.pid))
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



def default_session_id():
    return "conv-replacement" if scenario == "resume-lost" else "conv-new"


def serve(on_prompt=prompt, new_id=default_session_id):
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
                sid = new_id()
                keep(sid)
                result(mid, {"sessionId": sid, "configOptions": OPTIONS})
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
            on_prompt(mid, params.get("sessionId"), said_in(message))
        elif mid is not None:
            error(mid, -32601, "Method not found")


if __name__ == "__main__":
    serve()
