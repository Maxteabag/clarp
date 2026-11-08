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

    argv = status_fork.fork_argv(turn, "status?", (2, 1, 285))

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
    assert entry["hooks"][0]["command"].endswith("read_only_guard.py || exit 2")
    assert settings["disableAllHooks"] is False
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
    (0, "[]", "", "exit 0: unexpected output"),
    (0, json.dumps({"result": None, "usage": []}), "", "exit 0: "),
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


@pytest.mark.parametrize("version, fail_closed", [((2, 1, 285), False), ((2, 1, 295), True)])
def test_guard_fails_closed_with_every_mechanism_the_version_supports(version, fail_closed):
    (entry,) = json.loads(status_fork.guard_settings(version))["hooks"]["PreToolUse"]
    hook = entry["hooks"][0]
    # Exit 2 blocks a PreToolUse call on every version, so a guard that cannot start blocks.
    assert hook["command"].endswith(" || exit 2")
    assert ("onFailure" in hook) is fail_closed
    if fail_closed:
        assert hook["onFailure"] == "block"


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLARP_STATUS_FORK_MANAGED_DIR", str(tmp_path / "managed"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "user"))
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    return tmp_path, project


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("version, managed, project, refusal", [
    ((2, 1, 285), None, {"permissions": {"allow": ["Read(*)", "Grep"]}}, None),
    ((2, 1, 285), None, {"permissions": {"allow": ["Bash(touch *)"]}}, "pre-approve"),
    ((2, 1, 295), None, {"permissions": {"allow": ["Bash(touch *)"]}}, None),
    ((2, 1, 295), {"disableAllHooks": True}, None, "managed policy"),
    ((2, 1, 295), {"allowManagedHooksOnly": True}, None, "managed policy"),
    ((2, 1, 284), None, None, "older than"),
])
def test_preflight_refuses_when_the_guard_cannot_be_guaranteed(
        isolated_settings, monkeypatch, version, managed, project, refusal):
    root, project_dir = isolated_settings
    monkeypatch.setattr(status_fork, "claude_version", lambda binary: version)
    if managed:
        _write(root / "managed/managed-settings.json", managed)
    if project:
        _write(project_dir / ".claude/settings.json", project)
    if refusal is None:
        assert status_fork.preflight("claude", project_dir) == version
    else:
        with pytest.raises(status_fork.Refused, match=refusal):
            status_fork.preflight("claude", project_dir)


def test_preflight_refuses_without_the_guard(isolated_settings, monkeypatch):
    _, project_dir = isolated_settings
    monkeypatch.setattr(status_fork, "claude_version", lambda binary: (2, 1, 295))
    monkeypatch.setattr(status_fork, "GUARD", project_dir / "missing_guard.py")
    with pytest.raises(status_fork.Refused, match="missing or unreadable"):
        status_fork.preflight("claude", project_dir)


def test_refused_fork_launches_nothing_and_reports_why(monkeypatch):
    launched = []
    monkeypatch.setattr(status_fork, "clarp_fork_argv", lambda agent, question: (_ for _ in ()).throw(
        status_fork.Refused("managed policy disables hooks")))
    monkeypatch.setattr(status_fork.subprocess, "run", lambda *a, **k: launched.append(a))
    result = status_fork.fork_status({"backend": "claude", "cwd": "/tmp"}, "status?")
    assert launched == []
    assert result["ok"] is False
    assert result["error"] == "refused, nothing launched: managed policy disables hooks"


def _fake_backend(monkeypatch):
    class Backend:
        def build_cmd(self, native_id, **kwargs):
            return ["claude", "-p", "--dangerously-skip-permissions", "--resume", native_id]
    monkeypatch.setattr(status_fork, "_claude_backend", Backend)
    monkeypatch.setattr(status_fork, "claude_version", lambda binary: (2, 1, 285))


def _agent(cwd):
    return {"backend": "claude", "cwd": cwd, "native_id": "sid-1", "model": "",
            "effort": "", "persona": "Pebble", "session": "pebble"}


@pytest.mark.parametrize("saved, reason", [
    ("{tmp}/deleted-worktree", "does not exist"),
    ("{tmp}/a-file", "is not a directory"),
    ("relative/worktree", "not an absolute path"),
    ("", "not an absolute path"),
])
def test_unusable_saved_cwd_launches_nothing_even_from_a_permissive_caller(
        isolated_settings, monkeypatch, saved, reason):
    root, _ = isolated_settings
    _fake_backend(monkeypatch)
    caller = root / "caller-project"
    _write(caller / ".claude/settings.json", {"permissions": {"allow": ["Bash(*)", "Write(*)"]}})
    (root / "a-file").write_text("x")
    monkeypatch.chdir(caller)
    launched = []
    monkeypatch.setattr(status_fork.subprocess, "run", lambda *a, **k: launched.append(k))

    result = status_fork.fork_status(_agent(saved.format(tmp=root)), "status?")

    assert launched == []
    assert result["ok"] is False
    assert result["error"].startswith("refused, nothing launched")
    assert reason in result["error"]


def test_launch_runs_in_exactly_the_resolved_directory_it_checked(isolated_settings, monkeypatch):
    root, project = isolated_settings
    _fake_backend(monkeypatch)
    (root / "alias").symlink_to(project, target_is_directory=True)
    monkeypatch.chdir(root)
    real_run, launches = subprocess.run, []

    def run(argv, **kwargs):
        if argv[0] != "claude":  # the guard self-test runs for real
            return real_run(argv, **kwargs)
        launches.append(kwargs["cwd"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "ok"}), stderr="")
    monkeypatch.setattr(status_fork.subprocess, "run", run)

    result = status_fork.fork_status(_agent(str(root / "alias")), "status?")

    assert result["ok"] is True
    assert launches == [project.resolve()]


@pytest.mark.parametrize("allow_on", ["real-side", "alias-side"])
def test_settings_on_either_side_of_a_symlinked_cwd_are_checked(
        isolated_settings, monkeypatch, allow_on):
    root, _ = isolated_settings
    monkeypatch.setattr(status_fork, "claude_version", lambda binary: (2, 1, 285))
    real = root / "real-parent" / "worktree"
    real.mkdir(parents=True)
    link_parent = root / "link-parent"
    link_parent.mkdir()
    (link_parent / "worktree").symlink_to(real, target_is_directory=True)
    holder = root / "real-parent" if allow_on == "real-side" else link_parent
    _write(holder / ".claude/settings.json", {"permissions": {"allow": ["Bash(touch *)"]}})

    saved, resolved = status_fork.launch_dir(str(link_parent / "worktree"))
    assert resolved == real.resolve()
    with pytest.raises(status_fork.Refused, match="pre-approve"):
        status_fork.preflight("claude", resolved, alias=saved)
