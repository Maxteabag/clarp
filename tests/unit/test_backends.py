"""Tests for `lib.backends` — the AI-CLI backend registry that routes a
turn to Claude (clarp_runner) or Codex (codex_runner) and picks the right
history parser per backend.
"""
from __future__ import annotations

import pathlib
import sys

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import backends          # noqa: E402


def test_normalize_known_and_unknown():
    assert backends.normalize("claude") == "claude"
    assert backends.normalize("codex") == "codex"
    assert backends.normalize("CODEX") == "codex"
    assert backends.normalize("agy") == "agy"
    assert backends.normalize("AGY") == "agy"
    assert backends.normalize("grok") == "grok"
    assert backends.normalize("opencode") == "opencode"
    assert backends.normalize("antigravity") == "agy"
    # Unknown / empty / None all fall back to the default so a malformed
    # agent row can never strand the user on a backend with no runner.
    assert backends.normalize("gemini") == backends.DEFAULT == "claude"
    assert backends.normalize("") == "claude"
    assert backends.normalize(None) == "claude"


def test_is_valid_and_label():
    assert backends.is_valid("codex") is True
    assert backends.is_valid("agy") is True
    assert backends.is_valid("grok") is True
    assert backends.is_valid("opencode") is True
    assert backends.is_valid("nope") is False
    assert backends.label("codex") == "Codex"
    assert backends.label("claude") == "Claude"
    assert backends.label("agy") == "Antigravity"
    assert backends.label("grok") == "Grok"
    assert backends.label("opencode") == "OpenCode"
    assert backends.label("junk") == "Claude"   # falls back to default label


def test_capabilities_are_explicit_per_backend():
    assert backends.capabilities("claude").supports_fork is True
    assert backends.capabilities("claude").supports_transcript_streaming is True
    assert backends.capabilities("claude").required_binary == "claude"
    assert backends.capabilities("codex").supports_fork is False
    assert backends.capabilities("codex").supports_transcript_streaming is False
    assert backends.capabilities("agy").supports_fork is False
    assert backends.capabilities("agy").supports_transcript_streaming is False
    assert backends.capabilities("grok").required_binary == "grok"
    assert backends.capabilities("opencode").required_binary == "opencode"
    assert backends.ids() == ("claude", "codex", "agy", "grok", "opencode")


def test_claude_capability_uses_configured_cli(monkeypatch):
    from lib import clarp_runner

    monkeypatch.setattr(clarp_runner, "configured_claude_bin", lambda: "clarp")
    assert backends.capabilities("claude").required_binary == "clarp"


def test_spawn_turn_routes_grok_and_opencode(monkeypatch):
    calls: dict[str, dict] = {}
    from lib import grok_runner, opencode_runner

    def fake_grok(**kw):
        calls["grok"] = kw
        return "g"

    def fake_opencode(**kw):
        calls["opencode"] = kw
        return "o"

    monkeypatch.setattr(grok_runner, "spawn_turn", fake_grok)
    monkeypatch.setattr(opencode_runner, "spawn_turn", fake_opencode)
    assert backends.spawn_turn("grok", text="hi", cwd=pathlib.Path("/tmp")) == "g"
    assert backends.spawn_turn("opencode", text="yo", cwd=pathlib.Path("/tmp")) == "o"
    assert calls["grok"]["text"] == "hi"
    assert calls["opencode"]["text"] == "yo"


