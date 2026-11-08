"""``clarp-status-fork``: read-only, cache-reusing forks and the live fact sheet."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from lib import agents, artifacts, background_jobs, db

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "status_fork_under_test", ROOT / "skills/clarp-status-fork/scripts/status_fork.py")
status_fork = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status_fork)


def test_fork_keeps_the_request_prefix_but_cannot_act_or_persist():
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
    assert "--tools" not in argv
    # It cannot act and leaves no conversation behind.
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert {"--fork-session", "--no-session-persistence"} <= set(argv)
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[-1] == "status?"


def test_fact_sheet_reports_work_the_agent_has_not_seen_yet():
    owner = agents.create_agent(persona="Pebble", voice_id="v", cwd="/tmp", session="pebble")
    helper = agents.create_agent(persona="Tester", voice_id="v", cwd="/tmp", session="tester")
    with db.conn() as con:
        con.execute("UPDATE agents SET parent_agent_id=?, role='helper', helper_state='reported' "
                    "WHERE agent_id=?", (owner, helper))
    job = background_jobs.upsert(session="tester", job_id="watch-1", kind="worker",
                                 title="Startup gate watcher")
    background_jobs.finish(job["job_id"], generation=job["generation"], status="succeeded")
    card = artifacts.create_decision(session="pebble", title="Preview",
                                     question="Build the new preview?")
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

    facts = status_fork.live_facts(db.conn(), {"agent_id": owner})

    assert "tester: reported" in facts
    assert "[tester] Startup gate watcher: succeeded, ended" in facts
    assert "Build the new preview?: cancelled (dismissed by user)" in facts
