"""Tests for `lib.clarp_runner` — the per-turn clarp -p dispatcher.

We don't run real `clarp` here. Instead the tests:
  * verify `build_cmd` produces the expected argv for both fresh and
    resumed turns
  * substitute a fake clarp binary (a tiny python script) via PATH so
    `spawn_turn` actually fork/execs something deterministic, and we
    can assert the callbacks fire with the right session_id from a
    canned stream-json payload
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import sys
import textwrap
import time

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import clarp_runner          # noqa: E402
from lib import config                # noqa: E402
from lib import agents as agents_db    # noqa: E402
from lib.protocol import SSEType       # noqa: E402


# ---- build_cmd ---------------------------------------------------------


def test_build_cmd_no_session_omits_continuity_flag():
    """No session id at all → no --continue, no --resume, no
    --session-id. Legacy / safety fallback."""
    cmd = clarp_runner.build_cmd()
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--continue" not in cmd
    assert "--resume" not in cmd
    assert "--session-id" not in cmd
    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--include-partial-messages" in cmd


def test_build_cmd_can_use_clarp_provider(monkeypatch):
    config.reset_cache_for_tests()
    monkeypatch.setenv("CLAUDE_PWA_CLAUDE_CLI", "clarp")
    try:
        assert clarp_runner.build_cmd()[0] == "clarp"
    finally:
        config.reset_cache_for_tests()


def test_build_cmd_existing_session_uses_resume():
    cmd = clarp_runner.build_cmd("abc-123")
    assert "--resume" in cmd
    i = cmd.index("--resume")
    assert cmd[i + 1] == "abc-123"
    assert "--session-id" not in cmd
    assert "--continue" not in cmd


def test_build_cmd_new_session_uses_session_id_flag():
    """A freshly-minted UUID for a brand new conversation — pass it via
    --session-id so claude adopts our uuid as the session identifier
    rather than generating its own (which would force us to wait for
    system.init to discover what id was used)."""
    cmd = clarp_runner.build_cmd("new-uuid-here", is_new_session=True)
    assert "--session-id" in cmd
    i = cmd.index("--session-id")
    assert cmd[i + 1] == "new-uuid-here"
    assert "--resume" not in cmd
    assert "--continue" not in cmd


def test_build_cmd_includes_dangerously_skip_permissions():
    """Production already runs claude with this flag; tests pin it so a
    future refactor doesn't silently drop the permission bypass and
    block voice turns waiting for an approval the PWA can't surface."""
    cmd = clarp_runner.build_cmd()
    assert "--dangerously-skip-permissions" in cmd
    assert "--input-format" in cmd
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"


def test_build_cmd_model_pin_opt_in():
    """Empty model → no --model (default behavior). Set → passed through."""
    assert "--model" not in clarp_runner.build_cmd()
    cmd = clarp_runner.build_cmd(model="claude-haiku-4-5-20251001")
    i = cmd.index("--model")
    assert cmd[i + 1] == "claude-haiku-4-5-20251001"


def test_build_cmd_effort_pin_opt_in():
    """Empty effort → no --effort. Set → passed through (Claude --effort)."""
    assert "--effort" not in clarp_runner.build_cmd()
    cmd = clarp_runner.build_cmd(model="opus", effort="high")
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_build_cmd_persona_identity_opt_in():
    assert "--append-system-prompt" not in clarp_runner.build_cmd()
    cmd = clarp_runner.build_cmd(persona="Bella", session="bella")
    i = cmd.index("--append-system-prompt")
    prompt = cmd[i + 1]
    assert "assistant persona named Bella" in prompt
    assert "When the user addresses Bella" in prompt


