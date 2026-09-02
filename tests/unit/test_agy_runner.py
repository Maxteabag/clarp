"""AGY 1.1.21 stream-json runtime parity tests."""
from __future__ import annotations

import os
import json
import pathlib
import stat
import sys
import textwrap
import threading
import time

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agy_runner          # noqa: E402
from lib import agents as agents_db  # noqa: E402
from lib import error_classify  # noqa: E402
from lib import message_store  # noqa: E402
from lib.conversation import load_conversation  # noqa: E402
from lib.protocol import AgentState  # noqa: E402
from lib.turn_dispatch import _result_detail  # noqa: E402


def test_build_cmd_fresh_vs_resume():
    fresh = agy_runner.build_cmd("", is_new_session=True)
    assert fresh[0] == "agy"
    assert "--dangerously-skip-permissions" in fresh
    # The prompt is NOT a bare positional and -p must not appear without a
    # value (that bug made agy treat --dangerously-skip-permissions as the
    # prompt). build_cmd carries no prompt; spawn_turn adds --print=<prompt>.
    assert "-p" not in fresh
    assert not any(a.startswith("--print") for a in fresh)
    assert "--conversation" not in fresh
    assert fresh[fresh.index("--output-format") + 1] == "stream-json"
    resume = agy_runner.build_cmd("conv-9")
    assert "--conversation" in resume
    assert resume[resume.index("--conversation") + 1] == "conv-9"


def test_build_cmd_model_pin_opt_in():
    """Only strict discovered-style slugs and low/medium/high efforts pass."""
    assert "--model" not in agy_runner.build_cmd("")
    cmd = agy_runner.build_cmd(
        "", is_new_session=True, model="gemini-3.7-flash-low")
    assert cmd[cmd.index("--model") + 1] == "gemini-3.7-flash-low"
    effort_cmd = agy_runner.build_cmd("", effort="high")
    assert effort_cmd[effort_cmd.index("--effort") + 1] == "high"
    with pytest.raises(ValueError, match="unavailable AGY model"):
        agy_runner.build_cmd("", model="4.8")
    with pytest.raises(ValueError, match="effort"):
        agy_runner.build_cmd("", effort="ultra")
    with pytest.raises(ValueError, match="compatibility is unknown"):
        agy_runner.build_cmd("", model="gemini-3.7-flash-low", effort="high")


def test_invalid_options_fail_before_tempfile(monkeypatch):
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("tempfile must not be created")

    monkeypatch.setattr(agy_runner.tempfile, "mkstemp", forbidden)
    with pytest.raises(ValueError, match="unavailable AGY model"):
        agy_runner.spawn_turn(text="hi", cwd=pathlib.Path("/tmp"),
                              model="4.8")
    assert called is False


_FAKE_CONV = "11111111-2222-3333-4444-555555555555"
_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "agy"


