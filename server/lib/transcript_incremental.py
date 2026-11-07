"""Incremental transcript import, in shadow mode.

Every watcher tick re-parses a whole transcript and compares every row with
the store, although a tick almost always only appends a few records. An
incremental import would re-parse from the last user turn instead, resuming
from a checkpoint. This module holds that machinery and runs it as a SHADOW:
the full import stays the only thing that writes rows clients see; on a
bounded sample of ticks the incremental result is computed next to it, never
written, and compared. Divergences are logged; nothing switches over on its
own (docs/transcript-incremental-import.md).

The pieces:

* ``Checkpoint`` (table ``transcript_import_checkpoints``): the file identity
  and parser version it is valid for, the byte offset and turn number of the
  boundary user turn the next import re-parses from, the true turn count,
  hashes of the bytes before the boundary and of the complete bytes after it,
  a digest of the turns before it, the facts the reference scan needs about
  those turns, and the store's carried state at that turn (client provenance
  cursor, heartbeat/leader/dreaming origin, agy authority, twin adoption).
  It is written inside the full import's last transaction.
* ``decide``: incremental or full. Full on a missing checkpoint, a parser
  version change, a new inode, a shrink, mtime going backwards, changed bytes
  before the boundary or in the region already parsed, a due audit, a tail
  over budget, or a new record referring to work before the boundary (a late
  tool result, a sub-agent notification, an out-of-order agy step).
* ``ShadowBudget``: off unless ``[transcript_shadow] enabled = true``; at most
  one run in ``sample_every`` imports, never two at once, skipped under load
  or IO/memory pressure, and capped in CPU seconds per hour.
* ``import_transcript``: the import the watcher and /log run. Without the
  shadow it is exactly parse + ``store_transcript_turns``.
"""
from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import pathlib
import re
import sys
import threading
import time
import types
from time import monotonic
from dataclasses import dataclass, replace
from typing import Any, Callable

SUPPORTED_BACKENDS = ("claude", "codex", "agy")

# The sources whose behaviour a checkpoint depends on. Any edit to one of them
# changes the parser version, so every checkpoint taken by older code is
# refused and the next import is a full one.
_VERSION_SOURCES = {
    "claude": ("claude_transcript.py", "claude_reply_evidence.py", "backend/claude.py"),
    "codex": ("codex_transcript.py", "codex_tool_cells.py", "backend/codex.py"),
    "agy": ("agy_transcript.py", "codex_transcript.py", "codex_tool_cells.py", "opencode_transcript.py",
            "claude_transcript.py", "claude_reply_evidence.py", "backend/agy.py"),
}
_COMMON_SOURCES = ("transcript_incremental.py", "backend/base.py", "message_writes.py",
                   "message_import_rows.py", "message_import_common.py",
                   "message_context.py", "message_turns.py", "voice_markup.py",
                   # The store's ack-token decisions (AUT-07).
                   "origins.py", "heartbeat.py", "dreaming.py", "team_leader.py",
                   "team_store.py", "oracle_delegations.py")

_HEAD_BYTES = 4096
_WINDOW_BYTES = 64 * 1024
_SAMPLES = 16
_SAMPLE_BYTES = 4096
# Boundary candidates tried per checkpoint, latest first.
_CANDIDATES = 6
# Signed bounded-audit assumption (801/802), for authoritative tails only.
# Older/tighter shadow settings may require an earlier full import.
_TAIL_AUDIT_TICKS = 20
_TAIL_AUDIT_SECONDS = 600.0


@functools.cache
def parser_version(backend: str) -> str:
    root = pathlib.Path(__file__).resolve().parent
    digest = hashlib.sha256(backend.encode())
    for name in (*_VERSION_SOURCES[backend], *_COMMON_SOURCES):
        digest.update(name.encode() + b"\0" + (root / name).read_bytes())
    return digest.hexdigest()[:24]


# ---- file identity and hashes -------------------------------------------

@dataclass(frozen=True)
class Identity:
    device: int
    inode: int
    size: int
    mtime_ns: int


