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
    run_hook("Stop", {"conversationId": CONVERSATION, "fullyIdle": True,
                      "terminationReason": "ERROR", "error": "timeout"})
    row = build_agent_snapshot(None)["agents"][0]
    assert row["busy"] is False
    assert row["latest_state"] == "interrupted"
    assert row["activity"]["status"] == "error"
    assert row["activity"]["summary"]


def test_configuration_observes_lifecycle_without_permission_hooks():
    from lib.agy_hooks import hook_configuration
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


def test_background_work_is_distinct_from_completed_turn(tmp_path, monkeypatch):
    make_agent(tmp_path, monkeypatch)
    run_hook("Stop", {"conversationId": CONVERSATION, "terminationReason": "NO_TOOL_CALL",
                      "fullyIdle": False})
    assert build_agent_snapshot(None)["agents"][0]["latest_state"] == "background"


def test_install_and_uninstall_preserve_other_hooks(tmp_path):
    from lib.agy_hooks import configure_hooks, hook_configuration
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
    from lib.agy_hooks import configure_hooks
    config = tmp_path / ".gemini/config/hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text(existing)
    assert configure_hooks(tmp_path / "share", tmp_path) is False
    assert configure_hooks(tmp_path / "share", tmp_path, remove=True) is False
    assert config.read_text() == existing


def test_install_preserves_symlinked_config(tmp_path):
    from lib.agy_hooks import configure_hooks
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
    from lib import agy_runner
    agent_id = make_agent(tmp_path, monkeypatch)
    hook = str(ROOT / "plugin/hooks/agy_state.py")
    fake = tmp_path / "agy"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, {hook!r}, 'Stop'], "
        f"input={json.dumps({'conversationId': CONVERSATION, 'terminationReason': 'NO_TOOL_CALL', 'fullyIdle': True})!r}, "
        "text=True, stdout=subprocess.DEVNULL, check=True)\n")
    fake.chmod(0o700)
    monkeypatch.setattr(agy_runner, "AGY_BIN", str(fake))
    errors = []
    handle = agy_runner.spawn_turn(
        text="fixture", cwd=tmp_path, agent_id=agent_id, session="marcus",
        isolated=isolated, on_error=errors.append)
    handle.drain_thread.join(timeout=5)
    assert not handle.drain_thread.is_alive()
    assert len(errors) == 1  # The fixture deliberately has no stream result.
    kinds = [r[0] for r in db.conn().execute(
        "SELECT kind FROM state_log WHERE agent_id=? ORDER BY state_id", (agent_id,))]
    assert kinds == (["spawned"] if isolated else ["spawned", "thinking"])


@pytest.mark.parametrize("remove", [False, True])
def test_optional_hook_write_failure_is_a_refusal(tmp_path, monkeypatch, remove):
    from lib import agy_hooks
    if remove:
        assert agy_hooks.configure_hooks(tmp_path / "share", tmp_path)
    def denied(*args):
        raise PermissionError("read-only configuration")
    monkeypatch.setattr(agy_hooks, "_write_hooks", denied)
    assert agy_hooks.configure_hooks(tmp_path / "share", tmp_path, remove=remove) is False


def test_generated_command_uses_managed_python_and_survives_old_release(tmp_path):
    from lib.agy_hooks import hook_configuration
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
