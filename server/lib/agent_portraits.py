"""Bounded portrait variants backed by Clarp's durable media/avatar storage."""
from __future__ import annotations

import hashlib
import os
import pathlib
import secrets
from typing import Any
from urllib.parse import quote

from . import db


MAX_PORTRAITS = 3
MAX_PORTRAIT_BYTES = 20 * 1024 * 1024


class PortraitError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def list_for_session(session: str, *, portrait_dir: pathlib.Path) -> dict[str, Any]:
    agent = _ensure_current_primary(session, portrait_dir)
    return _collection(agent)


def add_media_asset(*, session: str, asset_id: str,
                    portrait_dir: pathlib.Path) -> dict[str, Any]:
    agent = _ensure_current_primary(session, portrait_dir)
    asset = db.conn().execute(
        """SELECT * FROM media_assets
             WHERE asset_id=? AND deleted_at IS NULL""",
        ((asset_id or "").strip(),),
    ).fetchone()
    if not asset:
        raise PortraitError("media asset not found", status=404)
    asset = dict(asset)
    if asset["agent_id"] != agent["agent_id"] or asset["session"] != agent["session"]:
        raise PortraitError("media asset belongs to another agent", status=409)
    path = pathlib.Path(str(asset["storage_path"] or ""))
    if not path.is_file():
        raise PortraitError("media asset content unavailable", status=409)

    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        _prune_unavailable_alternates(con, agent["agent_id"])
        existing = con.execute(
            """SELECT portrait_id FROM agent_portraits
                 WHERE agent_id=? AND sha256=? AND deleted_at IS NULL""",
            (agent["agent_id"], asset["sha256"]),
        ).fetchone()
        if existing is None:
            count = con.execute(
                """SELECT COUNT(*) AS n FROM agent_portraits
                     WHERE agent_id=? AND deleted_at IS NULL""",
                (agent["agent_id"],),
            ).fetchone()["n"]
            if int(count) >= MAX_PORTRAITS:
                raise PortraitError("an agent can retain at most three portraits", status=409)
            con.execute(
                """INSERT INTO agent_portraits (
                       portrait_id,agent_id,media_asset_id,storage_path,sha256,
                       mime_type,created_by,created_at,is_primary,deleted_at
                   ) VALUES (?,?,?,?,?,?,?,?,0,NULL)""",
                (_new_id(), agent["agent_id"], asset["asset_id"], str(path),
                 asset["sha256"], asset["mime_type"],
                 str(asset.get("created_by") or "agent"),
                 int(asset["created_at"])),
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return _collection(agent)


def select_primary(*, session: str, portrait_id: str,
                   portrait_dir: pathlib.Path) -> dict[str, Any]:
    agent = _ensure_current_primary(session, portrait_dir)
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            """SELECT * FROM agent_portraits
                 WHERE portrait_id=? AND agent_id=? AND deleted_at IS NULL""",
            ((portrait_id or "").strip(), agent["agent_id"]),
        ).fetchone()
        if not row:
            raise PortraitError("portrait not found", status=404)
        path = pathlib.Path(str(row["storage_path"] or ""))
        if not path.is_file():
            raise PortraitError("portrait content unavailable", status=409)
        con.execute(
            "UPDATE agent_portraits SET is_primary=0 WHERE agent_id=? AND deleted_at IS NULL",
            (agent["agent_id"],),
        )
        con.execute(
            "UPDATE agent_portraits SET is_primary=1 WHERE portrait_id=?",
            (row["portrait_id"],),
        )
        con.execute(
            "UPDATE agents SET avatar_path=? WHERE agent_id=? AND deleted_at IS NULL",
            (str(path), agent["agent_id"]),
        )
        _prune_unavailable_alternates(con, agent["agent_id"])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return _collection({**agent, "avatar_path": str(path)})


