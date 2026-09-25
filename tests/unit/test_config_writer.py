"""Characterization tests for lib.config_writer (single-key TOML updates).

Pins: value rendering per Python type, section creation, in-place key
replacement, insertion before the next table header, comment-line
immunity, 0600 permissions, atomic temp-file replacement, and that the
result always parses with tomllib.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from lib.config_writer import set_toml_value, toml_value


@pytest.mark.parametrize("value,expected", [
    (True, "true"),
    (False, "false"),
    (0, "0"),
    (42, "42"),
    (-7, "-7"),
    ("plain", '"plain"'),
    ('with "quotes" and \\ slash', '"with \\"quotes\\" and \\\\ slash"'),
    ("multi\nline", '"multi\\nline"'),
    ("", '""'),
    # Non-bool/int values are stringified, so floats and None become strings.
    (1.5, '"1.5"'),
    (None, '"None"'),
])
def test_toml_value_rendering(value, expected):
    assert toml_value(value) == expected


def _read(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def test_creates_file_and_section_when_missing(tmp_path):
    path = tmp_path / "nested" / "config.toml"
    set_toml_value(path, "tts", "provider", "cartesia")
    assert path.read_text() == '[tts]\nprovider = "cartesia"\n'
    assert _read(path) == {"tts": {"provider": "cartesia"}}


def test_file_mode_is_owner_only_and_no_temp_file_left(tmp_path):
    path = tmp_path / "config.toml"
    set_toml_value(path, "auth", "token", "secret")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_appends_new_section_after_blank_line(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport = 7699\n")
    set_toml_value(path, "tts", "provider", "eleven")
    assert path.read_text() == (
        '[server]\nport = 7699\n\n[tts]\nprovider = "eleven"\n')


def test_does_not_add_extra_blank_line_when_file_ends_blank(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport = 7699\n\n")
    set_toml_value(path, "tts", "provider", "eleven")
    assert path.read_text() == (
        '[server]\nport = 7699\n\n[tts]\nprovider = "eleven"\n')


def test_replaces_existing_key_in_place(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[tts]\nprovider = "cartesia"\nspeed = 1\n')
    set_toml_value(path, "tts", "provider", "eleven")
    assert path.read_text() == '[tts]\nprovider = "eleven"\nspeed = 1\n'


def test_adds_key_at_end_of_its_section_before_next_header(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[tts]\nprovider = "cartesia"\n[server]\nport = 1\n')
    set_toml_value(path, "tts", "speed", 2)
    assert path.read_text() == (
        '[tts]\nprovider = "cartesia"\nspeed = 2\n[server]\nport = 1\n')
    assert _read(path) == {"tts": {"provider": "cartesia", "speed": 2},
                           "server": {"port": 1}}


def test_same_key_in_other_section_is_untouched(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[a]\nenabled = false\n[b]\nenabled = false\n")
    set_toml_value(path, "b", "enabled", True)
    assert _read(path) == {"a": {"enabled": False}, "b": {"enabled": True}}


def test_commented_out_key_is_not_treated_as_the_target(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[tts]\n# provider = "old"\nspeed = 1\n')
    set_toml_value(path, "tts", "provider", "new")
    text = path.read_text()
    assert '# provider = "old"' in text
    assert text.count("provider") == 2
    assert _read(path) == {"tts": {"provider": "new", "speed": 1}}


def test_indented_key_matches_and_is_rewritten_flush_left(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[tts]\n    speed = 1\n")
    set_toml_value(path, "tts", "speed", 3)
    assert path.read_text() == "[tts]\nspeed = 3\n"


def test_key_with_inline_comment_is_replaced_whole(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[tts]\nspeed = 1 # fast\n")
    set_toml_value(path, "tts", "speed", 2)
    assert path.read_text() == "[tts]\nspeed = 2\n"


def test_replaces_file_atomically_via_sibling_temp_name(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("[a]\nx = 1\n")
    seen = []
    original_replace = Path.replace

    def spy(self, target):
        seen.append((self.name, Path(target).name))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    set_toml_value(path, "a", "x", 2)
    assert seen == [(".config.toml.next", "config.toml")]


def test_section_header_with_trailing_comment_is_recognised(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[tts] # voice output\nprovider = "cartesia"\n')
    set_toml_value(path, "tts", "speed", 2)
    assert _read(path) == {"tts": {"provider": "cartesia", "speed": 2}}