def _stream(response: str, *, status="SUCCESS", error="", tool=False,
            usage=None) -> str:
    turn_usage = usage or {
        "input_tokens": 10, "output_tokens": 4, "thinking_tokens": 2,
        "cache_read_tokens": 3, "total_tokens": 14,
    }
    rows = [
        {"event": "init", "conversation_id": _FAKE_CONV,
         "init": {"model": "gemini-3.7-flash-low", "tools": ["run_command"]}},
        {"event": "step_update", "step_update": {
            "conversation_id": _FAKE_CONV, "step_index": 0,
            "state": "DONE", "step_type": "user_input"}},
    ]
    if tool:
        rows.extend([
            {"event": "step_update", "step_update": {
                "conversation_id": _FAKE_CONV, "step_index": 1,
                "state": "ACTIVE", "step_type": "tool",
                "tool_name": "run_command", "tool_info": {
                    "name": "run_command", "parameters": {"CommandLine": "printf OK"}}}},
            {"event": "step_update", "step_update": {
                "conversation_id": _FAKE_CONV, "step_index": 1,
                "state": "DONE", "step_type": "tool", "tool_name": "run_command",
                "duration_seconds": 0.02, "tool_info": {
                    "name": "run_command", "parameters": {"CommandLine": "printf OK"},
                    "output": "OK"}}},
        ])
    rows.append({"event": "step_update", "step_update": {
        "conversation_id": _FAKE_CONV, "step_index": 2,
        "state": "DONE", "step_type": "agent_response",
        "text_delta": response, "duration_seconds": 1.2, "usage": turn_usage}})
    result = {"conversation_id": _FAKE_CONV, "status": status,
              "response": response, "duration_seconds": 1.3,
              "num_turns": 1, "usage": turn_usage}
    if error:
        result["error"] = error
    rows.append({"event": "result", "result": result})
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def _install_fake_agy(tmp_bin: pathlib.Path, stdout: str, *, rc: int = 0) -> None:
    tmp_bin.mkdir(parents=True, exist_ok=True)
    fake = tmp_bin / "agy"
    script = textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, os, json
        argv = sys.argv[1:]
        log = None
        for i, a in enumerate(argv):
            if a == "--log-file" and i + 1 < len(argv):
                log = argv[i + 1]
        if log:
            with open(log, "w") as f:
                f.write("printmode.go:130] Print mode: conversation={_FAKE_CONV}, sending message\\n")
        dump = os.environ.get("AGY_FAKE_ARGV_OUT")
        if dump:
            with open(dump, "w") as f:
                json.dump(argv, f)
        sys.stdout.write({stdout!r})
        sys.stdout.flush()
        sys.exit({rc})
    """)
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGY_FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    return lambda stdout, rc=0: _install_fake_agy(bin_dir, stdout, rc=rc)


def _wait_for(pred, *, timeout=4.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _make_agy_agent(persona="Elli", session="elli") -> str:
    agent_id = agents_db.create_agent(
        persona=persona, voice_id="v-agy", cwd="/tmp",
        session=session, backend="agy")
    # Direct runner tests bypass TurnDispatchService; provide its equivalent
    # ownership row so provider side effects remain fail-closed.
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="")
    return agent_id


def _open_owned_turn(agent_id: str, trace_id: str) -> str:
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id=trace_id)
    return trace_id


def _queued_texts(agent_id):
    return [r["text"] for r in agents_db.conn().execute(
        "SELECT text FROM tts_queue WHERE agent_id = ? ORDER BY enqueued_at",
        (agent_id,)).fetchall()]


def _write_agy_transcript(agy_home: pathlib.Path, rows: list[dict]) -> None:
    logs = agy_home / "brain" / _FAKE_CONV / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n")


def _load_agy_conversation(session: str) -> dict:
    return load_conversation(
        session=session, claude_finder=lambda _sid: None,
        claude_parser=lambda _path: [])


def test_spawn_turn_binds_conversation_speaks_and_results(fake_agy, tmp_path):
    agent_id = _make_agy_agent()
    trace_id = _open_owned_turn(agent_id, "spawn-success")
    fake_agy(_stream("Here is the answer.\n<speak>The answer is forty-two.</speak>\n"))
    sids, results = [], []
    handle = agy_runner.spawn_turn(
        text="what's the answer?", cwd=tmp_path, agent_id=agent_id,
        session="elli", on_session_init=sids.append,
        on_result=results.append, voice_preamble=True, trace_id=trace_id)
    handle.wait(timeout=8.0)

    assert _wait_for(lambda: sids == [_FAKE_CONV]), f"conversation bind: {sids}"
    assert _wait_for(lambda: len(results) == 1), "on_result must fire"
    assert results[0]["agy_reported_usage"]["normalized_step_sum"][
        "cache_read_input_tokens"] == 3
    assert results[0]["agy_reported_usage"]["step_duration_seconds_sum"] == 1.2
    assert results[0]["duration_ms"] == 1300
    assert "tokens_in" not in _result_detail(results[0], trace_id="t")
    assert _wait_for(lambda: any("forty-two" in t for t in _queued_texts(agent_id))), \
        "the <speak> block should be enqueued for TTS"
    kinds = [r["kind"] for r in agents_db.conn().execute(
        "SELECT kind FROM state_log WHERE agent_id = ?", (agent_id,)).fetchall()]
    assert AgentState.THINKING in kinds


def test_final_response_is_authoritative_and_persisted_once(
    fake_agy, tmp_path, monkeypatch,
):
    agent_id = _make_agy_agent(persona="Arnold", session="arnold")
    agents_db.set_focus(agent_id)
    agy_home = tmp_path / "agy-home"
    monkeypatch.setenv("CLAUDE_PWA_AGY_HOME", str(agy_home))
    _write_agy_transcript(agy_home, [
        {"step_index": 0, "type": "USER_INPUT", "created_at": "t1",
         "content": "<USER_REQUEST>new prompt</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "t2",
         "content": "<speak>Latest answer.</speak>"},
    ])
    fake_agy(_stream("<speak>Latest answer.</speak>"))
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-final")

    handle = agy_runner.spawn_turn(
        text="new prompt", cwd=tmp_path, backend_session_id=_FAKE_CONV,
        agent_id=agent_id, session="arnold", trace_id="turn-final")
    handle.wait(timeout=8.0)

    assert _wait_for(lambda: len(_queued_texts(agent_id)) >= 1)
    assert _queued_texts(agent_id) == ["Latest answer."]
    assistant = agents_db.conn().execute(
        "SELECT text, source_file FROM messages WHERE agent_id=? AND role='assistant'",
        (agent_id,),).fetchall()
    assert len(assistant) == 1
    assert assistant[0]["source_file"].startswith("final:")


def test_finalized_reply_survives_next_turn_live_update(tmp_path):
    agent_id = _make_agy_agent(persona="Durable", session="durable")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-a")
    assert agents_db.finalize_live_assistant_message(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-a", text="answer A") is not None

    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-b")
    assert agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-b", text="partial B") is not None

    rows = agents_db.conn().execute(
        """SELECT text, source_file FROM messages
             WHERE agent_id=? AND role='assistant' ORDER BY seq""",
        (agent_id,),).fetchall()
    assert {(row["text"], row["source_file"].split(":", 1)[0]) for row in rows} \
        == {("answer A", "final"), ("partial B", "live")}


def test_preempted_turn_cannot_promote_live_row(tmp_path):
    agent_id = _make_agy_agent(persona="PromotionFence", session="promotion")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-a")
    assert agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-a", text="partial A") is not None
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-b")

    assert agents_db.finalize_live_assistant_message(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-a", text="answer A") is None
    assert agents_db.conn().execute(
        "SELECT 1 FROM messages WHERE source_file LIKE 'final:%'",
    ).fetchone() is None


def test_prompt_is_bound_to_print_flag_not_a_positional(fake_agy, tmp_path):
    """Regression: the prompt must ride on --print (so agy doesn't treat
    --dangerously-skip-permissions as the prompt). The real user text must
    appear only as --print's value."""
    agent_id = _make_agy_agent(persona="Sam", session="sam")
    fake_agy(_stream("<speak>hi</speak>"))
    handle = agy_runner.spawn_turn(
        text="what is 2 plus 2?", cwd=tmp_path, agent_id=agent_id,
        session="sam", voice_preamble=False)
    handle.wait(timeout=8.0)
    _wait_for(lambda: (tmp_path / "argv.json").is_file())
    argv = json.loads((tmp_path / "argv.json").read_text())
    print_args = [a for a in argv if a.startswith("--print=")]
    assert len(print_args) == 1, f"expected one --print=…, got {argv}"
    # The prompt rides on --print=; even on a silent turn it now carries the
    # always-on no-interactive-questions preamble. Strip recovers the user text.
    from lib.codex_runner import strip_voice_preamble
    assert strip_voice_preamble(print_args[0][len("--print="):]) == "what is 2 plus 2?"
    # The skip-permissions flag must be its own token, never the prompt.
    assert "--dangerously-skip-permissions" in argv
    # No bare positional carrying the prompt.
    assert "what is 2 plus 2?" not in argv


