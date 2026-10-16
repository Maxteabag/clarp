"""The test session's guard against real provider CLI launches (tests/conftest.py).

Nothing here may ever reach a real model. In-process cases request the
``backend_cli_guard`` fixture (without the guard they cannot even start), use
names or paths that cannot run anything real (sentinel fakes in tmp, paths
that do not exist), and acknowledge the refusals they provoke with ``take()``.
Fail-closed behaviour is shown by inner pytest sessions in a child process
whose PATH holds only sentinel CLIs and system directories, with its own HOME:
even with no guard at all, such a child can reach nothing real.
"""
from __future__ import annotations

import os
import pathlib
import pty
import subprocess
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
NAMES = ("claude", "clarp", "codex", "agy", "grok", "opencode", "gemini", "tmux")


def _sentinel(path: pathlib.Path, marker: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {marker}\nexit 0\n")
    path.chmod(0o755)
    return path


def _inner_session(tmp_path, source: str) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
    """``source`` as its own pytest session, with this repo's tests/conftest.py,
    in a child whose PATH reaches only sentinels (which mark that they ran)."""
    marker = tmp_path / "sentinel-ran"
    bin_dir = tmp_path / "sentinel-bin"
    for name in NAMES:
        _sentinel(bin_dir / name, marker)
    suite = tmp_path / "inner"
    suite.mkdir()
    (suite / "conftest.py").write_text(textwrap.dedent(f"""
        import sys
        sys.path[:0] = [{str(REPO)!r}, {str(REPO / 'server')!r}]
        pytest_plugins = ["tests.conftest"]        # the session setup under test
    """))
    (suite / "test_inner.py").write_text(f"REPO = {str(REPO)!r}\n" + textwrap.dedent(source))
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home)}
    for key in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_CURRENT_TEST"):
        env.pop(key, None)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rA", "-n", "0",
         "--rootdir", str(suite), "--basetemp", str(tmp_path / "inner-tmp"), str(suite)],
        cwd=suite, env=env, capture_output=True, text=True, timeout=300)
    return done, marker


# ---- 1. decisive: the real /turn-queue/resume handler -----------------------------------

RESUME = """
def test_the_real_resume_handler_reaches_the_claude_backend(tmp_path):
    import importlib.util, pathlib
    from types import SimpleNamespace
    from lib import agents, turn_queue
    agent_id = agents.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                   session="mike", backend="claude")
    agents.start_runtime(agent_id, "mike")
    turn_queue.enqueue(queue_id="u-1", agent_id=agent_id, session="mike", text="ordinary words",
                       trace_id="t-1", client_msg_id="u-1", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    turn_queue.set_paused(agent_id, True)
    spec = importlib.util.spec_from_file_location("srv_resume", pathlib.Path(REPO) / "server/server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    answer = {}
    handler = server.Handler.__new__(server.Handler)
    handler.server = SimpleNamespace(ctx=SimpleNamespace(
        default_session="mike", stream=None, runtime_client=None, agents_path=tmp_path / "a.json"))
    handler._read_json = lambda: {"session": "mike"}
    handler._json_ok = lambda payload: answer.update(status=200, body=payload)
    handler._json_error = lambda status, message: answer.update(status=status, body=message)
    server.Handler._handle_turn_queue_resume(handler)
    assert answer["status"] == 200          # the handler swallows the failed launch
"""


def test_1_the_real_resume_handler_launch_fails_its_test_at_teardown(tmp_path):
    done, marker = _inner_session(tmp_path, RESUME)
    out = done.stdout + done.stderr
    assert not marker.exists(), marker.read_text()           # nothing was executed
    assert done.returncode == 1, out
    assert "1 passed, 1 error" in out, out                    # the call passed; teardown failed it
    assert "ERROR at teardown of test_the_real_resume_handler_reaches_the_claude_backend" in out, out
    assert "real provider CLI launch attempted (blocked)" in out and "claude" in out, out


# ---- 2. a swallowed refusal still fails the test ----------------------------------------

SWALLOWED = """
import subprocess

def test_the_app_catches_everything():
    try:
        subprocess.run(["claude", "-p", "hello"], capture_output=True, timeout=5)
    except Exception:
        pass
"""


def test_2_a_swallowed_launch_still_fails_its_test(tmp_path):
    done, marker = _inner_session(tmp_path, SWALLOWED)
    out = done.stdout + done.stderr
    assert not marker.exists(), marker.read_text()
    assert done.returncode == 1 and "1 passed, 1 error" in out, out
    assert "real provider CLI launch attempted (blocked)" in out, out


# ---- 3. every launch boundary is intercepted (in-process; acknowledged with take()) -------

def _blocked(guard, call):
    with pytest.raises(OSError):
        call()
    attempts = guard.take()
    assert attempts, "the launch was not intercepted"
    return attempts


@pytest.mark.parametrize("name", NAMES)
def test_3_popen_of_each_provider_cli_name(backend_cli_guard, name):
    _blocked(backend_cli_guard, lambda: subprocess.Popen([name, "--version"]))


@pytest.mark.parametrize("argv", [
    ["env", "FOO=1", "claude", "-p", "x"],
    ["timeout", "5", "codex", "exec", "x"],
    ["setsid", "nohup", "grok"],
    ["sh", "-c", "agy chat x"],
    ["node", "/somewhere/node_modules/.bin/opencode", "run"],
])
def test_3_launches_through_wrappers_shells_and_runners(backend_cli_guard, argv):
    _blocked(backend_cli_guard, lambda: subprocess.run(argv, capture_output=True))


def test_3_a_shell_string(backend_cli_guard):
    _blocked(backend_cli_guard, lambda: subprocess.run("gemini --prompt x", shell=True))