def replace_alternates_with_media_assets(
    *, session: str, asset_ids: list[str], portrait_dir: pathlib.Path,
    expected_job: tuple[str, int] | None = None,
) -> dict[str, Any]:
    """Replace only alternates, preserving the current primary and Agent avatar."""
    if len(asset_ids) != MAX_PORTRAITS - 1 or len(set(asset_ids)) != len(asset_ids):
        raise PortraitError("exactly two distinct generated assets are required")
    agent = _ensure_current_primary(session, portrait_dir)
    assets = []
    for asset_id in asset_ids:
        row = db.conn().execute(
            "SELECT * FROM media_assets WHERE asset_id=? AND deleted_at IS NULL",
            ((asset_id or "").strip(),),
        ).fetchone()
        if not row:
            raise PortraitError("generated media asset not found", status=404)
        item = dict(row)
        if item["agent_id"] != agent["agent_id"] or item["session"] != agent["session"]:
            raise PortraitError("generated media belongs to another agent", status=409)
        if not pathlib.Path(str(item["storage_path"] or "")).is_file():
            raise PortraitError("generated media content unavailable", status=409)
        assets.append(item)

    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        if expected_job:
            job_id, generation = expected_job
            active = con.execute(
                "SELECT 1 FROM background_jobs WHERE job_id=? AND generation=? "
                "AND status IN ('queued','running')",
                (job_id, int(generation)),
            ).fetchone()
            if not active:
                raise PortraitError("portrait generation was cancelled", status=409)
        con.execute(
            "UPDATE agent_portraits SET deleted_at=? WHERE agent_id=? "
            "AND deleted_at IS NULL AND is_primary=0",
            (db.now_ms(), agent["agent_id"]),
        )
        for asset in assets:
            con.execute(
                """INSERT INTO agent_portraits (
                       portrait_id,agent_id,media_asset_id,storage_path,sha256,
                       mime_type,created_by,created_at,is_primary,deleted_at
                   ) VALUES (?,?,?,?,?,?,?,?,0,NULL)""",
                (_new_id(), agent["agent_id"], asset["asset_id"],
                 str(asset["storage_path"]), asset["sha256"], asset["mime_type"],
                 str(asset.get("created_by") or "portrait_generation"),
                 int(asset["created_at"])),
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return _collection(agent)


def get_content(portrait_id: str) -> dict[str, Any] | None:
    row = db.conn().execute(
        """SELECT * FROM agent_portraits
             WHERE portrait_id=? AND deleted_at IS NULL""",
        ((portrait_id or "").strip(),),
    ).fetchone()
    return dict(row) if row else None


def _ensure_current_primary(
    session: str, portrait_dir: pathlib.Path
) -> dict[str, Any]:
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            """SELECT agent_id,session,avatar_path,created_at FROM agents
                 WHERE session=? AND deleted_at IS NULL""",
            ((session or "").strip(),),
        ).fetchone()
        if not row:
            raise PortraitError("unknown session", status=404)
        agent = dict(row)
        _prune_unavailable_alternates(con, agent["agent_id"])
        raw_path = str(agent.get("avatar_path") or "")
        path = pathlib.Path(raw_path)
        if not raw_path or not path.is_file():
            con.execute("COMMIT")
            return agent
        if path.stat().st_size > MAX_PORTRAIT_BYTES:
            con.execute("COMMIT")
            return agent
        with path.open("rb") as handle:
            raw = handle.read(MAX_PORTRAIT_BYTES + 1)
        if len(raw) > MAX_PORTRAIT_BYTES:
            con.execute("COMMIT")
            return agent
        mime = _image_mime(raw)
        if mime is None:
            con.execute("COMMIT")
            return agent
        digest = hashlib.sha256(raw).hexdigest()
        immutable_path = _write_immutable(
            raw, digest=digest, mime=mime, portrait_dir=portrait_dir)
        row = con.execute(
            """SELECT portrait_id FROM agent_portraits
                 WHERE agent_id=? AND sha256=? AND deleted_at IS NULL""",
            (agent["agent_id"], digest),
        ).fetchone()
        if row is None:
            count = int(con.execute(
                """SELECT COUNT(*) AS n FROM agent_portraits
                     WHERE agent_id=? AND deleted_at IS NULL""",
                (agent["agent_id"],),
            ).fetchone()["n"])
            if count >= MAX_PORTRAITS:
                retired = con.execute(
                    """SELECT portrait_id FROM agent_portraits
                         WHERE agent_id=? AND deleted_at IS NULL AND is_primary=0
                         ORDER BY created_at, portrait_id LIMIT 1""",
                    (agent["agent_id"],),
                ).fetchone()
                if retired:
                    con.execute(
                        "UPDATE agent_portraits SET deleted_at=? WHERE portrait_id=?",
                        (db.now_ms(), retired["portrait_id"]),
                    )
                else:
                    raise PortraitError(
                        "portrait primary reconciliation conflict", status=409)
            portrait_id = _new_id()
            con.execute(
                """INSERT INTO agent_portraits (
                       portrait_id,agent_id,media_asset_id,storage_path,sha256,
                       mime_type,created_by,created_at,is_primary,deleted_at
                   ) VALUES (?,?,NULL,?,?,?,?,?,0,NULL)""",
                (portrait_id, agent["agent_id"], str(immutable_path), digest,
                 mime,
                 "legacy_avatar", int(agent.get("created_at") or db.now_ms())),
            )
        else:
            portrait_id = row["portrait_id"]
        con.execute(
            "UPDATE agent_portraits SET is_primary=0 WHERE agent_id=? AND deleted_at IS NULL",
            (agent["agent_id"],),
        )
        con.execute(
            "UPDATE agent_portraits SET is_primary=1 WHERE portrait_id=?",
            (portrait_id,),
        )
        con.execute("COMMIT")
        return agent
    except Exception:
        con.execute("ROLLBACK")
        raise


