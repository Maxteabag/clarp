"""Muse Code headless runner (``muse exec --json``).

Event fixtures are compacted real muse 1.4.13 output (tests/fixtures/muse):
one ``read_file`` tool call answered with "2", and one echo-provider turn.
docs/architecture/muse-backend.md is the contract.
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import sys
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from lib import agents as agents_db  # noqa: E402
from lib import turn_lifecycle  # noqa: E402
from lib.backend import muse  # noqa: E402
from lib.backend.registry import by_id  # noqa: E402
from lib.protocol import AgentState, SSEType  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "muse"
MUSE = by_id("muse")
SID = "06a488a5-eb75-4aff-9c34-7ef0b3c89246"


@pytest.fixture(autouse=True)
def _fake_clis_are_this_tests_own(fake_backend_cli, tmp_path):
    """This module's fake ``muse`` is a file it writes under tmp_path."""
    fake_backend_cli.dir(tmp_path)


def _events(name: str = "exec-tool-call.jsonl") -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line]


def _envelope(payload_type: str, **payload) -> dict:
    return {"schema_version": 1, "stream": {"kind": "session", "id": SID}, "sequence": 1,
            "record_type": "event", "payload_type": payload_type, "payload": payload}


def _delta(text: str) -> dict:
    return _envelope("run.output.delta", kind="run_output_delta", text=text)


def _terminal(state: str, reason=None, text="") -> dict:
    return _envelope(f"run.terminal.{state}", kind="run_terminal", terminal=state,
                     reason=reason, text=text)


class _Stream:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def broadcast(self, event: dict) -> None:
        self.events.append(event)


_STATES: list = []


@pytest.fixture(autouse=True)
def _events_fed_directly_end_their_turn(monkeypatch):
    """``_run`` feeds events without the drain thread, so it also ends the
    turn the way the drain's ``finally`` does: no pacer outlives the test.
    Their cancelled timer threads get a bounded moment to exit; nothing is
    cancelled here, so a timer the code itself left running still shows."""
    from lib import live_pacing
    started: list = []
    real = live_pacing._thread_timer
    monkeypatch.setattr(live_pacing, "_thread_timer",
                        lambda delay, fn: started.append(real(delay, fn)) or started[-1])
    yield
    while _STATES:
        pacer = getattr(_STATES.pop(), "_live_pacer", None)
        if pacer is not None:
            pacer.cancel()
    for timer in started:
        if timer.finished.is_set():         # cancelled (or fired): let it exit
            timer.join(timeout=1.0)


def _run(events, *, stream=None, enqueue=None, on_error=None):
    st = muse._TurnState(session_id=SID)
    _STATES.append(st)
    for ev in events:
        MUSE._handle_event(ev, st, agent_id="agent-1", session="sess-1", trace_id="trace-1",
                           on_error=on_error, stream=stream,
                           enqueue=enqueue or (lambda **_k: 1))
    return st


@pytest.fixture
def quiet(monkeypatch):
    """Record lifecycle states and live rows without a database."""
    seen = {"states": [], "live": []}
    monkeypatch.setattr(turn_lifecycle, "transition",
                        lambda agent_id, event, detail=None: seen["states"].append(
                            (turn_lifecycle.target(event), (detail or {}).get("tool", ""))))
    monkeypatch.setattr(agents_db, "upsert_live_assistant_message",
                        lambda **kw: seen["live"].append(kw["text"]) or {"changed": True})
    monkeypatch.setattr(agents_db, "latest_turn_synthesize_audio", lambda agent_id: True)
    monkeypatch.setattr(agents_db, "get_by_agent_id",
                        lambda agent_id: {"persona": "Muse", "voice_id": "v1"})
    monkeypatch.setattr(agents_db, "get_focus", lambda: "agent-1")
    monkeypatch.setattr(agents_db, "get_trace", lambda agent_id: "")
    monkeypatch.setattr(MUSE, "live_text_interval", 0.0)
    return seen


# --- argv -------------------------------------------------------------------

