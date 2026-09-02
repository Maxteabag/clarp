"""Grok Build headless runner tests."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import sys

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import grok_runner  # noqa: E402
from lib import grok_transcript  # noqa: E402


def test_build_cmd_fresh_and_resume():
    fresh = grok_runner.build_cmd("sid-1", is_new_session=True, model="grok-4.6",
                                  effort="high")
    assert fresh[0] == "grok"
    assert "--output-format" in fresh
    assert fresh[fresh.index("--session-id") + 1] == "sid-1"
    assert fresh[fresh.index("--model") + 1] == "grok-4.6"
    assert fresh[fresh.index("--reasoning-effort") + 1] == "high"
    resume = grok_runner.build_cmd("sid-1")
    assert resume[resume.index("--resume") + 1] == "sid-1"
    assert "--session-id" not in resume


def _install_fake_grok(tmp_bin: pathlib.Path, stdout: str, *, rc: int = 0) -> None:
    fake = tmp_bin / "grok"
    payload = json.dumps(stdout)
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "out = os.environ.get('GROK_FAKE_ARGV_OUT')\n"
        "if out:\n"
        "    json.dump(sys.argv, open(out, 'w'))\n"
        f"sys.stdout.write({payload})\n"
        f"raise SystemExit({rc})\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def test_spawn_turn_binds_session_and_results(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("GROK_FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    events = [
        {"type": "turn_started", "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        {"type": "assistant", "content": "<speak>Hi from Grok.</speak>"},
    ]
    _install_fake_grok(bin_dir, "".join(json.dumps(row) + "\n" for row in events))
    results: list[dict] = []
    sessions: list[str] = []
    handle = grok_runner.spawn_turn(
        text="hello", cwd=tmp_path,
        on_session_init=lambda sid: sessions.append(sid) or True,
        on_result=results.append, enqueue=lambda **_k: 1,
    )
    handle.drain_thread.join(timeout=5)
    assert results
    assert "Hi from Grok" in results[0]["last_agent_message"]
    assert sessions
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[0].endswith("grok")
    assert "-p" in argv


def test_missing_grok_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        grok_runner.spawn_turn(text="hi", cwd=tmp_path)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError as error:
        assert "grok" in str(error)


def test_parse_turns_and_list_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PWA_GROK_HOME", str(tmp_path))
    cwd = "/tmp/proj"
    encoded = grok_transcript._encode_cwd(cwd)
    session_dir = tmp_path / "sessions" / encoded / "sess-1"
    session_dir.mkdir(parents=True)
    (session_dir / "chat_history.jsonl").write_text(
        json.dumps({"type": "user", "content": "please fix it"}) + "\n"
        + json.dumps({"type": "assistant", "content": "done",
                      "tool_calls": [{"name": "Edit", "arguments": {}}]}) + "\n"
    )
    (session_dir / "summary.json").write_text(json.dumps({
        "generated_title": "Fix it",
    }))
    turns = grok_transcript.parse_turns(session_dir / "chat_history.jsonl")
    assert turns[0]["role"] == "user"
    assert turns[1]["role"] == "assistant"
    assert turns[1]["tools"][0]["name"] == "Edit"
    listed = grok_transcript.list_sessions(cwd, home=tmp_path)
    assert listed[0]["id"] == "sess-1"
    assert listed[0]["title"] == "Fix it"
    assert grok_transcript.find_latest_jsonl("sess-1", home=tmp_path)
