import pathlib

import pytest

from lib import avatar_settings, model_avatars


@pytest.fixture
def bundled(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "avatars" / "models"
    root.mkdir(parents=True)
    for family in ("opus", "fable", "gemini", "codex", "grok"):
        (root / f"rachel.{family}.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + family.encode())
    return root


def test_model_id_names_its_family():
    assert model_avatars.family_for_model("claude-opus-5") == "opus"
    assert model_avatars.family_for_model("fable") == "fable"
    assert model_avatars.family_for_model("gemini-3.7-flash-high") == "gemini"
    assert model_avatars.family_for_model("gpt-5.2-codex") == "codex"
    assert model_avatars.family_for_model("grok-4.6") == "grok"
    assert model_avatars.family_for_model("") == ""
    assert model_avatars.family_for_model("gpt-oss-120b-medium") == ""


def test_the_model_outranks_the_cli_that_runs_it(bundled):
    """An Antigravity Agent pinned to a Claude model is an Opus portrait."""
    assert model_avatars.url_for(
        "Rachel", "agy", "claude-opus-4-6-thinking", root=bundled,
    ).startswith("/static/avatars/models/rachel.opus.png?v=")


def test_the_configured_default_speaks_for_an_agent_that_pins_no_model(bundled):
    assert model_avatars.url_for(
        "Rachel", "claude", "", root=bundled, default_model="claude-fable-5-1",
    ).startswith("/static/avatars/models/rachel.fable.png?v=")


def test_a_single_family_cli_stands_in_for_an_unknown_model(bundled):
    assert model_avatars.url_for(
        "Rachel", "codex", "", root=bundled,
    ).startswith("/static/avatars/models/rachel.codex.png?v=")
    assert model_avatars.url_for(
        "Rachel", "agy", "", root=bundled,
    ).startswith("/static/avatars/models/rachel.gemini.png?v=")


def test_a_multi_family_cli_alone_is_not_evidence_of_a_model(bundled):
    """The Claude CLI spans Opus, Fable, Sonnet and Haiku; it names none."""
    assert model_avatars.url_for("Rachel", "claude", "", root=bundled) == ""


def test_a_persona_without_art_for_its_family_keeps_its_own_portrait(bundled):
    assert model_avatars.url_for("Rachel", "claude", "haiku", root=bundled) == ""
    assert model_avatars.url_for("Mike", "codex", "gpt-5.4", root=bundled) == ""


def test_the_url_is_content_versioned_so_replacing_art_busts_the_cache(bundled):
    first = model_avatars.url_for("Rachel", "grok", "grok-4.6", root=bundled)
    assert first == model_avatars.url_for("Rachel", "grok", "grok-4.6", root=bundled)

    (bundled / "rachel.grok.png").write_bytes(b"\x89PNG\r\n\x1a\nredrawn")
    assert model_avatars.url_for("Rachel", "grok", "grok-4.6", root=bundled) != first


def test_a_persona_name_becomes_the_same_slug_the_clients_compute():
    assert model_avatars.slug("Rachel") == "rachel"
    assert model_avatars.slug("Dreamer-Lenses") == "dreamer-lenses"
    assert model_avatars.slug("Ada Lovelace!") == "adalovelace"
    assert model_avatars.slug("  ") == ""


def test_the_preference_is_off_until_the_user_turns_it_on():
    assert avatar_settings.get() == {"model_avatars": False}
    assert avatar_settings.update(True) == {"model_avatars": True}
    assert avatar_settings.get() == {"model_avatars": True}
    assert avatar_settings.update(False) == {"model_avatars": False}
