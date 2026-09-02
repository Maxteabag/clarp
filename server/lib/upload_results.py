"""Idempotent storage for client uploads.

The iOS app and Share Extension retry ambiguous network failures with the same
logical upload id.  This module makes that retry return the original path
instead of creating another file.  Uploads without an id (the iOS share
extension's one-shot path) keep the random-filename behaviour in ``server.py``.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import tempfile


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class UploadIDCollisionError(ValueError):
    pass


def normalize_upload_id(value: str) -> str:
    value = (value or "").strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError("invalid upload id")
    return value


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = pathlib.Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def store(*, session_dir: pathlib.Path, upload_id: str, name: str,
          content_type: str, blob: bytes,
          record_root: pathlib.Path | None = None) -> pathlib.Path:
    """Store or replay one logical upload under a per-id filesystem lock."""
    upload_id = normalize_upload_id(upload_id)
    key = hashlib.sha256(upload_id.encode()).hexdigest()
    content_sha = hashlib.sha256(blob).hexdigest()
    record_root = record_root or session_dir
    records_dir = record_root / ".upload-records"
    locks_dir = record_root / ".upload-locks"
    records_dir.mkdir(parents=True, exist_ok=True)
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{key}.lock"
    record_path = records_dir / f"{key}.json"

    with lock_path.open("a+b") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        if record_path.exists():
            try:
                record = json.loads(record_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise UploadIDCollisionError(
                    "upload id has an unreadable existing record") from exc
            if (record.get("upload_id") != upload_id
                    or record.get("session_dir") != str(session_dir.resolve())
                    or record.get("name") != name
                    or record.get("content_type") != content_type
                    or record.get("sha256") != content_sha
                    or int(record.get("size", -1)) != len(blob)):
                raise UploadIDCollisionError(
                    "upload id reused for different content")
            existing = pathlib.Path(str(record.get("path") or ""))
            if not existing.is_file() or existing.read_bytes() != blob:
                raise UploadIDCollisionError(
                    "upload id record does not match the stored file")
            return existing

        # The deterministic prefix also lets a retry recover if the process
        # died after the blob rename but before the record rename.
        prefix = f"u-{key[:24]}-"
        matches = list(session_dir.glob(prefix + "*"))
        expected = session_dir / f"{prefix}{name}"
        if matches:
            if len(matches) != 1 or matches[0] != expected or matches[0].read_bytes() != blob:
                raise UploadIDCollisionError(
                    "upload id reused for different content")
        else:
            _atomic_write(expected, blob)

        record = {
            "upload_id": upload_id,
            "session_dir": str(session_dir.resolve()),
            "name": name,
            "content_type": content_type,
            "sha256": content_sha,
            "size": len(blob),
            "path": str(expected),
        }
        _atomic_write(
            record_path,
            json.dumps(record, separators=(",", ":"), sort_keys=True).encode(),
        )
        return expected