def identity(path: pathlib.Path) -> Identity:
    st = os.stat(path)
    return Identity(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def complete_size(path: pathlib.Path, size: int) -> int:
    """Bytes up to and including the last newline before ``size``: what is
    certainly whole. A half-written record after it is re-read next time."""
    with open(path, "rb") as f:
        end = size
        while end > 0:
            start = max(0, end - 65536)
            f.seek(start)
            chunk = f.read(end - start)
            at = chunk.rfind(b"\n")
            if at >= 0:
                return start + at + 1
            end = start
    return 0


def prefix_hash(path: pathlib.Path, offset: int) -> str:
    """Hash of the bytes before ``offset``: the head, sixteen samples spread
    across it and the 64 KiB just before it. Reading all of a 185 MB prefix
    per tick would cost what the incremental import saves; a same-length
    in-place edit that misses every sampled block is left to the audit."""
    digest = hashlib.blake2b(str(offset).encode(), digest_size=16)
    with open(path, "rb") as f:
        def block(start: int, length: int) -> None:
            start = max(0, min(start, offset))
            f.seek(start)
            digest.update(f.read(max(0, min(length, offset - start))))
        block(0, _HEAD_BYTES)
        for i in range(1, _SAMPLES + 1):
            block(i * offset // (_SAMPLES + 1), _SAMPLE_BYTES)
        block(offset - _WINDOW_BYTES, _WINDOW_BYTES)
    return digest.hexdigest()


def range_hash(path: pathlib.Path, start: int, end: int) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start
        while remaining > 0:
            chunk = f.read(min(remaining, 1 << 20))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def turn_digests(turns: list[dict]) -> list[bytes]:
    return [hashlib.blake2b(json.dumps(turn, sort_keys=True, default=str).encode(),
                            digest_size=16).digest() for turn in turns]


def prefix_turns_hash(digests: list[bytes], count: int) -> str:
    digest = hashlib.blake2b(str(count).encode(), digest_size=16)
    for item in digests[:count]:
        digest.update(item)
    return digest.hexdigest()


# ---- parsing a byte range -----------------------------------------------

class _Range(io.RawIOBase):
    def __init__(self, path: pathlib.Path, start: int, end: int):
        self._file = open(path, "rb")
        self._file.seek(start)
        self._left = max(0, end - start)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self._left <= 0:
            return 0
        view = memoryview(buffer)[:self._left]
        count = self._file.readinto(view)
        self._left -= count or 0
        return count or 0

    def close(self) -> None:
        self._file.close()
        super().close()


class _RangePath(pathlib.PosixPath):
    """The transcript path, but ``open()`` reads only bytes [start, end).

    The parsers take a path and open it themselves; this lets them parse a
    tail unchanged. Everything derived from it (``parent``, ``stem``) is the
    real path, so Claude still finds its sub-agent transcripts."""

    def __init__(self, path, *, start: int, end: int):
        super().__init__(path)
        self.range_start, self.range_end = start, end

    def with_segments(self, *segments):
        return pathlib.Path(*segments)

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if mode not in ("r", "rt"):
            raise ValueError(f"a transcript range is read-only text, not {mode!r}")
        raw = _Range(pathlib.Path(self), self.range_start, self.range_end)
        return io.TextIOWrapper(io.BufferedReader(raw), encoding=encoding,
                                errors=errors, newline=newline)


def parse_range(backend: str, path: pathlib.Path, start: int, end: int) -> list[dict]:
    from . import backends
    return backends.parse_turns(backend, _RangePath(path, start=start, end=end))


def _lines(path: pathlib.Path, start: int, end: int):
    """(offset, raw line) for every newline-terminated line in [start, end)."""
    with open(path, "rb") as f:
        f.seek(start)
        offset = start
        while offset < end:
            raw = f.readline(end - offset)
            if not raw:
                return
            if raw.endswith(b"\n"):
                yield offset, raw
            offset += len(raw)


def _records(path: pathlib.Path, start: int, end: int):
    for _offset, raw in _lines(path, start, end):
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


# ---- what a tail may refer to -------------------------------------------
# A record after the boundary that changes a turn before it (a tool result,
# a sub-agent's notification, an agy step filed out of order) would be lost
# by a tail-only parse. These scans find such references; any one means a
# full import. ``parser_carry`` is what they need to know about the turns
# before the boundary.

_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>(.*?)</tool-use-id>", re.DOTALL)
_AGY_TASK_END_RE = re.compile(r"\b(finished|was canceled) with result:|background tasks have been stopped", re.I)


def _claude_carry(prefix: list[dict], boundary_record: dict) -> dict[str, Any]:
    pending, subagents = [], []
    for turn in prefix:
        for tool in turn.get("tools") or []:
            if tool.get("status") not in ("ok", "error"):
                pending.append(str(tool.get("id") or ""))
        for cell in turn.get("display_cells") or []:
            if cell.get("kind") == "subagents":
                subagents.append(str(cell.get("id") or ""))
    return {"pending_tool_ids": pending, "subagent_ids": subagents}


def _agy_carry(prefix: list[dict], boundary_record: dict) -> dict[str, Any]:
    step = boundary_record.get("step_index")
    return {"boundary_step": step if isinstance(step, int) else 0,
            "running_tools": sum(1 for turn in prefix for tool in turn.get("tools") or []
                                 if tool.get("status") == "running")}


_PARSER_CARRIES = {"claude": _claude_carry, "agy": _agy_carry}


def parser_carry(backend: str, prefix: list[dict], boundary_record: dict) -> dict[str, Any]:
    carry = _PARSER_CARRIES.get(backend)
    return carry(prefix, boundary_record) if carry else {}


def _claude_reference(records, carry: dict) -> str:
    pending = set(carry.get("pending_tool_ids") or [])
    subagents = set(carry.get("subagent_ids") or [])
    defined: set[str] = set()
    uuids: set[str] = set()
    for record in records:
        kind = record.get("type")
        if kind == "assistant" and record.get("parentUuid") \
                and record["parentUuid"] not in uuids:
            return "claude_parent_before_boundary"
        if record.get("uuid"):
            uuids.add(record["uuid"])
        texts = []
        if kind == "queue-operation" and isinstance(record.get("content"), str):
            texts.append(record["content"])
        content = (record.get("message") or {}).get("content")
        if isinstance(content, str):
            texts.append(content)
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id"):
                defined.add(str(block["id"]))
            elif block.get("type") == "tool_result":
                ref = str(block.get("tool_use_id") or block.get("toolUseId") or "")
                if not ref:
                    return "claude_unnamed_tool_result"
                if ref not in defined:
                    return "claude_late_tool_result"
            elif block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
        for text in texts:
            if "<task-notification>" in text and any(
                    ref.strip() in subagents and ref.strip() not in defined
                    for ref in _TOOL_USE_ID_RE.findall(text)):
                return "claude_late_subagent_notification"
    return ""


def _codex_turn_id(payload: dict) -> str:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(payload.get("turn_id") or metadata.get("turn_id") or "")


def _codex_reference(records, carry: dict) -> str:
    defined: set[str] = set()
    opened: set[str] = set()
    for record in records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        inner = payload.get("type") or record.get("type") or ""
        turn_id = _codex_turn_id(payload)
        if inner in ("task_started", "turn_context") or record.get("type") == "turn_context":
            if turn_id:
                opened.add(turn_id)
            continue
        if turn_id and turn_id not in opened and inner not in ("task_complete", "token_count"):
            return "codex_turn_before_boundary"
        item = payload.get("item") or record.get("item")
        if isinstance(item, dict) and (item.get("id") or item.get("call_id")):
            defined.add(str(item.get("id") or item.get("call_id")))
        if inner in ("function_call", "custom_tool_call"):
            defined.add(str(payload.get("call_id") or payload.get("id") or ""))
        elif inner in ("function_call_output", "custom_tool_call_output", "patch_apply_end"):
            ref = str(payload.get("call_id") or "")
            if not ref or ref not in defined:
                return "codex_output_before_boundary"
    return ""


def _agy_reference(records, carry: dict) -> str:
    boundary = int(carry.get("boundary_step") or 0)
    running = int(carry.get("running_tools") or 0)
    for record in records:
        step = record.get("step_index")
        if isinstance(step, int) and step < boundary:
            return "agy_step_out_of_order"
        if running and record.get("type") == "SYSTEM_MESSAGE" and \
                _AGY_TASK_END_RE.search(str(record.get("content") or "")):
            return "agy_background_before_boundary"
    return ""


_REFERENCE_SCANS = {"claude": _claude_reference, "codex": _codex_reference,
                    "agy": _agy_reference}


def reference_outside_tail(backend: str, path: pathlib.Path, start: int, end: int,
                           carry: dict) -> str:
    """Why a record in [start, end) reaches before the boundary, or ''."""
    return _REFERENCE_SCANS[backend](_records(path, start, end), carry)


_BOUNDARY_MARKERS = {
    "claude": lambda raw: b'"type":"user"' in raw and b'"tool_result"' not in raw
    and b'"isMeta":true' not in raw,
    # A Codex turn's own records (its turn_id) start at task_started, ahead
    # of the user message, so the boundary is there.
    "codex": lambda raw: b'"task_started"' in raw,
    "agy": lambda raw: b'"USER_INPUT"' in raw,
}


def boundary_candidates(backend: str, path: pathlib.Path, end: int,
                        max_tail_bytes: int) -> list[tuple[int, dict]]:
    """Latest offsets that may open the last user turn, newest first."""
    marker = _BOUNDARY_MARKERS[backend]
    found: list[tuple[int, dict]] = []
    for offset, raw in _lines(path, max(0, end - max_tail_bytes), end):
        if marker(raw.replace(b'": "', b'":"')):
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            found.append((offset, record if isinstance(record, dict) else {}))
    return found[::-1][:_CANDIDATES]


# ---- checkpoints -----------------------------------------------------------

@dataclass
class Checkpoint:
    agent_id: str
    backend_session_id: str
    source_file: str
    backend: str
    parser_version: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    complete_size: int
    boundary_offset: int
    boundary_seq: int
    turn_count: int
    prefix_hash: str
    tail_hash: str
    prefix_turns_hash: str
    parser_carry: dict
    store_carry: dict
    imports_since_audit: int
    audited_at: int
    updated_at: int


def load_checkpoint(database, agent_id: str, backend_session_id: str,
                    source_file: str) -> Checkpoint | None:
    row = database.execute(
        """SELECT * FROM transcript_import_checkpoints
            WHERE agent_id=? AND backend_session_id=? AND source_file=?""",
        (agent_id, backend_session_id, source_file)).fetchone()
    if row is None:
        return None
    values = dict(row)
    values["parser_carry"] = json.loads(values.pop("parser_carry_json"))
    values["store_carry"] = json.loads(values.pop("store_carry_json"))
    return Checkpoint(**values)


def write_checkpoint(database, checkpoint: Checkpoint) -> None:
    """Upsert, but never replace a checkpoint of the same file that covers
    more of it: of two imports racing, the one that read less may commit last."""
    database.execute(
        """INSERT INTO transcript_import_checkpoints (
               agent_id, backend_session_id, source_file, backend, parser_version,
               device, inode, size, mtime_ns, complete_size, boundary_offset,
               boundary_seq, turn_count, prefix_hash, tail_hash, prefix_turns_hash,
               parser_carry_json, store_carry_json, imports_since_audit,
               audited_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(agent_id, backend_session_id, source_file) DO UPDATE SET
               backend=excluded.backend, parser_version=excluded.parser_version,
               device=excluded.device, inode=excluded.inode, size=excluded.size,
               mtime_ns=excluded.mtime_ns, complete_size=excluded.complete_size,
               boundary_offset=excluded.boundary_offset,
               boundary_seq=excluded.boundary_seq, turn_count=excluded.turn_count,
               prefix_hash=excluded.prefix_hash, tail_hash=excluded.tail_hash,
               prefix_turns_hash=excluded.prefix_turns_hash,
               parser_carry_json=excluded.parser_carry_json,
               store_carry_json=excluded.store_carry_json,
               imports_since_audit=excluded.imports_since_audit,
               audited_at=excluded.audited_at, updated_at=excluded.updated_at
             WHERE excluded.complete_size >= transcript_import_checkpoints.complete_size
                OR excluded.inode <> transcript_import_checkpoints.inode
                OR excluded.device <> transcript_import_checkpoints.device
                OR excluded.parser_version <> transcript_import_checkpoints.parser_version""",
        (checkpoint.agent_id, checkpoint.backend_session_id, checkpoint.source_file,
         checkpoint.backend, checkpoint.parser_version, checkpoint.device,
         checkpoint.inode, checkpoint.size, checkpoint.mtime_ns,
         checkpoint.complete_size, checkpoint.boundary_offset,
         checkpoint.boundary_seq, checkpoint.turn_count, checkpoint.prefix_hash,
         checkpoint.tail_hash, checkpoint.prefix_turns_hash,
         json.dumps(checkpoint.parser_carry, separators=(",", ":")),
         json.dumps(checkpoint.store_carry, separators=(",", ":")),
         checkpoint.imports_since_audit, checkpoint.audited_at, checkpoint.updated_at))


@dataclass
class Boundary:
    """Where a new checkpoint will sit, chosen from a full parse."""
    offset: int
    seq: int
    parser_carry: dict
    prefix_hash: str
    tail_hash: str
    prefix_turns_hash: str
    complete_size: int


def choose_boundary(backend: str, path: pathlib.Path, turns: list[dict],
                    digests: list[bytes], ident: Identity,
                    max_tail_bytes: int) -> tuple[Boundary | None, str]:
    """The latest user-turn boundary whose tail, parsed alone, reproduces the
    full parse's tail exactly and refers to nothing before it."""
    complete = complete_size(path, ident.size)
    reasons = []
    for offset, record in boundary_candidates(backend, path, complete, max_tail_bytes):
        tail = parse_range(backend, path, offset, ident.size)
        seq = len(turns) - len(tail)
        if seq < 0 or not tail or tail[0].get("role") != "user" or tail != turns[seq:]:
            reasons.append("tail_differs")
            continue
        carry = parser_carry(backend, turns[:seq], record)
        why = reference_outside_tail(backend, path, offset, complete, carry)
        if why:
            reasons.append(why)
            continue
        return Boundary(offset=offset, seq=seq, parser_carry=carry,
                        prefix_hash=prefix_hash(path, offset),
                        tail_hash=range_hash(path, offset, complete),
                        prefix_turns_hash=prefix_turns_hash(digests, seq),
                        complete_size=complete), ""
    return None, (reasons[0] if reasons else "no_boundary")


def decide(checkpoint: Checkpoint | None, backend: str, path: pathlib.Path,
           ident: Identity, *, now_ms: int, audit_every: int, audit_seconds: float,
           max_tail_bytes: int) -> str:
    """'' when an incremental import from ``checkpoint`` is safe, otherwise
    the reason a full import is needed."""
    if checkpoint is None:
        return "no_checkpoint"
    if checkpoint.backend != backend or checkpoint.parser_version != parser_version(backend):
        return "parser_version"
    if (checkpoint.device, checkpoint.inode) != (ident.device, ident.inode):
        return "inode"
    if ident.size < checkpoint.complete_size:
        return "shrink"
    if ident.mtime_ns < checkpoint.mtime_ns:
        return "mtime_backwards"
    if checkpoint.imports_since_audit >= audit_every or \
            now_ms - checkpoint.audited_at >= audit_seconds * 1000:
        return "audit"
    if ident.size - checkpoint.boundary_offset > max_tail_bytes:
        return "tail_too_large"
    if prefix_hash(path, checkpoint.boundary_offset) != checkpoint.prefix_hash:
        return "prefix_rewritten"
    if range_hash(path, checkpoint.boundary_offset, checkpoint.complete_size) \
            != checkpoint.tail_hash:
        return "tail_rewritten"
    why = reference_outside_tail(backend, path, checkpoint.boundary_offset,
                                 complete_size(path, ident.size),
                                 checkpoint.parser_carry)
    return why


# ---- the shadow budget -----------------------------------------------------

_DEFAULTS = {
    "enabled": False,
    "sample_every": 20,
    "max_cpu_seconds_per_hour": 30.0,
    "max_load_per_cpu": 0.75,
    "max_io_pressure": 10.0,
    "max_memory_pressure": 10.0,
    "max_tail_bytes": 16 * 1024 * 1024,
    "audit_every": 50,
    "audit_seconds": 3600.0,
}


def settings() -> dict[str, Any]:
    from . import config
    raw = getattr(config.load(), "transcript_shadow", {}) or {}
    out = dict(_DEFAULTS)
    for key, default in _DEFAULTS.items():
        if key in raw:
            out[key] = type(default)(raw[key])
    if os.environ.get("CLARP_TRANSCRIPT_SHADOW", "").strip().lower() in ("0", "false", "off", "no"):
        out["enabled"] = False   # the kill switch outranks the config file
    return out


def _pressure(resource: str) -> float | None:
    try:
        with open(f"/proc/pressure/{resource}") as f:
            first = f.readline()
    except OSError:
        return None
    match = re.search(r"avg10=([0-9.]+)", first)
    return float(match.group(1)) if match else None


class ShadowBudget:
    """Whether this import may run the shadow, and what the shadow cost."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = threading.Lock()
        self._imports = 0
        self._hour_started = time.monotonic()
        self._hour_cpu = 0.0
        self._hour_wall = 0.0
        self._hour_runs = 0
        self.skipped: dict[str, int] = {}

    def admit(self, conf: dict[str, Any]) -> str:
        """'' and the run lock held, or why the shadow is skipped."""
        with self._lock:
            if not conf["enabled"]:
                return "disabled"
            self._imports += 1
            if self._imports % max(1, int(conf["sample_every"])):
                return "not_sampled"
            if time.monotonic() - self._hour_started >= 3600:
                self._hour_started = time.monotonic()
                self._hour_cpu = self._hour_wall = 0.0
                self._hour_runs = 0
            if self._hour_cpu >= conf["max_cpu_seconds_per_hour"]:
                return self._skip("hourly_cpu_cap")
        load = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
        if load > conf["max_load_per_cpu"]:
            return self._skip("load")
        io_pressure = _pressure("io")
        if io_pressure is not None and io_pressure > conf["max_io_pressure"]:
            return self._skip("io_pressure")
        memory_pressure = _pressure("memory")
        if memory_pressure is not None and memory_pressure > conf["max_memory_pressure"]:
            return self._skip("memory_pressure")
        if not self._running.acquire(blocking=False):
            return self._skip("concurrent")
        return ""

    def _skip(self, reason: str) -> str:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        return reason

    def release(self, cpu_s: float, wall_s: float) -> dict[str, Any]:
        with self._lock:
            self._hour_cpu += cpu_s
            self._hour_wall += wall_s
            self._hour_runs += 1
            spent = {"hour_cpu_s": round(self._hour_cpu, 3),
                     "hour_wall_s": round(self._hour_wall, 3),
                     "hour_runs": self._hour_runs,
                     "skipped": dict(self.skipped)}
            self.skipped.clear()
        self._running.release()
        return spent


BUDGET = ShadowBudget()


# ---- the import ------------------------------------------------------------

_ROW_FIELDS = ("id", "role", "text", "origin", "sender_agent_id", "timestamp",
               "tools", "display_cells", "kind")


def _row_view(row: dict) -> tuple:
    return tuple(json.dumps(row.get(name), sort_keys=True, default=str) for name in _ROW_FIELDS)



@dataclass
class _TailState:
    checkpoint: Checkpoint
    raw_inputs: tuple
    file_digest: Any
    audited_monotonic: float


def _tail_weight(key, revision, state) -> int:
    if state is None:
        return 1
    # Include payload strings and row/container overhead, not just entry count.
    total = len(json.dumps(state.checkpoint.__dict__, default=str)) + 1024
    for component in state.raw_inputs:
        total += sys.getsizeof(component)
        for row in component:
            total += sys.getsizeof(row)
            if not isinstance(row, str):
                total += sum(sys.getsizeof(value) for value in row)
    return total


from .revisioned_cache import RevisionedCache
_TAIL_STATES = RevisionedCache("transcript-tail-state", max_entries=64,
                              max_weight=32 * 1024 * 1024, weight=_tail_weight)


def _tail_key(path, agent_id, backend_session_id):
    from . import db
    return (str(db.DB_PATH), agent_id, backend_session_id, str(path))


def _prefix_has_effects(turns) -> bool:
    """P1 leaves effect-bearing prefixes on the full recovery path.

    Their effects may need retry even when their message row is unchanged.
    They cannot be skipped merely because bytes or rows were checkpointed.
    """
    from . import dreaming, heartbeat, team_leader, team_store
    for turn in turns:
        # Discovery is external to the parent bytes: a missing child/meta
        # directory at bootstrap does not make this prefix immutable.
        if (any(cell.get("kind") == "subagents" for cell in turn.get("display_cells") or [])
                or any(tool.get("name") in ("Agent", "Task") for tool in turn.get("tools") or [])):
            return True
        text = str(turn.get("text") or "")
        if turn.get("role") == "user":
            if (heartbeat.should_skip_heartbeat_prompt(text)
                    or team_leader.should_skip_leader_tick_prompt(text)
                    or dreaming.should_skip_dream_prompt(text)):
                return True
        elif turn.get("role") == "assistant":
            if (team_store.extract_team_blocks(text)
                    or heartbeat.HEARTBEAT_OK in text or team_leader.LEADER_NOOP in text
                    or dreaming.process_assistant_text("", text, origin="dreaming", live=True)
                    != (False, text)):
                return True
    return False



def _read_tail_digest(path, start, end, digest=None):
    """Extend a COPY with [start,end); ordinary ticks assume append-only801.

    Only full audits read the entire previously certified prefix. The copied
    process-local state is published only with successful checkpoint lineage.
    """
    digest = digest.copy() if digest is not None else hashlib.blake2b(digest_size=16)
    with open(path, "rb") as source:
        source.seek(start)
        remaining = end - start
        while remaining > 0:
            block = source.read(min(remaining, 1 << 20))
            if not block:
                raise OSError("transcript shrank during digest read")
            digest.update(block)
            remaining -= len(block)
    return digest


def _tail_log(code, *, retention=False):
    """Fixed codes and unit counts; no session, path, text or message IDs."""
    from .log import log
    log("transcriptTailRetention" if retention else "transcriptTailDecision",
        f"code={code} count=1")


def _refuse_tail(code):
    _tail_log("full_" + code)
    return None


def _checkpoint_refused(status):
    reasons = status.get("refused", ())
    for code in reasons:
        _tail_log("checkpoint_" + code, retention=True)
    return bool(reasons)


def _cache_committed_tail(key, revision, checkpoint, raw_inputs, file_digest, audited_monotonic):
    from . import db
    if raw_inputs is None or file_digest is None:
        _tail_log("checkpoint_not_written", retention=True)
        return
    try:
        current = identity(pathlib.Path(checkpoint.source_file))
    except OSError:
        _tail_log("file_unavailable_after_store", retention=True)
        return
    if current != Identity(checkpoint.device, checkpoint.inode, checkpoint.size, checkpoint.mtime_ns):
        _tail_log("file_moved_before_publication", retention=True)
        return
    if load_checkpoint(db.conn(), checkpoint.agent_id, checkpoint.backend_session_id,
                       checkpoint.source_file) != checkpoint:
        _tail_log("checkpoint_superseded", retention=True)
        return
    if raw_inputs[4]:
        _tail_log("oracle_history", retention=True)
        return
    if any(row["origin"] not in ("user", "agent", None, "") for row in raw_inputs[1]):
        _tail_log("automation_provenance", retention=True)
        return
    state = _TailState(checkpoint, raw_inputs, file_digest, audited_monotonic)
    _TAIL_STATES.put(key, revision, state)
    found, retained = _TAIL_STATES.get(key, revision)
    if not found or retained is not state:
        _tail_log("frontier_not_retained", retention=True)


def _try_tail(backend, path, agent_id, backend_session_id, conf, started):
    from . import db, message_writes
    key = _tail_key(path, agent_id, backend_session_id)
    revision = parser_version(backend)
    found, state = _TAIL_STATES.get(key, revision)
    # Invalidate before any attempt, including a failure after a partial batch
    # or external effect. Only successful committed progress re-admits carry.
    _TAIL_STATES.put(key, revision, None)
    if not found or state is None:
        return _refuse_tail("no_frontier")
    cp = load_checkpoint(db.conn(), agent_id, backend_session_id, str(path))
    if cp != state.checkpoint:
        return _refuse_tail("checkpoint_changed")
    before = identity(path)
    if complete_size(path, before.size) != before.size:
        return _refuse_tail("partial_record")
    age = monotonic() - state.audited_monotonic
    if age < 0:
        return _refuse_tail("audit_clock_unknown")
    tick_limit = min(_TAIL_AUDIT_TICKS, int(conf["audit_every"]))
    time_limit = min(_TAIL_AUDIT_SECONDS, float(conf["audit_seconds"]))
    # Audit BEFORE the twentieth attempted tail (19 completed tails), or the
    # first import after 600s. Appends never renew either audit deadline.
    due = cp.imports_since_audit >= tick_limit - 1 or age >= time_limit
    reason = decide(cp, backend, path, before, now_ms=db.now_ms(),
                    audit_every=0 if due else sys.maxsize, audit_seconds=float("inf"),
                    max_tail_bytes=int(conf["max_tail_bytes"]))
    if reason == "audit":
        previous = _read_tail_digest(path, 0, cp.complete_size)
        if previous.digest() != state.file_digest.digest():
            return _refuse_tail("audit_prefix_rewritten")
    if reason:
        return _refuse_tail(reason)
    current_digest = _read_tail_digest(path, cp.complete_size, before.size, state.file_digest)
    turns = parse_range(backend, path, cp.boundary_offset, before.size)
    parsed = time.perf_counter()
    if identity(path) != before:
        return _refuse_tail("file_moved_during_parse")
    next_cp = replace(cp, size=before.size, mtime_ns=before.mtime_ns,
                      complete_size=before.size, turn_count=cp.boundary_seq + len(turns),
                      tail_hash=range_hash(path, cp.boundary_offset, before.size),
                      imports_since_audit=cp.imports_since_audit + 1, updated_at=db.now_ms())
    raw_inputs = None
    checkpoint_status = {}

    def publish(database, captured):
        nonlocal raw_inputs
        if identity(path) != before:
            return
        write_checkpoint(database, next_cp)
        raw_inputs = message_writes._prepare_import_prelude(
            database, agent_id, backend_session_id, [], raw_only=True)

    try:
        message_writes.store_transcript_tail(
            agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=str(path), turns=turns, start_seq=cp.boundary_seq,
            carry=cp.store_carry, expected_prelude=state.raw_inputs, on_stored=publish,
            checkpoint_status=checkpoint_status)
    except message_writes._TailInvalidated:
        return _refuse_tail("carry_inputs_changed")
    _tail_log("tail_applied")
    if not _checkpoint_refused(checkpoint_status):
        _cache_committed_tail(key, revision, next_cp, raw_inputs, current_digest,
                              state.audited_monotonic)
    return {"rows": next_cp.turn_count, "parse_s": parsed - started,
            "store_s": time.perf_counter() - parsed, "shadow": "not_sampled_tail",
            "apply_mode": "tail", "parsed_bytes": before.size - cp.boundary_offset}


def _store_full_with_tail_checkpoint(backend, path, agent_id, backend_session_id,
                                     turns, before, conf):
    from . import db, message_store, message_writes
    key = _tail_key(path, agent_id, backend_session_id)
    revision = parser_version(backend)
    kwargs = {}
    checkpoint = None
    raw_inputs = None
    current_digest = None
    audited_monotonic = None
    checkpoint_status = {}
    refusal = "file_identity_unavailable"
    refusal_logged = False
    try:
        if before is None:
            pass
        elif not turns:
            refusal = "empty_transcript"
        elif turns[0].get("role") != "user":
            refusal = "assistant_prefix"
        elif identity(path) != before:
            refusal = "file_moved_before_bootstrap"
        elif complete_size(path, before.size) != before.size:
            refusal = "partial_record"
        else:
            boundary, why = choose_boundary(backend, path, turns, turn_digests(turns), before,
                                            int(conf["max_tail_bytes"]))
            if boundary is None:
                refusal = "no_safe_boundary_" + why
            elif _prefix_has_effects(turns[:boundary.seq]):
                refusal = "effect_bearing_prefix"
            else:
                refusal = ""
                # Age starts at full-prefix certification, never at an append.
                audited_monotonic = monotonic()
                current_digest = _read_tail_digest(path, 0, before.size)
                captured = {}

                def publish(database, carry):
                    nonlocal checkpoint, raw_inputs
                    if not carry or identity(path) != before:
                        return
                    now = db.now_ms()
                    checkpoint = Checkpoint(
                        agent_id, backend_session_id, str(path), backend, parser_version(backend),
                        before.device, before.inode, before.size, before.mtime_ns,
                        boundary.complete_size, boundary.offset, boundary.seq, len(turns),
                        boundary.prefix_hash, boundary.tail_hash, boundary.prefix_turns_hash,
                        boundary.parser_carry, dict(carry), 0, now, now)
                    write_checkpoint(database, checkpoint)
                    raw_inputs = message_writes._prepare_import_prelude(
                        database, agent_id, backend_session_id, [], raw_only=True)

                kwargs = {"capture_at": boundary.seq, "captured": captured, "on_stored": publish}
    except OSError:
        # Losing optimization input must not discard an already completed full parse.
        _tail_log("bootstrap_file_io", retention=True)
        refusal_logged = True
        refusal = "bootstrap_file_io"
        kwargs = {}
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id=backend_session_id,
        source_file=str(path), turns=turns, checkpoint_status=checkpoint_status, **kwargs)
    if _checkpoint_refused(checkpoint_status):
        return
    if refusal:
        if not refusal_logged:
            _tail_log(refusal, retention=True)
    elif checkpoint is None:
        _tail_log("checkpoint_not_written", retention=True)
    else:
        _cache_committed_tail(key, revision, checkpoint, raw_inputs, current_digest, audited_monotonic)


def import_transcript(*, backend: str, path: pathlib.Path, agent_id: str,
                      backend_session_id: str,
                      parse: Callable[[pathlib.Path], list[dict]] | None = None,
                      ) -> dict[str, Any]:
    """Parse and store one transcript in full (authoritative), plus the
    shadow when the budget admits it. Returns the import's timings."""
    from . import backends, message_store
    started = time.perf_counter()
    path = pathlib.Path(path)
    implementation = backends.get(backend)
    native_parse = parse is None or (
        isinstance(parse, types.MethodType) and implementation is not None
        and parse.__self__ is implementation
        and parse.__func__ is type(implementation).parse_transcript)
    parse = parse or (lambda p: backends.parse_turns(backend, p))
    skip = "unsupported_backend"
    if backend in SUPPORTED_BACKENDS:
        try:
            conf = settings()
        except Exception as exc:  # noqa: BLE001 - a bad [transcript_shadow] must not stop imports
            from .log import log_exception
            log_exception("transcriptShadowSettingsFail", exc)
            conf = dict(_DEFAULTS, enabled=False)
        skip = BUDGET.admit(conf)
    if skip:
        tail_enabled = (native_parse and backend in SUPPORTED_BACKENDS
                        and implementation is not None
                        and implementation.supports_transcript_tail_apply)
        before = None
        if tail_enabled:
            try:
                before = identity(path)
                tail = _try_tail(backend, path, agent_id, backend_session_id, conf, started)
            except OSError as exc:
                from .log import log_exception
                log_exception("transcriptTailFileSkip", exc)
                _tail_log("full_file_io")
            except Exception:
                _tail_log("tail_failed")
                raise
            else:
                if tail is not None:
                    return tail
        if not tail_enabled:
            if not native_parse:
                _tail_log("full_custom_parser")
            elif implementation is None or backend not in SUPPORTED_BACKENDS:
                _tail_log("full_backend_unsupported")
            else:
                _tail_log("full_backend_tail_disabled")
        turns = parse(path)
        parsed = time.perf_counter()
        if tail_enabled:
            _store_full_with_tail_checkpoint(backend, path, agent_id, backend_session_id,
                                             turns, before, conf)
        else:
            message_store.store_transcript_turns(
                agent_id=agent_id, backend_session_id=backend_session_id,
                source_file=str(path), turns=turns)
        return {"rows": len(turns), "parse_s": parsed - started,
                "store_s": time.perf_counter() - parsed, "shadow": skip,
                "apply_mode": "full", "parsed_bytes": before.size if before else None}
    _tail_log("full_shadow_admitted")
    shadow = _Shadow(backend, path, agent_id, backend_session_id, conf)
    try:
        try:
            before: Identity | None = identity(path)
        except OSError:
            before = None   # the parse below reports a missing file itself
        turns = parse(path)
        parsed = time.perf_counter()
        shadow.before_store(before, turns)
        checkpoint_status = {}
        out = message_store.store_transcript_turns(
            agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=str(path), turns=turns, checkpoint_status=checkpoint_status,
            **shadow.store_kwargs())
        _checkpoint_refused(checkpoint_status)
        stored = time.perf_counter()
        shadow.after_store(out)
        return {"rows": len(turns), "parse_s": parsed - started,
                "store_s": stored - parsed, "shadow": shadow.verdict}
    finally:
        shadow.finish()


# The columns a turn re-stamp may change (turn_json) and those it must not.
_RESTAMP_COLUMNS = ("message_id, source_file, seq, role, timestamp, text, kind, tool_name,"
                    " tools_json, display_cells_json, origin, sender_agent_id,"
                    " prompt_admission_id, trace_id, phase, turn_json")


def _restamp_view(row) -> tuple:
    """(trace_id, turn_json, every other content column) of one message row."""
    content = tuple(row[name] for name in row.keys()
                    if name not in ("message_id", "turn_json", "revision"))
    return row["trace_id"] or "", row["turn_json"], content


def _summary_moved_off(changed: set[str], planned: set[str],
                       before: dict[str, tuple], after: dict[str, tuple]) -> set[str]:
    """Changed rows whose only change is a turn summary moving to a planned row.

    The plan applies no writes, so when a tail row belongs to a turn that began
    before the boundary, re-stamping that turn (stamp_turn) moves its summary
    off a row the plan never saw change; the tail import does the same. Such a
    row is explained only if its content is identical, it lost the summary,
    and the summary of its turn now sits on a row the plan did change."""
    holders: dict[str, set[str]] = {}
    for message_id, (trace, turn_json, _) in after.items():
        if trace and turn_json is not None:
            holders.setdefault(trace, set()).add(message_id)
    explained = set()
    for message_id in changed:
        old, new = before.get(message_id), after.get(message_id)
        if old is None or new is None:
            continue
        trace = new[0]
        if (trace and old[0] == trace and old[2] == new[2]
                and old[1] is not None and new[1] is None
                and holders.get(trace) and holders[trace] <= planned):
            explained.add(message_id)
    return explained


def _summary_consistent(database, agent_id: str, trace_id: str) -> bool:
    """Whether a turn's rows hold the summary stamp_turn decides for it: on a
    settled turn exactly one row, its last, with that summary; on an
    unsettled or unknown turn none. Rows are read across backend sessions."""
    from . import message_writes
    stamped = message_writes.turn_stamp(database, agent_id, trace_id)
    if stamped is None:
        return True
    _, rows, summary = stamped
    holders = [row for row in rows if row["turn_json"] is not None]
    if summary is None:
        return not holders
    return [row["message_id"] for row in holders] == [rows[-1]["message_id"]] \
        and holders[0]["turn_json"] == summary


def _witness_state(database, agent_id: str, backend_session_id: str) -> dict[str, Any]:
    """What other writers could change under an import: the heads of the
    agent's other conversations and fingerprints of its turns and of this
    conversation's agy authority rows."""
    heads = database.execute(
        """SELECT COALESCE(MAX(revision), 0), COALESCE(MAX(replace_revision), 0)
             FROM conversation_heads WHERE agent_id = ? AND backend_session_id != ?""",
        (agent_id, backend_session_id)).fetchone()
    turns = hashlib.blake2b(digest_size=16)
    for row in database.execute(
            """SELECT turn_id, trace_id, started_at, settled_at, outcome FROM turns
                WHERE agent_id = ? ORDER BY turn_id""", (agent_id,)):
        turns.update(json.dumps(list(row), default=str).encode())
    authority = hashlib.blake2b(digest_size=16)
    for row in database.execute(
            """SELECT trace_id, assistant_start_ordinal, assistant_end_ordinal,
                      terminal_status, authoritative_message_id, updated_at
                 FROM agy_turn_authority WHERE agent_id = ? AND backend_session_id = ?
                ORDER BY trace_id""", (agent_id, backend_session_id)):
        authority.update(json.dumps(list(row), default=str).encode())
    return {"other_heads": [int(heads[0]), int(heads[1])],
            "turns": turns.hexdigest(), "authority": authority.hexdigest()}


def _witness(database, agent_id: str, backend_session_id: str, source_file: str,
             floor: int, before: dict[str, Any] | None) -> dict[str, Any]:
    """Whether anything besides this import wrote what it reads between the
    plan's snapshot and the comparison. Diagnostic only: it never changes a
    verdict; it lets a divergence be told from concurrency noise.

    The import writes this conversation's transcript rows and, through
    stamp_turn, every row (in any conversation) of the turns it may stamp: the
    turns of the rows it changed and its newest turn, which it re-stamps on
    every import. It also writes the tools of agy turns whose text the stream
    owns onto their authoritative rows (_attach_authority_tools), so this
    conversation's agy authority turns and rows count as its own too. Of the
    agent's other rows changed since the floor (client,
    live and final rows, and rows of other conversations), ``foreign_rows``
    counts those outside those turns (another writer), ``ambiguous_rows``
    those inside them (the stamp or another writer). Another conversation's
    head moving counts only when rows the stamp may have written do not
    account for it."""
    after = _witness_state(database, agent_id, backend_session_id)
    changed = database.execute(
        """SELECT message_id, backend_session_id, source_file, trace_id FROM messages
            WHERE agent_id = ? AND revision > ?""", (agent_id, floor)).fetchall()
    own = [row for row in changed
           if row["backend_session_id"] == backend_session_id
           and row["source_file"] == source_file]
    footprint = {row["trace_id"] for row in own if row["trace_id"]}
    newest = database.execute(
        """SELECT trace_id FROM messages
            WHERE agent_id = ? AND backend_session_id = ? AND source_file = ?
              AND trace_id IS NOT NULL ORDER BY seq DESC LIMIT 1""",
        (agent_id, backend_session_id, source_file)).fetchone()
    if newest is not None:
        footprint.add(newest["trace_id"])
    authority_ids: set[str] = set()
    for row in database.execute(
            """SELECT trace_id, authoritative_message_id FROM agy_turn_authority
                WHERE agent_id = ? AND backend_session_id = ?""",
            (agent_id, backend_session_id)):
        footprint.add(row["trace_id"])
        if row["authoritative_message_id"]:
            authority_ids.add(row["authoritative_message_id"])
    others = [row for row in changed if row not in own]
    ambiguous = sum(1 for row in others
                    if row["trace_id"] in footprint or row["message_id"] in authority_ids)
    out = {"foreign_rows": len(others) - ambiguous, "ambiguous_rows": ambiguous}
    if before is not None:
        other_sessions = [row for row in others
                          if row["backend_session_id"] != backend_session_id]
        explained = bool(other_sessions) and all(
            row["trace_id"] in footprint or row["message_id"] in authority_ids
            for row in other_sessions)
        out.update(other_heads_moved=after["other_heads"] != before["other_heads"]
                   and not explained,
                   turns_changed=after["turns"] != before["turns"],
                   authority_changed=after["authority"] != before["authority"])
    return out


class _Shadow:
    """One shadow run beside a full import. Its own failures are logged and
    end the run; they never reach the authoritative import."""

    def __init__(self, backend, path, agent_id, backend_session_id, conf):
        self.backend, self.path = backend, path
        self.agent_id, self.backend_session_id = agent_id, backend_session_id
        self.conf = conf
        self.verdict = "error"
        self.detail: dict[str, Any] = {}
        self.cpu = 0.0
        self.wall = 0.0
        self.boundary: Boundary | None = None
        self.ident: Identity | None = None
        self.captured: dict[str, Any] = {}
        self.plan: dict[str, Any] | None = None
        self.old: Checkpoint | None = None
        self.ids_before: set[str] = set()
        self.rows_before: dict[str, tuple] = {}
        self.out_seqs: list[int] = []
        self.failed = False

    def _timed(self, step: str, work: Callable[[], Any]) -> Any:
        if self.failed:
            return None
        cpu, wall = time.thread_time(), time.perf_counter()
        try:
            return work()
        except Exception as exc:  # noqa: BLE001 - logged; the full import goes on
            from .log import log_exception
            log_exception("transcriptShadowFail", exc, detail=f"{self.agent_id} {step}")
            self.failed = True
            self.verdict = f"error:{step}"
            return None
        finally:
            self.cpu += time.thread_time() - cpu
            self.wall += time.perf_counter() - wall
            self.detail[f"{step}_ms"] = round((time.perf_counter() - wall) * 1000, 3)

    def before_store(self, before: Identity | None, turns: list[dict]) -> None:
        self._timed("pre", lambda: self._before(before, turns))

    def _before(self, before: Identity | None, turns: list[dict]) -> None:
        from . import db, message_writes
        after = identity(self.path)
        if before is None or after != before:
            # The file grew while it was parsed: the full parse and any range
            # parse would not have read the same bytes.
            self.verdict = "skipped:file_moved"
            self.failed = True
            return
        self.ident = after
        digests = turn_digests(turns)
        self.digests = digests
        self.turns = turns
        database = db.conn()
        self.old = load_checkpoint(database, self.agent_id, self.backend_session_id,
                                   str(self.path))
        reason = decide(self.old, self.backend, self.path, after, now_ms=db.now_ms(),
                        audit_every=int(self.conf["audit_every"]),
                        audit_seconds=float(self.conf["audit_seconds"]),
                        max_tail_bytes=int(self.conf["max_tail_bytes"]))
        self.detail["decision"] = reason or "incremental"
        if self.old is not None and reason == "" and \
                (self.old.size, self.old.mtime_ns) == (after.size, after.mtime_ns):
            # Nothing new: the import has nothing to do and the checkpoint
            # already describes this file.
            self.verdict = "unchanged"
            self.detail["decision"] = "unchanged"
            return
        if not reason:
            old = self.old
            tail = parse_range(self.backend, self.path, old.boundary_offset, after.size)
            self.detail["tail_turns"] = len(tail)
            self.detail["tail_bytes"] = after.size - old.boundary_offset
            mismatches = []
            if old.boundary_seq + len(tail) != len(turns):
                mismatches.append("turn_count")
            elif tail != turns[old.boundary_seq:]:
                mismatches.append("tail_turns")
            if prefix_turns_hash(digests, old.boundary_seq) != old.prefix_turns_hash:
                mismatches.append("prefix_changed")
            self.mismatches = mismatches
            self.rows_before = {row["message_id"]: _restamp_view(row) for row in database.execute(
                f"SELECT {_RESTAMP_COLUMNS} FROM messages WHERE agent_id=? AND backend_session_id=?",
                (self.agent_id, self.backend_session_id)).fetchall()}
            self.ids_before = set(self.rows_before)
            self.witness_before = _witness_state(database, self.agent_id,
                                                 self.backend_session_id)
            self.plan = message_writes.plan_transcript_tail(
                agent_id=self.agent_id, backend_session_id=self.backend_session_id,
                source_file=str(self.path), turns=tail, start_seq=old.boundary_seq,
                carry=old.store_carry)
        else:
            self.verdict = f"fallback:{reason}"
        self.boundary, why = choose_boundary(
            self.backend, self.path, turns, digests, after,
            int(self.conf["max_tail_bytes"]))
        if self.boundary is None:
            self.detail["checkpoint"] = f"none:{why}"

    def store_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"out_seqs": self.out_seqs} if self.plan is not None else {}
        if self.failed or self.boundary is None:
            return kwargs
        return {**kwargs, "capture_at": self.boundary.seq, "captured": self.captured,
                "on_stored": self._write}

    def _write(self, database, captured: dict[str, Any]) -> None:
        from . import db
        boundary, ident = self.boundary, self.ident
        if not captured:
            raise RuntimeError("the import reached no checkpoint turn")
        now = db.now_ms()
        write_checkpoint(database, Checkpoint(
            agent_id=self.agent_id, backend_session_id=self.backend_session_id,
            source_file=str(self.path), backend=self.backend,
            parser_version=parser_version(self.backend), device=ident.device,
            inode=ident.inode, size=ident.size, mtime_ns=ident.mtime_ns,
            complete_size=boundary.complete_size, boundary_offset=boundary.offset,
            boundary_seq=boundary.seq, turn_count=len(self.turns),
            prefix_hash=boundary.prefix_hash, tail_hash=boundary.tail_hash,
            prefix_turns_hash=boundary.prefix_turns_hash,
            parser_carry=boundary.parser_carry, store_carry=dict(captured),
            imports_since_audit=0, audited_at=now, updated_at=now))
        self.detail["checkpoint"] = f"written:{boundary.seq}/{len(self.turns)}"

    def after_store(self, out: list[dict]) -> None:
        if self.plan is not None:
            self._timed("compare", lambda: self._compare(out))

    def _compare(self, out: list[dict]) -> None:
        from . import db
        plan, old = self.plan, self.old
        floor = plan["revision_floor"]
        mismatches = list(self.mismatches)
        full_tail = [row for row, seq in zip(out, self.out_seqs) if seq >= old.boundary_seq]
        full_prefix = [row for row, seq in zip(out, self.out_seqs) if seq < old.boundary_seq]
        planned = plan["out"]
        if [_row_view(r) for r in planned] != [_row_view(r) for r in full_tail]:
            mismatches.append("rows")
        if [int(r["revision"]) > floor for r in planned] != \
                [int(r["revision"]) > floor for r in full_tail]:
            mismatches.append("revisions")
        if any(int(r["revision"]) > floor for r in full_prefix):
            mismatches.append("prefix_rows_rewritten")
        database = db.conn()
        rows = database.execute(
            f"SELECT {_RESTAMP_COLUMNS}, revision FROM messages"
            " WHERE agent_id=? AND backend_session_id=?",
            (self.agent_id, self.backend_session_id)).fetchall()
        ids_after = {row["message_id"] for row in rows}
        transcript_changed = {row["message_id"] for row in rows
                              if row["source_file"] == str(self.path)
                              and int(row["revision"]) > floor}
        planned_changed = {r["id"] for r in planned if int(r["revision"]) > floor} \
            | plan["updated"]
        unexplained = transcript_changed - planned_changed
        unexplained -= _summary_moved_off(
            unexplained, planned_changed, getattr(self, "rows_before", {}),
            {row["message_id"]: _restamp_view(row) for row in rows})
        if unexplained:
            mismatches.append("changed_rows")
        # The plan cannot predict turn summaries, so check them directly: every
        # turn this import changed carries what stamp_turn gives it now.
        # stamp_turn writes a turn's rows in every backend session, so the
        # turns to check come from the agent's rows, not just this
        # conversation's. The floor is a global clock value: any row of the
        # agent above it changed since the plan's snapshot.
        changed_traces = {row["trace_id"] for row in database.execute(
            """SELECT DISTINCT trace_id FROM messages
                WHERE agent_id = ? AND revision > ? AND trace_id IS NOT NULL""",
            (self.agent_id, floor)).fetchall()}
        if any(not _summary_consistent(database, self.agent_id, trace)
               for trace in sorted(changed_traces)):
            mismatches.append("summary")
        # A row the plan adopts (a streamed twin) keeps its id; the plan, which
        # never applies its writes, still sees it as live and deletes it.
        planned_deleted = (plan["deleted"] & self.ids_before) - {r["id"] for r in planned}
        if (self.ids_before - ids_after) != planned_deleted:
            mismatches.append("deleted_rows")
        self.detail["mismatches"] = mismatches
        self.detail["witness"] = _witness(
            database, self.agent_id, self.backend_session_id, str(self.path), floor,
            getattr(self, "witness_before", None))
        if mismatches:
            self.detail["examples"] = sorted(unexplained)[:5]
        self.verdict = "diverge:" + ",".join(mismatches) if mismatches else "match"

    def finish(self) -> None:
        spent = BUDGET.release(self.cpu, self.wall)
        if self.verdict == "error" and not self.failed:
            self.verdict = "error:incomplete"
        try:
            from . import eventlog
            eventlog.emit(
                "transcript", "import_shadow",
                level="warning" if self.verdict.startswith(("diverge", "error")) else "info",
                agent_id=self.agent_id, backend_session_id=self.backend_session_id,
                detail={"backend": self.backend, "verdict": self.verdict,
                        "shadow_cpu_ms": round(self.cpu * 1000, 3),
                        "shadow_wall_ms": round(self.wall * 1000, 3),
                        "source_bytes": self.ident.size if self.ident else None,
                        **self.detail, **spent})
        except Exception as exc:  # noqa: BLE001
            from .log import log_exception
            log_exception("transcriptShadowLogFail", exc, detail=self.agent_id)
