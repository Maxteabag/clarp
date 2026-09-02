"""Tests for the session-map adapter over the SQLite agent source of truth."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.agent_store import (  # noqa: E402
    AGENT_ROSTER,
    AGENT_VOICE_POOL,
    load_agents,
    save_agents,
    pick_unused_voice,
)


def test_empty_db_returns_empty_dict():
    assert load_agents() == {}


def test_save_creates_agent_and_reload_returns_it():
    save_agents({"mike": {"name": "Mike", "voice_id": "V1", "cwd": "/tmp"}})
    reloaded = load_agents()
    assert set(reloaded.keys()) == {"mike"}
    assert reloaded["mike"]["name"] == "Mike"
    assert reloaded["mike"]["voice_id"] == "V1"
    # The DB-backed shape carries an agent_id that the JSON shape did not.
    assert reloaded["mike"]["agent_id"]


def test_save_diff_updates_voice_id():
    save_agents({"mike": {"name": "Mike", "voice_id": "V1", "cwd": "/tmp"}})
    save_agents({"mike": {"name": "Mike", "voice_id": "V2", "cwd": "/tmp"}})
    assert load_agents()["mike"]["voice_id"] == "V2"


def test_save_diff_updates_persona_and_cwd():
    save_agents({"mike": {"name": "Mike", "voice_id": "V1", "cwd": "/tmp"}})
    save_agents({"mike": {"name": "Michael", "voice_id": "V1", "cwd": "/"}})
    reloaded = load_agents()["mike"]
    assert reloaded["name"] == "Michael"
    assert reloaded["cwd"] == "/"


def test_save_diff_soft_deletes_missing_session():
    save_agents({
        "mike":   {"name": "Mike", "voice_id": "V1", "cwd": "/tmp"},
        "rachel": {"name": "Rachel", "voice_id": "V2", "cwd": "/tmp"},
    })
    save_agents({"mike": {"name": "Mike", "voice_id": "V1", "cwd": "/tmp"}})
    assert set(load_agents().keys()) == {"mike"}


def test_pick_unused_voice_picks_first_free():
    agents = {"a": {"voice_id": AGENT_VOICE_POOL[0]}}
    picked = pick_unused_voice(agents)
    assert picked == AGENT_VOICE_POOL[1]


def test_pick_unused_voice_cycles_when_all_taken(capsys):
    agents = {f"a{i}": {"voice_id": v} for i, v in enumerate(AGENT_VOICE_POOL)}
    picked = pick_unused_voice(agents)
    assert picked in AGENT_VOICE_POOL
    assert "pickUnusedVoiceCycle" in capsys.readouterr().err


def test_roster_has_known_personas():
    assert "Mike" in AGENT_ROSTER
    assert AGENT_ROSTER["Mike"]
