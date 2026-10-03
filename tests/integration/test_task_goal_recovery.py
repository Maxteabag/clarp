"""Real isolated Host, SQLite, scheduler, dispatcher and provider subprocesses.

No paid inference: the existing QA Host refuses real provider backends.
"""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.error
import pytest
from tests.integration.test_qa_turns import QAHost

ROOT = Path(__file__).resolve().parents[2]


def helper(host, *args):
    env = {
        **os.environ,
        "CLAUDE_PWA_DB": str(host.root / "state.sqlite"),
        "CLARP_CODE_ROOT": str(ROOT / "server"),
    }
    return json.loads(
        subprocess.check_output(
            [sys.executable, str(ROOT / "scripts/agent_tasks.py"), *args],
            env=env,
            text=True,
        )
    )


def create(host):
    host.request(
        "/send",
        {
            "session": "rachel",
            "text": "Status interview: explain current progress, then end this turn",
            "client_msg_id": "interview",
            "synthesize_audio": False,
        },
    )
    host.wait_reply("rachel", "Status interview")
    return helper(
        host,
        "goal",
        "rachel",
        "outcome",
        "Deliver the unchanged outcome",
        json.dumps([{"id": "probe", "title": "Probe outcome"}]),
        json.dumps(
            {
                "outcome": "Deliver the unchanged outcome",
                "criteria": ["Observable result"],
                "limits": "No paid inference",
                "enroll": True,
            }
        ),
    )


def action(host, p, kind, data):
    return host.request(
        "/task-plan/action",
        {
            "plan_id": p["plan_id"],
            "revision": p["revision"],
            "action": kind,
            "data": data,
        },
    )["plan"]


def arm(host, p, **continuation):
    return action(
        host,
        p,
        "checkpoint",
        {
            "progress": "Interview answered; outcome is unfinished",
            "next_work": "Reassess the new evidence and choose useful work",
            "continuation": {"due_at": int(time.time() * 1000), **continuation},
        },
    )


def wait_wake(host, count=1):
    for _ in range(200):
        with sqlite3.connect(host.root / "state.sqlite") as con:
            rows = con.execute(
                "SELECT message_id,text FROM messages WHERE message_id LIKE 'u-task-goal-%'"
            ).fetchall()
        if len(rows) >= count:
            host.wait_reply("rachel", "Continue your durable outcome commitment")
            return rows
        time.sleep(0.05)
    raise AssertionError("No durable goal wake reached real dispatcher/provider")


@pytest.fixture
def host(tmp_path):
    h = QAHost(tmp_path / "goal-host")
    try:
        yield h.start()
    finally:
        h.stop()


def test_early_final_interview_then_recovery_and_host_restart(host):
    p = create(host)
    host.request(
        "/send",
        {
            "session": "rachel",
            "text": "Status question during the unfinished goal; answer and finish this turn",
            "client_msg_id": "status-during-goal",
            "synthesize_audio": False,
        },
    )
    host.wait_reply("rachel", "Status question during the unfinished goal")
    current = host.request("/task-plans?session=rachel")["plans"][0]
    assert current["goal"]["checkpoint"] is None and current["status"] == "active"
    # Advance only the persisted idle-grace deadline, avoiding a two-minute
    # wall-clock sleep. No checkpoint/rearm/status is fabricated by the owner.
    with sqlite3.connect(host.root / "state.sqlite") as con:
        con.execute(
            "UPDATE task_plans SET goal_json=json_set(goal_json,'$.continuation.due_at',?) WHERE plan_id=?",
            (int(time.time() * 1000) - 1, p["plan_id"]),
        )
    rows = wait_wake(host)
    assert len(rows) == 1 and "Reassess current conditions" in rows[0][1]
    p = host.request("/task-plans?session=rachel")["plans"][0]
    assert p["status"] == "active" and p["completed_count"] == 0
    # A new checkpoint schedules real persistent work; stop the Host before due.
    p = arm(host, p, due_at=int(time.time() * 1000) + 1500)
    host.stop()
    host.start()
    rows = wait_wake(host, 2)
    assert len({r[0] for r in rows}) == 2
    assert (
        host.request("/task-plans?session=rachel")["plans"][0]["goal"]["outcome"]
        == "Deliver the unchanged outcome"
    )


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_external_result_and_user_pause_cancel_boundaries(host, outcome):
    p = create(host)
    p = arm(
        host,
        p,
        kind="dependency",
        key="external-build",
        reason="Build worker pending",
        due_at=int(time.time() * 1000) + 60000,
    )
    external = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.exit(" + ("0" if outcome == "succeeded" else "2") + ")",
        ]
    )
    p = action(
        host,
        p,
        "dependency",
        {
            "key": "external-build",
            "outcome": outcome,
            "evidence": "Actual isolated worker exited " + str(external.returncode),
        },
    )
    wait_wake(host)
    p = host.request("/task-plans?session=rachel")["plans"][0]
    assert p["goal"]["continuation"]["dependency_result"]["outcome"] == outcome
    p = action(host, p, "pause", {"reason": "User paused this outcome"})
    with pytest.raises(urllib.error.HTTPError) as error:
        action(
            host,
            p,
            "checkpoint",
            {"progress": "Must not resume silently", "next_work": "Do work"},
        )
    assert error.value.code == 409
    p = action(host, p, "cancel", {"reason": "User cancelled this outcome"})
    assert p["status"] == "cancelled"
    with sqlite3.connect(host.root / "state.sqlite") as con:
        assert (
            con.execute(
                "SELECT count(*) FROM messages WHERE message_id LIKE 'u-task-goal-%'"
            ).fetchone()[0]
            == 1
        )


