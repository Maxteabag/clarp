"""SQLite-indexed media assets published by agents for chat/gallery display."""
from __future__ import annotations

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
    "application/pdf", "application/json", "text/plain", "text/csv",
}
SAFE_MEDIA_MIME = SAFE_IMAGE_MIME | SAFE_FILE_MIME


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
    if not storage_path.exists():
        storage_path.write_bytes(blob)

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
    }
    db.conn().execute(
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
    return _public_row(row)


def get(asset_id: str) -> dict[str, Any] | None:
    row = db.conn().execute(
        """SELECT * FROM media_assets
             WHERE asset_id = ? AND deleted_at IS NULL""",
        (asset_id,),
    ).fetchone()
    return dict(row) if row else None


def list_for_session(session: str, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    image_mimes = sorted(SAFE_IMAGE_MIME)
    placeholders = ",".join("?" for _ in image_mimes)
    rows = db.conn().execute(
        f"""SELECT * FROM media_assets
            WHERE session = ? AND deleted_at IS NULL
              AND mime_type IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?""",
        (session, *image_mimes, limit),
    ).fetchall()
    return [_public_row(dict(r)) for r in rows]


def to_response(row: dict[str, Any]) -> dict[str, Any]:
    return _public_row(row)


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    asset_id = row["asset_id"]
    caption = row.get("caption") or row.get("source_name") or "file"
    is_image = row["mime_type"] in SAFE_IMAGE_MIME
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
    if declared in {"text/plain", "text/csv"}:
        try: blob.decode("utf-8")
        except UnicodeDecodeError: return ""
        return declared
    if declared == "application/json":
        try: json.loads(blob)
        except (UnicodeDecodeError, json.JSONDecodeError): return ""
        return declared
    return ""


def _extension(name: str, mime: str) -> str:
    ext = pathlib.PurePath(name).suffix.lower()
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