def test_3_the_claude_absolute_path_fallback(backend_cli_guard):
    # claude.py: shutil.which(executable) or executable, then the --version probe.
    from lib.backend import claude
    _blocked(backend_cli_guard,
             lambda: subprocess.run(["/nonexistent-guard-probe/claude", "--version"]))
    assert claude._thinking_display_support("/nonexistent-guard-probe/claude", 0.0) is False
    assert backend_cli_guard.take()                           # refused, and the app swallowed it


def test_3_anything_under_the_clarp_toolchains(backend_cli_guard):
    probe = os.path.join(backend_cli_guard.toolchains, "guard-probe-missing", "bin", "anything")
    _blocked(backend_cli_guard, lambda: subprocess.Popen([probe]))


def test_3_a_renamed_alias_of_an_installed_cli(backend_cli_guard, tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    real = _sentinel(tmp_path / "installed" / "v2.1.0", marker)   # stands in for an installed CLI
    alias = tmp_path / "bin" / "cc"
    alias.parent.mkdir()
    alias.symlink_to(real)
    monkeypatch.setattr(backend_cli_guard, "ambient", frozenset({os.path.realpath(real)}))
    _blocked(backend_cli_guard, lambda: subprocess.Popen([str(alias)]))
    assert not marker.exists()


def test_3_the_stream_json_runner(backend_cli_guard, tmp_path):
    from lib.backend import stream_json
    _blocked(backend_cli_guard,
             lambda: stream_json.popen_turn(["claude", "-p", "x"], cwd=tmp_path, session="s"))


def test_3_the_codex_app_server_client(backend_cli_guard):
    from lib import codex_app_server
    _blocked(backend_cli_guard, lambda: codex_app_server._Client("agent", "session"))


def test_3_os_system_and_posix_spawn(backend_cli_guard):
    with pytest.raises(OSError):
        os.system("claude --version")
    with pytest.raises(OSError):
        os.posix_spawnp("codex", ["codex", "--version"], dict(os.environ))
    assert len(backend_cli_guard.take()) == 2


@pytest.mark.parametrize("fork", [os.fork, lambda: pty.fork()[0]])
def test_3_exec_in_a_forked_child_like_the_terminal(backend_cli_guard, fork):
    # terminal_ws: pty.fork(), then os.execvp(argv[0], argv) in the child.
    pid = fork()
    if pid == 0:
        try:
            os.execvp("claude", ["claude"])
        finally:
            os._exit(99)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 127
    assert any("os.execvp" in entry for entry in backend_cli_guard.take())


# ---- 4. positive control: a registered disposable fake runs ---------------------------------

def test_4_a_registered_fake_runs_and_an_unregistered_one_is_blocked(backend_cli_guard, fake_backend_cli,
                                                                     tmp_path):
    ran = tmp_path / "fake-ran"
    fake = fake_backend_cli(_sentinel(tmp_path / "bin" / "claude", ran))
    assert subprocess.run([str(fake), "-p", "x"]).returncode == 0
    assert ran.read_text().strip().endswith("claude -p x")
    assert backend_cli_guard.take() == []
    other_ran = tmp_path / "other-ran"
    other = _sentinel(tmp_path / "other" / "claude", other_ran)        # same kind of path, not registered
    _blocked(backend_cli_guard, lambda: subprocess.run([str(other)]))
    assert not other_ran.exists()


def test_4_the_opt_in_refuses_anything_that_is_not_a_disposable_fake(fake_backend_cli, tmp_path):
    with pytest.raises(ValueError):
        fake_backend_cli("/usr/bin/env")                            # outside tmp_path and tests/
    with pytest.raises(ValueError):
        fake_backend_cli(tmp_path / "missing")
    plain = tmp_path / "not-executable"
    plain.write_text("x")
    with pytest.raises(ValueError):
        fake_backend_cli(plain)


# ---- 5. isolation and restoration -------------------------------------------------------------

ISOLATION = """
import subprocess

def _fake(tmp_path):
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\\nexit 0\\n")
    path.chmod(0o755)
    return path

def test_a_registered(fake_backend_cli, tmp_path):
    assert subprocess.run([str(fake_backend_cli(_fake(tmp_path)))]).returncode == 0

def test_b_the_next_test_is_guarded_again(tmp_path):
    try:
        subprocess.run([str(_fake(tmp_path))])
    except OSError:
        pass                                   # swallowed: only the teardown can fail it
"""


def test_5_an_opt_in_never_leaks_into_the_next_test(tmp_path):
    done, marker = _inner_session(tmp_path, ISOLATION)
    out = done.stdout + done.stderr
    assert "PASSED test_inner.py::test_a_registered" in out, out
    assert "ERROR test_inner.py::test_b_the_next_test_is_guarded_again" in out, out
    assert "2 passed, 1 error" in out, out


def test_5_the_patched_primitives_are_restored(backend_cli_guard):
    # Known stand-ins first (nothing is launched meanwhile), then the guard over
    # them, then its undo: exactly the stand-ins come back.
    def inert(*args, **kwargs):
        raise AssertionError("not called")
    stand_ins = {"__init__": inert, "execvp": inert, "system": inert, "posix_spawnp": inert}
    base = pytest.MonkeyPatch()
    base.setattr(subprocess.Popen, "__init__", stand_ins["__init__"])
    for name in ("execvp", "system", "posix_spawnp"):
        base.setattr(os, name, stand_ins[name])
    try:
        mp = pytest.MonkeyPatch()
        backend_cli_guard.install(mp)
        assert subprocess.Popen.__init__ is not inert and os.execvp is not inert
        assert os.system is not inert and os.posix_spawnp is not inert
        mp.undo()
        assert subprocess.Popen.__init__ is inert and os.execvp is inert
        assert os.system is inert and os.posix_spawnp is inert
    finally:
        base.undo()