def test_spawn_turn_nonzero_exit_calls_on_error(fake_agy, tmp_path):
    agent_id = _make_agy_agent(persona="Domi", session="domi")
    fake_agy("not-json\n", rc=3)
    errs = []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="domi",
        on_error=errs.append)
    handle.wait(timeout=8.0)
    assert _wait_for(lambda: len(errs) == 1), "on_error must fire on nonzero exit"


def test_spawn_turn_expands_tilde_cwd(fake_agy):
    agent_id = _make_agy_agent(persona="Bella", session="bella")
    fake_agy(_stream("<speak>ok</speak>"))
    sids = []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=pathlib.Path("~"), agent_id=agent_id,
        session="bella", on_session_init=sids.append)
    handle.wait(timeout=8.0)
    assert _wait_for(lambda: sids == [_FAKE_CONV]), f"tilde cwd should expand+run: {sids}"


def test_missing_agy_raises_filenotfound(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(FileNotFoundError):
        agy_runner.spawn_turn(text="hi", cwd=tmp_path, agent_id="x")


def test_interrupt_safe_when_idle():
    assert agy_runner.interrupt("nobody") == 0


def test_success_envelope_with_nonzero_exit_is_error(fake_agy, tmp_path):
    agent_id = _make_agy_agent(persona="Tooler", session="tooler")
    fake_agy(_stream("TOOL_DONE", tool=True), rc=7)
    results, errors = [], []
    handle = agy_runner.spawn_turn(
        text="run", cwd=tmp_path, agent_id=agent_id, session="tooler",
        on_result=results.append, on_error=errors.append)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(errors) == 1)
    assert results == []
    assert error_classify.classify_error(errors[0]) == error_classify.RUNNER_EXIT
    assert _queued_texts(agent_id) == []
    assistant = agents_db.conn().execute(
        "SELECT 1 FROM messages WHERE agent_id=? AND role='assistant'",
        (agent_id,),).fetchall()
    assert assistant == []
    rows = agents_db.conn().execute(
        "SELECT kind, detail FROM state_log WHERE agent_id=?", (agent_id,)).fetchall()
    tool = next(row for row in rows if row["kind"] == AgentState.TOOL)
    detail = json.loads(tool["detail"])
    assert detail["tool"] == "Bash"
    assert detail["input"]["command"] == "printf OK"
    assert detail["agy_raw_evidence"]["turn_execution_id"]