def test_build_cmd_scopes_mcp_per_agent_selection(tmp_path, monkeypatch):
    """By default a turn loads NO MCP servers (--strict-mcp-config) so a
    heavy/flaky server can't block startup. An agent's per-agent selection
    (set from the app, stored in the DB) is written to a scoped config file
    containing ONLY the chosen servers from the global catalog."""
    import json as _json
    from lib.config import Config
    monkeypatch.setenv("HOME", str(tmp_path))   # scoped configs write under HOME
    monkeypatch.setattr(clarp_runner._config, "load",
                        lambda *a, **k: Config(mcp_strict=True))
    monkeypatch.setattr(clarp_runner._config, "read_global_mcp_servers",
                        lambda: {"alpha": {"type": "http", "url": "x"},
                                 "beta": {"type": "stdio", "command": "y"}})

    # No per-agent selection → strict, and NO --mcp-config.
    monkeypatch.setattr(clarp_runner.agents_db, "get_by_session", lambda s: None)
    cmd = clarp_runner.build_cmd("sid", session="adam")
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" not in cmd

    # Selection ["alpha"] → strict + a scoped config with only alpha.
    monkeypatch.setattr(
        clarp_runner.agents_db, "get_by_session",
        lambda s: {"mcp_servers": '{"configured":true,"servers":["alpha"]}'})
    cmd = clarp_runner.build_cmd("sid", session="bella")
    assert "--strict-mcp-config" in cmd
    path = cmd[cmd.index("--mcp-config") + 1]
    written = _json.loads(open(path).read())
    assert list(written["mcpServers"].keys()) == ["alpha"]

    # Explicitly selecting none loads nothing.
    monkeypatch.setattr(
        clarp_runner.agents_db, "get_by_session",
        lambda s: {"mcp_servers": '{"configured":true,"servers":[]}'})
    cmd = clarp_runner.build_cmd("sid", session="bella")
    assert "--mcp-config" not in cmd


def test_build_cmd_strict_off_loads_global_mcp(tmp_path, monkeypatch):
    """mcp_strict=False restores the old behavior (inherit global MCP)."""
    from lib.config import Config
    monkeypatch.setattr(clarp_runner._config, "load",
                        lambda *a, **k: Config(mcp_strict=False))
    cmd = clarp_runner.build_cmd("sid", session="adam")
    assert "--strict-mcp-config" not in cmd


# ---- spawn_turn end-to-end via a fake clarp on PATH --------------------


