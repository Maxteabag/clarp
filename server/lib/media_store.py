"""SQLite-indexed media assets published by agents for chat/gallery display."""
from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import pathlib
import secrets
import struct
from typing import Any

from . import agents as agents_db
from . import db


MAX_MEDIA_BYTES = 75 * 1024 * 1024
SAFE_IMAGE_MIME = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/heic",
    "image/heif",
}
SAFE_FILE_MIME = {
    "audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/x-wav",
    "video/mp4", "video/quicktime", "video/webm",
    "application/pdf", "application/json", "text/plain", "text/csv", "text/markdown",
}
TEXT_FILE_MIME = {"text/plain", "text/csv", "text/markdown"}
# Hosts without a system mime table cannot guess these from the name.
_MARKDOWN_EXTENSIONS = {".md", ".markdown"}
SAFE_MEDIA_MIME = SAFE_IMAGE_MIME | SAFE_FILE_MIME


@contextlib.contextmanager
def _write_lock():
    """The database write lock for a block: its own BEGIN IMMEDIATE ... COMMIT, or, inside a
    caller's open transaction (a podcast image is published inside one), that transaction,
    which already holds the lock and commits or rolls back with the caller."""
    con = db.conn()
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


class MediaError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def publish(
    *,
    session: str,
    blob: bytes,
    source_name: str,
    content_type: str,
    caption: str = "",
    created_by: str = "agent",
    media_dir: pathlib.Path,
) -> dict[str, Any]:
    session = (session or "").strip()
    if not session:
        raise MediaError("session required")
    if not blob or len(blob) > MAX_MEDIA_BYTES:
        raise MediaError("bad size")
    agent = agents_db.get_by_session(session)
    if not agent:
        raise MediaError("unknown session", status=404)

    safe_name = _safe_name(source_name, content_type)
    mime = _detect_mime(blob, content_type, safe_name)
    if mime not in SAFE_MEDIA_MIME:
        raise MediaError("unsupported media type")

    sha = hashlib.sha256(blob).hexdigest()
    ext = _extension(safe_name, mime)
    blob_dir = media_dir / "blobs" / sha[:2]
    blob_dir.mkdir(parents=True, exist_ok=True)
    storage_path = blob_dir / f"{sha}{ext}"

    width, height = _image_size(blob, mime)
    asset_id = "asset_" + secrets.token_urlsafe(18).replace("-", "_")
    now = db.now_ms()
    row = {
        "asset_id": asset_id,
        "agent_id": agent["agent_id"],
        "session": session,
        "source_name": safe_name,
        "sha256": sha,
        "mime_type": mime,
        "bytes": len(blob),
        "width": width,
        "height": height,
        "storage_path": str(storage_path),
        "caption": caption.strip()[:500],
        "created_by": created_by.strip()[:80] or "agent",
        "created_at": now,
        "deleted_at": None,
        "favorite_at": None,
    }
    # The file is checked and written under the same write lock as the row, and a delete
    # releases a shared file under that lock too (release_blob): a publish of the same
    # bytes either commits its row first, so the delete keeps the file, or finds the file
    # gone and writes it again. Never a row whose file a concurrent delete removed.
    with _write_lock() as con:
        if not storage_path.exists():
            storage_path.write_bytes(blob)
        con.execute(
            """INSERT INTO media_assets (
                   asset_id, agent_id, session, source_name, sha256, mime_type,
                   bytes, width, height, storage_path, caption, created_by,
                   created_at, deleted_at
               ) VALUES (
                   :asset_id, :agent_id, :session, :source_name, :sha256,
                   :mime_type, :bytes, :width, :height, :storage_path, :caption,
                   :created_by, :created_at, :deleted_at
               )""",
            row,
        )
    return _public_row(row, used_as=_used_as([row]).get(asset_id, []))


def get(asset_id: str) -> dict[str, Any] | None:
    row = db.conn().execute(
        """SELECT * FROM media_assets
             WHERE asset_id = ? AND deleted_at IS NULL""",
        (asset_id,),
    ).fetchone()
    return dict(row) if row else None


