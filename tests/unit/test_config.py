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


@pytest.mark.parametrize("value,expected", [
    ('["python3", "/path with spaces/selector.py"]', ("python3", "/path with spaces/selector.py")),
    ('"shell command"', ()), ('[123]', ()), ('[""]', ()), ('[]', ()),
])
def test_claude_account_switch_command_requires_explicit_argv(tmp_path, value, expected):
    path = tmp_path / "config.toml"
    path.write_text(f'[agents]\nclaude_account_switch_command = {value}\n')
    config.reset_cache_for_tests()
    assert config.load(path).claude_account_switch_command == expected


@pytest.mark.parametrize('mode', ['tailscale', 'lan', 'manual', 'relay'])
def test_remote_network_mode_requires_authentication(tmp_path, mode):
    path = tmp_path / 'config.toml'
    path.write_text(f'[server]\nauth_token = ""\n[network]\nmode = "{mode}"\n')
    with pytest.raises(config.ConfigError, match='authentication'):
        config.load(path)


def test_load_reloads_when_asked_for_a_different_path(tmp_path):
    first = tmp_path / "a.toml"
    second = tmp_path / "b.toml"
    first.write_text('[server]\nport = 7001\n')
    second.write_text('[server]\nport = 7002\n')
    config.reset_cache()
    assert config.load(first).port == 7001
    # Same path inside the stat interval: served from the cache without
    # touching the file again.
    first.write_text('[server]\nport = 7999\n')
    assert config.load(first).port == 7001
    # A different path is a different answer, not the stale cache.
    assert config.load(second).port == 7002
    config.reset_cache()


def test_rewritten_config_reloads_after_the_stat_interval(tmp_path, monkeypatch):
    path = tmp_path / "live.toml"
    path.write_text('[server]\nport = 7101\n')
    clock = [1000.0]
    monkeypatch.setattr(config.time, "monotonic", lambda: clock[0])
    config.reset_cache()
    assert config.load(path).port == 7101
    stats = []
    real_stat = config._file_signature
    monkeypatch.setattr(config, "_file_signature", lambda p: stats.append(1) or real_stat(p))
    path.write_text('[server]\nport = 7102\n')
    import os
    os.utime(path, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    clock[0] += 0.5
    assert config.load().port == 7101 and stats == []      # inside the interval: no stat
    clock[0] += 1.0
    assert config.load().port == 7102 and len(stats) == 2  # one check, one reload
    clock[0] += 1.0
    assert config.load(path).port == 7102 and len(stats) == 3  # unchanged: check only
    # A malformed rewrite keeps the last good Config instead of taking the Host down.
    path.write_text('[server\nport = ')
    os.utime(path, ns=(3_000_000_000_000_000_000, 3_000_000_000_000_000_000))
    clock[0] += 1.0
    assert config.load().port == 7102
    config.reset_cache()
    with pytest.raises(config.ConfigError):
        config.load(path)
    config.reset_cache()


def test_concurrent_first_loads_share_one_parse(tmp_path, monkeypatch):
    import threading
    path = tmp_path / "c.toml"
    path.write_text('[server]\nport = 7003\n')
    config.reset_cache()
    parses = []
    real = config.tomllib.load

    def counting(fh):
        parses.append(1)
        return real(fh)

    monkeypatch.setattr(config.tomllib, "load", counting)
    results = []
    threads = [threading.Thread(target=lambda: results.append(config.load(path).port))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [7003] * 8
    assert len(parses) == 1
    config.reset_cache()


def test_anonymous_launch_names_speak_with_their_archetype_voice(tmp_path):
    # Issue 127: "Claude-5628" carries no voice of its own; the Cartesia map is
    # keyed on the archetype name the launch path minted it from.
    config.reset_cache_for_tests()
    loaded = config.load(tmp_path / "missing.toml")
    assert loaded.cartesia_voice_for("Claude") is not None
    assert loaded.cartesia_voice_for("Claude-5628") == loaded.cartesia_voice_for("Claude")
    assert loaded.cartesia_voice_for("Codex-0a1b") == loaded.cartesia_voice_for("Codex")
    # Only the four-hex-digit suffix is an anonymous name; other names stay unmapped.
    assert loaded.cartesia_voice_for("Nobody-5628") is None
    assert loaded.cartesia_voice_for("Claude-Smith") is None
    config.reset_cache_for_tests()