def test_trace_owned_cleanup_preserves_newer_turn_partial(tmp_path):
    agent_id = _make_agy_agent(persona="Overlap", session="overlap")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-a")
    agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-a", text="old partial")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-b")
    agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-b", text="new partial")
    assert not agents_db.delete_live_assistant_message(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-a")
    rows = agents_db.conn().execute(
        "SELECT text FROM messages WHERE agent_id=? AND role='assistant'",
        (agent_id,),).fetchall()
    assert [row["text"] for row in rows] == ["new partial"]
    assert agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-a", text="stale reclaim") is None
    rows = agents_db.conn().execute(
        "SELECT text FROM messages WHERE agent_id=? AND role='assistant'",
        (agent_id,),).fetchall()
    assert [row["text"] for row in rows] == ["new partial"]


def test_trace_cleanup_does_not_delete_stable_durable_row(tmp_path):
    agent_id = _make_agy_agent(persona="Stable", session="stable")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        source_file="/fixture/transcript.jsonl",
        turns=[{"id": "stable-assistant", "role": "assistant",
                "text": "historical", "timestamp": "t"}],)
    assert not agents_db.delete_live_assistant_message(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="failed-turn")
    row = agents_db.conn().execute(
        "SELECT text FROM messages WHERE message_id='stable-assistant'").fetchone()
    assert row["text"] == "historical"


def test_turn_owned_restore_recovers_same_id_mutation(tmp_path):
    agent_id = _make_agy_agent(persona="Restore", session="restore")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-a")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=_FAKE_CONV, source_file="fixture",
        turns=[{"id": "stable", "role": "assistant", "text": "before",
                "timestamp": "t1"}])
    snapshot = agents_db.capture_assistant_state(
        agent_id=agent_id, backend_session_id=_FAKE_CONV)
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=_FAKE_CONV, source_file="fixture",
        turns=[{"id": "stable", "role": "assistant", "text": "mutated",
                "timestamp": "t2"}])
    assert agents_db.restore_assistant_state(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-a", snapshot=snapshot)
    assert agents_db.conn().execute(
        "SELECT text FROM messages WHERE message_id='stable'").fetchone()["text"] \
        == "before"


def test_preempted_turn_cannot_restore_over_new_owner(tmp_path):
    agent_id = _make_agy_agent(persona="Fence", session="fence")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-a")
    snapshot = agents_db.capture_assistant_state(
        agent_id=agent_id, backend_session_id=_FAKE_CONV)
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-b")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=_FAKE_CONV, source_file="fixture-b",
        turns=[{"id": "turn-b-message", "role": "assistant", "text": "B",
                "timestamp": "t"}])
    assert not agents_db.restore_assistant_state(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-a", snapshot=snapshot)
    assert agents_db.conn().execute(
        "SELECT text FROM messages WHERE message_id='turn-b-message'").fetchone()["text"] \
        == "B"


def test_terminal_commit_serializes_against_transcript_import(tmp_path, monkeypatch):
    agent_id = _make_agy_agent(persona="Atomic", session="atomic")
    trace_id = _open_owned_turn(agent_id, "atomic-turn")
    snapshot = agents_db.begin_agy_assistant_turn(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id=trace_id, observed_assistant_count=0)
    assert snapshot is not None
    entered = threading.Event()
    release = threading.Event()
    imported = threading.Event()
    original = message_store._restore_assistant_state_txn

    def pause_restore(*args, **kwargs):
        restored = original(*args, **kwargs)
        entered.set()
        assert release.wait(2)
        return restored

    monkeypatch.setattr(message_store, "_restore_assistant_state_txn", pause_restore)
    commit_result = []
    commit_thread = threading.Thread(target=lambda: commit_result.append(
        agents_db.commit_agy_assistant_turn(
            agent_id=agent_id, backend_session_id=_FAKE_CONV,
            trace_id=trace_id, snapshot=snapshot,
            terminal_status="success", text="final")))
    commit_thread.start()
    assert entered.wait(2)

    def import_provider():
        agents_db.store_transcript_turns(
            agent_id=agent_id, backend_session_id=_FAKE_CONV,
            source_file="agy-transcript",
            turns=[{"role": "assistant", "text": "provisional",
                    "timestamp": "t"}])
        imported.set()

    import_thread = threading.Thread(target=import_provider)
    import_thread.start()
    assert not imported.wait(0.1), "import interleaved inside terminal transaction"
    release.set()
    commit_thread.join(2)
    import_thread.join(2)
    assert imported.is_set()
    assert commit_result and commit_result[0]["text"] == "final"
    rows = agents_db.conn().execute(
        "SELECT text,source_file FROM messages WHERE agent_id=? AND role='assistant'",
        (agent_id,),).fetchall()
    assert [(row["text"], row["source_file"]) for row in rows] == [
        ("final", f"final:{trace_id}")]


def test_restore_preserves_concurrent_baseline_team_inbox_progress(tmp_path):
    agent_id = _make_agy_agent(persona="Inbox", session="inbox")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-a")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=_FAKE_CONV, source_file="fixture",
        turns=[{"id": "stable", "role": "assistant", "text": "before",
                "timestamp": "t"}])
    agents_db.conn().execute(
        """INSERT INTO team_messages (
               team_message_id,team_id,source_agent_id,source_message_id,
               trace_id,text,created_at)
           VALUES ('tm-base','team',?,'stable','t','before',1)""", (agent_id,))
    agents_db.conn().execute(
        "INSERT INTO team_inbox (team_message_id,agent_id,status) "
        "VALUES ('tm-base',?,'unread')", (agent_id,))
    snapshot = agents_db.capture_assistant_state(
        agent_id=agent_id, backend_session_id=_FAKE_CONV)
    agents_db.conn().execute(
        "UPDATE team_inbox SET status='read', read_at=2 "
        "WHERE team_message_id='tm-base'")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=_FAKE_CONV, source_file="fixture",
        turns=[{"id": "stable", "role": "assistant", "text": "mutated",
                "timestamp": "t2"}])
    assert agents_db.restore_assistant_state(
        agent_id=agent_id, backend_session_id=_FAKE_CONV,
        trace_id="turn-a", snapshot=snapshot)
    inbox = agents_db.conn().execute(
        "SELECT status,read_at FROM team_inbox WHERE team_message_id='tm-base'").fetchone()
    assert (inbox["status"], inbox["read_at"]) == ("read", 2)


