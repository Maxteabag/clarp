"""Transactional first-install seeding of the built-in persona roster."""
from __future__ import annotations

from pathlib import Path

from . import agents as agents_db, backends, db
from .config import load as load_config


def seed_defaults(backend: str, *, cwd: Path | None = None) -> int:
    database = db.conn()
    selected_backend = backends.normalize(backend)
    roster = load_config().roster
    root = cwd or Path.home()
    created = 0
    try:
        database.execute("BEGIN IMMEDIATE")
        # Any row, including a soft-deleted one, proves this is not a fresh DB.
        if database.execute("SELECT 1 FROM agents LIMIT 1").fetchone() is not None:
            database.execute("COMMIT")
            return 0
        first_agent_id = None
        used_sessions: set[str] = set()
        for name, voice_id in roster.items():
            base = "".join(
                character for character in name.lower()
                if character.isalnum() or character in "._-"
            ) or "agent"
            session = base
            suffix = 2
            while session in used_sessions:
                session = f"{base}-{suffix}"
                suffix += 1
            used_sessions.add(session)
            agent_id = agents_db.create_agent(
                persona=name, voice_id=voice_id, cwd=str(root), session=session,
                backend=selected_backend,
            )
            first_agent_id = first_agent_id or agent_id
            created += 1
        agents_db.set_focus(first_agent_id)
        database.execute("COMMIT")
    except BaseException:
        database.execute("ROLLBACK")
        raise
    return created