def _install_fake_clarp(
    tmp_bin: pathlib.Path,
    events: list[dict],
    *,
    stderr: str = "",
    rc: int = 0,
) -> None:
    """Drop fake Claude-compatible CLI scripts into `tmp_bin` that emit the supplied
    stream-json events on stdout, one per line, then exits 0. Marks the
    file executable so PATH lookups find it."""
    tmp_bin.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(e) for e in events)
    script = textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        sys.stdout.write({payload!r} + "\\n")
        sys.stdout.flush()
        sys.stderr.write({stderr!r})
        sys.stderr.flush()
        raise SystemExit({rc})
    """)
    for name in ("claude", "clarp"):
        fake = tmp_bin / name
        fake.write_text(script)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_clarp(tmp_path, monkeypatch):
    """PATH-shim a fake `clarp`. Returns a builder that lets each test
    declare the JSON events the fake should emit on stdout."""
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    def _install(events, *, stderr="", rc=0):
        _install_fake_clarp(bin_dir, events, stderr=stderr, rc=rc)
    return _install


def _wait_for(pred, *, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


class _FakeStream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def test_spawn_turn_fires_on_session_init_with_session_id(fake_clarp, tmp_path):
    fake_clarp([
        {"type": "system", "subtype": "init",
         "session_id": "the-uuid-we-want"},
        {"type": "result", "subtype": "success", "result": "ok"},
    ])
    captured_sids: list[str] = []
    captured_results: list[dict] = []
    handle = clarp_runner.spawn_turn(
        text="hello",
        cwd=tmp_path,
        on_session_init=captured_sids.append,
        on_result=captured_results.append,
    )
    handle.wait(timeout=5.0)
    assert _wait_for(lambda: captured_sids == ["the-uuid-we-want"]), (
        f"on_session_init never fired with the right sid; got {captured_sids}"
    )
    assert _wait_for(lambda: len(captured_results) == 1)


def test_rejected_session_bind_stops_before_assistant_events(fake_clarp, tmp_path):
    fake_clarp([
        {"type": "system", "subtype": "init", "session_id": "conflict"},
        {"type": "assistant", "message": {"content": "must not leak"}},
        {"type": "result", "subtype": "success", "result": "must not leak"},
    ])
    results, errors = [], []
    handle = clarp_runner.spawn_turn(
        text="hi", cwd=tmp_path, on_session_init=lambda _sid: False,
        on_result=results.append, on_error=errors.append)
    handle.wait(timeout=5)
    assert _wait_for(lambda: errors == ["backend session binding rejected"])
    assert results == []


def test_spawn_turn_streams_assistant_partials_to_message_store(fake_clarp, tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    stream = _FakeStream()
    fake_clarp([
        {"type": "system", "subtype": "init", "session_id": "sid-live"},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hel"}],
        }},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
        }},
        {"type": "result", "subtype": "success", "result": "ok"},
    ])

    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-live")
    handle = clarp_runner.spawn_turn(
        text="hi",
        cwd=tmp_path,
        backend_session_id="sid-live",
        session="rachel",
        agent_id=agent_id,
        trace_id="trace-live",
        stream=stream,
    )
    handle.wait(timeout=5.0)
    # Process exit does not imply the stdout drainer has committed its rows.
    handle.drain_thread.join(timeout=5.0)
    assert not handle.drain_thread.is_alive()

    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="sid-live")
    assert [m["text"] for m in visible] == ["hello"]
    assert visible[0]["kind"] == "live"
    assert any(e.get("type") == SSEType.TRANSCRIPT_UPDATED for e in stream.events)


def test_spawn_turn_appends_delta_partials(fake_clarp, tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    fake_clarp([
        {"type": "system", "subtype": "init", "session_id": "sid-delta"},
        {"type": "assistant", "delta": {"text": "hel"}},
        {"type": "assistant", "delta": {"text": "lo"}},
        {"type": "result", "subtype": "success", "result": "ok"},
    ])

    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-delta")
    handle = clarp_runner.spawn_turn(
        text="hi",
        cwd=tmp_path,
        backend_session_id="sid-delta",
        session="rachel",
        agent_id=agent_id,
        trace_id="trace-delta",
    )
    handle.wait(timeout=5.0)

    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="sid-delta")
    assert [m["text"] for m in visible] == ["hello"]


def test_spawn_turn_handles_claude_stream_event_text_deltas(fake_clarp, tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    fake_clarp([
        {"type": "system", "subtype": "init", "session_id": "sid-stream-event"},
        {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "O"},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "K"},
        }},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "OK"}],
        }},
        {"type": "result", "subtype": "success", "result": "ok"},
    ])

    agents_db.open_turn(
        agent_id=agent_id, source="pwa", trace_id="trace-stream-event")
    handle = clarp_runner.spawn_turn(
        text="hi",
        cwd=tmp_path,
        backend_session_id="sid-stream-event",
        session="rachel",
        agent_id=agent_id,
        trace_id="trace-stream-event",
    )
    handle.wait(timeout=5.0)

    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="sid-stream-event")
    assert [m["text"] for m in visible] == ["OK"]


def test_isolated_spawn_turn_returns_all_assistant_text_blocks(fake_clarp, tmp_path):
    fake_clarp([
        {"type": "system", "subtype": "init", "session_id": "sid-isolated"},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "DREAM_STAGE_OUTPUT run_id=dream_1 round_id=dround_1 stage=SEED\n"}],
        }},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Read"}],
        }},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "D1 [new]: grounded direction"}],
        }},
        {"type": "result", "subtype": "success", "result": "ok"},
    ])
    captured: list[dict] = []

    handle = clarp_runner.spawn_turn(
        text="dream",
        cwd=tmp_path,
        backend_session_id="sid-isolated",
        is_new_session=True,
        isolated=True,
        on_result=captured.append,
    )
    handle.wait(timeout=5.0)

    assert _wait_for(lambda: len(captured) == 1)
    text = captured[0]["_assistant_text"]
    assert "DREAM_STAGE_OUTPUT" in text
    assert "D1 [new]: grounded direction" in text


def test_spawn_turn_propagates_missing_clarp_as_filenotfound(tmp_path,
                                                              monkeypatch):
    """Empty PATH → clarp not found → FileNotFoundError raised so /send
    can surface a clear 500 rather than starting a zombie subprocess."""
    monkeypatch.setenv("PATH", "")
    with pytest.raises(FileNotFoundError):
        clarp_runner.spawn_turn(text="hi", cwd=tmp_path)


def test_spawn_turn_zero_exit_without_result_calls_on_error(fake_clarp, tmp_path):
    """A clarp invocation that emits no system.init (e.g. exited early
    due to an upstream rate-limit) must surface to the dispatcher. A pure
    log-only path leaves the UI on silence / Connected."""
    fake_clarp([
        {"type": "rate_limit_event", "rate_limit_info": {"status": "blocked"}},
    ])
    sids: list[str] = []
    errs: list[str] = []
    handle = clarp_runner.spawn_turn(
        text="hi", cwd=tmp_path,
        on_session_init=sids.append,
        on_error=errs.append,
    )
    handle.wait(timeout=5.0)
    assert sids == [], "no system.init was emitted → callback must stay silent"
    assert _wait_for(lambda: len(errs) == 1)
    assert "without stream-json result" in errs[0]


def test_spawn_turn_zero_exit_usage_limit_stderr_calls_on_error(fake_clarp, tmp_path):
    fake_clarp(
        [],
        stderr="You've hit your session limit · resets 3:20pm (Europe/Oslo)",
    )
    errs: list[str] = []
    handle = clarp_runner.spawn_turn(
        text="hi", cwd=tmp_path,
        on_error=errs.append,
    )
    handle.wait(timeout=5.0)
    assert _wait_for(lambda: len(errs) == 1)
    assert "session limit" in errs[0]


def test_spawn_turn_drainer_is_daemon_so_python_can_exit(fake_clarp, tmp_path):
    """Daemonised so a zombie clarp can't block test interpreter shutdown."""
    fake_clarp([{"type": "system", "subtype": "init", "session_id": "x"}])
    handle = clarp_runner.spawn_turn(text="x", cwd=tmp_path)
    handle.wait(timeout=5.0)
    # Drainer thread should be a daemon — if for some reason the
    # subprocess hung, this guarantees the process can still exit.
    assert handle.drain_thread.daemon is True