def test_retry_baseline_does_not_reimport_failed_provider_transcript(
    tmp_path, monkeypatch,
):
    agent_id = _make_agy_agent(persona="RetryBase", session="retrybase")
    agy_home = tmp_path / "agy-home"
    monkeypatch.setenv("CLAUDE_PWA_AGY_HOME", str(agy_home))
    _write_agy_transcript(agy_home, [
        {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "t",
         "content": "failed provider text"},
    ])
    _open_owned_turn(agent_id, "retry")
    state = agy_runner._TurnState(evidence_scope={"agent_id": agent_id})
    agy_runner._bind_session(
        _FAKE_CONV, state, trace_id="retry", on_session_init=lambda _sid: True)
    assert state.baseline_snapshot["messages"] == []


@pytest.mark.parametrize("response", ["", "ok"])
def test_empty_string_is_valid_but_missing_response_is_error(
    fake_agy, tmp_path, response,
):
    agent_id = _make_agy_agent(persona="Empty", session=f"empty-{response or 'blank'}")
    trace_id = _open_owned_turn(agent_id, f"empty-{response or 'blank'}")
    fake_agy(_stream(response))
    results, errors = [], []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="empty",
        on_result=results.append, on_error=errors.append, trace_id=trace_id)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(results) == 1)
    assert results[0]["last_agent_message"] == response
    assert errors == []


def test_authoritative_empty_response_retracts_streamed_partial(
    fake_agy, tmp_path, monkeypatch,
):
    agent_id = _make_agy_agent(persona="Retract", session="retract")
    _open_owned_turn(agent_id, "retract-trace")
    agy_home = tmp_path / "agy-home"
    monkeypatch.setenv("CLAUDE_PWA_AGY_HOME", str(agy_home))
    _write_agy_transcript(agy_home, [
        {"step_index": 0, "type": "USER_INPUT", "created_at": "t1",
         "content": "<USER_REQUEST>hi</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "t2",
         "content": "provisional"},
    ])
    rows = [
        {"event": "init", "conversation_id": _FAKE_CONV, "init": {}},
        {"event": "step_update", "step_update": {
            "conversation_id": _FAKE_CONV, "step_index": 1,
            "state": "ACTIVE", "step_type": "agent_response",
            "text_delta": "provisional"}},
        {"event": "result", "result": {
            "conversation_id": _FAKE_CONV, "status": "SUCCESS",
            "response": "", "usage": {}}},
    ]
    fake_agy("\n".join(json.dumps(row) for row in rows) + "\n")
    results = []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="retract",
        trace_id="retract-trace", on_result=results.append)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(results) == 1)
    live = agents_db.conn().execute(
        "SELECT 1 FROM messages WHERE agent_id=? AND source_file LIKE 'live:%'",
        (agent_id,),).fetchall()
    assert live == []


