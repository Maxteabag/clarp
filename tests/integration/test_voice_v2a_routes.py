"""V2a pre-read P2, through the route: a contact's voice edit (Android sends the whole map)
reads the stored value as main does and never moves a stored bare id to another provider."""
from __future__ import annotations

import json

from lib import config, personas
from .test_avatar_settings_endpoint import running_server, _post  # noqa: F401

UUID = "a0000000-0000-4000-8000-00000000000a"
ADA = "c0000000-0000-4000-8000-0000000000ad"


def test_a_contact_edit_through_the_route_keeps_the_stored_reading(running_server, monkeypatch):
    monkeypatch.setattr(config, "_CACHED", config.Config(tts_provider="cartesia", cartesia_api_key="ck"))
    status, created = _post(running_server, "/personas", {"name": "Wanda", "voice_id": json.dumps({"gemini": "Kore"}),
                                                          "generate_avatar": False})
    assert status == 201
    personas.set_stored_voice(created["persona"]["id"], UUID)            # a value from before V2a
    status, _ = _post(running_server, "/personas/update", {
        "original_name": "Wanda", "name": "Wanda", "voice_id": json.dumps({"gemini": "Kore"})})
    assert status == 200
    assert json.loads(personas.get("Wanda")["voice_id"]) == {"elevenlabs": UUID, "gemini": "Kore"}
    status, _ = _post(running_server, "/personas/update", {
        "original_name": "Wanda", "name": "Wanda", "voice_id": ADA})          # the incoming value by its form
    assert json.loads(personas.get("Wanda")["voice_id"]) == {"cartesia": ADA, "elevenlabs": UUID, "gemini": "Kore"}
