"""Bounded physical readback of one newly injected history-only seed.

This certifies bytes observed in the owned, newly materialized file. It does
NOT certify RPC acknowledgement, native quiescence, archive durability or
permission to retry an injection. Those authorities belong to the controller.
The file is read only; no provider or database operation happens here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import pathlib
import stat

from . import codex_transcript, native_rotation_context as context

_MAX_BYTES = 8 * 1024 * 1024
_MAX_RECORDS = 8192


class ReadbackUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SeedReadback:
    native_thread_id: str
    item_id: str
    text_sha256: str
    file_sha256: str
    witness: tuple[int, int, int, int, int]


def _witness(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _read(path, max_bytes):
    # A bad materialized-path answer must not block on FIFO open, follow a
    # redirected symlink, or reach a device before its type can be checked.
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= max_bytes:
            raise ReadbackUnavailable("seed history is empty, non-regular or exceeds the readback bound")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise ReadbackUnavailable("seed history grew beyond the readback bound")
        after = os.fstat(fd)
        current = path.stat()
        if _witness(before) != _witness(after) or _witness(after) != _witness(current) or size != after.st_size:
            raise ReadbackUnavailable("seed history changed or was replaced during readback")
        return b"".join(chunks), _witness(after)
    finally:
        os.close(fd)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native record field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite native record value")


def _verify_records(raw, expected_item, native_thread_id):
    if not raw.endswith(b"\n") or raw.count(b"\n") > _MAX_RECORDS:
        raise ReadbackUnavailable("seed history is incomplete or exceeds the record bound")
    found = headers = 0
    expected_id, expected_text = expected_item["id"], context.item_text(expected_item)
    for line in raw.splitlines():
        if not line.strip():
            continue
        record = json.loads(line, object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            raise ReadbackUnavailable("seed history has an invalid native record")
        payload = record["payload"]
        if record.get("type") == "session_meta":
            if headers or found or payload.get("id") != native_thread_id:
                raise ReadbackUnavailable("seed history has missing, repeated or foreign identity")
            headers += 1
            continue
        if headers != 1:
            raise ReadbackUnavailable("seed history has no authoritative native header")
        kind = payload.get("type") or record.get("type")
        if record.get("type") == "response_item" and kind == "message":
            if payload.get("id") == expected_id:
                if (found or not context.is_context_item(payload, native_thread_id) or
                        context.item_text(payload) != expected_text):
                    raise ReadbackUnavailable("seed history has a duplicate or changed owned item")
                found += 1
                continue
            if payload.get("role") in ("system", "developer"):
                continue
            if (payload.get("role") == "user" and codex_transcript._is_injected_user_context(
                    codex_transcript._response_message_text(payload))):
                continue
            raise ReadbackUnavailable("seed history contains ordinary input or model output")
        if (record.get("type") == "turn_context" or
                (record.get("type") == "event_msg" and kind in ("token_count", "thread_settings_applied"))):
            continue
        raise ReadbackUnavailable("seed history contains execution or unknown native records")
    if headers != 1 or found != 1:
        raise ReadbackUnavailable("seed history does not contain exactly one owned item")


def verify_seed(path: pathlib.Path, expected_item: dict, *, native_thread_id: str,
                max_bytes: int = _MAX_BYTES) -> SeedReadback:
    """Read the exact newly owned path, never a filename-derived native ID.

    The controller must bind the path to its correlated native response and
    hold fresh-thread admission through this read. A stable fstat alone is
    not a whole-controller quiescence or power-loss durability certificate.
    Failure/unknown readback never authorizes another thread or injection.
    """
    if (type(max_bytes) is not int or not 0 < max_bytes <= _MAX_BYTES or
            not context.is_context_item(expected_item, native_thread_id)):
        raise ReadbackUnavailable("invalid owned seed readback authority")
    try:
        raw, witness = _read(pathlib.Path(path), max_bytes)
        _verify_records(raw, expected_item, native_thread_id)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise ReadbackUnavailable("seed history could not be certified") from exc
    return SeedReadback(native_thread_id, expected_item["id"],
                        hashlib.sha256(context.item_text(expected_item).encode("utf-8")).hexdigest(),
                        hashlib.sha256(raw).hexdigest(), witness)