@pytest.mark.parametrize("terminal,response,expected", [
    ("success", "authoritative", ["authoritative"]),
    ("empty", "", []),
    ("error", "provisional", []),
])
def test_post_terminal_log_import_respects_turn_authority(
    fake_agy, tmp_path, monkeypatch, terminal, response, expected,
):
    session = f"post-terminal-{terminal}"
    agent_id = _make_agy_agent(persona=terminal, session=session)
    agents_db.bind_backend_session(agent_id, _FAKE_CONV)
    trace_id = _open_owned_turn(agent_id, f"trace-{terminal}")
    agy_home = tmp_path / f"agy-{terminal}"
    monkeypatch.setenv("CLAUDE_PWA_AGY_HOME", str(agy_home))
    if terminal == "error":
        stream = _stream(response, status="ERROR", error="provider failed")
    else:
        stream = _stream(response)
    fake_agy(stream)
    results, errors = [], []
    handle = agy_runner.spawn_turn(
        text="prompt", cwd=tmp_path, backend_session_id=_FAKE_CONV,
        agent_id=agent_id, session=session, trace_id=trace_id,
        on_result=results.append, on_error=errors.append)
    handle.wait(timeout=8)
    assert _wait_for(lambda: bool(results or errors))

    # The real provider transcript arrives after the stream terminal. Its
    # PLANNER_RESPONSE is provisional input, not a second source of truth.
    _write_agy_transcript(agy_home, [
        {"step_index": 0, "type": "USER_INPUT", "created_at": "t1",
         "content": "<USER_REQUEST>prompt</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "t2",
         "content": response or "provisional resurrection"},
    ])
    loaded = _load_agy_conversation(session)
    assert [turn["text"] for turn in loaded["turns"]
            if turn["role"] == "assistant"] == expected
    rows = agents_db.conn().execute(
        """SELECT text,source_file FROM messages
             WHERE agent_id=? AND role='assistant'""", (agent_id,),).fetchall()
    assert [row["text"] for row in rows] == expected
    if expected:
        assert rows[0]["source_file"] == f"final:{trace_id}"


def test_malformed_result_calls_one_parser_error(fake_agy, tmp_path):
    agent_id = _make_agy_agent(persona="Bad", session="bad")
    rows = [
        {"event": "init", "conversation_id": _FAKE_CONV, "init": {}},
        {"event": "result", "result": {"conversation_id": _FAKE_CONV,
                                          "status": "SUCCESS", "response": 7}},
    ]
    fake_agy("\n".join(json.dumps(row) for row in rows) + "\n", rc=9)
    results, errors = [], []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="bad",
        on_result=results.append, on_error=errors.append)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(errors) == 1)
    assert "response must be a string" in errors[0]
    assert results == []


def test_drain_exception_after_result_forces_error(fake_agy, tmp_path, monkeypatch):
    agent_id = _make_agy_agent(persona="Drain", session="drain")
    fake_agy(_stream("would-be success"))
    monkeypatch.setattr(
        agy_runner, "stderr_text",
        lambda _proc: (_ for _ in ()).throw(RuntimeError("stderr drain failed")))
    results, errors = [], []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="drain",
        on_result=results.append, on_error=errors.append)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(errors) == 1)
    assert "drain error after result" in errors[0]
    assert results == []


def test_derived_failure_restores_full_turn_state(fake_agy, tmp_path, monkeypatch):
    agent_id = _make_agy_agent(persona="Derived", session="derived")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=_FAKE_CONV, source_file="fixture",
        turns=[{"id": "stable", "role": "assistant", "text": "before",
                "timestamp": "t0"}])
    trace_id = _open_owned_turn(agent_id, "derived-failure")
    fake_agy(_stream("terminal text"))

    def fail_after_derived(**kwargs):
        database = agents_db.conn()
        database.execute(
            "UPDATE messages SET text='mutated' WHERE message_id='stable'")
        database.execute(
            """INSERT INTO team_messages (
                   team_message_id,team_id,source_agent_id,source_message_id,
                   trace_id,text,created_at)
               VALUES ('tm-derived','team',?,? ,?,'terminal text',1)""",
            (agent_id, kwargs["row"]["id"], trace_id))
        database.execute(
            "INSERT INTO team_inbox (team_message_id,agent_id,status) "
            "VALUES ('tm-derived',?,'unread')", (agent_id,))
        raise RuntimeError("derived hook failed")

    monkeypatch.setattr(
        agents_db, "apply_final_assistant_side_effects", fail_after_derived)
    results, errors = [], []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="derived",
        trace_id=trace_id, on_result=results.append, on_error=errors.append)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(errors) == 1)
    assert results == []
    rows = agents_db.conn().execute(
        "SELECT message_id,text FROM messages WHERE agent_id=? AND role='assistant'",
        (agent_id,),).fetchall()
    assert [(row["message_id"], row["text"]) for row in rows] == [
        ("stable", "before")]
    assert agents_db.conn().execute(
        "SELECT 1 FROM team_messages WHERE team_message_id='tm-derived'",
    ).fetchone() is None
    assert agents_db.conn().execute(
        "SELECT 1 FROM team_inbox WHERE team_message_id='tm-derived'",
    ).fetchone() is None


