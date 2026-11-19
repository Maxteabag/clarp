"""A turn the Rust Host runs is that Host's: our hooks stay out of it (R111).

Where Clarp's plugin is enabled for every Claude process on a machine, the Python hooks fire inside
a turn the Rust Host runs. Its launcher sets CLARP_RUST_MANAGED_TURN=1 for the Claude children it
manages; with exactly '1' every hook exits 0 in its first import of ours (plugin/hooks/_clarp_lib.py),
before lib, resolution, a log line, a marker or any output. And the Python Host's own launchers
never hand that marker, or any other identity the Host inherited, to a child they start
(hook_runtime.INHERITED_IDENTITY_ENV).

Each hook runs here as the process Claude Code starts: python3 <plugin>/hooks/<hook>.py, the payload
on stdin, the test's private database and cache (conftest's _isolated_db) in its environment."""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time

import pytest

from lib import agents as agents_db
from lib import db, hook_runtime, turn_lifecycle
from lib.protocol import AgentState
from lib.turn_lifecycle import TurnEvent

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOKS = ROOT / "plugin" / "hooks"
MARKER = "CLARP_RUST_MANAGED_TURN"
SEVEN = ("pwa_source_flag", "tool_activity", "tool_finished", "stop_state", "pre_compact",
         "notification_state", "subagent_stop")


def test_the_seven_are_every_hook_the_plugin_registers():
    registered = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    commands = [hook["command"] for groups in registered.values() for group in groups
                for hook in group["hooks"]]
    assert sorted(command.rsplit("/", 1)[1].rstrip('"').removesuffix(".py") for command in commands) \
        == sorted(SEVEN)


def _agent(case, tmp_path):
    """The agent the hook's identity points at, as the case has it; None for a missing one."""
    if case == "a missing agent":
        return None
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    agents_db.start_runtime(agent_id, "mike")
    if case != "an agent not yet bound":
        agents_db.bind_backend_session(agent_id, "live-uuid")
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-1")
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED,
                              {"source": "pwa", "dispatch": "claude", "trace_id": "turn-1"})
    agents_db.set_trace(agent_id, "turn-1")
    if case == "an archived agent":
        agents_db.set_archived(agent_id, True)
    return agent_id