def lookup(asset_id: str) -> dict[str, Any] | None:
    """The asset's row, a deleted one included (``get`` leaves deleted rows out)."""
    row = db.conn().execute(
        "SELECT * FROM media_assets WHERE asset_id = ?", (asset_id,)).fetchone()
    return dict(row) if row else None


def is_image(row: dict[str, Any]) -> bool:
    return row.get("mime_type") in SAFE_IMAGE_MIME


_IMAGE_MIMES = tuple(sorted(SAFE_IMAGE_MIME))
_IMAGE_IN = ",".join("?" for _ in _IMAGE_MIMES)


def list_for_session(session: str, limit: int = 100) -> list[dict[str, Any]]:
    return list_page(session, limit=limit)["assets"]


def list_page(session: str, *, limit: int = 100, before: str = "",
              favorites: bool = False) -> dict[str, Any]:
    """One page of a chat's images (not deleted), newest first, with the totals.

    Without ``favorites``: by ``created_at`` then ``asset_id``, both descending, walking
    ``idx_media_assets_session_page`` from the cursor. With it: favourites only, by
    ``favorite_at`` then ``asset_id``, walking ``idx_media_assets_session_favorite``.
    ``before`` is the ``next_before`` of the previous page; it names the last row shown,
    so a row added or deleted meanwhile neither repeats nor skips one. One page reads
    ``limit + 1`` rows and the portraits of its chat once (``used_as``).
    Raises MediaError (400) for a cursor this function did not write.
    """
    limit = max(1, min(int(limit or 100), 500))
    key = "favorite_at" if favorites else "created_at"
    clauses = ["session = ?", "deleted_at IS NULL", f"mime_type IN ({_IMAGE_IN})"]
    params: list[Any] = [session, *_IMAGE_MIMES]
    if favorites:
        clauses.append("favorite_at IS NOT NULL")
    if before:
        at, asset_id = _read_cursor(before, favorites)
        # A row value, never "a < ? OR (a = ? AND b < ?)": with the OR the planner seeks only
        # the chat and walks it from the top to the cursor (7 ms at row 49,800 of 50,000);
        # the row value seeks the cursor itself (0.2 ms at any depth).
        clauses.append(f"({key}, asset_id) < (?, ?)")
        params += [at, asset_id]
    con = db.conn()
    rows = [dict(r) for r in con.execute(
        f"""SELECT * FROM media_assets WHERE {' AND '.join(clauses)}
            ORDER BY {key} DESC, asset_id DESC LIMIT ?""",
        (*params, limit + 1),
    ).fetchall()]
    more = len(rows) > limit
    rows = rows[:limit]
    used = _used_as(rows)
    total = con.execute(
        f"""SELECT COUNT(*) FROM media_assets
             WHERE session = ? AND deleted_at IS NULL AND mime_type IN ({_IMAGE_IN})""",
        (session, *_IMAGE_MIMES),
    ).fetchone()[0]
    # Only an image can be marked, so the favourites index alone answers this.
    favorites_total = con.execute(
        """SELECT COUNT(*) FROM media_assets
             WHERE session = ? AND deleted_at IS NULL AND favorite_at IS NOT NULL""",
        (session,),
    ).fetchone()[0]
    return {
        "assets": [_public_row(r, used_as=used.get(r["asset_id"], [])) for r in rows],
        "next_before": _cursor(rows[-1], favorites) if more else None,
        "total": int(total),
        "favorites_total": int(favorites_total),
    }