def test_build_cmd_creates_and_continues_with_the_same_session_id():
    cmd = MUSE.build_cmd("sid-1", model="muse-wonder-1.0", effort="low")
    assert cmd[:3] == ["muse", "exec", "--json"]
    assert "--yolo" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "sid-1"
    assert cmd[cmd.index("--model") + 1] == "muse-wonder-1.0"
    assert cmd[cmd.index("--reasoning-effort") + 1] == "low"
    assert "resume" not in cmd and "--resume" not in cmd
    bare = MUSE.build_cmd("sid-1")
    assert "--model" not in bare and "--reasoning-effort" not in bare


# --- the real tool-call turn ------------------------------------------------

def test_the_real_turn_answers_counts_tokens_and_shows_the_tool(quiet):
    stream = _Stream()
    st = _run(_events(), stream=stream)
    assert st.last_agent_message == "2"
    assert st.model == "muse-wonder-1.0"
    assert (st.tokens_in, st.tokens_out) == (68449, 154)
    assert st.allowance["window"]["resets_at_ms"] == 1792927534000
    assert (AgentState.TOOL, "read_file") in quiet["states"]
    assert quiet["states"][0][0] == AgentState.THINKING
    assert quiet["live"][-1] == "2"
    assert stream.events and all(e["type"] == SSEType.TRANSCRIPT_UPDATED for e in stream.events)
    assert st.failed_error == ""


def test_reminder_tasks_and_workspace_facts_change_nothing(quiet):
    noise = [e for e in _events() if e["payload_type"].startswith(("task.", "session."))]
    st = _run(noise)
    assert st.live_text == "" and st.last_agent_message == ""
    assert quiet["live"] == []


def test_a_tool_result_closes_the_live_segment(quiet):
    tool = next(e for e in _events() if e["payload_type"] == "tool.result")
    st = _run([_delta("Let me look."), tool, _delta("Done.")])
    assert "Let me look." in quiet["live"]
    assert st.live_text == "Done."
    assert st.turn_text == "Let me look.Done."


def test_only_completed_speak_blocks_are_voiced_once(quiet):
    spoken: list[str] = []
    _run([_delta("<speak>Hi from"), _delta(" Muse.</speak> and"), _delta(" more prose.")],
         enqueue=lambda **kw: spoken.append(kw["text"]) or 1)
    assert spoken == ["Hi from Muse."]


@pytest.mark.parametrize("state", ["failed", "aborted"])
def test_a_failed_terminal_fails_the_turn_with_its_reason(quiet, state):
    errors: list[str] = []
    st = _run([_delta("partial"), _terminal(state, reason="provider stream closed")],
              on_error=errors.append)
    assert errors and "provider stream closed" in errors[0]
    assert st.failed_error == errors[0]


@pytest.mark.parametrize("state", ["cancelled", "interrupted"])
def test_a_stopped_terminal_is_not_an_error(quiet, state):
    errors: list[str] = []
    st = _run([_delta("partial"), _terminal(state)], on_error=errors.append)
    assert errors == [] and st.failed_error == ""
    assert st.stopped is True


# --- orchestrator routing -----------------------------------------------------

def test_routing_runs_without_a_session_log_and_reads_the_answer():
    cmd = MUSE.routing_cmd("pick one", model="m", effort="low")
    assert cmd[:3] == ["muse", "exec", "--json"]
    assert "--no-session-log" in cmd and "--session-id" not in cmd
    assert cmd[-2:] == ["--", "pick one"]
    stdout = "".join(json.dumps(e) + "\n" for e in _events())
    assert MUSE.routing_text(stdout) == "2"


# --- a real subprocess ----------------------------------------------------------

def _install_fake_muse(bin_dir: pathlib.Path, stdout: str, *, rc: int = 0, stderr: str = "") -> None:
    fake = bin_dir / "muse"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "out = os.environ.get('MUSE_FAKE_ARGV_OUT')\n"
        "if out:\n"
        "    json.dump(sys.argv, open(out, 'w'))\n"
        f"sys.stdout.write({json.dumps(stdout)})\n"
        f"sys.stderr.write({json.dumps(stderr)})\n"
        f"raise SystemExit({rc})\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fake_muse(tmp_path, monkeypatch, quiet):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("MUSE_FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    monkeypatch.setattr(MUSE, "broadcast_transcript", lambda *a, **k: None)

    def install(stdout: str = "".join(json.dumps(e) + "\n" for e in _events()), **kw):
        _install_fake_muse(bin_dir, stdout, **kw)
    install.argv = lambda: json.loads((tmp_path / "argv.json").read_text())
    return install


