"""Native conversation forks per backend."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import backends, codex_app_server
from lib.backend.base import Unsupported

FAKE_CODEX = Path(__file__).resolve().parents[2] / "tests/qa/fake_codex.py"


def test_claude_forks_into_the_childs_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / ".claude" / "projects" / "-src" / "conv.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"type": "user", "sessionId": "conv"}) + "\n")
    new = backends.get("claude").fork_conversation("conv", source_cwd="/src", cwd="/dst")
    copied = tmp_path / ".claude" / "projects" / "-dst" / f"{new}.jsonl"
    assert new != "conv" and json.loads(copied.read_text())["sessionId"] == new


def test_codex_forks_through_the_app_server(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CLARP_QA_PROVIDER_ROOT", str(home))
    codex = backends.get("codex")
    monkeypatch.setattr(codex, "required_binary", str(FAKE_CODEX))
    codex_app_server._CLIENTS.clear()
    (home / "sessions" / "rollout-thread-a.jsonl").write_text(
        json.dumps({"type": "event_msg", "payload": {"message": "hello"}}) + "\n")
    new = codex.fork_conversation("thread-a", source_cwd=str(tmp_path), cwd=str(tmp_path))
    assert new and new != "thread-a"
    assert (home / "sessions" / f"rollout-{new}.jsonl").read_text().count("hello") == 1
    assert codex.supports_fork


def test_codex_fork_of_an_unknown_thread_raises(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CLARP_QA_PROVIDER_ROOT", str(home))
    codex = backends.get("codex")
    monkeypatch.setattr(codex, "required_binary", str(FAKE_CODEX))
    with pytest.raises(FileNotFoundError):
        codex.fork_conversation("missing", source_cwd=str(tmp_path), cwd=str(tmp_path))


@pytest.mark.parametrize("backend", ["grok", "agy", "opencode", "deepseek"])
def test_backends_without_a_native_fork_say_so(backend):
    b = backends.get(backend)
    assert not b.supports_fork
    with pytest.raises(Unsupported):
        b.fork_conversation("x", source_cwd="/", cwd="/")
