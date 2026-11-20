"""Review of 485, item 6: a limited device's ``shared_with`` counts only agents that device sees.

A limited device is answered the same snapshot as a full one (SharedSnapshot keys its bodies
on include_archived only), so every agent ``shared_with`` counts is a row of the body it
receives. This holds that down: same store, a limited and a full device, both bodies; each
row's shared_with equals the other rows of that very body speaking with the same voice.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from lib import agents as agents_db, config, db, device_pairing
from .test_avatar_settings_endpoint import running_server  # noqa: F401

SHARED_VOICE = json.dumps({"cartesia": "c0000000-0000-4000-8000-0000000000aa"})


def _snapshot(base, token, query=""):
    request = urllib.request.Request(base + "/agents/snapshot" + query,
                                     headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _token(scope):
    return device_pairing.exchange(device_pairing.issue(scope=scope)["code"])["token"]


def _counted_in_body(body):
    """shared_with as the body itself shows it: other unarchived rows with the same voice."""
    rows = [a for a in body["agents"] if not a.get("archived_at") and a.get("voice_profile")]
    out = {}
    for a in rows:
        sp = a["voice_profile"]["speech"]
        key = (sp["provider"], sp["voice_id"])
        out[a["agent_id"]] = sum(1 for b in rows if b is not a and sp["voice_id"]
                                 and (b["voice_profile"]["speech"]["provider"],
                                      b["voice_profile"]["speech"]["voice_id"]) == key)
    return out


@pytest.mark.parametrize("query", ["", "?include_archived=0"])
def test_a_limited_device_counts_only_the_agents_it_is_shown(running_server, tmp_path, monkeypatch, query):
    monkeypatch.setattr(config, "_CACHED", config.Config(tts_provider="cartesia", cartesia_api_key="ck"))
    for name in ("Ada", "Bea", "Cid"):
        agents_db.create_agent(persona=name, voice_id=SHARED_VOICE, cwd=str(tmp_path),
                               session=name.lower())
    gone = agents_db.create_agent(persona="Dot", voice_id=SHARED_VOICE, cwd=str(tmp_path), session="dot")
    db.conn().execute("UPDATE agents SET archived_at = ? WHERE agent_id = ?", (db.now_ms(), gone))

    limited = _snapshot(running_server, _token("limited"), query)
    full = _snapshot(running_server, _token("full"), query)
    assert [a["agent_id"] for a in limited["agents"]] == [a["agent_id"] for a in full["agents"]]
    for body in (limited, full):
        shown = {a["agent_id"]: a["voice_profile"]["speech"]["shared_with"]
                 for a in body["agents"] if not a.get("archived_at") and a.get("voice_profile")}
        assert shown == _counted_in_body(body)
    profiles = {a["agent_id"]: a.get("voice_profile") for a in limited["agents"]}
    assert profiles == {a["agent_id"]: a.get("voice_profile") for a in full["agents"]}
    # The three that share speak with one voice and count two others; the archived one is never counted.
    assert sorted(v["speech"]["shared_with"] for v in profiles.values() if v
                  and v["speech"]["voice_id"] == json.loads(SHARED_VOICE)["cartesia"]) == [2, 2, 2]