def deleted_since(*, since: int, session: str = "", limit: int = 1000) -> dict[str, Any]:
    """The images deleted strictly after ``since`` (ms), oldest first (ties by ``asset_id``),
    every chat or one: ``{"deleted": [{asset_id, session, deleted_at}], "next_since",
    "server_now"}`` (contract 489, section 3b, the exact paging rule).

    A page never ends inside a millisecond: when the limit falls among images deleted in the
    same millisecond, the page takes all of that millisecond, past the limit. ``next_since`` is
    then the ``deleted_at`` of the page's last image when more remain, else None, so asking
    again with it never repeats an image, never skips one and always advances.

    The read holds the write lock and ``server_now`` is the millisecond before the one it was
    taken in: a delete still committing waits until it is visible here, and every later delete
    is stamped at or after that millisecond, so strictly after ``server_now``. Asking again with
    ``since=server_now`` misses nothing (an image deleted in the millisecond of the read may
    come twice). Reads ``idx_media_assets_deleted`` or, for one chat,
    ``idx_media_assets_session_deleted`` alone (both cover the columns read)."""
    limit = max(1, min(int(limit or 1000), 1000))
    clauses = ["deleted_at IS NOT NULL", f"mime_type IN ({_IMAGE_IN})"]
    params: list[Any] = [*_IMAGE_MIMES]
    if session:
        clauses.insert(0, "session = ?")
        params.insert(0, session)
    where = " AND ".join(clauses)
    columns = "asset_id, session, deleted_at"
    with _write_lock() as con:
        server_now = db.now_ms() - 1
        rows = [dict(r) for r in con.execute(
            f"""SELECT {columns} FROM media_assets WHERE {where} AND deleted_at > ?
                ORDER BY deleted_at, asset_id LIMIT ?""",
            (*params, int(since), limit + 1)).fetchall()]
        next_since = None
        if len(rows) > limit:
            last = rows[limit - 1]
            rows = rows[:limit]
            # The rest of the last millisecond, past the limit.
            rows += [dict(r) for r in con.execute(
                f"""SELECT {columns} FROM media_assets
                     WHERE {where} AND deleted_at = ? AND asset_id > ?
                     ORDER BY deleted_at, asset_id""",
                (*params, last["deleted_at"], last["asset_id"])).fetchall()]
            more = con.execute(
                f"SELECT 1 FROM media_assets WHERE {where} AND deleted_at > ? LIMIT 1",
                (*params, last["deleted_at"])).fetchone()
            next_since = last["deleted_at"] if more else None
    return {"deleted": rows, "next_since": next_since, "server_now": server_now}