def test_spawn_turn_callback_exception_is_isolated(fake_clarp, tmp_path):
    """A bad on_session_init must not crash the drainer thread or
    prevent subsequent events (result) from firing."""
    fake_clarp([
        {"type": "system", "subtype": "init", "session_id": "sid-A"},
        {"type": "result", "subtype": "success"},
    ])
    results: list[dict] = []
    def bad_init(_sid):
        raise RuntimeError("intentional test failure inside callback")
    handle = clarp_runner.spawn_turn(
        text="x", cwd=tmp_path,
        on_session_init=bad_init,
        on_result=results.append,
    )
    handle.wait(timeout=5.0)
    assert _wait_for(lambda: len(results) == 1), (
        f"on_result must still fire even if on_session_init raised; "
        f"got {results}"
    )


def test_spawn_turn_silent_turn_is_not_timed_out(tmp_path, monkeypatch):
    """No turn timer: a turn that emits system.init then stays silent for a
    while — which the old post-init watchdog would have SIGTERMed — must run to
    its natural completion. Recovery from a genuinely hung turn is via
    preempt-kill (a new message), not a timer."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "claude"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, json, time
        sys.stdout.write(json.dumps(
            {{"type": "system", "subtype": "init", "session_id": "sid"}}) + "\\n")
        sys.stdout.flush()
        time.sleep(1.0)  # silent — the old post-init watchdog would kill here
        sys.stdout.write(json.dumps(
            {{"type": "result", "subtype": "success", "result": "ok"}}) + "\\n")
        sys.stdout.flush()
    """))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    results: list[dict] = []
    errs: list[str] = []
    handle = clarp_runner.spawn_turn(
        text="hi", cwd=tmp_path,
        on_result=results.append, on_error=errs.append)
    handle.wait(timeout=10.0)
    assert _wait_for(lambda: len(results) == 1), \
        "a silent turn should complete, not be killed by a timer"
    assert errs == [], f"a silent turn must NOT be timed out; got {errs}"


