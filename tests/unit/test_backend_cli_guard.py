"""The test session's guard against real provider CLI launches (tests/conftest.py).

Nothing here may ever reach a real model. In-process cases request the
``backend_cli_guard`` fixture (without the guard they cannot even start), use
names or paths that cannot run anything real (sentinel fakes in tmp, paths
that do not exist), and acknowledge the refusals they provoke with ``take()``.
Fail-closed behaviour is shown by inner pytest sessions in a child process
whose PATH holds only sentinel CLIs and system directories, with its own HOME:
even with no guard at all, such a child can reach nothing real. The in-process
cases run with a PATH of sentinels only and ask for ``--version`` at most, so
a regression in the guard would reach a sentinel, never a real CLI or model.
Network cases use zero-packet UDP connects to TEST-NET-1 and ``.invalid`` names.
"""
from __future__ import annotations

import os
import pathlib
import pty
import socket
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


@pytest.fixture
def sentinel_path(tmp_path, monkeypatch):
    """PATH holds only sentinels for every provider CLI (they mark that they ran)."""
    marker = tmp_path / "sentinel-path-ran"
    bin_dir = tmp_path / "sentinel-path"
    for name in NAMES:
        _sentinel(bin_dir / name, marker)
    monkeypatch.setenv("PATH", str(bin_dir))
    yield marker
    assert not marker.exists(), marker.read_text()


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
def test_3_popen_of_each_provider_cli_name(backend_cli_guard, sentinel_path, name):
    _blocked(backend_cli_guard, lambda: subprocess.Popen([name, "--version"]))


@pytest.mark.parametrize("argv", [
    ["env", "FOO=1", "claude", "--version"],
    ["timeout", "5", "codex", "--version"],
    ["setsid", "nohup", "grok", "--version"],
    ["sh", "-c", "agy --version"],
    ["node", "/somewhere/node_modules/.bin/opencode", "--version"],
    # M3: a runner's subcommand, a package spec, a wrapper option with a value.
    ["uv", "run", "claude", "--version"],
    ["npx", "-y", "@anthropic-ai/claude-code", "--version"],
    ["pnpm", "dlx", "@openai/codex@latest", "--version"],
    ["nice", "-n", "10", "claude", "--version"],
    ["ionice", "-c", "3", "nice", "-n", "19", "codex", "--version"],
    ["env", "-S", "claude --version"],
    ["sh", "-lc", "cd /tmp && exec gemini --version"],
])
def test_3_launches_through_wrappers_shells_and_runners(backend_cli_guard, sentinel_path, argv):
    _blocked(backend_cli_guard, lambda: subprocess.run(argv, capture_output=True))


def test_3_a_shell_string(backend_cli_guard, sentinel_path):
    _blocked(backend_cli_guard, lambda: subprocess.run("gemini --version", shell=True))


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


def test_3_the_stream_json_runner(backend_cli_guard, sentinel_path, tmp_path):
    from lib.backend import stream_json
    _blocked(backend_cli_guard,
             lambda: stream_json.popen_turn(["claude", "--version"], cwd=tmp_path, session="s"))


def test_3_the_codex_app_server_client(backend_cli_guard, sentinel_path):
    from lib import codex_app_server
    _blocked(backend_cli_guard, lambda: codex_app_server._Client("agent", "session"))


def test_3_os_system_and_posix_spawn(backend_cli_guard, sentinel_path):
    with pytest.raises(OSError):
        os.system("claude --version")
    with pytest.raises(OSError):
        os.posix_spawnp("codex", ["codex", "--version"], dict(os.environ))
    assert len(backend_cli_guard.take()) == 2


@pytest.mark.parametrize("fork", [os.fork, lambda: pty.fork()[0]])
def test_3_exec_in_a_forked_child_like_the_terminal(backend_cli_guard, sentinel_path, fork):
    # terminal_ws: pty.fork(), then os.execvp(argv[0], argv) in the child.
    pid = fork()
    if pid == 0:
        try:
            os.execvp("claude", ["claude", "--version"])
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
    assert subprocess.run([str(fake), "--version"]).returncode == 0
    assert ran.read_text().strip().endswith("claude --version")
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


def test_5_the_guard_is_installed_once_and_stays_installed(backend_cli_guard):
    # Installed in pytest_configure for the whole session and never undone, so a
    # timer firing between tests or after the last one meets it too. Installing
    # again wraps nothing twice.
    import backend_cli_guard_core as core
    assert core.GUARD is backend_cli_guard and core.GUARD.installed
    before = (subprocess.Popen.__init__, os.execvp, os.system, os.posix_spawnp, socket.socket.connect)
    core.install(child=False)
    assert (subprocess.Popen.__init__, os.execvp, os.system, os.posix_spawnp, socket.socket.connect) == before
    assert subprocess.Popen.__init__ is core._guarded_popen_init
    assert not getattr(core._ORIGINALS["execvp"], "__clarp_guard__", False)
    assert not getattr(core._ORIGINALS["connect"], "__clarp_guard__", False)


