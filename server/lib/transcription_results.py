"""Durable idempotency cache for authoritative voice transcriptions."""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import time

from . import db


_VALID_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_locks_guard = threading.Lock()
_locks: dict[str, tuple[threading.Lock, int]] = {}
_RETENTION_MS = 30 * 24 * 60 * 60 * 1000


class JobIDCollisionError(ValueError):
    """The client reused a transcription ID for different request data."""


def normalize_job_id(value: str | None) -> str:
    job_id = (value or "").strip()
    if job_id and not _VALID_JOB_ID.fullmatch(job_id):
        raise ValueError("invalid transcription id")
    return job_id


def request_fingerprint(audio: bytes, content_type: str, model: str,
                        hands_free: bool) -> str:
    digest = hashlib.sha256()
    digest.update(audio)
    digest.update(b"\0")
    digest.update(content_type.encode())
    digest.update(b"\0")
    digest.update(model.encode())
    digest.update(b"\0")
    digest.update(b"1" if hands_free else b"0")
    return digest.hexdigest()


@contextlib.contextmanager
def serialize(job_id: str):
    """Coalesce concurrent retries for one job within this server process."""
    if not job_id:
        yield
        return
    with _locks_guard:
        existing = _locks.get(job_id)
        lock, users = existing if existing is not None else (threading.Lock(), 0)
        _locks[job_id] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _locks_guard:
            current_lock, current_users = _locks[job_id]
            if current_users == 1:
                del _locks[job_id]
            else:
                _locks[job_id] = (current_lock, current_users - 1)


def load(job_id: str, fingerprint: str) -> dict | None:
    if not job_id:
        return None
    row = db.conn().execute(
        "SELECT request_sha256, response_json FROM transcription_results "
        "WHERE job_id = ?", (job_id,),
    ).fetchone()
    if row is None:
        return None
    if row["request_sha256"] != fingerprint:
        raise JobIDCollisionError("transcription id reused for different audio")
    return json.loads(row["response_json"])


def store(job_id: str, fingerprint: str, response: dict) -> None:
    if not job_id:
        return
    now = int(time.time() * 1000)
    payload = json.dumps(response, separators=(",", ":"))
    con = db.conn()
    con.execute(
        "INSERT INTO transcription_results "
        "(job_id, request_sha256, response_json, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO NOTHING",
        (job_id, fingerprint, payload, now),
    )
    row = con.execute(
        "SELECT request_sha256 FROM transcription_results WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if row is None or row["request_sha256"] != fingerprint:
        raise JobIDCollisionError("transcription id reused for different audio")
    con.execute(
        "DELETE FROM transcription_results WHERE created_at < ?",
        (now - _RETENTION_MS,),
    )


def delete(job_id: str) -> None:
    if not job_id:
        return
    with serialize(job_id):
        db.conn().execute(
            "DELETE FROM transcription_results WHERE job_id = ?", (job_id,))