def _collection(agent: dict[str, Any]) -> dict[str, Any]:
    rows = [dict(row) for row in db.conn().execute(
        """SELECT * FROM agent_portraits
             WHERE agent_id=? AND deleted_at IS NULL
             ORDER BY is_primary DESC, created_at, portrait_id""",
        (agent["agent_id"],),
    ).fetchall()]
    portraits = [_public(row) for row in rows]
    primary = next((row["portrait_id"] for row in rows if row["is_primary"]), None)
    return {
        "contract": "agent-portraits.v1",
        "session": agent["session"],
        "agent_id": agent["agent_id"],
        "primary_portrait_id": primary,
        "portraits": portraits,
        "max_portraits": MAX_PORTRAITS,
    }


def _public(row: dict[str, Any]) -> dict[str, Any]:
    available = pathlib.Path(str(row["storage_path"] or "")).is_file()
    version = str(row["sha256"] or "")[:16]
    portrait_id = str(row["portrait_id"])
    return {
        "portrait_id": portrait_id,
        "media_asset_id": row.get("media_asset_id"),
        "role": "primary" if row.get("is_primary") else "alternate",
        "content_version": version,
        "url": (f"/agent-portraits/{quote(portrait_id, safe='')}/content?v={version}"
                if available else ""),
        "source": "media_asset" if row.get("media_asset_id") else "legacy_avatar",
        "created_by": row.get("created_by") or "agent",
        "created_at": int(row.get("created_at") or 0),
        "available": available,
    }


def _prune_unavailable_alternates(con, agent_id: str) -> None:
    rows = con.execute(
        """SELECT portrait_id,storage_path FROM agent_portraits
             WHERE agent_id=? AND deleted_at IS NULL AND is_primary=0""",
        (agent_id,),
    ).fetchall()
    now = db.now_ms()
    for row in rows:
        if not pathlib.Path(str(row["storage_path"] or "")).is_file():
            con.execute(
                "UPDATE agent_portraits SET deleted_at=? WHERE portrait_id=?",
                (now, row["portrait_id"]),
            )


def _write_immutable(
    raw: bytes, *, digest: str, mime: str, portrait_dir: pathlib.Path
) -> pathlib.Path:
    extension = {
        "image/png": ".png", "image/jpeg": ".jpg",
        "image/gif": ".gif", "image/webp": ".webp",
    }[mime]
    folder = pathlib.Path(portrait_dir) / "portrait-blobs" / digest[:2]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{digest}{extension}"
    if target.exists():
        return target
    temporary = folder / f".{digest}.{secrets.token_hex(6)}.tmp"
    temporary.write_bytes(raw)
    try:
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def _image_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _new_id() -> str:
    return "portrait_" + secrets.token_urlsafe(18).replace("-", "_")
