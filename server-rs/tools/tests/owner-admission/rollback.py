"""Exercise candidate admission and the predecessor's real Recorder in an attested Host."""
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
SKILL = Path.home() / "dotfiles/skills/test-clarp-host/scripts"
TOOL = SKILL / "clarp-test-host"
ROOT = "/data/toolchain/bin"


def run(*args, data=None):
    return subprocess.check_output([TOOL, *args], input=data, text=True).strip()


def api(method, path, body=None):
    args = [] if body is None else [json.dumps(body)]
    return json.loads(run("api", method, path, *args))


def until(read, predicate, label):
    deadline = time.monotonic() + 90
    while True:
        value = read()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"{label}: {value!r}")
        time.sleep(.15)


def exercise(release, scenario):
    os.environ["CLARP_TEST_FIXTURE"] = "owner-admission-" + secrets.token_hex(6)
    print(run("up", "--release", release), flush=True)
    try:
        uri = "fixture://ai-swarm/" + os.environ["CLARP_TEST_FIXTURE"]
        subprocess.run(["bash", "-c", 'source "$1" "$2"', "_",
                        SKILL / "guard_not_live.sh", uri], check=True)
        run("sh", f"mkdir -p {ROOT} && cat > {ROOT}/claude && chmod 755 {ROOT}/claude",
            data=(HERE / "claude.py").read_text())
        run("sh", "cat > /data/clarp/config/config.toml",
            data='claude_chat_lane = false\n[conversation_forks]\nagents=["hazel-74f6"]\n')
        created = api("POST", "/agents", {"name":"Fixture Hazel","session":"hazel-74f6",
                      "backend":"claude","cwd":"/data/workspace","test":True})["agent"]
        session, agent = created["session"], created["agent_id"]
        assert session == "hazel-74f6" and re.fullmatch("[a-zA-Z0-9-]+", agent)
        api("POST", "/agent-chat-view", {"session":session,"chat_view":"conversation"})
        def send(id_, text, **extra):
            return api("POST", "/send", {"session":session,"client_msg_id":id_,"text":text,**extra})
        def lookup(id_):
            return api("GET", f"/send-admission?session={session}&client_msg_id={id_}")
        def route(id_, expected):
            return until(lambda:lookup(id_), lambda r:r["admission_route"] == expected, expected)
        def reader():
            return json.loads(run("sh", "/opt/clarp/bin/read_predecessor " + shlex.quote("/data/clarp/data/agents/" + agent)))
        def touch(name):
            run("sh", f"touch {ROOT}/{name}")
        def launches():
            return [json.loads(line) for line in run("sh", f"cat {ROOT}/launches").splitlines()]
        def bound():
            return next(row for row in api("GET","/agents/snapshot")["agents"] if row["agent_id"] == agent)

        assert send("main","U: main work")["admission_route"] == "started"
        until(bound, lambda row:bool(row.get("backend_session_id")), "main bound")
        text = "U: ADMISSION FAIL" if scenario == "giveback" else "U: ADMISSION"
        ack = send("one",text)
        assert ack["admission_route"] == "conversation_fork", ack
        until(lambda:run("sh", f"test -f {ROOT}/fork-ready && echo yes || echo no"), lambda s:s=="yes","fork ready")
        if scenario == "giveback":
            other = send("two","U: second reserved")
            assert other["admission_route"] == "conversation_fork", other
            later = send("later","U: ordinary later",queue_if_busy=True)
            assert "admission_route" not in later
            before = reader()
            assert [r["id"] for r in before] == ["one","two","later"] and not any(r["reserved"] for r in before), before
            touch("release-fork")
            for key,receipt in [("one",ack),("two",other)]:
                released = route(key,"waiting")
                assert released["admission_revision"] > receipt["admission_revision"]
                print("giveback", receipt, released, flush=True)
            assert reader() == before
            items = api("GET",f"/turn-queue?session={session}")["items"]
            assert [(i["client_msg_id"],i["status"]) for i in items] == [(i["id"],"queued") for i in before],items
            print("predecessor read before ordinary consumption",before,flush=True)
        elif scenario == "post-read":
            touch("release-fork")
            read = route("one","started")
            touch("fail-after-read")
            until(lambda:run("sh", f"test -f {ROOT}/failed-after-read && echo yes || echo no"),lambda s:s=="yes","post-read exit")
            assert lookup("one") == read
            assert reader() == []
        else:
            # Reap both shim processes at actual container restart, while the
            # durable fork still owns an unread message.
            assert lookup("one")["admission_revision"] == ack["admission_revision"]
            print("restart while reserved",ack,flush=True)
            print(run("restart"),flush=True)
        touch("finish")
        final = route("one","started")
        until(bound,lambda row:row["busy"] is False,"all done")
        seen = launches()
        matches = [r["whose"] for r in seen if r["said"] == text]
        assert matches == (["fork"] if scenario == "post-read" else ["fork","main"]),seen
        assert send("one",text)["admission_revision"] == final["admission_revision"]
        assert reader() == []
        print(scenario,final,"launches",matches,flush=True)
    finally:
        print(run("down"),flush=True)


if __name__ == "__main__":
    for case in ("giveback","post-read","restart"):
        exercise(sys.argv[1], case)