def test_provider_error_preserves_classifier_boundary(fake_agy, tmp_path):
    for idx, (message, category) in enumerate([
        ("HTTP 429 rate limited", error_classify.TRANSIENT),
        ("billing hard limit reached", error_classify.USAGE_LIMIT),
    ]):
        agent_id = _make_agy_agent(persona=f"E{idx}", session=f"e{idx}")
        fake_agy(_stream("", status="ERROR", error=message))
        errors = []
        handle = agy_runner.spawn_turn(
            text="hi", cwd=tmp_path, agent_id=agent_id, session=f"e{idx}",
            on_error=errors.append)
        handle.wait(timeout=8)
        assert _wait_for(lambda: len(errors) == 1)
        assert error_classify.classify_error(errors[0]) == category


@pytest.mark.parametrize("fixture_name,expected", [
    ("1.1.21-success.jsonl", "FIXTURE_OK\n"),
    ("1.1.21-tool.jsonl", "TOOL_DONE\n"),
])
def test_real_1_1_21_fixtures(fake_agy, tmp_path, fixture_name, expected):
    agent_id = _make_agy_agent(persona=fixture_name, session=fixture_name)
    trace_id = _open_owned_turn(agent_id, f"fixture-{fixture_name}")
    fake_agy((_FIXTURES / fixture_name).read_text())
    results, errors = [], []
    handle = agy_runner.spawn_turn(
        text="fixture", cwd=tmp_path, agent_id=agent_id,
        session=fixture_name, on_result=results.append, on_error=errors.append,
        trace_id=trace_id)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(results) == 1)
    assert results[0]["last_agent_message"] == expected
    assert errors == []
    usage = results[0]["agy_reported_usage"]
    if fixture_name == "1.1.21-success.jsonl":
        assert results[0]["duration_ms"] == 1157
        assert usage["terminal_values"] == {
            "input_tokens": 18821, "output_tokens": 5,
            "thinking_tokens": 0, "cache_read_input_tokens": 0,
            "total_tokens": 18826,
        }
        assert len(usage["step_event_refs"]) == 1
    else:
        assert results[0]["duration_ms"] == 2511
        assert usage["normalized_step_sum"] == {
            "input_tokens": 21549, "output_tokens": 84,
            "thinking_tokens": 0, "cache_read_input_tokens": 16286,
            "total_tokens": 21633,
        }
        tool_rows = agents_db.conn().execute(
            "SELECT detail FROM state_log WHERE agent_id=? AND kind=?",
            (agent_id, AgentState.TOOL),).fetchall()
        assert len(tool_rows) == 2
        for tool_row in tool_rows:
            evidence = json.loads(tool_row["detail"])["agy_raw_evidence"]
            assert evidence["provider_event_ref"]
            assert evidence["turn_execution_id"]


def test_provider_event_revision_dedupe_conflict_and_stale_order():
    st = agy_runner._TurnState(evidence_scope={
        "provider_instance_id": "computer:agy",
        "account_auth_generation": None,
        "turn_execution_id": "computer:turn:1",
    })
    event = {"event": "init", "conversation_id": _FAKE_CONV, "init": {}}
    first = agy_runner._provider_evidence(event, 1, st)
    assert first is not None
    assert agy_runner._provider_evidence(event, 1, st) is None
    with pytest.raises(ValueError, match="conflicting"):
        agy_runner._provider_evidence(
            {**event, "init": {"model": "different"}}, 1, st)
    assert agy_runner._provider_evidence(
        {"event": "future", "conversation_id": _FAKE_CONV}, 3, st) is not None
    assert agy_runner._provider_evidence(
        {"event": "late", "conversation_id": _FAKE_CONV}, 2, st) is None
    other_turn = agy_runner._TurnState(evidence_scope={
        "provider_instance_id": "computer:agy",
        "account_auth_generation": None,
        "turn_execution_id": "computer:turn:2",
    })
    second = agy_runner._provider_evidence(event, 1, other_turn)
    assert second["provider_event_ref"] != first["provider_event_ref"]


