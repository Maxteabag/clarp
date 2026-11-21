"""Review 4 of 515, finding 3: the rename route itself pins the voices an agent spoke with
(POST /agent-rename), not only the function it calls."""
from __future__ import annotations

import json

from lib import agents as agents_db, config, voice_profile
from .test_avatar_settings_endpoint import running_server, _post  # noqa: F401

ADA = "c0000000-0000-4000-8000-0000000000ad"


def test_a_rename_through_the_route_keeps_the_voice(running_server, monkeypatch, tmp_path):
    cfg = config.Config(tts_provider="cartesia", cartesia_api_key="ck", cartesia_voices={"Ada": ADA})
    monkeypatch.setattr(config, "_CACHED", cfg)
    agents_db.create_agent(persona="Ada", voice_id="{}", cwd=str(tmp_path), session="ada-r")
    before = voice_profile.speech(voice_profile.HostView.build(cfg, stored={}),
                                  agents_db.get_by_session("ada-r"))
    assert before["voice_id"] == ADA                      # through its persona list today
    status, _ = _post(running_server, "/agent-rename", {"session": "ada-r", "name": "Someone Else"})
    assert status == 200
    row = agents_db.get_by_session("ada-r")
    assert row["persona"] == "Someone Else" and json.loads(row["voice_id"]) == {"cartesia": ADA}
    after = voice_profile.speech(voice_profile.HostView.build(cfg, stored={}), row)
    assert (after["provider"], after["voice_id"]) == ("cartesia", ADA)
