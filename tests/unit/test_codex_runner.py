"""Tests for `lib.codex_runner` — the `codex exec --json` dispatcher.

No real `codex` runs. We:
  * pin build_cmd's argv for fresh vs resume turns
  * PATH-shim a fake `codex` that emits a canned JSONL event stream, then
    assert the runner binds the session, records agent state, and enqueues
    the spoken <speak> text into the TTS queue — i.e. it reproduces, off a
    single stdout stream, the side-effects Claude gets from hooks + the
    transcript watcher.
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

from lib import codex_runner          # noqa: E402
from lib import agents as agents_db    # noqa: E402
from lib.protocol import AgentState    # noqa: E402


# ---- build_cmd ---------------------------------------------------------

def test_build_cmd_fresh_has_json_and_bypass_no_resume():
    cmd = codex_runner.build_cmd("", is_new_session=True)
    assert cmd[:2] == ["codex", "exec"]
    assert "--json" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "resume" not in cmd


def test_build_cmd_isolated_uses_ephemeral_workspace_sandbox():
    cmd = codex_runner.build_cmd("", is_new_session=True, isolated=True)
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "--ephemeral" in cmd
    assert "resume" not in cmd


def test_build_cmd_resume_appends_resume_and_id():
    cmd = codex_runner.build_cmd("sess-uuid-9")
    assert "resume" in cmd
    i = cmd.index("resume")
    assert cmd[i + 1] == "sess-uuid-9"
    # flags come before the subcommand
    assert cmd.index("--json") < i


def test_build_cmd_model_and_reasoning_effort_opt_in():
    """Empty → no overrides (Codex defaults). Set → --model and the
    -c model_reasoning_effort override are passed, before any resume."""
    assert "--model" not in codex_runner.build_cmd("")
    assert "-c" not in codex_runner.build_cmd("")

    cmd = codex_runner.build_cmd("sess-1", model="gpt-5-codex",
                                 reasoning_effort="low")
    assert cmd[cmd.index("--model") + 1] == "gpt-5-codex"
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=low"
    # Overrides precede the resume subcommand.
    assert cmd.index("-c") < cmd.index("resume")


# ---- fake codex on PATH ------------------------------------------------

def _install_fake_codex(tmp_bin: pathlib.Path, events: list[dict]) -> None:
    tmp_bin.mkdir(parents=True, exist_ok=True)
    fake = tmp_bin / "codex"
    payload = "\n".join(json.dumps(e) for e in events)
    script = textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        sys.stdout.write({payload!r} + "\\n")
        sys.stdout.flush()
    """)
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return lambda events: _install_fake_codex(bin_dir, events)


def _wait_for(pred, *, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _make_codex_agent(persona="Rachel", session="rachel") -> str:
    return agents_db.create_agent(persona=persona, voice_id="v-codex",
                                  cwd="/tmp", session=session,
                                  backend="codex")


def _queued_texts(agent_id: str) -> list[str]:
    """tts_queue.recent() omits the text column; read it straight from the DB."""
    rows = agents_db.conn().execute(
        "SELECT text FROM tts_queue WHERE agent_id = ? ORDER BY enqueued_at",
        (agent_id,),
    ).fetchall()
    return [r["text"] for r in rows]


def test_spawn_turn_binds_session_records_state_and_speaks(fake_codex, tmp_path):
    """The REAL `codex exec --json` stdout schema (verified against
    codex-cli 0.135.0: thread.started / turn.started / item.completed /
    turn.completed) drives session bind + THINKING + a TTS enqueue for the
    <speak> block, and carries usage tokens through to on_result."""
    agent_id = _make_codex_agent()
    fake_codex([
        {"type": "thread.started", "thread_id": "codex-sess-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "item_0", "type": "agent_message",
            "text": "Working on it. <speak>Hello from Rachel.</speak>"}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20}},
    ])

    sids: list[str] = []
    results: list[dict] = []
    handle = codex_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="rachel",
        on_session_init=sids.append, on_result=results.append,
    )
    handle.wait(timeout=5.0)

    assert _wait_for(lambda: sids == ["codex-sess-1"]), f"session bind: {sids}"
    assert _wait_for(lambda: len(results) == 1), "on_result must fire on clean exit"
    # Usage from turn.completed flows into the result.
    assert results[0]["usage"]["input_tokens"] == 100
    assert results[0]["usage"]["output_tokens"] == 20

    # The <speak> block reached the TTS queue.
    def _spoken():
        return any("Hello from Rachel." in t for t in _queued_texts(agent_id))
    assert _wait_for(_spoken), "the <speak> block should have been enqueued for TTS"

    # turn.started recorded a THINKING state row.
    kinds = [r["kind"] for r in agents_db.conn().execute(
        "SELECT kind FROM state_log WHERE agent_id = ?", (agent_id,)).fetchall()]
    assert AgentState.THINKING in kinds


