from __future__ import annotations

import pytest

from lib import config


def test_missing_config_uses_loopback_safe_default(tmp_path):
    config.reset_cache_for_tests()
    loaded = config.load(tmp_path / "missing.toml")
    assert loaded.bind_addr == "127.0.0.1"


def test_malformed_config_fails_closed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[server\n")
    config.reset_cache_for_tests()
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_roster_merges_over_builtin_defaults(tmp_path):
    """A [roster] in config.toml must MERGE over the built-in roster, never
    replace it — so a config that predates newly-added personas can't silently
    hide them. (Regression: an old [roster] listing only some personas used to
    shadow the rest.)"""
    path = tmp_path / "config.toml"
    path.write_text('[roster]\nMike = "custom-voice-id"\nNewbie = "newbie-voice"\n')
    config.reset_cache_for_tests()
    loaded = config.load(path)
    # Every built-in persona still present...
    for name in config.DEFAULT_ROSTER:
        assert name in loaded.roster, f"built-in persona {name} was shadowed"
    # ...the config override wins for an existing persona...
    assert loaded.roster["Mike"] == "custom-voice-id"
    # ...and a config-only persona is added.
    assert loaded.roster["Newbie"] == "newbie-voice"


def test_cartesia_voices_merge_over_defaults(tmp_path):
    """[cartesia.voices] also merges over the built-ins rather than replacing."""
    path = tmp_path / "config.toml"
    path.write_text('[cartesia.voices]\nMike = "cartesia-override"\n')
    config.reset_cache_for_tests()
    loaded = config.load(path)
    for name in config.DEFAULT_CARTESIA_VOICES:
        assert name in loaded.cartesia_voices
    assert loaded.cartesia_voices["Mike"] == "cartesia-override"


def test_no_roster_section_uses_full_builtin(tmp_path):
    """No [roster] at all → the complete built-in roster."""
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport = 7682\n")
    config.reset_cache_for_tests()
    loaded = config.load(path)
    assert set(loaded.roster) == set(config.DEFAULT_ROSTER)


def test_claude_cli_provider_defaults_to_official(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[agents]\n")
    config.reset_cache_for_tests()
    loaded = config.load(path)
    assert loaded.claude_cli == "claude"


def test_claude_cli_provider_can_use_clarp(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[agents]\nclaude_cli = "clarp"\n')
    config.reset_cache_for_tests()
    loaded = config.load(path)
    assert loaded.claude_cli == "clarp"

    config.reset_cache_for_tests()
    monkeypatch.setenv("CLAUDE_PWA_CLAUDE_CLI", "claude")
    loaded = config.load(path)
    assert loaded.claude_cli == "claude"