def test_a_new_agent_gets_a_minted_session_bound_before_the_run(tmp_path, fake_muse):
    fake_muse()
    sessions: list[str] = []
    results: list[dict] = []
    handle = MUSE.start_turn(text="-count lines", cwd=tmp_path, is_new_session=True,
                             on_session_init=lambda sid: sessions.append(sid) or True,
                             on_result=results.append, enqueue=lambda **_k: 1)
    handle.drain_thread.join(timeout=10)
    argv = fake_muse.argv()
    minted = argv[argv.index("--session-id") + 1]
    assert str(uuid.UUID(minted)) == minted
    assert sessions and sessions[0] == minted
    # The prompt (with Clarp's usual preamble) follows "--", so a leading dash
    # is never read as a flag.
    assert argv[-2] == "--" and argv[-1].endswith("-count lines")
    assert results and results[0]["last_agent_message"] == "2"
    assert results[0]["usage"] == {"input_tokens": 68449, "output_tokens": 154}


def test_a_resumed_agent_continues_its_own_session(tmp_path, fake_muse):
    fake_muse()
    results: list[dict] = []
    handle = MUSE.start_turn(text="again", cwd=tmp_path, backend_session_id=SID,
                             on_session_init=lambda sid: True,
                             on_result=results.append, enqueue=lambda **_k: 1)
    handle.drain_thread.join(timeout=10)
    argv = fake_muse.argv()
    assert argv[argv.index("--session-id") + 1] == SID
    assert results


def test_a_nonzero_exit_reports_stderr(tmp_path, fake_muse):
    fake_muse("", rc=1, stderr="muse: not logged in; run `muse login`")
    errors: list[str] = []
    results: list[dict] = []
    handle = MUSE.start_turn(text="hi", cwd=tmp_path, backend_session_id=SID,
                             on_session_init=lambda sid: True, on_error=errors.append,
                             on_result=results.append, enqueue=lambda **_k: 1)
    handle.drain_thread.join(timeout=10)
    assert errors and "muse login" in errors[0]
    assert results == []


def test_a_missing_muse_binary_says_how_to_get_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="muse"):
        MUSE.start_turn(text="hi", cwd=tmp_path)


# --- teardown: no live-text timer outlives the turn ---------------------------

@pytest.fixture
def pacer_timers(monkeypatch):
    """Every timer a live pacer starts during the test."""
    from lib import live_pacing
    started: list = []
    real = live_pacing._thread_timer

    def record(delay, fn):
        timer = real(delay, fn)
        started.append(timer)
        return timer
    monkeypatch.setattr(live_pacing, "_thread_timer", record)
    return started


def _stream_then(*events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


@pytest.mark.parametrize("ending", ["failed terminal", "nonzero exit", "drain error"])
def test_a_turn_that_ends_with_held_text_leaves_no_pacer_timer(
        tmp_path, fake_muse, pacer_timers, monkeypatch, ending):
    # "Done." has no word break, so the pacer holds it back; whatever ends
    # the turn, its timer must not keep running into the next one.
    if ending == "failed terminal":
        fake_muse(_stream_then(_delta("Done."), _terminal("failed", reason="boom")))
    elif ending == "nonzero exit":
        fake_muse(_stream_then(_delta("Done.")), rc=1, stderr="muse: crashed")
    else:
        fake_muse(_stream_then(_delta("Done."), _delta(" And")))
        calls = iter([True])
        monkeypatch.setattr(agents_db, "latest_turn_synthesize_audio",
                            lambda agent_id: next(calls))     # 2nd delta raises
    handle = MUSE.start_turn(text="hi", cwd=tmp_path, backend_session_id=SID,
                             agent_id="agent-1", session="sess-1",
                             on_session_init=lambda sid: True, on_error=lambda e: None,
                             on_result=lambda r: None, enqueue=lambda **_k: 1)
    handle.drain_thread.join(timeout=10)
    assert pacer_timers, "the held text never reached a pacer"
    for timer in pacer_timers:
        timer.join(timeout=0.2)
    assert [t for t in pacer_timers if t.is_alive()] == []