def test_rejected_session_bind_ignores_buffered_assistant_output(fake_codex, tmp_path):
    agent_id = _make_codex_agent(persona="Reject", session="reject")
    fake_codex([
        {"type": "thread.started", "thread_id": "conflicting-thread"},
        {"type": "item.completed", "item": {
            "id": "msg", "type": "agent_message",
            "text": "<speak>must not leak</speak>"}},
        {"type": "turn.completed", "usage": {"output_tokens": 4}},
    ])
    results, errors = [], []
    handle = codex_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="reject",
        on_session_init=lambda _sid: False,
        on_result=results.append, on_error=errors.append)
    handle.wait(timeout=5)
    assert _wait_for(lambda: errors == ["backend session binding rejected"])
    assert results == []
    assert _queued_texts(agent_id) == []


def test_agent_message_updates_stream_through_one_throttled_live_row(
    fake_codex, tmp_path, monkeypatch,
):
    agent_id = _make_codex_agent(persona="Caleb", session="caleb")
    fake_codex([
        {"type": "thread.started", "thread_id": "codex-live-1"},
        {"type": "turn.started"},
        {"type": "item.updated", "item": {
            "id": "msg-1", "type": "agent_message", "text": "Hel"}},
        {"type": "item.updated", "item": {
            "id": "msg-1", "type": "agent_message", "text": "Hello"}},
        {"type": "item.completed", "item": {
            "id": "msg-1", "type": "agent_message", "text": "Hello world"}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 1, "output_tokens": 2}},
    ])
    calls: list[str] = []
    original = agents_db.upsert_live_assistant_message

    def record_live(**kwargs):
        calls.append(kwargs["text"])
        return original(**kwargs)

    monkeypatch.setattr(agents_db, "upsert_live_assistant_message", record_live)
    agents_db.open_turn(
        agent_id=agent_id, source="pwa", trace_id="trace-live-1")
    handle = codex_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="caleb",
        trace_id="trace-live-1",
    )
    handle.wait(timeout=5.0)
    handle.drain_thread.join(timeout=5.0)
    assert not handle.drain_thread.is_alive()

    assert _wait_for(lambda: calls and calls[-1] == "Hello world")
    assert calls == ["Hel", "Hello world"]
    rows = agents_db.conn().execute(
        """SELECT text, kind FROM messages
             WHERE agent_id = ? AND source_file LIKE 'live:%'""",
        (agent_id,),
    ).fetchall()
    assert [(row["text"], row["kind"]) for row in rows] == [
        ("Hello world", "live")
    ]


def test_turn_failed_calls_on_error_not_result(fake_codex, tmp_path):
    agent_id = _make_codex_agent(persona="Bella", session="bella-fail")
    fake_codex([
        {"type": "thread.started", "thread_id": "codex-sess-fail"},
        {"type": "turn.started"},
        {"type": "turn.failed", "error": {
            "message": "You've hit your usage limit. Try again at 3:29 PM."}},
    ])

    errors: list[str] = []
    results: list[dict] = []
    handle = codex_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="bella-fail",
        on_error=errors.append, on_result=results.append,
    )
    handle.wait(timeout=5.0)

    assert _wait_for(lambda: errors), "on_error must fire for turn.failed"
    assert "usage limit" in errors[0]
    assert results == []


def test_spawn_turn_flat_event_shape_also_binds(fake_codex, tmp_path):
    """Some codex versions emit flatter lines (no payload envelope). The
    parser must handle both."""
    agent_id = _make_codex_agent(persona="Domi", session="domi")
    fake_codex([
        {"type": "session_meta", "id": "flat-sess-2"},
        {"type": "agent_message", "message": "<speak>Flat works.</speak>"},
    ])
    sids: list[str] = []
    handle = codex_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="domi",
        on_session_init=sids.append,
    )
    handle.wait(timeout=5.0)
    assert _wait_for(lambda: sids == ["flat-sess-2"]), f"flat session bind: {sids}"


def test_speak_dedupes_across_agent_message_and_task_complete(fake_codex, tmp_path):
    """The same <speak> region appearing in a streamed agent_message and
    again in task_complete.last_agent_message must only be spoken once."""
    agent_id = _make_codex_agent(persona="Bella", session="bella")
    fake_codex([
        {"type": "session_meta", "payload": {"id": "s3"}},
        {"type": "event_msg", "payload": {"type": "agent_message",
                                          "message": "<speak>Only once.</speak>"}},
        {"type": "event_msg", "payload": {
            "type": "task_complete",
            "last_agent_message": "<speak>Only once.</speak>"}},
    ])
    handle = codex_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="bella",
    )
    handle.wait(timeout=5.0)
    time.sleep(0.2)
    rows = [t for t in _queued_texts(agent_id) if "Only once." in t]
    assert len(rows) == 1, f"expected exactly one enqueue, got {len(rows)}"