# ---- 6. H1: attempts outside any test's window ------------------------------------------------

OUTSIDE = """
import subprocess, threading, time
import pytest

@pytest.fixture(scope="session", autouse=True)
def _late_session_fixture():
    yield
    time.sleep(0.6)                           # the timer below fires during session teardown

def test_arms_a_timer_and_returns():
    def late():
        try:
            subprocess.run(["claude", "--version"])
        except OSError:
            pass                              # swallowed, and no test owns it any more
    timer = threading.Timer(0.2, late)
    timer.daemon = True
    timer.start()
"""


def test_6_an_attempt_outside_any_test_fails_the_session(tmp_path):
    done, marker = _inner_session(tmp_path, OUTSIDE)
    out = done.stdout + done.stderr
    assert not marker.exists(), marker.read_text()
    assert "1 passed" in out, out
    assert done.returncode == 1, out
    assert "attempted outside any test (blocked)" in out and "claude" in out, out


RETRY = """
import time
from lib import turn_dispatch

def test_a_arms_the_default_retry_timer(tmp_path):
    marker = tmp_path.parent / "retry-fired"
    turn_dispatch._default_retry_scheduler(0.3, lambda: marker.write_text("fired"))

def test_b_the_timer_never_fires_after_its_test(tmp_path):
    time.sleep(0.8)
    assert not (tmp_path.parent / "retry-fired").exists()
    assert not list(tmp_path.parent.parent.glob("*/retry-fired"))
"""


def test_6_the_dispatchers_retry_timer_never_outlives_its_test(tmp_path):
    done, _ = _inner_session(tmp_path, RETRY)
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "2 passed" in out, out


# ---- 7. H2: a child Python process is guarded, whatever PATH it builds ------------------------

CHILD = """
import os, subprocess, sys

def test_a_child_interpreter_launch_fails_its_owner(tmp_path):
    fake = tmp_path / "abs" / "claude"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\\necho ran >> " + str(tmp_path / "child-ran") + "\\n")
    fake.chmod(0o755)
    code = ("import os, subprocess\\n"
            "os.environ['PATH'] = '/usr/bin:/bin'\\n"                 # a rebuilt PATH
            "try:\\n"
            "    subprocess.run([%r, '--version'])\\n"                  # an absolute path
            "except OSError:\\n"
            "    pass\\n") % str(fake)
    subprocess.run([sys.executable, "-c", code], check=True)
    assert not (tmp_path / "child-ran").exists()
"""


def test_7_a_child_interpreter_is_guarded_and_reports_to_its_test(tmp_path):
    done, marker = _inner_session(tmp_path, CHILD)
    out = done.stdout + done.stderr
    assert not marker.exists(), marker.read_text()
    assert done.returncode == 1 and "1 passed, 1 error" in out, out
    assert "real provider CLI launch or network access attempted (blocked)" in out and "claude" in out, out


# ---- 8. H3/M1: the session's HOME, adapter dirs and toolchain mode ------------------------------

def test_8_home_adapters_and_toolchains_are_the_sessions_own():
    real_home = os.environ["CLARP_TEST_REAL_HOME"]
    root = os.environ["CLARP_TEST_GUARD_DIR"]
    assert os.path.realpath(os.environ["HOME"]) != os.path.realpath(real_home)
    assert os.environ["HOME"].startswith(root)
    assert os.path.expanduser("~") == os.environ["HOME"]
    for var in ("CLARP_TTS_ADAPTERS_DIR", "CLARP_STT_ADAPTERS_DIR"):
        assert os.environ[var].startswith(root)
        assert not any(pathlib.Path(os.environ[var]).iterdir())
    assert os.environ["CLARP_TOOLCHAIN_MODE"] == "none"
    for var in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK"):
        assert var not in os.environ
    from lib import custom_stt_adapters, custom_tts_adapters
    assert str(custom_tts_adapters.ROOT).startswith(root)
    assert str(custom_stt_adapters.ROOT).startswith(root)


@pytest.mark.parametrize("connect", [
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).connect(("192.0.2.1", 9)),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).connect_ex(("192.0.2.1", 9)),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"", ("192.0.2.1", 9)),
    lambda: socket.getaddrinfo("guard-probe.invalid", 443),
])
def test_8_non_loopback_network_is_refused_and_recorded(backend_cli_guard, connect):
    attempts = _blocked(backend_cli_guard, connect)
    assert any("socket." in entry for entry in attempts)