def test_spawn_turn_routes_to_the_right_runner(monkeypatch):
    """spawn_turn dispatches by backend; both stream-driven Codex and the
    Claude live-partial path receive the SSE stream."""
    calls: dict[str, dict] = {}

    from lib import clarp_runner, codex_app_server

    def fake_clarp(**kw):
        calls["claude"] = kw
        return "clarp-handle"

    def fake_codex(**kw):
        calls["codex"] = kw
        return "codex-handle"

    monkeypatch.setattr(clarp_runner, "spawn_turn", fake_clarp)
    monkeypatch.setattr(codex_app_server, "spawn_turn", fake_codex)

    h1 = backends.spawn_turn("claude", text="hi", cwd=pathlib.Path("/tmp"),
                             agent_id="a1", stream="SSE", session="mike")
    assert h1 == "clarp-handle"
    assert calls["claude"]["stream"] == "SSE"
    assert calls["claude"]["text"] == "hi"

    h2 = backends.spawn_turn("codex", text="yo", cwd=pathlib.Path("/tmp"),
                             agent_id="a2", stream="SSE", session="rachel")
    assert h2 == "codex-handle"
    # Codex keeps the stream so it can push transcript-updated SSEs.
    assert calls["codex"]["stream"] == "SSE"


def test_interrupt_routes_by_backend(monkeypatch):
    from lib import clarp_runner, codex_app_server
    seen: list[str] = []
    monkeypatch.setattr(clarp_runner, "interrupt",
                        lambda aid: seen.append(f"claude:{aid}") or 1)
    monkeypatch.setattr(codex_app_server, "interrupt",
                        lambda aid: seen.append(f"codex:{aid}") or 2)

    assert backends.interrupt("claude", "x") == 1
    assert backends.interrupt("codex", "y") == 2
    assert backends.interrupt_any("z") == 3   # both registries poked
    assert seen == ["claude:x", "codex:y", "claude:z", "codex:z"]


def test_active_handles_routes_by_backend(monkeypatch):
    from lib import agy_runner, clarp_runner, codex_app_server
    monkeypatch.setattr(clarp_runner, "active_handles", lambda aid: [f"claude:{aid}"])
    monkeypatch.setattr(codex_app_server, "active_handles", lambda aid: [f"codex:{aid}"])
    monkeypatch.setattr(agy_runner, "active_handles", lambda aid: [f"agy:{aid}"])

    assert backends.active_handles("claude", "x") == ["claude:x"]
    assert backends.active_handles("codex", "x") == ["codex:x"]
    assert backends.active_handles("agy", "x") == ["agy:x"]


def test_history_dispatch_picks_codex_parser(monkeypatch):
    from lib import codex_transcript
    from lib import transcript_log

    monkeypatch.setattr(codex_transcript, "find_latest_jsonl",
                        lambda sid: pathlib.Path(f"/codex/{sid}.jsonl"))
    monkeypatch.setattr(transcript_log, "find_latest_jsonl",
                        lambda sid, projects_root=None: pathlib.Path(f"/claude/{sid}.jsonl"))

    assert str(backends.find_session_jsonl("codex", "s1")) == "/codex/s1.jsonl"
    assert str(backends.find_session_jsonl("claude", "s2")) == "/claude/s2.jsonl"


def test_resume_transcript_dispatch_picks_agy_parser(monkeypatch):
    from lib import agy_transcript
    monkeypatch.setattr(agy_transcript, "find_latest_jsonl",
                        lambda sid: pathlib.Path(f"/agy/{sid}/transcript.jsonl"))

    assert str(backends.find_resume_transcript(
        "agy", "conversation-7", cwd="/tmp",
    )) == "/agy/conversation-7/transcript.jsonl"


def test_stream_kwargs_strips_synthesize_audio():
    """Regression: agy/codex turns crashed with `spawn_turn() got an
    unexpected keyword argument 'synthesize_audio'`. turn_dispatch passes
    synthesize_audio (for the Claude path); the stream runners read it from
    the DB instead, so backends must strip it before delegating to them."""
    out = backends._stream_kwargs({
        "text": "hi", "cwd": "/tmp", "stream": None,
        "voice_preamble": True, "synthesize_audio": True,
    })
    assert "synthesize_audio" not in out, "synthesize_audio must be stripped"
    # The args the stream runners DO accept survive.
    assert out["text"] == "hi"
    assert out["stream"] is None
    assert out["voice_preamble"] is True
