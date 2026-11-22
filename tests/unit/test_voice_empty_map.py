"""An empty provider map ("{}") names no voice, exactly as "" did (issue 476).

The start pass of the one stored shape writes "" as "{}". Every check that asked "has this agent
a voice?" by the raw text's truthiness read "{}" as a voice: a ring or an announcement for a
voiceless agent then tried a synthesis that could only fail.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib import agent_rings, agents as agents_db, config, personas
from lib.agent_lifecycle import AgentLifecycleService
from lib.context import ServerContext
from lib.voice import has_voice


@pytest.mark.parametrize("raw, expected", [
    ("", False), (None, False), ("{}", False), ("  {} ", False), ('{"cartesia": ""}', False),
    ('{"cartesia":"c-1"}', True), ("21m00Tcm4TlvDq8ikWAM", True)])
def test_has_voice(raw, expected):
    assert has_voice(raw) is expected


class _Tts:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, voice_id, session=None):
        self.calls.append(voice_id)
        return "/tmp/clip.mp3"


@pytest.mark.parametrize("voice", ["", "{}"])
def test_a_ring_for_a_voiceless_agent_synthesises_nothing(monkeypatch, voice):
    tts = _Tts()
    monkeypatch.setitem(agent_rings._context, "tts", tts)
    assert agent_rings._voice("Hello", {"voice_id": voice, "session": "s"}) == ""
    assert tts.calls == []
    assert agent_rings._voice("Hello", {"voice_id": '{"cartesia":"c-1"}', "session": "s"}) == "/audio/clip.mp3"
    assert tts.calls == ['{"cartesia":"c-1"}']


@pytest.mark.parametrize("voice", ["", "{}"])
def test_an_announcement_for_a_voiceless_agent_synthesises_nothing(voice):
    tts = _Tts()
    ctx = SimpleNamespace(tts=tts, async_announcements=False)
    ServerContext.speak_announcement(ctx, "Ready.", voice)
    ServerContext.speak_announcement_async(ctx, "Ready.", voice)
    assert tts.calls == []


@pytest.fixture
def cartesia(monkeypatch):
    cfg = config.Config(tts_provider="cartesia", tts_fallback="none", cartesia_api_key="ck",
                        cartesia_pool=("p1", "p2", "p3", "p4"), cartesia_voices={})
    monkeypatch.setattr(config, "load", lambda path=None: cfg)
    monkeypatch.setattr(config, "_CACHED", cfg)
    return cfg


def _ctx(tmp_path):
    return SimpleNamespace(agents_path=tmp_path / "unused.json",
                           stream=SimpleNamespace(broadcast=lambda event: None),
                           speak_announcement=lambda *a, **k: None)


def test_a_relaunch_given_an_empty_map_keeps_the_agents_voice(cartesia, tmp_path):
    agents_db.create_agent(persona="Zed", voice_id='{"cartesia":"c-zed"}', cwd=str(tmp_path), session="zed")
    AgentLifecycleService(_ctx(tmp_path)).create({"name": "Zed", "replace_sid": "zed", "voice_id": "{}"})
    assert agents_db.get_by_session("zed")["voice_id"] == '{"cartesia":"c-zed"}'


def test_a_new_agent_given_an_empty_map_speaks_with_its_contacts_voice(cartesia, tmp_path):
    """clarp-admin sends "{}" when no voice is named; like "" it means "no voice given"."""
    personas.create(name="Wilma", voice_id='{"cartesia":"c-wilma"}')
    session = AgentLifecycleService(_ctx(tmp_path)).create(
        {"name": "Wilma", "cwd": str(tmp_path), "voice_id": "{}"}).session
    assert json.loads(agents_db.get_by_session(session)["voice_id"])["cartesia"] == "c-wilma"


def test_editing_a_contact_with_an_empty_map_fills_it_from_the_pool(cartesia):
    """As contact assignment does: a contact edited with no voice, or with one for another
    provider only, ends with a voice for the configured provider."""
    personas.create(name="Cora", voice_id="")
    from lib import db
    db.conn().execute("UPDATE personas SET voice_id = '{}' WHERE name = 'Cora'")
    edited = personas.update(original_name="Cora", name="Cora", voice_id="")
    assert json.loads(edited["voice_id"]).get("cartesia") in {"p1", "p2", "p3", "p4"}
    db.conn().execute("UPDATE personas SET voice_id = '{}' WHERE name = 'Cora'")
    edited = personas.update(original_name="Cora", name="Cora", voice_id='{"gemini":"Kore"}')
    voices = json.loads(edited["voice_id"])
    assert voices["gemini"] == "Kore" and voices.get("cartesia") in {"p1", "p2", "p3", "p4"}
    # A voice the edit names is kept, and nothing is added when nothing is missing.
    edited = personas.update(original_name="Cora", name="Cora", voice_id='{"cartesia":"mine"}')
    assert json.loads(edited["voice_id"]) == {"cartesia": "mine", "gemini": "Kore"}


def test_editing_a_contact_leaves_a_bare_legacy_value_it_does_not_edit(cartesia):
    personas.create(name="Lex", voice_id="")
    from lib import db
    db.conn().execute("UPDATE personas SET voice_id = '21m00Tcm4TlvDq8ikWAM' WHERE name = 'Lex'")
    edited = personas.update(original_name="Lex", name="Lex", voice_id="", personality="calm")
    assert edited["voice_id"] == "21m00Tcm4TlvDq8ikWAM"
