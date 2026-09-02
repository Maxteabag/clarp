from __future__ import annotations

from lib import agents, db, roster_seed
from lib.config import DEFAULT_ROSTER


def test_seed_defaults_creates_full_roster_once(tmp_path):
    assert roster_seed.seed_defaults("codex", cwd=tmp_path) == len(DEFAULT_ROSTER)
    rows = agents.session_dict()
    assert set(rows) == {name.lower() for name in DEFAULT_ROSTER}
    assert {row["backend"] for row in rows.values()} == {"codex"}
    assert agents.get_focus_session() == "mike"
    assert db.conn().execute("SELECT COUNT(*) FROM runtimes").fetchone()[0] == 0
    assert roster_seed.seed_defaults("claude", cwd=tmp_path) == 0
    assert {row["backend"] for row in agents.session_dict().values()} == {"codex"}


def test_seed_defaults_does_not_resurrect_deleted_database(tmp_path):
    agent_id = agents.create_agent(
        persona="Mike", voice_id="voice", cwd=str(tmp_path), session="mike")
    agents.soft_delete(agent_id)
    assert roster_seed.seed_defaults("codex", cwd=tmp_path) == 0
    assert agents.session_dict() == {}


def test_seed_defaults_mints_unique_sessions_for_custom_names(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(roster_seed, "load_config", lambda: SimpleNamespace(roster={
        "A B": "one", "AB": "two", "!!!": "three",
    }))
    assert roster_seed.seed_defaults("claude", cwd=tmp_path) == 3
    assert set(agents.session_dict()) == {"ab", "ab-2", "agent"}
