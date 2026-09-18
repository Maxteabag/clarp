"""Durable persona definitions, separate from disposable running sessions."""
from __future__ import annotations

import base64
import json
import pathlib
import secrets
import sqlite3
import threading

from . import config, db
from .avatar_urls import versioned_avatar_url
from .deployment import LAYOUT

_seed_lock = threading.Lock()
_seeded_db = ""


def ensure_builtins() -> None:
    global _seeded_db
    marker = str(db.DB_PATH)
    if _seeded_db == marker:
        return
    with _seed_lock:
        if _seeded_db == marker:
            return
        _sync_builtins()
        _seeded_db = marker


def _sync_builtins() -> None:
    cfg = config.load()
    for name in cfg.roster.keys():
        voice = json.dumps({
            "elevenlabs": cfg.roster.get(name, ""),
            "cartesia": cfg.cartesia_voice_for(name),
        }, separators=(",", ":"))
        db.conn().execute(
            """INSERT OR IGNORE INTO personas
                 (persona_id,name,voice_id,personality,builtin,created_at)
               VALUES (?,?,?,?,1,?)""",
            ("builtin-" + name.casefold(), name, voice,
             config.persona_personality(name), db.now_ms()),
        )
        db.conn().execute(
            """UPDATE personas
                SET builtin = 1, voice_id = ?, personality = ?, deleted_at = NULL
                WHERE name = ? COLLATE NOCASE AND deleted_at IS NULL""",
            (voice, config.persona_personality(name), name),
        )


def list_all() -> list[dict]:
    ensure_builtins()
    return [dict(row) for row in db.conn().execute(
        "SELECT * FROM personas WHERE deleted_at IS NULL ORDER BY builtin DESC, created_at")]


def get(name: str) -> dict | None:
    ensure_builtins()
    row = db.conn().execute(
        "SELECT * FROM personas WHERE name = ? COLLATE NOCASE AND deleted_at IS NULL",
        ((name or "").strip(),),
    ).fetchone()
    return dict(row) if row else None


def create(*, name: str, voice_id: str, avatar_symbol: str = "",
           personality: str = "", avatar_base64: str = "") -> dict:
    name = name.strip()
    if not name or len(name) > 60:
        raise ValueError("A personality name is required (maximum 60 characters).")
    if get(name):
        raise ValueError(f"{name} already exists.")
    if not voice_id:
        raise ValueError("Choose a voice.")
    persona_id = "persona-" + secrets.token_hex(8)
    avatar_path = ""
    if avatar_base64:
        raw = base64.b64decode(avatar_base64, validate=True)
        if len(raw) > 512_000:
            raise ValueError("Avatar is too large.")
        folder = LAYOUT.data_root / "avatars"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{persona_id}.jpg"
        path.write_bytes(raw)
        avatar_path = str(path)
    try:
        db.conn().execute(
            """INSERT INTO personas
                 (persona_id,name,voice_id,avatar_symbol,avatar_path,personality,builtin,created_at)
               VALUES (?,?,?,?,?,?,0,?)""",
            (persona_id, name, voice_id, avatar_symbol[:64], avatar_path,
             personality[:4000], db.now_ms()),
        )
    except sqlite3.IntegrityError as exc:
        if avatar_path:
            try: pathlib.Path(avatar_path).unlink()
            except OSError: pass
        raise ValueError(f"{name} already exists.") from exc
    return get(name) or {}


def _store_avatar(current: dict, avatar_base64: str) -> str:
    """Persist a new portrait for a persona, or keep the existing one."""
    avatar_path = str(current.get("avatar_path") or "")
    if not avatar_base64:
        return avatar_path
    raw = base64.b64decode(avatar_base64, validate=True)
    if len(raw) > 512_000:
        raise ValueError("Avatar is too large.")
    folder = LAYOUT.data_root / "avatars"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{current['persona_id']}.jpg"
    path.write_bytes(raw)
    return str(path)


def update_avatar(*, original_name: str, avatar_symbol: str = "",
                  avatar_base64: str = "") -> dict:
    """Change only how a Contact looks, for a builtin as well as a custom one.

    A builtin's name, voice and personality come from config and are rewritten
    on every boot by `_sync_builtins`, so they stay read-only. Its portrait is
    not config-managed and nothing else can repair it: `_sync_builtins`
    promotes a custom row to builtin by name, which froze whatever avatar that
    row happened to carry (a Grok contact stuck wearing the `xmark.circle`
    dismiss glyph is how this surfaced) with no way to edit it in the app.
    """
    current = get(original_name)
    if not current:
        raise ValueError("No such Contact.")
    avatar_path = _store_avatar(current, avatar_base64)
    db.conn().execute(
        """UPDATE personas
              SET avatar_symbol = ?, avatar_path = ?
            WHERE persona_id = ? AND deleted_at IS NULL""",
        (avatar_symbol[:64], avatar_path, current["persona_id"]),
    )
    return get(current["name"]) or {}


def update(*, original_name: str, name: str, voice_id: str,
           avatar_symbol: str = "", personality: str = "",
           avatar_base64: str = "") -> dict:
    current = get(original_name)
    if not current:
        raise ValueError("Only saved custom Contacts can be edited.")
    if current.get("builtin"):
        # Everything else on a builtin is config-managed; take the avatar and
        # leave the rest rather than refusing the whole edit.
        return update_avatar(original_name=original_name,
                             avatar_symbol=avatar_symbol,
                             avatar_base64=avatar_base64)
    name = name.strip()
    if not name or len(name) > 60:
        raise ValueError("A Contact name is required (maximum 60 characters).")
    collision = get(name)
    if collision and collision["persona_id"] != current["persona_id"]:
        raise ValueError(f"{name} already exists.")
    if not voice_id:
        raise ValueError("Choose a voice.")
    avatar_path = _store_avatar(current, avatar_base64)
    db.conn().execute(
        """UPDATE personas
              SET name = ?, voice_id = ?, avatar_symbol = ?, avatar_path = ?,
                  personality = ?
            WHERE persona_id = ? AND builtin = 0 AND deleted_at IS NULL""",
        (name, voice_id, avatar_symbol[:64], avatar_path,
         personality[:4000], current["persona_id"]),
    )
    return get(name) or {}


def delete(name: str) -> bool:
    persona = get(name)
    if not persona or persona.get("builtin"):
        return False
    db.conn().execute(
        "UPDATE personas SET deleted_at = ? WHERE persona_id = ?",
        (db.now_ms(), persona["persona_id"]),
    )
    path = pathlib.Path(str(persona.get("avatar_path") or ""))
    # Running and historical Agent rows snapshot the Contact presentation path.
    # Removing the saved Contact must not break those Chats or their avatars.
    referenced = bool(path and db.conn().execute(
        "SELECT 1 FROM agents WHERE avatar_path = ? LIMIT 1", (str(path),),
    ).fetchone())
    if path.is_file() and not referenced:
        try: path.unlink()
        except OSError: pass
    return True


def public(row: dict) -> dict:
    return {
        "id": row["persona_id"], "name": row["name"],
        "voice_id": row.get("voice_id") or "",
        "avatar_symbol": row.get("avatar_symbol") or "",
        "avatar_url": versioned_avatar_url(
            "/persona-avatars", row["persona_id"], str(row.get("avatar_path") or "")),
        "personality": row.get("personality") or "",
        "builtin": bool(row.get("builtin")),
    }
