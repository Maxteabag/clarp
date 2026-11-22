"""clarp-admin prompt to an agent that moved: sent on to the Rust Host installed on this
machine when that is where it went, otherwise a refusal that names the destination."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("clarp_admin_moved", ROOT / "bin/clarp-admin.py")
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)


def _args(**kw):
    base = dict(to="mochi", text="hello", from_session="arnold", origin=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _tool(tmp_path):
    out = tmp_path / "called.txt"
    tool = tmp_path / "clarp-admin"
    tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {out}\necho "${{CLAUDE_PWA_CONFIG:-unset}}" >> {out}\n')
    tool.chmod(0o755)
    return tool, out


def test_a_prompt_to_an_agent_moved_to_the_local_rust_host_is_sent_there(tmp_path, monkeypatch):
    tool, out = _tool(tmp_path)
    monkeypatch.setattr(admin, "RUST_HOST_TOOL", tool)
    monkeypatch.setattr(admin, "_local_rust_host_id", lambda: "rust-1")
    monkeypatch.setenv("CLAUDE_PWA_CONFIG", "/python/host/config")
    body = {"error": "agent_moved", "moved_to": {"host": "elitebook Rust", "id": "rust-1"}}
    assert admin._prompt_moved_agent(_args(), body) == 0
    lines = out.read_text().splitlines()
    assert lines[:7] == ["prompt", "--to", "mochi", "--text", "hello", "--from", "arnold"]
    assert lines[-1] == "unset"  # the Rust tool must not read the Python Host's config


def test_another_destination_stays_a_refusal_that_names_it(tmp_path, monkeypatch, capsys):
    tool, out = _tool(tmp_path)
    monkeypatch.setattr(admin, "RUST_HOST_TOOL", tool)
    monkeypatch.setattr(admin, "_local_rust_host_id", lambda: "rust-1")
    body = {"error": "agent_moved", "moved_to": {"host": "laptopstudio Rust", "id": "rust-2"}}
    assert admin._prompt_moved_agent(_args(), body) == 4
    assert not out.exists()
    assert "moved to laptopstudio Rust; nothing was sent" in capsys.readouterr().err


def test_no_destination_or_no_local_tool_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "RUST_HOST_TOOL", tmp_path / "missing")
    monkeypatch.setattr(admin, "_local_rust_host_id", lambda: "rust-1")
    assert admin._prompt_moved_agent(_args(), {"error": "agent_moved", "moved_to": None}) == 4
    assert admin._prompt_moved_agent(
        _args(), {"error": "agent_moved", "moved_to": {"host": "x", "id": "rust-1"}}) == 4
