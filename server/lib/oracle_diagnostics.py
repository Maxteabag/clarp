"""Bounded private Oracle JSONL journal; never persist raw audio or credentials."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from . import xdg
from .log import log_exception


class OracleJournal:
    def __init__(self, directory: Path | None = None, *, max_bytes: int = 8 * 1024 * 1024):
        self.directory = directory or xdg.data_dir() / "oracle-diagnostics"
        self.session_id = str(uuid.uuid4())
        self.path = self.directory / f"{self.session_id}.jsonl"
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.sequence = 0
        self.audio_bytes = {"client": 0, "server": 0}
        self.failed = False
        self.started = time.monotonic()

    def event(self, direction: str, raw: str) -> None:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        if not isinstance(kind, str):
            return
        fields = {k: value[k] for k in (
            "event_id", "item_id", "response_id", "call_id", "name", "arguments",
            "transcript", "delta", "audio_start_ms", "audio_end_ms") if k in value}
        if kind in {"input_audio_buffer.append", "response.output_audio.delta"}:
            # Only count encoded lengths: do not copy/serialize audio payloads.
            encoded = value.get("audio", value.get("delta", ""))
            with self.lock:
                self.audio_bytes[direction] += len(encoded) * 3 // 4 if isinstance(encoded, str) else 0
            return
        for key in ("session", "response", "item"):
            obj = value.get(key)
            if not isinstance(obj, dict):
                continue
            for field in ("id", "status", "usage", "status_details", "call_id", "output"):
                if field in obj:
                    fields[f"{key}.{field}"] = obj[field]
        if kind == "session.update":
            contract = value.get("session", {})
            fields["contract_sha256"] = hashlib.sha256(
                json.dumps(contract, sort_keys=True).encode()).hexdigest()
            fields["contract"] = contract  # Already sanitized by the Host proxy.
        if kind == "error":
            error = value.get("error", {})
            if isinstance(error, dict):
                fields["error"] = {k: error[k] for k in ("type", "code", "message", "event_id") if k in error}
        self.record(kind, {"direction": direction, **fields})

    def record(self, kind: str, fields: dict | None = None) -> None:
        with self.lock:
            if self.failed:
                return
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.sequence += 1
                data = (json.dumps({
                    "session_id": self.session_id, "sequence": self.sequence,
                    "timestamp_ms": int(time.time() * 1000),
                    "elapsed_ms": round((time.monotonic() - self.started) * 1000),
                    "event": kind, "fields": fields or {},
                }, ensure_ascii=False) + "\n").encode()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    if os.fstat(fd).st_size + len(data) > self.max_bytes:
                        raise OSError("Oracle diagnostic session reached size limit")
                    with os.fdopen(fd, "ab", closefd=False) as handle:
                        handle.write(data)
                finally:
                    os.close(fd)
                if self.sequence == 1:
                    files = sorted(self.directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                    for old in files[20:]:
                        if old != self.path:
                            old.unlink(missing_ok=True)
            except OSError as exc:
                self.failed = True
                log_exception("oracleDiagnosticsWriteFail", exc)

    def close(self):
        self.record("session.close", {"approx_audio_bytes": dict(self.audio_bytes)})
