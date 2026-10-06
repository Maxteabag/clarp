"""External AGY lifecycle hooks must survive snapshot reconciliation."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from lib import agents, db
from lib.snapshot import build_agent_snapshot
from lib.state_watcher import StateLogWatcher

ROOT = Path(__file__).resolve().parents[2]
CONVERSATION = "11111111-2222-4333-8444-555555555555"


def run_hook(event, payload):
    env = {**os.environ, "CLAUDE_PWA_DB": str(db.DB_PATH)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "plugin/hooks/agy_state.py"), event],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}  # Observation never grants permissions.


def make_agent(tmp_path, monkeypatch, backend="agy"):
    monkeypatch.setenv("CLAUDE_PWA_AGY_HOME", str(tmp_path / "agy"))
    # A test run inside a Clarp turn inherits that turn's token.
    monkeypatch.delenv("CLARP_PROVIDER_TURN", raising=False)
    monkeypatch.delenv("CLARP_AGY_MANAGED_TURN", raising=False)
    agent_id = agents.create_agent(
        persona="Marcus", voice_id="", cwd=str(tmp_path),
        session="marcus", backend=backend)
    agents.start_runtime(agent_id, "marcus")
    agents.bind_backend_session(agent_id, CONVERSATION)
    return agent_id


def test_terminal_turn_reports_working_to_snapshot_and_sse(tmp_path, monkeypatch):
    agent_id = make_agent(tmp_path, monkeypatch)
    presence = tmp_path / "agy/presence" / f"{CONVERSATION}.lock"
    presence.parent.mkdir(parents=True)
    with presence.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_hook("PreInvocation", {"conversationId": CONVERSATION})
        row = build_agent_snapshot(None)["agents"][0]
        assert row["busy"] is True
        assert row["latest_state"] == "thinking"
        assert row["activity"]["summary"] == "Thinking"
        assert row["turn_started_at"] > 0
        events = []
        stream = type("Stream", (), {"broadcast": lambda self, e: events.append(e)})()
        StateLogWatcher(stream)._poll_once()
        assert any(e.get("kind") == "thinking" and e["type"] == "agent-state"
                   for e in events)
        run_hook("Stop", {"conversationId": CONVERSATION,
                          "terminationReason": "NO_TOOL_CALL", "fullyIdle": True})
        row = build_agent_snapshot(None)["agents"][0]
        assert row["busy"] is False  # An open terminal is not ongoing work.
        assert row["latest_state"] == "done"
    assert agents.latest_state(agent_id)["kind"] == "done"


def test_terminal_crash_does_not_leave_working_forever(tmp_path, monkeypatch):
    make_agent(tmp_path, monkeypatch)
    run_hook("PreInvocation", {"conversationId": CONVERSATION})
    row = build_agent_snapshot(None)["agents"][0]
    assert row["busy"] is False
    assert row["latest_state"] == "idle"


@pytest.mark.parametrize("payload", [None, [], {}, {"conversationId": "unbound"}])
def test_unknown_or_malformed_sessions_are_noops(tmp_path, monkeypatch, payload):
    agent_id = make_agent(tmp_path, monkeypatch)
    run_hook("PreInvocation", payload)
    assert agents.latest_state(agent_id)["kind"] == "spawned"


def test_other_backends_and_managed_turns_are_ignored(tmp_path, monkeypatch):
    agent_id = make_agent(tmp_path, monkeypatch, backend="codex")
    run_hook("PreInvocation", {"conversationId": CONVERSATION})
    assert agents.latest_state(agent_id)["kind"] == "spawned"
    db.conn().execute("UPDATE agents SET backend='agy' WHERE agent_id=?", (agent_id,))
    monkeypatch.setenv("CLARP_AGY_MANAGED_TURN", "1")
    run_hook("Stop", {"conversationId": CONVERSATION, "fullyIdle": True})
    assert agents.latest_state(agent_id)["kind"] == "spawned"


def test_timeout_emits_visible_interruption(tmp_path, monkeypatch):
    make_agent(tmp_path, monkeypatch)
    run_hook("PreInvocation", {"conversationId": CONVERSATION})
    run_hook("Stop", {"conversationId": CONVERSATION, "fullyIdle": True,
                      "terminationReason": "ERROR", "error": "timeout"})
    row = build_agent_snapshot(None)["agents"][0]
    assert row["busy"] is False
    assert row["latest_state"] == "interrupted"
    assert row["activity"]["status"] == "error"
    assert row["activity"]["summary"]


def test_configuration_observes_lifecycle_without_permission_hooks():
    from lib.backend.agy_hooks import hook_configuration
    hooks = hook_configuration(Path("/opt/clarp"))
    assert set(hooks) == {"PreInvocation", "Stop"}


def test_old_presence_file_and_rebound_session_do_not_keep_busy(tmp_path, monkeypatch):
    agent_id = make_agent(tmp_path, monkeypatch)
    presence = tmp_path / "agy/presence" / f"{CONVERSATION}.lock"
    presence.parent.mkdir(parents=True)
    presence.touch()
    run_hook("PreInvocation", {"conversationId": CONVERSATION})
    assert build_agent_snapshot(None)["agents"][0]["busy"] is False
    with presence.open("r") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_hook("PreInvocation", {"conversationId": CONVERSATION})
        agents.bind_backend_session(agent_id, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        assert build_agent_snapshot(None)["agents"][0]["busy"] is False


def test_a_turn_leaving_background_work_is_done(tmp_path, monkeypatch):
    # No hook reports when agy's background work ends (it ends with the CLI),
    # so "background" would never clear.
    make_agent(tmp_path, monkeypatch)
    run_hook("PreInvocation", {"conversationId": CONVERSATION})
    run_hook("Stop", {"conversationId": CONVERSATION, "terminationReason": "NO_TOOL_CALL",
                      "fullyIdle": False})
    assert build_agent_snapshot(None)["agents"][0]["latest_state"] == "done"


@pytest.mark.parametrize("reason", ["TERMINAL_STEP_TYPE", "MAX_INVOCATIONS", "HALTED_STEP"])
def test_ordinary_endings_are_done_not_interrupted(tmp_path, monkeypatch, reason):
    make_agent(tmp_path, monkeypatch)
    run_hook("PreInvocation", {"conversationId": CONVERSATION})
    run_hook("Stop", {"conversationId": CONVERSATION, "terminationReason": reason,
                      "error": "", "fullyIdle": True})
    assert build_agent_snapshot(None)["agents"][0]["latest_state"] == "done"

def test_install_and_uninstall_preserve_other_hooks(tmp_path):
    from lib.backend.agy_hooks import configure_hooks, hook_configuration
    home = tmp_path / "home"
    share = tmp_path / "custom share"
    config = home / ".gemini/config/hooks.json"
    config.parent.mkdir(parents=True)
    custom = {"lint": {"Stop": [{"command": "echo custom"}]}}
    config.write_text(json.dumps(custom))
    assert configure_hooks(share, home)
    assert json.loads(config.read_text()) == {
        **custom, "clarp-status": hook_configuration(share)}
    before = config.stat().st_mtime_ns
    assert configure_hooks(share, home)
    assert config.stat().st_mtime_ns == before
    assert configure_hooks(share, home, remove=True)
    assert json.loads(config.read_text()) == custom


@pytest.mark.parametrize("existing", ["not json", "[]", '{"clarp-status": {"enabled": false}}'])
def test_install_preserves_unmanaged_or_invalid_config(tmp_path, existing):
    from lib.backend.agy_hooks import configure_hooks
    config = tmp_path / ".gemini/config/hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text(existing)
    assert configure_hooks(tmp_path / "share", tmp_path) is False
    assert configure_hooks(tmp_path / "share", tmp_path, remove=True) is False
    assert config.read_text() == existing


def test_install_preserves_symlinked_config(tmp_path):
    from lib.backend.agy_hooks import configure_hooks
    target = tmp_path / "user-hooks.json"
    target.write_text("{}")
    config = tmp_path / ".gemini/config/hooks.json"
    config.parent.mkdir(parents=True)
    config.symlink_to(target)
    assert configure_hooks(tmp_path / "share", tmp_path) is False
    assert config.is_symlink()
    assert target.read_text() == "{}"


@pytest.mark.parametrize("isolated", [False, True])
def test_managed_runner_prevents_hooks_bypassing_stream_ownership(tmp_path, monkeypatch, isolated):
    from lib import backends
    agent_id = make_agent(tmp_path, monkeypatch)
    agents.open_turn(agent_id=agent_id, source="pwa", trace_id="t-managed")
    hook = str(ROOT / "plugin/hooks/agy_state.py")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "agy"
    # The hook agy runs on Stop, inside a turn Clarp started.
    fake.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, {hook!r}, 'Stop'], "
        f"input={json.dumps({'conversationId': CONVERSATION, 'terminationReason': 'NO_TOOL_CALL', 'fullyIdle': True})!r}, "
        "text=True, stdout=subprocess.DEVNULL, check=True)\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLARP_AGY_BIN", "agy")
    errors = []
    handle = backends.by_id(backends.AGY).start_turn(
        text="fixture", cwd=tmp_path, backend_session_id=CONVERSATION,
        agent_id=agent_id, session="marcus", trace_id="t-managed",
        isolated=isolated, on_session_init=None, on_result=lambda _e: None,
        on_error=errors.append)
    handle.wait(timeout=8.0)
    kinds = [r[0] for r in db.conn().execute(
        "SELECT kind FROM state_log WHERE agent_id=? ORDER BY state_id", (agent_id,))]
    # Stream-json owns a managed turn's state; the hook records nothing.
    assert "done" not in kinds


@pytest.mark.parametrize("remove", [False, True])
def test_optional_hook_write_failure_is_a_refusal(tmp_path, monkeypatch, remove):
    from lib.backend import agy_hooks
    if remove:
        assert agy_hooks.configure_hooks(tmp_path / "share", tmp_path)
    def denied(*args):
        raise PermissionError("read-only configuration")
    monkeypatch.setattr(agy_hooks, "_write_hooks", denied)
    assert agy_hooks.configure_hooks(tmp_path / "share", tmp_path, remove=remove) is False


def test_generated_command_uses_managed_python_and_survives_old_release(tmp_path):
    from lib.backend.agy_hooks import hook_configuration
    import shutil
    share = tmp_path / "custom share"
    release = share / "current"
    script = release / "plugin/hooks/agy_state.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys,json\nprint(json.dumps({'python':sys.executable}))\n")
    (release / "SERVICE_PYTHON").write_text(sys.executable + "\n")
    only_shell = tmp_path / "bin"
    only_shell.mkdir()
    for name in ("env", "sh"):
        (only_shell / name).symlink_to(shutil.which(name))
    command = hook_configuration(share)["PreInvocation"][0]["command"]
    env = {**os.environ, "PATH": str(only_shell)}  # No system python3.
    result = subprocess.run(command, shell=True, env=env, text=True,
                            capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"python": sys.executable}
    (release / "SERVICE_PYTHON").unlink()
    result = subprocess.run(command, shell=True, env=env, text=True,
                            capture_output=True, timeout=5)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {}


def test_agy_clarp_started_is_never_reported_by_the_hook(tmp_path, monkeypatch):
    from lib import provider_background_jobs as jobs
    agent_id = make_agent(tmp_path, monkeypatch)
    other = agents.create_agent(persona="Other", voice_id="", cwd=str(tmp_path),
                                session="other", backend="claude")

    def states():
        return db.conn().execute("SELECT COUNT(*) FROM state_log WHERE agent_id=?",
                                 (agent_id,)).fetchone()[0]

    def reported(env):
        before = states()
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        run_hook("PreInvocation", {"conversationId": CONVERSATION})
        for name in env:
            monkeypatch.delenv(name)
        return states() > before
    own, theirs = jobs.new_turn_token(), jobs.new_turn_token()
    jobs.record_identity(own, agent_id=agent_id, provider="agy", pid=os.getpid())
    jobs.record_identity(theirs, agent_id=other, provider="claude", pid=os.getpid())
    # Clarp's turn for this agent, also one whose token is not recorded yet.
    assert not reported({"CLARP_PROVIDER_TURN": own})
    assert not reported({"CLARP_PROVIDER_TURN": jobs.new_turn_token()})
    # agy run from another agent's turn, and the desktop's "open in terminal"
    # (the agent's session, no token), are terminal turns.
    assert reported({"CLARP_PROVIDER_TURN": theirs})
    assert reported({"CLAUDE_PWA_SESSION": "marcus"})


def test_a_terminal_turn_that_lost_its_stop_is_repaired_eventually(tmp_path, monkeypatch):
    from lib.backend import agy_hooks
    agent_id = make_agent(tmp_path, monkeypatch)
    presence = tmp_path / "agy/presence"
    presence.mkdir(parents=True)
    lock = (presence / f"{CONVERSATION}.lock").open("wb")
    fcntl.flock(lock, fcntl.LOCK_EX)     # the agy CLI is still open
    run_hook("PreInvocation", {"conversationId": CONVERSATION})
    assert agy_hooks.has_live_work(agent_id)
    db.conn().execute("UPDATE state_log SET ts = ts - ? WHERE agent_id = ?",
                      (agy_hooks.LIVE_STATE_MAX_AGE_MS + 1000, agent_id))
    assert not agy_hooks.has_live_work(agent_id)
    lock.close()


def test_a_host_restart_does_not_mark_a_terminal_turn_interrupted(tmp_path, monkeypatch):
    from lib import interrupted_turns
    agent_id = make_agent(tmp_path, monkeypatch)
    run_hook("PreInvocation", {"conversationId": CONVERSATION})
    assert interrupted_turns.orphaned_turn(agents.get_by_agent_id(agent_id)) is None


def test_agy_runs_outside_clarp_never_load_clarp(tmp_path, monkeypatch):
    # Every model call of every agy on the machine runs the hook; one whose
    # conversation no Clarp agent owns stops at a read-only lookup.
    make_agent(tmp_path, monkeypatch)
    env = {**os.environ, "CLAUDE_PWA_DB": str(db.DB_PATH)}
    probe = (f"import runpy, sys, io; sys.path.insert(0, {str(ROOT / 'plugin/hooks')!r});"
             "sys.argv=['agy_state.py','PreInvocation'];"
             "sys.stdin=io.StringIO('{\"conversationId\": \"someone-elses\"}');"
             f"runpy.run_path({str(ROOT / 'plugin/hooks/agy_state.py')!r}, run_name='__main__');"
             "print(any(m == 'lib' or m.startswith('lib.') for m in sys.modules), file=sys.stderr)")
    result = subprocess.run([sys.executable, "-c", probe], env=env, text=True,
                            capture_output=True, timeout=10)
    assert json.loads(result.stdout) == {}
    assert result.stderr.strip().endswith("False")