def test_8_loopback_still_works(backend_cli_guard):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=2)
    accepted, _ = server.accept()
    for sock in (accepted, client, server):
        sock.close()
    assert backend_cli_guard.take() == []


# ---- 9. M2: a registered fake is checked by content -------------------------------------------

def test_9_a_copy_of_an_installed_cli_is_never_a_fake(backend_cli_guard, fake_backend_cli, tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    installed = _sentinel(tmp_path / "installed" / "real-cli", marker)   # stands in for an installed CLI
    copy = tmp_path / "bin" / "claude"
    copy.parent.mkdir()
    copy.write_bytes(installed.read_bytes())
    copy.chmod(0o755)
    stat = installed.stat()
    monkeypatch.setattr(backend_cli_guard, "ambient", frozenset({os.path.realpath(installed)}))
    monkeypatch.setattr(backend_cli_guard, "_ambient_sizes", {stat.st_size: [os.path.realpath(installed)]})
    monkeypatch.setattr(backend_cli_guard, "_ambient_hashes", {})
    with pytest.raises(ValueError):
        fake_backend_cli(copy)
    fake_backend_cli.dir(tmp_path / "bin")
    _blocked(backend_cli_guard, lambda: subprocess.run([str(copy), "--version"]))
    assert not marker.exists()


def test_9_a_fake_that_names_an_installed_cli_is_refused(backend_cli_guard, fake_backend_cli, tmp_path, monkeypatch):
    real = tmp_path / "installed" / "claude-real"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(backend_cli_guard, "ambient", frozenset({os.path.realpath(real)}))
    wrapper = tmp_path / "bin" / "claude"
    wrapper.parent.mkdir()
    wrapper.write_text(f"#!/bin/sh\nexec {real} \"$@\"\n")       # would exec the real CLI by path
    wrapper.chmod(0o755)
    with pytest.raises(ValueError):
        fake_backend_cli(wrapper)
    fake_backend_cli.dir(tmp_path / "bin")
    _blocked(backend_cli_guard, lambda: subprocess.run([str(wrapper), "--version"]))


# ---- 10. N1: every resolver entry point and sendmsg ---------------------------------------------

@pytest.mark.parametrize("lookup", [
    lambda: socket.gethostbyname("guard-probe.invalid"),
    lambda: socket.gethostbyname_ex("guard-probe.invalid"),
    lambda: socket.gethostbyaddr("192.0.2.1"),
    lambda: socket.getnameinfo(("192.0.2.1", 9), 0),
    lambda: socket.getfqdn("guard-probe.invalid"),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg([b""], [], 0, ("192.0.2.1", 9)),
])
def test_10_name_lookups_and_sendmsg_are_refused_and_recorded(backend_cli_guard, lookup):
    try:
        lookup()
    except OSError:
        pass                     # getfqdn swallows the refused lookup; the record still fails it
    assert any("socket." in entry for entry in backend_cli_guard.take())


def test_10_loopback_forms_stay_local():
    import backend_cli_guard_core as core
    for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost", "api.localhost", "0.0.0.0", "",
                 socket.gethostname()):
        assert core._loopback(host), host
    for host in ("192.0.2.1", "guard-probe.invalid", "::ffff:192.0.2.1"):
        assert not core._loopback(host), host


# ---- 11. N2: only command positions count (parsed, never executed) ------------------------------

@pytest.mark.parametrize("command", [
    "git log --grep claude",
    "pytest -k codex tests/unit",
    "echo 'the claude backend' && grep -r gemini docs",
    "FOO=claude make test",
])
def test_11_a_provider_name_as_an_argument_is_not_a_launch(backend_cli_guard, command):
    assert backend_cli_guard.forbidden_shell(command, "/nonexistent") == ""


@pytest.mark.parametrize("argv", [
    ["uv", "run", "pytest", "-k", "codex"],
    ["uv", "run", "--group", "dev", "python", "-m", "pytest", "-k", "claude"],
    ["node", "scripts/build.js", "claude"],
])
def test_11_a_runner_judges_only_the_program_it_starts(backend_cli_guard, argv):
    assert backend_cli_guard.forbidden_argv(argv, "/nonexistent") == ""


@pytest.mark.parametrize("command", [
    "cd /tmp; claude --version",
    "make && FOO=1 codex --version",
    "true | gemini --version",
    "(grok --version)",
    "uv run --group dev claude --version",
    "npx -c 'opencode --version'",
    "npx -p @anthropic-ai/claude-code claude-code --version",
])
def test_11_a_provider_cli_at_a_command_position_is_a_launch(backend_cli_guard, command):
    assert backend_cli_guard.forbidden_shell(command, "/nonexistent") != ""
