"""Peer requests end to end: real clarp-admin, isolated QA Host, QA provider.

Rachel asks Mike for something. Mike's own answer stays in his chat (the
one-way transport); his explicit reply comes back to Rachel, who must actually
run a turn on it. No paid inference: the QA Host only runs the fake provider.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.integration.test_qa_turns import QAHost
from tests.integration.test_task_goal_recovery import action, create, wait_wake

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def host(tmp_path):
    h = QAHost(tmp_path / "goal-host")
    try:
        yield h.start()
    finally:
        h.stop()


def cli(host, tmp_path, *args):
    config = tmp_path / "cli-config"
    config.mkdir(exist_ok=True)
    port = int(host.url.rsplit(":", 1)[1].split("/")[0])
    (config / "config.toml").write_text(
        f'[server]\nbind_addr = "127.0.0.1"\nport = {port}\nauth_token = "qa-host-test"\n')
    env = {**os.environ, "CLARP_CONFIG_DIR": str(config), "CLAUDE_PWA_DB": str(host.root / "state.sqlite")}
    return subprocess.run([sys.executable, str(ROOT / "bin/clarp-admin.py"), *args],
                          env=env, capture_output=True, text=True, timeout=60)


def rows(host, sql, *args):
    with sqlite3.connect(host.root / "state.sqlite") as con:
        return con.execute(sql, args).fetchall()


def agent_id(host, session):
    return rows(host, "SELECT agent_id FROM agents WHERE session=?", session)[0][0]


def wakes(host):
    return rows(host, "SELECT timestamp FROM messages WHERE message_id LIKE 'u-task-goal-%' ORDER BY timestamp")


@pytest.mark.parametrize("case", ["goal-result", "blocked-then-result", "no-goal", "duplicate",
                                  "restart", "paused", "busy", "deadline"])
def test_a_request_comes_back_to_the_requester(host, tmp_path, case):
    p = create(host) if case != "no-goal" else None
    options = ["--goal", p["plan_id"]] if p else []
    if case == "deadline":
        options += ["--deadline", "3"]
    sent = cli(host, tmp_path, "request", "--to", "mike", "--from", "rachel", *options,
               "--text", "Please add a game event log")
    assert sent.returncode == 0, sent.stderr
    request_id = json.loads(sent.stdout)["request_id"]
    # Mike works and answers in his own chat; that answer does not reach Rachel.
    host.wait_reply("mike", "Please add a game event log")
    assert rows(host, "SELECT count(*) FROM messages WHERE agent_id=? AND sender_agent_id=?",
                agent_id(host, "rachel"), agent_id(host, "mike"))[0][0] == 0
    if case == "deadline":
        woken = wait_wake(host)
        assert "deadline passed" in woken[0][1]
        return

    def reply(kind, text):
        out = cli(host, tmp_path, "reply", "--request", request_id, "--from", "mike",
                  "--kind", kind, "--text", text)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    if case == "blocked-then-result":
        assert reply("blocked", "Waiting for Peter to approve activation")["via"] == "message"
        host.wait_reply("rachel", "Waiting for Peter to approve activation")   # Rachel ran a turn on it
        cont = host.request("/task-plans?session=rachel")["plans"][0]["goal"]["continuation"]
        assert cont["state"] == "waiting"   # the blocker informs; the wait goes on
    if case == "busy":
        host.request("/send", {"session": "rachel", "text": "[qa-slow] busy elsewhere",
                               "client_msg_id": "busy", "synthesize_audio": False})
        time.sleep(0.5)
    if case == "paused":
        p = action(host, host.request("/task-plans?session=rachel")["plans"][0], "pause",
                   {"reason": "User paused this outcome"})
    if case == "restart":
        host.stop()   # the result is recorded with the Host down
    via = reply("result", "The log is live as clarpForm.log()")["via"]
    if case == "restart":
        host.start()
    if case == "no-goal":
        assert via == "message"
        host.wait_reply("rachel", "The log is live as clarpForm.log()")
        return
    assert via.startswith("goal ")
    if case == "paused":
        time.sleep(3)
        assert wakes(host) == []   # the user's pause holds the result
        action(host, host.request("/task-plans?session=rachel")["plans"][0], "resume",
               {"reason": "User explicitly resumed this goal"})
    woken = wait_wake(host)   # waits for Rachel's actual turn on it
    plan = host.request("/task-plans?session=rachel")["plans"][0]
    result = plan["goal"]["continuation"]["dependency_result"]
    assert result["key"] == f"peer:{request_id}" and "clarpForm.log()" in result["evidence"]
    assert "clarpForm.log()" in woken[0][1]
    if case == "busy":
        busy_end = rows(host, "SELECT max(timestamp) FROM messages WHERE role='assistant' "
                              "AND text LIKE '%busy elsewhere%'")[0][0]
        assert busy_end and wakes(host)[0][0] > busy_end
    if case == "duplicate":
        assert "already recorded" in reply("result", "The log is live as clarpForm.log()")["via"]
        time.sleep(2)
        assert len(wakes(host)) == 1


def test_replies_that_cannot_belong_are_refused(host, tmp_path):
    sent = cli(host, tmp_path, "request", "--to", "mike", "--from", "rachel", "--text", "Please review")
    request_id = json.loads(sent.stdout)["request_id"]
    wrong = cli(host, tmp_path, "reply", "--request", request_id, "--from", "rachel",
                "--kind", "result", "--text", "I answer my own request")
    stale = cli(host, tmp_path, "reply", "--request", "r000000000000", "--from", "mike",
                "--kind", "result", "--text", "Nobody asked")
    assert wrong.returncode == stale.returncode == 4
    with sqlite3.connect(host.root / "state.sqlite") as con:
        con.execute("UPDATE agents SET archived_at=? WHERE session='rachel'", (int(time.time() * 1000),))
    gone = cli(host, tmp_path, "reply", "--request", request_id, "--from", "mike",
               "--kind", "result", "--text", "Done")
    assert gone.returncode == 4 and "archived" in gone.stderr
    assert rows(host, "SELECT count(*) FROM messages WHERE agent_id=? AND sender_agent_id=?",
                agent_id(host, "rachel"), agent_id(host, "mike"))[0][0] == 0