def _database():
    """Every table's every row: nothing a hook could write is left out."""
    con = db.conn()
    tables = [row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {table: con.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}


def _files(*roots):
    return sorted(str(path) for root in roots if root.exists() for path in root.rglob("*"))


def _stores():
    return [pathlib.Path(os.environ[name]) for name in ("CLARP_CACHE_DIR", "CLARP_SHARE_DIR",
                                                      "CLARP_CONFIG_DIR", "CLAUDE_PWA_LOG_DIR")]


def _hook(name, tmp_path, *, marker, session_id="live-uuid", flags=()):
    env = {**os.environ, "HOME": str(tmp_path / "home"), "CLAUDE_PWA_SESSION": "mike"}
    if marker is None:
        env.pop(MARKER, None)
    else:
        env[MARKER] = marker
    payload = {"session_id": session_id, "hook_event_name": name, "prompt": "hi",
               "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "toolu_1",
               "tool_response": {"stdout": "", "stderr": ""}, "message": "waiting",
               "transcript_path": str(tmp_path / "no-transcript.jsonl"), "cwd": str(tmp_path)}
    return subprocess.run([sys.executable, *flags, str(HOOKS / f"{name}.py")],
                          input=json.dumps(payload).encode(), env=env, cwd=str(tmp_path),
                          capture_output=True, timeout=60)


@pytest.mark.parametrize("case", ["a matching identity", "an archived agent", "an agent not yet bound",
                                  "a missing agent"])
@pytest.mark.parametrize("hook", SEVEN)
def test_with_the_marker_every_hook_exits_0_and_nothing_of_ours_runs(tmp_path, hook, case):
    agent_id = _agent(case, tmp_path)
    (tmp_path / "home").mkdir()
    before = (_database(), _files(*_stores(), tmp_path / "home"))
    done = _hook(hook, tmp_path, marker="1")
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    assert (_database(), _files(*_stores(), tmp_path / "home")) == before   # no row, state, bind, log
    if agent_id:
        assert agents_db.live_backend_session(agent_id) == ("" if case == "an agent not yet bound"
                                                            else "live-uuid")


@pytest.mark.parametrize("hook", SEVEN)
def test_with_the_marker_no_module_of_ours_but_the_bootstrap_is_imported(tmp_path, hook):
    """-X importtime lists every module the process imported, on stderr."""
    done = _hook(hook, tmp_path, marker="1", flags=("-X", "importtime"))
    assert done.returncode == 0 and done.stdout == b""
    imported = [line.rsplit("|", 1)[1].strip() for line in done.stderr.decode().splitlines()
                if line.startswith("import time:") and "|" in line]
    ours = [name for name in imported if name.split(".")[0] in ("lib", "_hook_report", *SEVEN)
            or name == "_clarp_lib"]
    assert ours == ["_clarp_lib"]


@pytest.mark.parametrize("value", [None, "0", "", "true"], ids=["unset", "0", "empty", "true"])
def test_only_the_exact_value_1_silences_the_hooks(tmp_path, value):
    agent_id = _agent("a matching identity", tmp_path)
    done = _hook("tool_activity", tmp_path, marker=value)
    assert done.returncode == 0
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.TOOL       # the hook acted


@pytest.mark.parametrize("value", [None, "0", "true"], ids=["unset", "0", "true"])
def test_the_prompt_hook_still_binds_a_new_agent_without_the_exact_marker(tmp_path, value):
    agent_id = _agent("an agent not yet bound", tmp_path)
    done = _hook("pwa_source_flag", tmp_path, marker=value, session_id="first-uuid")
    assert done.returncode == 0
    assert agents_db.live_backend_session(agent_id) == "first-uuid"


# ---- B: the Python Host's launchers clear every inherited identity ------------------------------

# The names, written out: the list test below pins hook_runtime's list against them, and the
# other tests do not need that list to exist (so they run, and fail one by one, on main).
NAMES = (MARKER, "CLARP_PROVIDER_TURN", "CLARP_AGY_MANAGED_TURN", "CLAUDE_PWA_SESSION", "CLARP_SESSION",
         "CLAUDE_CODE_SESSION_ID", "CLARP_BACKGROUND_ORIGIN", "CLARP_BACKGROUND_WORKER_PID")
INHERITED = {name: f"outer-{name.lower()}" for name in NAMES}
INHERITED[MARKER] = "1"


def _child_env(argv_env):
    return dict(line.split("=", 1) for line in argv_env.splitlines() if "=" in line)


def test_the_list_holds_every_identity_name_the_rust_host_and_ours_use():
    assert set(hook_runtime.INHERITED_IDENTITY_ENV) == set(NAMES)


def test_the_dispatchers_child_has_no_inherited_identity_even_when_the_host_has_it(tmp_path, monkeypatch):
    from lib.backend.stream_json import popen_turn
    for name, value in INHERITED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CLARP_UNRELATED_SETTING", "kept")
    proc = popen_turn([sys.executable, "-c", "import os; print('\\n'.join(f'{k}={v}' for k, v in os.environ.items()))"],
                      cwd=tmp_path, session="mike", env_extra={"CLARP_PROVIDER_TURN": "its-own-token"})
    out, _ = proc.communicate(timeout=30)
    env = _child_env(out)
    assert MARKER not in env
    assert env["CLAUDE_PWA_SESSION"] == "mike" and env["CLARP_PROVIDER_TURN"] == "its-own-token"
    assert not [name for name in INHERITED if env.get(name) == INHERITED[name]]
    assert env["CLARP_UNRELATED_SETTING"] == "kept" and env["CLAUDE_PWA_DB"] == os.environ["CLAUDE_PWA_DB"]


def test_an_agy_turn_still_carries_its_own_managed_marker(tmp_path, monkeypatch):
    from lib.backend.stream_json import popen_turn
    monkeypatch.setenv("CLARP_AGY_MANAGED_TURN", "0")
    proc = popen_turn([sys.executable, "-c", "import os; print('CLARP_AGY_MANAGED_TURN=' + os.environ.get('CLARP_AGY_MANAGED_TURN', ''))"],
                      cwd=tmp_path, session="mike", env_extra={"CLARP_AGY_MANAGED_TURN": "1"})
    out, _ = proc.communicate(timeout=30)
    assert _child_env(out)["CLARP_AGY_MANAGED_TURN"] == "1"


LAUNCHERS = ("backend/stream_json.py", "codex_app_server.py", "terminal_ws.py", "orchestrator_model.py",
             "model_fallbacks.py", "janitor_autonomy.py")


@pytest.mark.parametrize("module", LAUNCHERS)
def test_every_child_environment_these_launchers_build_starts_without_inherited_identity(module):
    """The dispatcher's turns, the terminal, the shared Codex app-server, and the one-shot model
    requests (the router, model fallbacks, the Janitors): each builds its child's environment from
    without_inherited_identity(os.environ), never from the Host's whole environment."""
    source = (ROOT / "server/lib" / module).read_text()
    assert "without_inherited_identity(os.environ)" in source
    assert "**os.environ" not in source and "dict(os.environ)" not in source
    assert "os.environ.copy()" not in source


def test_without_inherited_identity_keeps_everything_else(monkeypatch):
    for name, value in INHERITED.items():
        monkeypatch.setenv(name, value)
    built = hook_runtime.without_inherited_identity(os.environ)
    assert not set(INHERITED) & set(built)
    assert built == {k: v for k, v in os.environ.items() if k not in INHERITED}


def test_the_terminals_child_has_no_inherited_identity(tmp_path, monkeypatch):
    """The real PTY bridge over a socketpair, as test_terminal_pty_high_fd drives it."""
    from lib import terminal_ws
    from tests.integration.test_terminal_pty_high_fd import _KEY, _Handler, _read_session
    from lib import ws
    for name, value in INHERITED.items():
        monkeypatch.setenv(name, value)
    srv, cli = socket.socketpair()
    handler = _Handler(srv)
    serve = threading.Thread(target=terminal_ws._serve_pty, args=(handler,), daemon=True, kwargs=dict(
        key=_KEY, argv=[sys.executable, "-c", "import json, os; print(json.dumps(sorted(os.environ)))"], cwd=str(tmp_path),
        session="identity-test", agent_id=None, backend="shell", bsid=""))
    try:
        serve.start()
        _head, frames = _read_session(cli, time.monotonic() + 15.0)
        serve.join(timeout=15.0)
    finally:
        for f in (handler.rfile, handler.wfile, srv, cli):
            f.close()
    output = b"".join(p for op, p in frames if op == ws.OP_BINARY).decode()
    names = json.loads(output[output.index("["):output.rindex("]") + 1])   # the child's names
    assert "TERM" in names and "CLAUDE_PWA_DB" in names
    assert not set(INHERITED) & set(names)
