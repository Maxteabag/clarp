"""Durable persona definitions, separate from disposable running sessions."""
from __future__ import annotations

import base64
import json
import pathlib
import secrets
import sqlite3
import threading

from . import config, db, roster
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
        voices = {
            "elevenlabs": cfg.roster.get(name, ""),
            "cartesia": cfg.cartesia_voice_for(name),
        }
        if cfg.gemini_voice_for(name):
            voices["gemini"] = cfg.gemini_voice_for(name)
        from .voice import canonical
        voice = canonical(json.dumps(voices))      # the one stored shape (issue 476)
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


def get_by_id(persona_id: str) -> dict | None:
    ensure_builtins()
    row = db.conn().execute(
        "SELECT * FROM personas WHERE persona_id = ? AND deleted_at IS NULL",
        ((persona_id or "").strip(),),
    ).fetchone()
    return dict(row) if row else None


def create(*, name: str, voice_id: str = "", avatar_symbol: str = "",
           personality: str = "", avatar_base64: str = "") -> dict:
    name = name.strip()
    if not name or len(name) > 60:
        raise ValueError("A personality name is required (maximum 60 characters).")
    if get(name):
        raise ValueError(f"{name} already exists.")
    # V2b: a contact saved with no voice, or with a phone's auto-<uuid> stand-in, gets
    # its voices from the pool (voice_pool) for the configured provider and fallback.
    from . import voice_pool
    with voice_pool.assigning():      # the pick and its insert, against every other pick
        return _create_assigned(name=name, voice_id=voice_id, avatar_symbol=avatar_symbol,
                                personality=personality, avatar_base64=avatar_base64)


def _create_assigned(*, name: str, voice_id: str, avatar_symbol: str, personality: str,
                     avatar_base64: str) -> dict:
    from . import voice_pool
    from .voice import canonical
    if get(name):
        raise ValueError(f"{name} already exists.")
    voice_id = canonical(voice_pool.assign(config.load(), name, voice_id or "{}"))   # the one stored shape
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
           avatar_base64: str = "", base_voice_id: str | None = None) -> dict:
    """``voice_id`` is merged into the stored map per provider (voice.merge_provider_maps):
    with ``base_voice_id``, the map the client last read, a stale read never wipes
    another app's entry; without it, nothing is removed by its absence."""
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
        from . import contact_minting
        voice_id = str(current.get("voice_id") or "") or contact_minting.pick_available_voice(name)
    else:
        from .voice import merge_provider_maps
        voice_id = merge_provider_maps(current.get("voice_id"), voice_id, base_voice_id)
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


def set_stored_voice(persona_id: str, voice_id: str) -> None:
    """Rewrite a saved contact's stored voice value (the voice migration's --apply,
    only for a value that sounds the same in the one stored shape)."""
    db.conn().execute("UPDATE personas SET voice_id = ? WHERE persona_id = ? AND builtin = 0",
                      (voice_id, persona_id))


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


def public(
    row: dict,
    *,
    active_avatar_statuses: dict[str, str] | None = None,
) -> dict:
    from . import persona_avatar_generation
    avatar_url = versioned_avatar_url(
        "/persona-avatars", row["persona_id"], str(row.get("avatar_path") or ""))
    return {
        "id": row["persona_id"], "name": row["name"],
        "voice_id": row.get("voice_id") or "",
        "avatar_symbol": row.get("avatar_symbol") or "",
        "avatar_url": avatar_url,
        "avatar_generation_status": (
            None if avatar_url else persona_avatar_generation.avatar_generation_status_for_persona(
                row, active_by_persona_id=active_avatar_statuses)
        ),
        "personality": row.get("personality") or "",
        "tier": roster.tier_for_contact(row["name"]) or "",
        "builtin": bool(row.get("builtin")),
    }