def test_drain_dedupes_identical_text_and_tool_events(
    fake_agy, tmp_path, monkeypatch,
):
    agent_id = _make_agy_agent(persona="Replay", session="replay")
    trace_id = _open_owned_turn(agent_id, "replay-dedupe")
    lines = [line for line in _stream("DONE", tool=True).splitlines() if line]
    events = [json.loads(line) for line in lines]
    duplicated = []
    for event in events:
        duplicated.append(event)
        step = event.get("step_update") or {}
        if step.get("step_type") in {"tool", "agent_response"}:
            duplicated.append(event)
    fake_agy("\n".join(json.dumps(event) for event in duplicated) + "\n")
    writes = []
    original = agents_db.upsert_live_assistant_message

    def record(**kwargs):
        writes.append(kwargs["text"])
        return original(**kwargs)

    monkeypatch.setattr(agents_db, "upsert_live_assistant_message", record)
    results = []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="replay",
        on_result=results.append, trace_id=trace_id)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(results) == 1)
    assert writes == ["DONE"]
    tool_rows = agents_db.conn().execute(
        "SELECT detail FROM state_log WHERE agent_id=? AND kind=?",
        (agent_id, AgentState.TOOL),).fetchall()
    assert len(tool_rows) == 2  # one ACTIVE and one DONE, duplicate copies dropped


def test_identical_active_text_deltas_remain_ordered(fake_agy, tmp_path, monkeypatch):
    agent_id = _make_agy_agent(persona="Repeat", session="repeat")
    trace_id = _open_owned_turn(agent_id, "repeat-deltas")
    rows = [
        {"event": "init", "conversation_id": _FAKE_CONV, "init": {}},
        {"event": "step_update", "step_update": {
            "conversation_id": _FAKE_CONV, "step_index": 1,
            "state": "ACTIVE", "step_type": "agent_response", "text_delta": "ha"}},
        {"event": "step_update", "step_update": {
            "conversation_id": _FAKE_CONV, "step_index": 1,
            "state": "ACTIVE", "step_type": "agent_response", "text_delta": "ha"}},
        {"event": "result", "result": {
            "conversation_id": _FAKE_CONV, "status": "SUCCESS",
            "response": "haha", "usage": {}}},
    ]
    fake_agy("\n".join(json.dumps(row) for row in rows) + "\n")
    writes = []
    original = agents_db.upsert_live_assistant_message

    def record(**kwargs):
        writes.append(kwargs["text"])
        return original(**kwargs)

    monkeypatch.setattr(agents_db, "upsert_live_assistant_message", record)
    results = []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="repeat",
        on_result=results.append, trace_id=trace_id)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(results) == 1)
    assert writes == ["ha"]
    assert agents_db.conn().execute(
        "SELECT text FROM messages WHERE agent_id=? AND role='assistant'",
        (agent_id,),).fetchone()["text"] == "haha"


def test_resumed_conversation_mismatch_is_single_terminal_error(fake_agy, tmp_path):
    agent_id = _make_agy_agent(persona="Mismatch", session="mismatch")
    fake_agy(_stream("wrong conversation"), rc=8)
    results, errors = [], []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path,
        backend_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        agent_id=agent_id, session="mismatch",
        on_result=results.append, on_error=errors.append)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(errors) == 1)
    assert "does not match resumed turn" in errors[0]
    assert results == []


def test_rejected_init_cannot_persist_or_speak(fake_agy, tmp_path):
    agent_id = _make_agy_agent(persona="Reject", session="reject")
    fake_agy(_stream("<speak>must not leak</speak>"))
    results, errors = [], []
    handle = agy_runner.spawn_turn(
        text="hi", cwd=tmp_path, agent_id=agent_id, session="reject",
        on_session_init=lambda _sid: False,
        on_result=results.append, on_error=errors.append)
    handle.wait(timeout=8)
    assert _wait_for(lambda: len(errors) == 1)
    assert results == []
    assert _queued_texts(agent_id) == []
    assert agents_db.conn().execute(
        "SELECT 1 FROM messages WHERE agent_id=? AND role='assistant'",
        (agent_id,),).fetchall() == []


def test_preempted_owner_gate_blocks_all_stream_side_effects(fake_agy, tmp_path):
    agent_id = _make_agy_agent(persona="Preempt", session="preempt")
    fake_agy(_stream("<speak>must not leak</speak>", tool=True))
    events = []
    stream = type("Stream", (), {"broadcast": lambda _self, event: events.append(event)})()
    results, errors = [], []
    sessions = []
    with pytest.raises(RuntimeError, match="ownership lost"):
        agy_runner.spawn_turn(
            text="hi", cwd=tmp_path, agent_id=agent_id, session="preempt",
            trace_id="turn-a", stream=stream,
            run_if_owned=lambda _action: False,
            on_session_init=sessions.append,
            on_result=results.append, on_error=errors.append)
    assert results == [] and errors == []
    assert sessions == []
    assert _queued_texts(agent_id) == []
    assert events == []
    assert agents_db.conn().execute(
        "SELECT 1 FROM messages WHERE agent_id=? AND role='assistant'",
        (agent_id,),).fetchall() == []
    assert agents_db.conn().execute(
        "SELECT 1 FROM state_log WHERE agent_id=? AND kind=?",
        (agent_id, AgentState.THINKING),).fetchall() == []