def test_lost_worker_timeout_replans_and_stale_wake_is_rejected(host):
    p = create(host)
    p = arm(
        host,
        p,
        kind="dependency",
        key="lost-worker",
        reason="Worker should report",
        due_at=int(time.time() * 1000) + 300,
    )
    rows = wait_wake(host)
    assert "deadline passed" in rows[0][1]
    old_id = rows[0][0][2:]
    p = host.request("/task-plans?session=rachel")["plans"][0]
    p = action(
        host,
        p,
        "replan",
        {
            "reason": "The dependency vanished; a different method is appropriate",
            "steps": [
                {
                    "id": "replacement",
                    "title": "Probe current state and select a new approach",
                }
            ],
        },
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        host.request(
            "/send",
            {
                "session": "rachel",
                "text": "Stale wake must not execute",
                "client_msg_id": old_id,
                "synthesize_audio": False,
            },
        )
    assert error.value.code == 409
    assert p["counts"]["removed"] == 1 and p["completed_count"] == 0


def test_queued_goal_checkpoint_then_restart_retires_old_wake_and_runs_user_work(host):
    p = create(host)
    # Drive the production scheduler against the actual Host. User work lands
    # between its idle check and dispatch, creating a genuinely admitted wake
    # in the durable queue rather than fabricating a SQLite receipt.
    worker = """
import json, os, sys, urllib.request
sys.path.insert(0, os.environ['CLARP_CODE_ROOT'])
from lib import db, task_goal_recovery
def send(payload):
    request = urllib.request.Request(sys.argv[1] + '/send',
        data=json.dumps(payload).encode(), headers={
            'Authorization':'Bearer qa-host-test', 'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)
def dispatch(session, text, request):
    send(dict(session=session, text='[qa-slow] user before checkpoint',
        client_msg_id='checkpoint-active', synthesize_audio=False))
    result = send(dict(session=session, text=text, client_msg_id=request,
        queue_if_busy=True, synthesize_audio=False))
    assert result['queued']
    return result
assert task_goal_recovery.tick(dispatch, now=db.now_ms()+130000) == 1
"""
    subprocess.run([sys.executable, "-c", worker, host.url], check=True, env={
        **os.environ, "CLAUDE_PWA_DB": str(host.root / "state.sqlite"),
        "CLARP_CODE_ROOT": str(ROOT / "server")})
    old = host.request("/turn-queue?session=rachel")["items"]
    assert len(old) == 1
    old_id = old[0]["id"]
    current = host.request("/task-plans?session=rachel")["plans"][0]
    arm(host, current, due_at=int(time.time() * 1000) + 600000)
    queued = host.request("/send", {
        "session": "rachel", "text": "User work after stale wake",
        "client_msg_id": "user-after-stale", "queue_if_busy": True,
        "synthesize_audio": False})
    assert queued["queued"]
    host.stop()
    host.start()
    host.wait_reply("rachel", "User work after stale wake")
    with sqlite3.connect(host.root / "state.sqlite") as con:
        assert con.execute(
            "SELECT status,text,sender_agent_id FROM queued_turns WHERE queue_id=?",
            (old_id,)).fetchone() == ("cancelled", "", "")
        assert con.execute(
            "SELECT count(*) FROM messages WHERE message_id=?", ("u-" + old_id,)).fetchone()[0] == 0
        assert con.execute(
            "SELECT count(*) FROM messages WHERE message_id='u-user-after-stale'").fetchone()[0] == 1
    assert host.request("/turn-queue?session=rachel")["items"] == []


def test_named_context_checkpoint_is_atomic_and_retrievable_over_http(host):
    p = create(host)
    p = action(
        host,
        p,
        "checkpoint",
        {
            "progress": "Saved flexible working context",
            "next_work": "Read current notes and reassess",
            "documents": [
                {
                    "name": "architecture.md",
                    "format": "markdown",
                    "content": "# Strategy\nUse the proven path. Do not redo the completed probe.",
                    "document_revision": 0,
                    "reason": "Probe established the boundary",
                },
                {
                    "name": "active-state",
                    "format": "json",
                    "content": {
                        "next_slice": {"hypothesis": "reuse", "avoid": ["redo-probe"]}
                    },
                    "document_revision": 0,
                    "reason": "Persist current findings",
                },
            ],
            "continuation": {"kind": "blocked", "reason": "Waiting for manual review"},
        },
    )
    document = host.request(
        "/task-plan/document?plan_id=" + p["plan_id"] + "&name=active-state"
    )["document"]
    assert document["content"]["next_slice"]["avoid"] == ["redo-probe"]
    assert len(p["goal"]["documents"]) == 2
    with pytest.raises(urllib.error.HTTPError) as error:
        action(
            host,
            p,
            "checkpoint",
            {
                "progress": "Must rollback",
                "next_work": "Never scheduled",
                "documents": [
                    {
                        "name": "active-state",
                        "format": "json",
                        "content": {},
                        "document_revision": 0,
                        "reason": "stale copy",
                    }
                ],
                "continuation": {"due_at": int(time.time() * 1000)},
            },
        )
    assert error.value.code == 409
    assert (
        host.request("/task-plans?session=rachel")["plans"][0]["goal"]["checkpoint"][
            "progress"
        ]
        == "Saved flexible working context"
    )


def test_real_provider_capacity_failure_keeps_goal_unfinished_and_backed_off(host):
    p = create(host)
    host.stop()
    (host.root / "provider" / "quota-mode").write_text("blocked")
    host.start()
    p = arm(host, p)
    for _ in range(200):
        plans = host.request("/task-plans?session=rachel")["plans"]
        p = next(row for row in plans if row["plan_id"] == p["plan_id"])
        if p["goal"]["continuation"].get("observed_state") == "capacity":
            break
        time.sleep(0.05)
    assert p["status"] == "active" and p["completed_count"] == 0
    assert p["goal"]["continuation"]["observed_state"] == "capacity"
    assert p["goal"]["continuation"]["due_at"] > int(time.time() * 1000)
    assert all(not criterion["evidence"] for criterion in p["goal"]["criteria"])
    with sqlite3.connect(host.root / "state.sqlite") as con:
        assert (
            con.execute(
                "SELECT count(*) FROM messages WHERE message_id LIKE 'u-task-goal-%'"
            ).fetchone()[0]
            == 1
        )


def test_concurrent_http_edits_commit_exactly_one_revision(host):
    from concurrent.futures import ThreadPoolExecutor

    p = create(host)

    def edit(label):
        try:
            return action(
                host,
                p,
                "document",
                {
                    "name": "strategy.md",
                    "format": "markdown",
                    "content": label,
                    "document_revision": 0,
                    "reason": "Concurrent finding " + label,
                },
            )
        except urllib.error.HTTPError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["first", "second"]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count(409) == 1
    current = host.request("/task-plans?session=rachel")["plans"][0]
    assert current["revision"] == p["revision"] + 1
    document = host.request(
        "/task-plan/document?plan_id=" + p["plan_id"] + "&name=strategy.md"
    )["document"]
    assert document["revision"] == 1 and len(document["history"]) == 1
