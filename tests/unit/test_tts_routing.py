"""Tests for the Stop-hook's pwa-routing gate.

These pin the regression where any Claude Code session running outside a
registered PWA agent would dump TTS audio into Mike's queue via the literal
`"claude"` fallback.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.tts_routing import should_emit_to_pwa_dir  # noqa: E402


def test_registered_agent_emits():
    agents = {"claude": {"name": "Mike"}, "rachel": {"name": "Rachel"}}
    assert should_emit_to_pwa_dir("claude", agents) is True
    assert should_emit_to_pwa_dir("rachel", agents) is True


def test_unregistered_session_does_not_emit():
    agents = {"claude": {"name": "Mike"}}
    assert should_emit_to_pwa_dir("scratch", agents) is False


def test_empty_session_does_not_emit():
    # The old `or "claude"` fallback turned an empty string into a write
    # against Mike's slot. The gate prevents that.
    agents = {"claude": {"name": "Mike"}}
    assert should_emit_to_pwa_dir("", agents) is False


def test_no_agents_file_does_not_emit():
    assert should_emit_to_pwa_dir("claude", {}) is False
    assert should_emit_to_pwa_dir("claude", None) is False


def test_case_sensitive_match():
    # Agents are keyed lowercase. Capitalised session name shouldn't match.
    agents = {"claude": {"name": "Mike"}}
    assert should_emit_to_pwa_dir("Claude", agents) is False