def published_by(session: str, *, created_by_prefix: str, since: int,
                 until: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The images one producer published into a chat between two times, as (kept, discarded):
    kept are not deleted, discarded were deleted at or after ``since``. For a background job
    that publishes from its own process (portrait generation), read when the Host learns the
    job ended, so its gallery changes reach the events stream (media-changed). Reads the page
    index for the kept and the chat's tombstone index for the discarded, each from ``since``."""
    if not session:
        return [], []
    con = db.conn()
    columns = "asset_id, agent_id, session, created_by, created_at, deleted_at"
    kept = [dict(r) for r in con.execute(
        f"""SELECT {columns} FROM media_assets
             WHERE session = ? AND deleted_at IS NULL AND created_at >= ? AND created_at <= ?
               AND mime_type IN ({_IMAGE_IN})""",
        (session, int(since), int(until), *_IMAGE_MIMES)).fetchall()]
    discarded = [dict(r) for r in con.execute(
        f"""SELECT {columns} FROM media_assets
             WHERE session = ? AND deleted_at IS NOT NULL AND deleted_at >= ?
               AND mime_type IN ({_IMAGE_IN})""",
        (session, int(since), *_IMAGE_MIMES)).fetchall()]
    mine = lambda rows: sorted(
        (r for r in rows if str(r["created_by"] or "").startswith(created_by_prefix)
         and int(since) <= int(r["created_at"]) <= int(until)),
        key=lambda r: (r["created_at"], r["asset_id"]))
    return mine(kept), mine(discarded)


def _cursor(row: dict[str, Any], favorites: bool) -> str:
    at = row["favorite_at"] if favorites else row["created_at"]
    return f"{'f' if favorites else 'c'}{int(at)}.{row['asset_id']}"


def _read_cursor(raw: str, favorites: bool) -> tuple[int, str]:
    raw = (raw or "").strip()
    tag, (at, dot, asset_id) = raw[:1], raw[1:].partition(".")
    if tag != ("f" if favorites else "c") or not dot or not at.isdigit() \
            or not asset_id or len(asset_id) > 180 \
            or not all(c.isalnum() or c in "_-" for c in asset_id):
        raise MediaError("bad before")
    return int(at), asset_id


def _used_as(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """``{asset_id: [{"kind": "portrait", "agent_id", "persona"}, ...]}`` for rows that a
    live portrait holds: made from that asset, or the very same file. One query for all the
    rows, through ``idx_agent_portraits_agent_created`` (at most three live portraits per
    agent; a portrait is only ever made from its own agent's image)."""
    agent_ids = sorted({str(r["agent_id"]) for r in rows})
    if not agent_ids:
        return {}
    marks = ",".join("?" for _ in agent_ids)
    portraits = db.conn().execute(
        f"""SELECT p.agent_id, p.media_asset_id, p.storage_path,
                   (SELECT a.persona FROM agents a WHERE a.agent_id = p.agent_id) AS persona
              FROM agent_portraits p
             WHERE p.agent_id IN ({marks}) AND p.deleted_at IS NULL""",
        agent_ids,
    ).fetchall()
    portraits = sorted(portraits, key=lambda p: p["agent_id"])  # at most three per agent
    used: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        seen: set[str] = set()
        for portrait in portraits:
            if portrait["agent_id"] != row["agent_id"] or portrait["agent_id"] in seen:
                continue
            if portrait["media_asset_id"] == row["asset_id"] \
                    or portrait["storage_path"] == row["storage_path"]:
                seen.add(portrait["agent_id"])
                used.setdefault(row["asset_id"], []).append({
                    "kind": "portrait", "agent_id": portrait["agent_id"],
                    "persona": portrait["persona"]})
    return used


def set_favorite(asset_id: str, favorite: bool) -> tuple[dict[str, Any], dict[str, Any], int | None]:
    """Mark or unmark an image. Returns (its public row, its row, the ms of the change or
    None when nothing changed): setting what is already set keeps ``favorite_at``.
    Raises MediaError: 404 unknown, 410 deleted (``deleted_at`` on the error), 422 not an image."""
    with _write_lock() as con:
        row = _writable(con, asset_id)
        changed_at = None
        if (row["favorite_at"] is None) == bool(favorite):
            changed_at = db.now_ms()
            row["favorite_at"] = changed_at if favorite else None
            con.execute("UPDATE media_assets SET favorite_at = ? WHERE asset_id = ?",
                        (row["favorite_at"], asset_id))
    return _public_row(row, used_as=_used_as([row]).get(asset_id, [])), row, changed_at


class Removed(MediaError):
    def __init__(self, row: dict[str, Any]):
        super().__init__("removed", status=410)
        self.row = row


def _writable(con, asset_id: str) -> dict[str, Any]:
    row = con.execute("SELECT * FROM media_assets WHERE asset_id = ?",
                      (asset_id,)).fetchone()
    if not row:
        raise MediaError("not_found", status=404)
    row = dict(row)
    if not is_image(row):
        raise MediaError("not_an_image", status=422)
    if row["deleted_at"] is not None:
        raise Removed(row)
    return row


def delete(asset_id: str, *, deleted_by: str) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Delete an image: (the answer, its row, whether this call deleted it).

    Order: the row is marked deleted and committed first, then its file is released
    (``release_blob``). A crash between the two leaves a deleted row and its file on disk,
    never a row that says the image is there without its file; the next delete of the same
    id (safe to repeat, it answers the first ``deleted_at``) releases the file again.
    Raises MediaError: 404 unknown, 422 not an image."""
    with _write_lock() as con:
        row = con.execute("SELECT * FROM media_assets WHERE asset_id = ?",
                          (asset_id,)).fetchone()
        if not row:
            raise MediaError("not_found", status=404)
        row = dict(row)
        if not is_image(row):
            raise MediaError("not_an_image", status=422)
        changed = row["deleted_at"] is None
        if changed:
            row["deleted_at"], row["deleted_by"] = db.now_ms(), (deleted_by or "")[:120]
            con.execute(
                """UPDATE media_assets SET deleted_at = ?, deleted_by = ?
                    WHERE asset_id = ? AND deleted_at IS NULL""",
                (row["deleted_at"], row["deleted_by"], asset_id))
    release_blob(row["storage_path"], row["sha256"])
    answer = {"asset_id": asset_id, "deleted": True, "deleted_at": row["deleted_at"],
              "still_used_as": _used_as([row]).get(asset_id, [])}
    return answer, row, changed


def release_blob(raw_path: str, sha256: str) -> bool:
    """Remove a media blob file that only deleted assets name. Returns whether it did.

    The file stays while anything still uses it: an asset that is not deleted, a live
    portrait, an agent's or a contact's avatar (a portrait made from an image holds the
    very same file). Only a file some deleted asset names is ever removed, so a portrait's
    or an avatar's own file is never touched. The check and the unlink run under the write
    lock, which a publish of the same bytes also takes (see ``publish``). The file's assets
    are found by its content address (``idx_media_assets_sha``), never by walking the table;
    portraits, agents and contacts are small tables."""
    raw_path, sha256 = str(raw_path or ""), str(sha256 or "")
    if not raw_path or not sha256:
        return False
    with _write_lock() as con:
        owned = con.execute(
            """SELECT 1 FROM media_assets
                WHERE sha256 = ? AND storage_path = ? AND deleted_at IS NOT NULL LIMIT 1""",
            (sha256, raw_path)).fetchone()
        used = con.execute(
            """SELECT 1 FROM media_assets
                WHERE sha256 = ? AND storage_path = ? AND deleted_at IS NULL
               UNION ALL SELECT 1 FROM agent_portraits
                 WHERE storage_path = ? AND deleted_at IS NULL
               UNION ALL SELECT 1 FROM agents WHERE avatar_path = ?
               UNION ALL SELECT 1 FROM personas WHERE avatar_path = ?
               LIMIT 1""",
            (sha256, raw_path, raw_path, raw_path, raw_path)).fetchone()
        removed = False
        if owned and not used:
            try:
                pathlib.Path(raw_path).unlink()
                removed = True
            except FileNotFoundError:
                pass
    return removed


def to_response(row: dict[str, Any]) -> dict[str, Any]:
    return _public_row(row, used_as=_used_as([row]).get(row["asset_id"], []))


def _public_row(row: dict[str, Any], *, used_as: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    asset_id = row["asset_id"]
    caption = row.get("caption") or row.get("source_name") or "file"
    is_image = row["mime_type"] in SAFE_IMAGE_MIME
    favorite_at = row.get("favorite_at")
    return {
        "asset_id": asset_id,
        "session": row["session"],
        "source_name": row["source_name"],
        "file_name": row["source_name"],
        "mime_type": row["mime_type"],
        "bytes": row["bytes"],
        "size_bytes": row["bytes"],
        "width": row.get("width"),
        "height": row.get("height"),
        "caption": row.get("caption") or "",
        "created_by": row.get("created_by") or "agent",
        "created_at": row["created_at"],
        "url": f"/media/{asset_id}",
        "uri": f"clarp-media://asset/{asset_id}",
        "markdown": (f"![{_markdown_alt(caption)}](clarp-media://asset/{asset_id})"
                     if is_image else ""),
        "favorite": favorite_at is not None,
        "favorite_at": favorite_at,
        "used_as": list(used_as or []),
    }


def _safe_name(raw: str, content_type: str) -> str:
    base = pathlib.PurePath((raw or "").replace("\\", "/")).name.strip()
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ()")
    cleaned = cleaned.strip().replace(" ", "_").lstrip(".")
    if not cleaned:
        ext = mimetypes.guess_extension(_primary_type(content_type)) or ".bin"
        cleaned = "media" + ext
    return cleaned[:128]


def _primary_type(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _detect_mime(blob: bytes, content_type: str, name: str) -> str:
    declared = _primary_type(content_type)
    if declared in {"", "application/octet-stream", "text/x-markdown"} \
            and pathlib.PurePath(name).suffix.lower() in _MARKDOWN_EXTENSIONS:
        declared = "text/markdown"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    verified_file = _verified_file_mime(blob, declared)
    if verified_file:
        return verified_file
    if declared in SAFE_FILE_MIME:
        return "application/octet-stream"
    if declared in SAFE_IMAGE_MIME:
        return declared
    if declared and declared != "application/octet-stream":
        return declared
    guessed = _primary_type(mimetypes.guess_type(name)[0] or "")
    if guessed in SAFE_FILE_MIME:
        return "application/octet-stream"
    return guessed or "application/octet-stream"


def _verified_file_mime(blob: bytes, declared: str) -> str:
    if declared == "application/pdf" and blob.startswith(b"%PDF-"):
        return declared
    if declared in {"audio/wav", "audio/x-wav"} and len(blob) >= 12 \
            and blob[:4] == b"RIFF" and blob[8:12] == b"WAVE":
        return declared
    if declared == "audio/mpeg" and (blob.startswith(b"ID3") or (
            len(blob) >= 2 and blob[0] == 0xFF and blob[1] & 0xE0 == 0xE0)):
        return declared
    if declared == "video/webm" and blob.startswith(b"\x1aE\xdf\xa3"):
        return declared
    if declared in {"audio/mp4", "audio/x-m4a", "video/mp4", "video/quicktime"} \
            and len(blob) >= 12 and blob[4:8] == b"ftyp":
        return declared
    if declared in TEXT_FILE_MIME:
        try: blob.decode("utf-8")
        except UnicodeDecodeError: return ""
        return declared
    if declared == "application/json":
        try: json.loads(blob)
        except (UnicodeDecodeError, json.JSONDecodeError): return ""
        return declared
    return ""


def _extension(name: str, mime: str) -> str:
    # A name ending in a dot has no extension: CPython 3.14 gives such a name the suffix "."
    # where 3.12 and 3.13 give none; the Host answers the same on each (R90).
    ext = "" if name.endswith(".") else pathlib.PurePath(name).suffix.lower()
    if ext and all(c.isalnum() or c == "." for c in ext):
        return ext[:12]
    guessed = mimetypes.guess_extension(mime) or ".img"
    if guessed == ".jpe":
        guessed = ".jpg"
    return guessed


def _image_size(blob: bytes, mime: str) -> tuple[int | None, int | None]:
    try:
        if mime == "image/png" and len(blob) >= 24:
            return struct.unpack(">II", blob[16:24])
        if mime == "image/gif" and len(blob) >= 10:
            return struct.unpack("<HH", blob[6:10])
        if mime == "image/jpeg":
            return _jpeg_size(blob)
        if mime == "image/webp" and len(blob) >= 30:
            return _webp_size(blob)
    except (struct.error, ValueError):
        return None, None
    return None, None


def _jpeg_size(blob: bytes) -> tuple[int | None, int | None]:
    i = 2
    while i + 9 < len(blob):
        if blob[i] != 0xFF:
            i += 1
            continue
        marker = blob[i + 1]
        i += 2
        while marker == 0xFF and i < len(blob):
            marker = blob[i]
            i += 1
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > len(blob):
            break
        size = struct.unpack(">H", blob[i:i + 2])[0]
        if size < 2 or i + size > len(blob):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if i + 7 <= len(blob):
                height = struct.unpack(">H", blob[i + 3:i + 5])[0]
                width = struct.unpack(">H", blob[i + 5:i + 7])[0]
                return width, height
        i += size
    return None, None


def _webp_size(blob: bytes) -> tuple[int | None, int | None]:
    kind = blob[12:16]
    if kind == b"VP8 " and len(blob) >= 30:
        width = struct.unpack("<H", blob[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", blob[28:30])[0] & 0x3FFF
        return width, height
    if kind == b"VP8L" and len(blob) >= 25:
        b0, b1, b2, b3 = blob[21], blob[22], blob[23], blob[24]
        width = 1 + (((b1 & 0x3F) << 8) | b0)
        height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        return width, height
    if kind == b"VP8X" and len(blob) >= 30:
        width = 1 + int.from_bytes(blob[24:27], "little")
        height = 1 + int.from_bytes(blob[27:30], "little")
        return width, height
    return None, None


def _markdown_alt(text: str) -> str:
    return (text or "image").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")