def test_command_execution_item_flips_to_tool_state(fake_codex, tmp_path):
    """A modern item.* event of a tool type flips the agent to TOOL."""
    agent_id = _make_codex_agent(persona="Domi", session="domi2")
    fake_codex([
        {"type": "thread.started", "thread_id": "s-tool"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {
            "id": "i1", "type": "command_execution", "command": "ls -1"}},
        {"type": "item.completed", "item": {
            "id": "i1", "type": "command_execution", "command": "ls -1"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    handle = codex_runner.spawn_turn(
        text="run ls", cwd=tmp_path, agent_id=agent_id, session="domi2",
    )
    handle.wait(timeout=5.0)
    time.sleep(0.2)
    kinds = [r["kind"] for r in agents_db.conn().execute(
        "SELECT kind FROM state_log WHERE agent_id = ?", (agent_id,)).fetchall()]
    assert AgentState.TOOL in kinds


def test_spawn_turn_expands_tilde_cwd(fake_codex):
    """An agent whose cwd is the literal "~" must still spawn — Popen won't
    expand the tilde, so the runner must. Regression for the /send 500 that
    made Codex agents created with the default working dir do nothing."""
    agent_id = _make_codex_agent(persona="Elli", session="elli")
    fake_codex([
        {"type": "thread.started", "thread_id": "tilde-sess"},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    sids: list[str] = []
    # pathlib.Path("~") is exactly what _handle_send used to pass through.
    handle = codex_runner.spawn_turn(
        text="hi", cwd=pathlib.Path("~"), agent_id=agent_id,
        session="elli", on_session_init=sids.append,
    )
    handle.wait(timeout=5.0)
    assert _wait_for(lambda: sids == ["tilde-sess"]), (
        f"spawn with cwd='~' should expand and run; got {sids}")


def test_spawn_turn_missing_codex_raises_filenotfound(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(FileNotFoundError):
        codex_runner.spawn_turn(text="hi", cwd=tmp_path, agent_id="x")


def test_interrupt_is_safe_when_nothing_running():
    assert codex_runner.interrupt("no-such-agent") == 0


def test_voice_preamble_roundtrip():
    """apply_voice_preamble adds the <speak> instruction; strip_voice_preamble
    recovers the original message exactly (for the history pane)."""
    msg = "count the files in this repo"
    wrapped = codex_runner.apply_voice_preamble(msg)
    assert "<speak>" in wrapped and msg in wrapped
    assert wrapped != msg
    assert codex_runner.strip_voice_preamble(wrapped) == msg
    # No-op when the preamble isn't present.
    assert codex_runner.strip_voice_preamble("plain message") == "plain message"


def test_preamble_always_forbids_interactive_questions():
    """Every app-dispatched turn carries the no-interactive-questions rule,
    whether or not it's a spoken turn. Strip recovers the user's message in
    both cases."""
    msg = "should I refactor this?"

    # Non-voice turn: no <speak> guidance, but the no-question rule is present.
    silent = codex_runner.apply_voice_preamble(msg, voice=False)
    assert "interactive prompts" in silent
    assert "<speak>" not in silent
    assert codex_runner.strip_voice_preamble(silent) == msg

    # Voice turn: both the no-question rule AND the <speak> guidance.
    spoken = codex_runner.apply_voice_preamble(msg, voice=True)
    assert "interactive prompts" in spoken
    assert "<speak>" in spoken
    assert codex_runner.strip_voice_preamble(spoken) == msg


def test_voice_preamble_requests_conversational_delivery_for_all_speech():
    spoken = codex_runner.apply_voice_preamble("Explain the result.", voice=True)

    assert "Every spoken response should sound conversational" in spoken
    assert "do not reserve them for uncertainty" in spoken
    assert "When you're unsure or working through something complex" not in spoken
    assert "few or no fillers" not in spoken


def test_voice_preamble_can_hide_persona_identity():
    msg = "hello"
    wrapped = codex_runner.apply_voice_preamble(
        msg,
        voice=True,
        persona="Bella",
        session="bella",
    )
    assert "assistant persona named Bella" in wrapped
    assert "When the user addresses Bella" in wrapped
    assert codex_runner.strip_voice_preamble(wrapped) == msg