def test_spawn_turn_streaming_turn_completes(tmp_path, monkeypatch):
    """A turn that streams partial output over time then a result delivers
    cleanly (no timer to trip)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "claude"
    # Emit a partial line every 0.1s for ~0.6s (longer than the 0.3s idle
    # limit), then a clean result. Each line resets the deadline.
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, json, time
        sys.stdout.write(json.dumps(
            {{"type": "system", "subtype": "init", "session_id": "sid"}}) + "\\n")
        sys.stdout.flush()
        for _ in range(6):
            time.sleep(0.1)
            sys.stdout.write(json.dumps(
                {{"type": "stream_event", "subtype": "partial"}}) + "\\n")
            sys.stdout.flush()
        sys.stdout.write(json.dumps(
            {{"type": "result", "subtype": "success", "result": "ok"}}) + "\\n")
        sys.stdout.flush()
    """))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    results: list[dict] = []
    errs: list[str] = []
    handle = clarp_runner.spawn_turn(
        text="hi", cwd=tmp_path,
        on_result=results.append, on_error=errs.append)
    handle.wait(timeout=5.0)
    assert _wait_for(lambda: len(results) == 1), "clean result should arrive"
    assert errs == [], f"active turn must not be timed out; got {errs}"


def test_spawn_turn_delivers_prompt_on_stdin_not_argv(tmp_path, monkeypatch):
    """Regression: `--input-format stream-json` makes clarp read the user
    message from stdin. Passing the prompt as a positional argv with
    stdin=DEVNULL (the old bug) made clarp exit rc=0 with no init/result.
    Assert the prompt arrives as a stream-json user message on stdin and is
    NOT appended to argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    capture = tmp_path / "capture.json"
    fake = bin_dir / "claude"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, json
        stdin_data = sys.stdin.read()
        json.dump({{"argv": sys.argv[1:], "stdin": stdin_data}},
                  open({str(capture)!r}, "w"))
        sys.stdout.write(json.dumps(
            {{"type": "system", "subtype": "init", "session_id": "sid"}}) + "\\n")
        sys.stdout.write(json.dumps(
            {{"type": "result", "subtype": "success", "result": "ok"}}) + "\\n")
        sys.stdout.flush()
    """))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    handle = clarp_runner.spawn_turn(text="route this to Sam", cwd=tmp_path)
    handle.wait(timeout=5.0)

    assert _wait_for(capture.exists)
    rec = json.loads(capture.read_text())
    # Prompt must NOT be a positional arg.
    assert "route this to Sam" not in rec["argv"], (
        f"prompt leaked into argv: {rec['argv']}"
    )
    # Prompt must arrive as a stream-json user message on stdin.
    msg = json.loads(rec["stdin"].strip())
    assert msg["type"] == "user"
    assert msg["message"]["content"][0]["text"] == "route this to Sam"


def test_default_model_pin_means_cli_default():
    """The picker used to store "default" as a pin; Claude Code does not know
    that alias and would send it to the API verbatim (HTTP 400 "does not
    support this model"). It must dispatch as no --model at all."""
    assert "--model" not in clarp_runner.build_cmd(model="default")
    assert "--model" not in clarp_runner.build_cmd(model="Default ")
