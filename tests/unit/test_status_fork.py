"""``clarp-status-fork``: read-only, cache-reusing forks and the live fact sheet."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import agents, artifacts, background_jobs, db

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills/clarp-status-fork/scripts"
spec = importlib.util.spec_from_file_location("status_fork_under_test", SCRIPTS / "status_fork.py")
status_fork = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status_fork)


def test_fork_keeps_the_request_prefix_and_installs_the_read_only_guard():
    turn = ["claude", "-p", "--dangerously-skip-permissions",
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--include-partial-messages", "--verbose",
            "--thinking-display", "summarized", "--plugin-dir", "/p", "--strict-mcp-config",
            "--model", "claude-opus-5-5", "--effort", "max",
            "--append-system-prompt", "You are Pebble.", "--resume", "sid-1"]

    argv = status_fork.fork_argv(turn, "status?")

    # Everything that shapes the request stays, in order, so the agent's cache is reused.
    shaping = ["--thinking-display", "summarized", "--plugin-dir", "/p", "--strict-mcp-config",
               "--model", "claude-opus-5-5", "--effort", "max",
               "--append-system-prompt", "You are Pebble.", "--resume", "sid-1"]
    assert [a for a in argv if a in shaping] == shaping
    # No deny rules or tool filters: those remove tools from the request and lose the cache.
    assert not {"--tools", "--disallowedTools", "--disallowed-tools"} & set(argv)
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "permissions" not in settings
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"
    assert entry["hooks"][0]["command"].endswith("read_only_guard.py")
    assert {"--fork-session", "--no-session-persistence"} <= set(argv)
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[-1] == "status?"


@pytest.mark.parametrize("tool, denied", [
    ("Read", False), ("Grep", False), ("Glob", False),
    ("Bash", True), ("Write", True), ("Edit", True), ("NotebookEdit", True),
    ("WebFetch", True), ("Agent", True), ("mcp__teams__send_message", True),
])
def test_guard_denies_every_tool_that_is_not_a_read(tool, denied):
    proc = subprocess.run([sys.executable, str(SCRIPTS / "read_only_guard.py")],
                          input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                                            "tool_input": {}}),
                          capture_output=True, text=True, check=True)
    if denied:
        verdict = json.loads(proc.stdout)["hookSpecificOutput"]
        assert verdict["hookEventName"] == "PreToolUse"
        assert verdict["permissionDecision"] == "deny"
    else:
        # No decision: the normal permission rules (e.g. a Read(./.env) deny) still apply.
        assert proc.stdout == ""


def test_guard_denies_when_it_cannot_read_the_call():
    proc = subprocess.run([sys.executable, str(SCRIPTS / "read_only_guard.py")],
                          input="not json", capture_output=True, text=True, check=True)
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("returncode, stdout, stderr, expected", [
    (1, json.dumps({"is_error": True, "subtype": "error_during_execution", "result": "",
                    "usage": {}}), "", "exit 1: error_during_execution"),
    (0, json.dumps({"is_error": True, "result": "Prompt is too long"}), "",
     "exit 0: Prompt is too long"),
    (2, json.dumps({"result": "looks fine"}), "", "exit 2: looks fine"),
    (1, "", "No conversation found with session ID: sid-1", "exit 1: No conversation found"),
])
def test_failed_fork_is_reported_as_an_error_for_that_agent(returncode, stdout, stderr, expected):
    proc = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    result = status_fork.fork_result(proc)
    assert result["ok"] is False
    assert result["error"].startswith(expected)

    text = status_fork.report([{"persona": "Pebble", "session": "pebble", **result}])
    assert f"ERROR: {result['error']}" in text


def test_fact_sheet_reports_work_the_agent_has_not_seen_yet(monkeypatch):
    monkeypatch.setattr(status_fork, "DB", Path(os.environ["CLAUDE_PWA_DB"]))
    owner = agents.create_agent(persona="Pebble", voice_id="v", cwd="/tmp", session="pebble")
    helper = agents.create_agent(persona="Tester", voice_id="v", cwd="/tmp", session="tester")
    with db.conn() as con:
        con.execute("UPDATE agents SET parent_agent_id=?, role='helper', helper_state='reported' "
                    "WHERE agent_id=?", (owner, helper))
    job = background_jobs.upsert(session="tester", job_id="watch-1", kind="worker",
                                 title="Startup gate watcher")
    background_jobs.finish(job["job_id"], generation=job["generation"], status="succeeded")
    card = artifacts.create_decision(session="pebble", title="Preview",
                                     question="Build the new preview? It's ready")
    decision_id, revision = db.conn().execute(
        "SELECT decision_id, revision FROM artifact_decisions WHERE artifact_id=?",
        (card["artifact_id"],)).fetchone()
    artifacts.dismiss(decision_id, expected_revision=revision)
    # Both happened two hours ago: well inside the 12-hour window, but long enough
    # that a window computed in seconds instead of milliseconds would drop them.
    two_hours_ago = db.now_ms() - 2 * 3600 * 1000
    with db.conn() as con:
        con.execute("UPDATE background_jobs SET terminal_at=?, updated_at=? WHERE job_id=?",
                    (two_hours_ago, two_hours_ago, job["job_id"]))
        con.execute("UPDATE artifact_decisions SET resolved_at=? WHERE decision_id=?",
                    (two_hours_ago, decision_id))

    facts = status_fork.live_facts({"agent_id": owner})

    assert "tester: reported" in facts
    assert "[tester] Startup gate watcher: succeeded, ended" in facts
    assert "Build the new preview? It's ready: cancelled (dismissed by user)" in facts
    # A name with a quote cannot break out of the read-only query.
    with pytest.raises(SystemExit, match="no Clarp agent matches"):
        status_fork.resolve("x' OR '1'='1")
