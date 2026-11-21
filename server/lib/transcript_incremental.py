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
import json
import os
import pathlib
import re
import sys
import threading
import time
from time import thread_time as _thread_time  # bound once: tests replace the module's `time`
import types
from dataclasses import dataclass, replace
from contextvars import ContextVar
from typing import Any, Callable

SUPPORTED_BACKENDS = ("claude", "codex", "agy")

# The sources whose behaviour a checkpoint depends on. Any edit to one of them
# changes the parser version, so every checkpoint taken by older code is
# refused and the next import is a full one.
_VERSION_SOURCES = {
    "claude": ("claude_transcript.py", "claude_reply_evidence.py", "backend/claude.py", "transcript_paths.py"),
    "codex": ("codex_transcript.py", "codex_tool_cells.py", "backend/codex.py", "transcript_paths.py"),
    "agy": ("agy_transcript.py", "codex_transcript.py", "codex_tool_cells.py", "opencode_transcript.py",
            "claude_transcript.py", "claude_reply_evidence.py", "backend/agy.py", "transcript_paths.py"),
}
# What a checkpoint is valid for. ``parser_version`` hashes the parser's
# sources: ``_VERSION_SOURCES`` per backend and ``_PARSE_COMMON``: how a range
# is read and parsed and the parser carry a tail resumes from
# (transcript_ranges.py), and the helpers the parsers import (what a block is,
# the markup and preambles they strip, the activity rows they build). A change
# to any of them refuses every checkpoint
# of that backend, so the next import of each transcript is full once, as it
# must be. ``tests/unit/test_transcript_parser_version.py`` checks that every
# module a parser imports is in its list or named exempt, so a new helper
# cannot slip past.
#
# The store's sources (``_STORE_SOURCES``) decide what a full import WRITES for
# lines already imported, and the store carry a tail resumes from. So does this
# module, which decides where a boundary may stand and what a tail may refer to.
# They are not hashed: most changes to them (a heartbeat rule, a team block, a
# log line) leave every row of an imported line as it was, and hashing them
# re-read every transcript on every such install. Instead ``STORE_FORMAT`` is
# part of the version and is raised by hand when a change alters those rows,
# the store carry or the boundary rules. The golden test
# holds the digests of both lists and STORE_FORMAT: any change to a listed file
# fails it until its author records that decision.
_PARSE_COMMON = ("transcript_ranges.py", "backend/base.py", "message_text.py", "voice_markup.py",
                 "activity.py", "protocol.py", "text_util.py", "voice_preamble.py", "clarp_guidance.py",
                 "personalities.py", "error_classify.py")
_STORE_SOURCES = ("transcript_incremental.py", "message_writes.py",
                  "message_import_writer.py", "message_carry.py",
                  "message_import_rows.py", "message_import_common.py",
                  "message_context.py", "message_turns.py",
                  # The store's ack-token decisions (AUT-07).
                  "origins.py", "heartbeat.py", "dreaming.py", "team_leader.py",
                  "team_store.py", "oracle_delegations.py")
STORE_FORMAT = 1

_HEAD_BYTES = 4096
_WINDOW_BYTES = 64 * 1024
_SAMPLES = 16
_SAMPLE_BYTES = 4096
# Boundary candidates tried per checkpoint, latest first.
_CANDIDATES = 6


def sources_digest(names) -> str:
    """One digest of these source files' bytes, by name (the golden test's too)."""
    root = pathlib.Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + (root / name).read_bytes())
    return digest.hexdigest()


@functools.cache
def parser_version(backend: str) -> str:
    """The parser's sources and STORE_FORMAT, not the store's sources."""
    digest = hashlib.sha256(backend.encode())
    digest.update(f"store_format={STORE_FORMAT}".encode())
    digest.update(sources_digest((*_VERSION_SOURCES[backend], *_PARSE_COMMON)).encode())
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

# How a range is read and parsed, and the parser carry, live in transcript_ranges.py,
# a parser source hashed by parser_version; the names stay importable from here.
from .transcript_ranges import (  # noqa: F401 - moved; callers and tests use these names here
    _PARSER_CARRIES, _Range, _RangePath, _agy_carry, _claude_carry, _claude_children, parse_range,
    parser_carry)

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
    """Why a record in [start, end) reaches before the boundary, or ''. Also
    why an old Claude sub-agent cell would come out differently now."""
    why = _REFERENCE_SCANS[backend](_records(path, start, end), carry)
    if not why and "subagent_children" in carry:      # only a Claude carry has it
        children = carry["subagent_children"]
        if children is None or _claude_children(path, [key for key, _ in children]) != children:
            why = "claude_subagent_children_changed"
    return why


_BOUNDARY_MARKERS = {
    "claude": lambda raw: b'"type":"user"' in raw and b'"tool_result"' not in raw
    and b'"isMeta":true' not in raw,
    # A Codex turn's own records (its turn_id) start at task_started, ahead
    # of the user message, so the boundary is there.
    "codex": lambda raw: b'"task_started"' in raw,
    "agy": lambda raw: b'"USER_INPUT"' in raw,
}


def _opens_a_turn(backend: str, path: pathlib.Path, offset: int, length: int) -> bool:
    """Whether the one record at ``offset``, parsed alone, is a user turn."""
    turns = parse_range(backend, path, offset, offset + length)
    return bool(turns) and turns[0].get("role") == "user"


def boundary_candidates(backend: str, path: pathlib.Path, end: int,
                        max_tail_bytes: int, *, turns_only: bool = False
                        ) -> list[tuple[int, dict]]:
    """Latest offsets that may open the last user turn, newest first.

    A Claude user record that is not a tool result may open a turn, and many
    do not: the records the harness injects (a task notification, an account
    recovery, a system reminder) are user records the parser drops. A turn
    that has run long enough to collect ``_CANDIDATES`` of them after its
    prompt had no candidate left that could work, so its conversation got no
    checkpoint and every import of it was a full one. With ``turns_only``
    such a record is not counted: a candidate is a record that, parsed
    alone, is a user turn. (Only for a backend whose turn opens at its
    prompt, ``transcript_turn_opens_at_its_prompt``: a Codex turn opens at
    task_started, which is no turn alone.) Whether a candidate is a boundary
    is decided as before, by ``choose_boundary``."""
    from . import backends
    marker = _BOUNDARY_MARKERS[backend]
    alone = turns_only and getattr(backends.get(backend),
                                   "transcript_turn_opens_at_its_prompt", False)
    found: list[tuple[int, dict]] = []
    for offset, raw in _lines(path, max(0, end - max_tail_bytes), end):
        if marker(raw.replace(b'": "', b'":"')):
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            found.append((offset, record if isinstance(record, dict) else {}, len(raw)))
    kept: list[tuple[int, dict]] = []
    for offset, record, length in reversed(found):
        if alone and not _opens_a_turn(backend, path, offset, length):
            continue
        kept.append((offset, record))
        if len(kept) == _CANDIDATES:
            break
    return kept


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


def checkpoint_digest(checkpoint: Checkpoint) -> str:
    return hashlib.blake2b(json.dumps(checkpoint.__dict__, sort_keys=True, default=str,
                                      separators=(",", ":")).encode(), digest_size=16).hexdigest()


def inputs_record(raw_inputs, checkpoint: Checkpoint, session_rows: bool,
                  witness=None) -> dict | None:
    """What the frontier of ``checkpoint`` keeps of the database rows an
    import read (``_prepare_import_prelude`` with ``raw_only``): a record of
    constant size, lib.message_carry.record. None when there are no rows.
    ``witness`` is what the tail's guard left of the rows it hashed."""
    from . import message_carry
    if raw_inputs is None:
        return None
    return message_carry.record(
        raw_inputs, carry=checkpoint.store_carry, source_file=checkpoint.source_file,
        backend_session_id=checkpoint.backend_session_id, session_rows=session_rows,
        witness=witness, boundary_seq=checkpoint.boundary_seq)


def write_frontier(database, checkpoint: Checkpoint, file_hash: str, recorded: dict) -> None:
    """Beside its checkpoint, in the same transaction. Not for a session a
    frontier is never kept for (see ``_cache_committed_tail``), and not when
    the checkpoint row is another import's that covers more of the file.

    ``recorded`` (``inputs_record``) goes into the ``inputs_digest`` column
    as a short JSON text. Until the record existed that column held one
    digest of all the rows; such a row is never taken for a record, and a
    release that wrote those never reads this one: both check the parser
    version first, and it differs."""
    from . import message_carry
    key = (checkpoint.agent_id, checkpoint.backend_session_id, checkpoint.source_file)
    if load_checkpoint(database, *key) != checkpoint:
        return
    if recorded["oracle"]:
        database.execute(
            """DELETE FROM transcript_import_frontiers
                WHERE agent_id=? AND backend_session_id=? AND source_file=?""", key)
        return
    database.execute(
        """INSERT INTO transcript_import_frontiers (
               agent_id, backend_session_id, source_file, parser_version,
               checkpoint_digest, file_hash, inputs_digest, updated_at
           ) VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(agent_id, backend_session_id, source_file) DO UPDATE SET
               parser_version=excluded.parser_version,
               checkpoint_digest=excluded.checkpoint_digest,
               file_hash=excluded.file_hash, inputs_digest=excluded.inputs_digest,
               updated_at=excluded.updated_at""",
        (*key, checkpoint.parser_version, checkpoint_digest(checkpoint), file_hash,
         message_carry.dumps(recorded), checkpoint.updated_at))


def commit_inputs(database, agent_id: str, backend_session_id: str, session_rows: bool) -> tuple:
    """Inside the import's final transaction, beside the checkpoint: the
    conversation's head (message_writes._carry_head), and the turn rows and
    the agent's oracle delegations, which move no head (R119)."""
    from . import message_turns, message_writes
    return (message_writes._carry_head(database, agent_id, backend_session_id, session_rows),
            message_turns.TurnIndex.read_rows(database, agent_id),
            database.execute("SELECT * FROM oracle_delegations WHERE agent_id=? ORDER BY delegation_id",
                             (agent_id,)).fetchall())


def commit_snapshot(database, agent_id: str, backend_session_id: str, under: tuple,
                    session_rows: bool) -> tuple | None:
    """Right after the import's commit, with no lock held: the message rows as
    that commit left them, or None (R119).

    One read transaction (WAL: it holds no lock and blocks no writer). The
    head is read first: equal to the head at the commit, no message row of the
    conversation (or of the agent) has changed since, so the snapshot's rows
    are the rows the checkpoint was written against, as main read them inside.
    Moved: a write landed between the commit and this read, and those rows are
    gone; one line says so and no frontier is published (as before R119 a
    foreign commit during the import's yield)."""
    from . import message_writes
    database.execute("BEGIN")
    try:
        if message_writes._carry_head(database, agent_id, backend_session_id, session_rows) != under[0]:
            _tail_log("frontier_not_published_head_moved", retention=True)
            return None
        carry_rows = message_writes._carry_message_rows(database, agent_id, backend_session_id, session_rows)
        sends, user = message_writes._sends_and_latest_user(database, agent_id, backend_session_id, carry_rows)
        return sends, user, carry_rows
    finally:
        # Ended before anything else runs on this connection: an open read
        # would pin the WAL, and the frontier's BEGIN IMMEDIATE must not
        # start inside it.
        if database.in_transaction:
            database.execute("COMMIT")


def frontier_after_commit(checkpoint: Checkpoint, file_hash: str, under: tuple, rows: tuple,
                          witness=None, *, session_rows: bool = True) -> dict | None:
    """Publish the frontier of a checkpoint already committed, from what was
    read at its commit (R119): ``under`` (commit_inputs, read inside it) and
    ``rows`` (commit_snapshot, read right after it, outside the lock).

    The record is the one main wrote in the import's final transaction: a row
    written since is NOT in it, and the next tail's guard judges it with the
    carry rules, as it did. One short write transaction checks that the
    checkpoint row is still this one and writes the frontier; it reads no
    message row. Returns the record, or None."""
    from . import db
    from .lifecycle import transaction
    _head, turns, oracle = under
    sends, user, carry_rows = rows
    con = db.conn()
    with transaction(con):
        if load_checkpoint(con, checkpoint.agent_id, checkpoint.backend_session_id,
                           checkpoint.source_file) != checkpoint:
            _tail_log("frontier_not_published_checkpoint_moved", retention=True)
            return None
        recorded = inputs_record((turns, sends, user, carry_rows, oracle), checkpoint, session_rows, witness)
        write_frontier(con, checkpoint, file_hash, recorded)
    return recorded


def load_frontier(database, checkpoint: Checkpoint) -> dict | None:
    """The stored frontier of exactly this checkpoint, or None."""
    row = database.execute(
        """SELECT parser_version, checkpoint_digest, file_hash, inputs_digest
             FROM transcript_import_frontiers
            WHERE agent_id=? AND backend_session_id=? AND source_file=?""",
        (checkpoint.agent_id, checkpoint.backend_session_id, checkpoint.source_file)).fetchone()
    if (row is None or row["parser_version"] != checkpoint.parser_version
            or row["checkpoint_digest"] != checkpoint_digest(checkpoint)):
        return None
    return dict(row)


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
                    max_tail_bytes: int, *, turns_only: bool = False
                    ) -> tuple[Boundary | None, str]:
    """The latest user-turn boundary whose tail, parsed alone, reproduces the
    full parse's tail exactly and refers to nothing before it."""
    complete = complete_size(path, ident.size)
    reasons = []
    for offset, record in boundary_candidates(backend, path, complete, max_tail_bytes,
                                              turns_only=turns_only):
        tail = parse_range(backend, path, offset, ident.size)
        seq = len(turns) - len(tail)
        if seq < 0 or not tail or tail[0].get("role") != "user" or tail != turns[seq:]:
            reasons.append("tail_differs")
            continue
        carry = parser_carry(backend, turns[:seq], record, path)
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
           max_tail_bytes: int, tail_hash_later: bool = False) -> str:
    """'' when an incremental import from ``checkpoint`` is safe, otherwise
    the reason a full import is needed. With ``tail_hash_later`` the bytes
    from the boundary to the checkpoint's end are not read here: the caller
    parses them next and compares their digest then (``tail_rewritten``)."""
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
    if not tail_hash_later and range_hash(
            path, checkpoint.boundary_offset, checkpoint.complete_size) != checkpoint.tail_hash:
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
    # Off unless switched on: a full import looks for its boundary among the
    # records that ARE user turns, not among every user record. False: the
    # records the harness injects into a turn count as candidates, and six
    # of them after a prompt leave the conversation without a checkpoint.
    "boundary_skips_dropped_records": False,
    "audit_every": 50,
    "audit_seconds": 3600.0,
    # True: read and digest every imported byte of the file at every tail
    # import, as before. False: at the first tail after a Host start, at
    # every audit, and whenever the file looks touched (see _try_tail).
    "digest_every_tail": False,
    # Off unless switched on: a GROWN file whose audit is due has its
    # imported bytes read once and compared with the stored digest, and is
    # then imported as a tail, which starts the audit's clock again. False:
    # it is imported in full, as before.
    "audit_grown_by_digest": False,
    # With audit_grown_by_digest: every Nth due audit of a conversation is
    # still the full import, and the others are by digest. A full import
    # replays every turn, so it puts right a row a tail left wrong; by
    # digest only the bytes are checked. 1: every audit is a full import.
    "audit_full_every": 8,
    # True: an UNCHANGED file whose audit is due (after a Host start) has its
    # bytes read once and compared with the stored digest. False: it is
    # imported in full, as a grown file whose audit is due always is.
    "audit_unchanged_by_digest": True,
    # Off unless switched on: a full import takes the file's digest from the
    # bytes its parser was handed (_parsed_digests) instead of reading the
    # file a second time, when the file did not change while it was parsed.
    "full_digest_while_parsing": False,
    # What a tail may find changed among the database rows its frontier was
    # published with (lib.message_carry gives each rule's reason). False: a
    # change of that kind is a full import, as before the rule existed.
    "carry_session_rows": True,      # rows of the agent's other sessions
    "carry_later_turns": True,       # turn rows that began after the boundary's turns
    "carry_new_sends": True,         # a new /send row whose words were not said before
    "carry_new_notes": True,         # a new note row (POST /agent-notes)
    # Off unless switched on: a live row (the row a reply streams into) that
    # is new, rewritten or gone, unless a reply before the boundary carries
    # its trace.
    "carry_live_rows": False,
    # Off unless switched on (and only with carry_later_turns): a turn that
    # was running when the frontier was published has settled, and the rows
    # from the boundary on carry its stamps (lib.message_carry.SETTLED_TURNS).
    "carry_settled_turns": False,
    # Off unless switched on (and only with carry_live_rows): a changed live
    # row is refused only when a reply before the boundary with its trace has
    # words related to the row's, not for the trace alone
    # (lib.message_carry.LIVE_WORDS). A full import then keeps those words
    # in the store carry, for the newest turns before the boundary.
    "carry_live_rows_by_words": False,
    # Off unless switched on: a tail gives the writer away only once it has
    # written something (message_writes._TailWriter). False: its batch budget
    # runs from BEGIN IMMEDIATE, so a tail on a long conversation yields
    # before its first row.
    "tail_yield_after_first_write": False,
    # Off unless switched on: a tail whose file only grew while it was
    # imported publishes its frontier for the bytes it parsed, once those
    # are proven to be the file's still, and the next import is a tail.
    # False: it loses its frontier and the next import is full.
    "tail_keeps_frontier_when_grown": False,
    # Off unless switched on: a tail takes the two hashes of its range from
    # its own parse (the bytes are hashed as the parser reads them) instead
    # of reading the range once before the parse and once after it, when the
    # file did not change while it was parsed. A tail then reads its range
    # twice, for the references and for the parse, where it read it four
    # times.
    "tail_hashes_while_parsing": False,
}


# A checkpoint's count of audits by digest since its last full import. It has
# no column of its own, which would be a schema step, so it is a key beside
# the parser's carry: the reference scans take what they need from that carry
# by name and do not read it. Not beside the store's carry, where it was: the
# count moves at every audit by digest, a changed value is written whole, and
# the store's carry holds up to 64 KB of words (carry_live_rows_by_words)
# where the parser's is a few hundred bytes.
_AUDITS_BY_DIGEST = "audits_by_digest"


def _carry_line(what: str, how: str) -> str:
    """The transcriptCarryInputsChanged line: what stood in the way, and for a
    refused kind of message row how its rows changed (message_carry._how)."""
    return f"what={what} how={how}" if how else f"what={what}"


def carry_rules(conf) -> frozenset:
    from . import message_carry
    return frozenset(rule for rule in message_carry.RULES if conf.get(rule))


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

_IMPORT_CACHE_EPOCH = ContextVar("transcript_import_cache_epoch", default=None)
_IMPORT_LOG_METADATA = ContextVar("transcript_import_log_metadata", default=(None, None))


def _call_in_cache_epoch(importer, epoch):
    """Carry admission lifetime only, never a positive DB/file certificate."""
    token = _IMPORT_CACHE_EPOCH.set(epoch)
    try:
        return importer()
    finally:
        _IMPORT_CACHE_EPOCH.reset(token)


def _materialized_history(agent_id, session_id):
    """Existing committed history permits negative admission, never freshness."""
    from . import db
    return db.conn().execute(
        "SELECT 1 FROM messages WHERE agent_id=? AND backend_session_id=? AND seq>=0 LIMIT 1",
        (agent_id, session_id)).fetchone() is not None


class _FullImportGate:
    """Bounded negative admission for materialized contexts, not freshness.

    Active reservations are never evicted. Polls do not extend cooldowns;
    old inactive epochs/expired windows are discarded on admission. No IO,
    caller callbacks, sleeps or timers run under this lock.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._slots = {}
        self._weight = 0

    def reserve(self, key, epoch):
        charge = 512 + sys.getsizeof(key) + sum(sys.getsizeof(item) for item in key)
        with self._lock:
            now = time.monotonic()
            for old_key, (_, active, until, weight, old_epoch) in list(self._slots.items()):
                if not active and (old_epoch != epoch or until <= now):
                    del self._slots[old_key]
                    self._weight -= weight
            previous = self._slots.get(key)
            if previous is not None:
                return None, "running" if previous[1] else "cooldown"
            if len(self._slots) >= 256 or self._weight + charge > 2 * 1024 * 1024:
                return None, "capacity"
            ticket = object()
            self._slots[key] = (ticket, True, 0.0, charge, epoch)
            self._weight += charge
            return ticket, ""

    def finish(self, key, ticket, refused):
        with self._lock:
            current = self._slots.get(key)
            if current is None or current[0] is not ticket:
                return
            if refused:
                self._slots[key] = (ticket, False, time.monotonic() + 5.0, current[3], current[4])
            else:
                del self._slots[key]
                self._weight -= current[3]


_FULL_IMPORT_GATE = _FullImportGate()


class _FullImportAttempt:
    def __init__(self, enabled, backend, path, agent_id, session_id, before):
        from . import db
        epoch = _IMPORT_CACHE_EPOCH.get()
        self.key = None
        self.ticket = None
        self.reason = ""
        self.full_started = False
        self.retained = False
        self.metadata = (backend, before.size if before else None)
        if enabled and epoch is not None and before is not None:
            # Namespace/epoch isolates negative admission, not DB authority.
            # Same-source aliases cannot create duplicate eligible full work.
            self.key = (str(db.DB_PATH), epoch, backend, agent_id, session_id, str(path.resolve()),
                        before.device, before.inode)
            self.ticket, self.reason = _FULL_IMPORT_GATE.reserve(self.key, epoch)

    def __enter__(self):
        self.log_token = _IMPORT_LOG_METADATA.set(self.metadata)
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        try:
            if self.ticket is not None:
                _FULL_IMPORT_GATE.finish(self.key, self.ticket,
                                         exc_type is None and self.full_started and not self.retained)
        finally:
            _IMPORT_LOG_METADATA.reset(self.log_token)


def _completion_state(result) -> str:
    """Private native result, interpreted only by an opted-in import adapter."""
    if not isinstance(result, dict) or "_clarp_transcript_completion_v1" not in result:
        return "completed-changed"  # existing strict importer / legacy test callers
    state = result["_clarp_transcript_completion_v1"]
    return state if state in ("completed-changed", "completed-unchanged", "deferred") else "deferred"


class _CompletionImport:
    """Completion transport; NOT an audit or native file/DB certificate.

    The default remains strict legacy admission. The later audit implementation
    must supply its real epoch/generation probe AND publication fence before
    enabling warm hash reuse. This groundwork does not enable that optimization.
    A native authority's fence must be reentrant and shared by all its debt /
    generation mutations; importer IO is always outside that fence.
    """
    _clarp_transcript_completion_v1 = True

    def __init__(self, callback, context, *, eligibility=None, fence=None, after_complete=None):
        self._callback = callback
        self._context = context
        self._eligibility = eligibility
        self._fence = fence
        self._after_complete = after_complete

    def _clarp_transcript_eligibility(self):
        if self._eligibility is None:
            return self._context, None, True  # legacy, not an audit generation
        return self._eligibility()

    def _clarp_transcript_fence(self):
        from contextlib import nullcontext
        return self._fence() if self._fence is not None else nullcontext()

    def __call__(self):
        context, generation, eligible = self._clarp_transcript_eligibility()
        value = self._callback() if eligible else None
        state = _completion_state(value) if eligible else "deferred"
        return {"_clarp_transcript_completion_v1": (context, generation, state),
                "_native_result": value}

    def _clarp_transcript_completed(self, envelope):
        if self._after_complete is not None:
            self._after_complete(envelope["_native_result"])


def _completion_import(callback, *, backend, path, agent_id, backend_session_id,
                       after_complete=None):
    from . import db
    context = (str(db.DB_PATH), str(path), backend, agent_id, backend_session_id)
    return _CompletionImport(callback, context, after_complete=after_complete)


# ---- the import ------------------------------------------------------------

_ROW_FIELDS = ("id", "role", "text", "origin", "sender_agent_id", "timestamp",
               "tools", "display_cells", "kind")


def _row_view(row: dict) -> tuple:
    return tuple(json.dumps(row.get(name), sort_keys=True, default=str) for name in _ROW_FIELDS)



@dataclass
class _TailState:
    checkpoint: Checkpoint
    # The record of the database rows the frontier was published with
    # (``inputs_record``): the same few hundred bytes for any conversation.
    inputs: dict
    file_hash: str
    # The digest of the imported bytes as a running hash, when this process
    # read them: new bytes are added to it. None for a frontier taken from the
    # store after a Host start, whose bytes this process has not read yet.
    hasher: Any = None


def _tail_weight(key, revision, state) -> int:
    if state is None:
        return 1
    total = len(json.dumps(state.checkpoint.__dict__, default=str)) + 1024
    total += len(json.dumps(state.inputs, default=str))
    return total


from .revisioned_cache import RevisionedCache


@dataclass(frozen=True)
class _TailReason:
    code: str
    token: int
    weight: int = -1


def _reason_weight(key, revision, value):
    # Only keys/revisions and scalar codes, never checkpoint/row payloads.
    return 512 + sys.getsizeof(key) + sys.getsizeof(revision) + sum(
        sys.getsizeof(part) for part in key)


_TAIL_REASONS = RevisionedCache("transcript-tail-reasons", max_entries=4096,
                               max_weight=4 * 1024 * 1024, weight=_reason_weight)


class _TailCache(RevisionedCache):
    """Observe actual LRU transitions without changing their admission policy.

    Lock order is frontier -> diagnostic history; diagnostics never take the
    frontier lock in reverse. No logging/DB work occurs under either lock.
    """
    def __init__(self):
        self._sequence = 0
        self._resets = 0
        self._last_charge = 0
        self._diagnostic_loss = False
        super().__init__("transcript-tail-state", max_entries=256,
                         max_weight=32 * 1024 * 1024, weight=self._charge)

    def _charge(self, key, revision, value):
        self._last_charge = _tail_weight(key, revision, value)
        return self._last_charge

    def _store(self, key, revision, value):
        # Hold only keys/revisions/charged sizes for the transition, not an
        # extra reference to an evicted or overweight witness graph.
        try:
            before = {k: (entry[0], self._weights.get(k, 0), entry[1] is None)
                      for k, entry in self._entries.items()}
        except Exception:
            before = {}
            self._diagnostic_loss = True
        super()._store(key, revision, value)
        self._sequence += 1
        for victim in before.keys() - self._entries.keys() - {key}:
            old_revision, weight, was_empty = before[victim]
            self._remember(victim, old_revision,
                           _TailReason("evicted_empty" if was_empty else "evicted",
                                       self._sequence, weight))
        if key not in self._entries:
            reason = "overweight" if self._last_charge > self.max_weight else "unknown"
        else:
            reason = "consumed" if value is None else "admitted"
        self._remember(key, revision, _TailReason(reason, self._sequence, self._last_charge))

    def _remember(self, key, revision, reason):
        try:
            _TAIL_REASONS.put(key, revision, reason)
        except Exception:
            # Diagnostics cannot refuse or certify the actual import/cache.
            self._diagnostic_loss = True

    def observe(self, key, revision):
        with self._lock:
            before = self._sequence
        found, value = super().get(key, revision)  # exactly the existing get/LRU action
        reason = None
        if not found or value is None:
            with self._lock:
                resident = self._entries.get(key)
                try:
                    historical = _TAIL_REASONS.peek(key)
                except Exception:
                    historical = None
                    self._diagnostic_loss = True
                if self._sequence != before:
                    reason = "unknown_concurrent"
                elif self._diagnostic_loss:
                    # Failed replacement can leave a formerly valid old token.
                    # No history is authoritative until an explicit reset succeeds.
                    reason = "unknown_metadata"
                elif resident is not None and resident[0] != revision:
                    reason = "revision"
                elif historical is not None and historical[0] != revision:
                    reason = "unknown_revision"
                elif historical is not None:
                    recorded = historical[1].code
                    reason = recorded if recorded != "admitted" else "unknown_removal"
                else:
                    # No history is NOT proof this key never existed: history
                    # is bounded and may have reset/overflowed independently.
                    reason = "unknown_reset" if self._resets else "missing_unknown"
        return found, value, reason

    def consume(self, key, revision):
        with self._lock:
            self._store(key, revision, None)  # original pre-attempt invalidation
            return self._sequence

    def note_refusal(self, key, revision, token, code):
        if token is None:
            return
        try:
            with self._lock:
                entry = self._entries.get(key)
                recorded = _TAIL_REASONS.peek(key)
                if (entry is not None and entry[0] == revision and entry[1] is None
                        and recorded is not None and recorded[0] == revision
                        and recorded[1].token == token):
                    self._remember(key, revision, _TailReason(code, token))
        except Exception:
            self._diagnostic_loss = True

    def reset(self):
        with self._lock:
            self._entries.clear()
            self._weights.clear()
            self._pins.clear()
            self.weight = self.hits = self.misses = 0
            self._sequence += 1
            self._resets += 1
            try:
                _TAIL_REASONS.reset()
                self._diagnostic_loss = False
            except Exception:
                self._diagnostic_loss = True


_TAIL_STATES = _TailCache()
# The running hash of a file's imported bytes, by the checkpoint it belongs
# to. It outlives the frontier it was admitted with, which is consumed before
# every attempt: a frontier taken from the store again in the same process
# (an import that could not publish) need not read the file a second time.
_READ_PREFIXES = RevisionedCache("transcript-read-prefix", max_entries=256)
# Why the process has no frontier, when the stored one may stand in for it:
# it never had one (a Host start), lost it to the size limit, or the last
# import wrote nothing and could not publish. The stored frontier is still
# the last one that was published; its digests decide whether it holds.
#
# A tail whose publication was refused because another writer committed while
# it had given the writer away (`foreign_commit_yield`, or its connection
# changed in that gap) wrote rows but neither its checkpoint nor its frontier:
# the fence refuses both in the same place (`checkpoint_allowed`). So the
# store still holds the PREVIOUS pair, written together and consistent with
# each other (the frontier is keyed to that checkpoint's digest). The refused
# tail's rows stand above that pair's mark, so its guard would refuse them; a
# tail from the pair passes them only when its plan writes every one of them
# again exactly as it stands, reproduced from the transcript line for line,
# and they are still those rows when it applies (``_try_tail``). Anything not
# reproduced, and anything the foreign commit changed before the boundary, is
# refused as before and the import is full.
_YIELD_REFUSALS = frozenset({"publication_foreign_commit_yield", "publication_connection_changed_yield"})
_RESTORABLE = frozenset({"missing_unknown", "unknown_reset", "evicted", "evicted_empty",
                         "publication_not_written"}) | _YIELD_REFUSALS


def _stored_frontier(database, key, revision, agent_id, backend_session_id, path):
    """(checkpoint, state) as the store holds them, or (None, None). Whether
    the database rows still stand is not asked here: the tail's guard asks it
    of the record, with the rules, as it does of a frontier this process
    held."""
    from . import message_carry
    checkpoint = load_checkpoint(database, agent_id, backend_session_id, str(path))
    if checkpoint is None or checkpoint.parser_version != revision:
        return None, None
    stored = load_frontier(database, checkpoint)
    recorded = message_carry.loads(stored["inputs_digest"]) if stored is not None else None
    if recorded is None:
        return None, None
    hasher = None
    read = _READ_PREFIXES.peek(key)
    if read is not None and read[0] == revision and read[1][0] == stored["checkpoint_digest"]:
        hasher = read[1][1].copy()
    return checkpoint, _TailState(checkpoint, recorded, stored["file_hash"], hasher)


def _tail_key(path, agent_id, backend_session_id):
    from . import db
    return (str(db.DB_PATH), agent_id, backend_session_id, str(path))


def _prefix_effects(turns) -> str:
    """How the effects of the turns before a boundary stand: '' (none),
    'closed' (a full replay would repeat none of them) or 'open'.

    P1 kept every effect-bearing prefix on the full recovery path. Two kinds
    need no replay once their row is stored:

    - heartbeat and leader-tick prompts only set the store's carried origin
      and heartbeat key, which the checkpoint carries;
    - HEARTBEAT_OK is accounted only when its row changes, under a key
      claimed once in ``heartbeat_accounting``; a tail certifies the prefix
      bytes and every message row unchanged, so a replay would account
      nothing. LEADER_NOOP only logs.

    - a Claude sub-agent cell changes only through parent records after the
      boundary (tool result, task-notification), which
      ``reference_outside_tail`` refuses on every tail, or through its child
      and meta files, whose answer the checkpoint records
      (``subagent_children`` in the parser carry): a child that appears,
      goes or is bound to another call refuses the tail.

    Still open, so still full. Their effects may need retry, or differ on
    replay, even when their message row is unchanged; they cannot be skipped
    merely because bytes or rows were checkpointed:

    - any other sub-agent cell or an Agent/Task tool (Codex attaches collab
      events to the latest assistant turn, wherever it is);
    - a <team> block: team capture depends on the agent's current teams, which
      can change after the turn is old;
    - a dream prompt or dream output: a replayed DREAMING_OK marks whichever
      run is active now, so skipping it would change what a full import does.
    """
    from . import dreaming, heartbeat, team_leader, team_store
    found = ""
    for turn in turns:
        for cell in turn.get("display_cells") or []:
            if cell.get("kind") == "subagents":
                # Claude's cell (``ephemeral``) depends on nothing outside the
                # parent's bytes but the child directory, whose answer the
                # checkpoint records; any other sub-agent cell stays open.
                if cell.get("ephemeral") is not True:
                    return "open"
                found = "closed"
        if any(tool.get("name") in ("Agent", "Task") for tool in turn.get("tools") or []):
            return "open"
        text = str(turn.get("text") or "")
        if turn.get("role") == "user":
            if dreaming.should_skip_dream_prompt(text):
                return "open"
            if (heartbeat.should_skip_heartbeat_prompt(text)
                    or team_leader.should_skip_leader_tick_prompt(text)):
                found = "closed"
        elif turn.get("role") == "assistant":
            if (team_store.extract_team_blocks(text)
                    or dreaming.process_assistant_text("", text, origin="dreaming", live=True)
                    != (False, text)):
                return "open"
            if heartbeat.HEARTBEAT_OK in text or team_leader.LEADER_NOOP in text:
                found = "closed"
    return found


def _read_digest(path, previous_size, current_size, *, digest=None):
    """Digest the file up to ``current_size``; the digest at ``previous_size``
    too. With ``digest`` (a running hash that already holds the first
    ``previous_size`` bytes) only the bytes after them are read.

    Returns (digest at previous_size, digest at current_size, running hash).
    """
    start = 0 if digest is None else previous_size
    digest = hashlib.blake2b(digest_size=16) if digest is None else digest
    with open(path, "rb") as source:
        source.seek(start)

        def read_until(count):
            while count > 0:
                block = source.read(min(count, 1 << 20))
                if not block:
                    raise OSError("transcript shrank during prefix certification")
                digest.update(block)
                count -= len(block)
        read_until(previous_size - start)
        previous = digest.hexdigest()
        read_until(current_size - previous_size)
    return previous, digest.hexdigest(), digest


def _tail_file_hashes(path, previous_size, current_size):
    """One bounded-buffer read certifies ALL previously imported bytes.

    Durable sampled hashes remain format-compatible shadow guards. A full
    import, the first tail after a Host start, and a tail of a file that
    looks touched read every imported byte for this digest; other tails add
    the new bytes to the running hash (``digest_every_tail`` reads all of
    it every time). This read is outside SQLite transactions; its O(file
    bytes) cost is separate from range parsing and exact DB-input validation.
    """
    previous, current, _ = _read_digest(path, previous_size, current_size)
    return previous, current


def _prefix_changed(path, what, checkpoint) -> None:
    """One line naming the file whose imported bytes or identity no longer
    match what was imported, and which check found it. A full import follows."""
    from .log import log
    log("transcriptPrefixChanged",
        f"file={path} what={what} imported_bytes={checkpoint.complete_size} "
        f"boundary={checkpoint.boundary_offset}")


def _size_bucket(size):
    if size is None:
        return "unknown"
    for limit, label in ((1, "lt1m"), (16, "lt16m"), (64, "lt64m"),
                         (256, "lt256m"), (1024, "lt1024m")):
        if size < limit * 1024 * 1024:
            return label
    return "ge1024m"


def _only_grew(before: Identity, now: Identity) -> bool:
    """The same file, longer, and not touched backwards: bytes were added."""
    return ((now.device, now.inode) == (before.device, before.inode)
            and now.size > before.size and now.mtime_ns >= before.mtime_ns)


def _tail_log(code, *, retention=False, reason=None, backend=None, size=None, extra=""):
    """Fixed codes and unit counts; no session, path, text or message IDs."""
    from .log import log
    detail = f"code={code} count=1"
    if reason is not None:
        detail += f" reason={reason}"
    if extra:
        detail += f" {extra}"
    if not retention:
        observed_backend, observed_size = _IMPORT_LOG_METADATA.get()
        if backend is None:
            backend = observed_backend
            size = size if size is not None else observed_size
        detail += f" backend={backend if backend in SUPPORTED_BACKENDS else 'other'}"
        detail += f" size={_size_bucket(size)}"
    log("transcriptTailRetention" if retention else "transcriptTailDecision", detail)


def _refuse_tail(code, *, reason=None, backend=None, size=None):
    _tail_log("full_" + code, reason=reason, backend=backend, size=size)
    return None


def _checkpoint_refused(status):
    reasons = status.get("refused", ())
    for code in reasons:
        # A foreign commit across a yield: whether this conversation's own head moved
        # in it (a send, its live stream) or another writer's commit came. Counted
        # only; the refusal is the same either way.
        same = status.get("same_conversation") if code == "foreign_commit_yield" else None
        _tail_log("checkpoint_" + code, retention=True,
                  extra="" if same is None else f"same_conversation={'yes' if same else 'no'}")
    return bool(reasons)


def _checkpoint_reason(status):
    reasons = status.get("refused", ())
    known = {"foreign_commit_yield", "connection_changed_yield",
             "yield_without_release_sample", "data_version_read_failure"}
    if len(reasons) == 1 and reasons[0] in known:
        return "publication_" + reasons[0]
    return "publication_refused_unknown"


def _cache_committed_tail(key, revision, checkpoint, recorded, file_hash, *, reason_token=None,
                          hasher=None, grown_ok=False, unwritten=""):
    from . import db
    if recorded is None or file_hash is None:
        _TAIL_STATES.note_refusal(key, revision, reason_token, "publication_not_written")
        _tail_log("checkpoint_not_written", retention=True, reason=unwritten or "not_published")
        return
    try:
        current = identity(pathlib.Path(checkpoint.source_file))
    except OSError:
        _TAIL_STATES.note_refusal(key, revision, reason_token, "publication_file_unavailable")
        _tail_log("file_unavailable_after_store", retention=True)
        return
    described = Identity(checkpoint.device, checkpoint.inode, checkpoint.size, checkpoint.mtime_ns)
    if current != described and not (grown_ok and _only_grew(described, current)):
        _TAIL_STATES.note_refusal(key, revision, reason_token, "publication_file_moved")
        _tail_log("file_moved_before_publication", retention=True)
        return
    if load_checkpoint(db.conn(), checkpoint.agent_id, checkpoint.backend_session_id,
                       checkpoint.source_file) != checkpoint:
        _TAIL_STATES.note_refusal(key, revision, reason_token, "publication_superseded")
        _tail_log("checkpoint_superseded", retention=True)
        return
    if recorded["oracle"]:
        _TAIL_STATES.note_refusal(key, revision, reason_token, "oracle_history")
        _tail_log("oracle_history", retention=True)
        return
    state = _TailState(checkpoint, recorded, file_hash, hasher)
    if hasher is not None:
        _READ_PREFIXES.put(key, revision, (checkpoint_digest(checkpoint), hasher.copy()))
    _TAIL_STATES.put(key, revision, state)
    found, retained = _TAIL_STATES.get(key, revision)
    if not found or retained is not state:
        _tail_log("frontier_not_retained", retention=True)


def _stream_owns_turns(database, agent_id: str, backend_session_id: str) -> bool:
    """Whether this conversation has turns whose text the stream owns
    (``agy_turn_authority``).

    A reply inside such a turn's range is stored as no row, yet a full replay
    names it in its batch under the authority's trace and so removes a
    leftover live row of that trace. ``carry_live_rows`` asks the replies
    STORED before the boundary for their traces and cannot see that one, so
    for a conversation with any such turn the rule is left out: a changed live
    row is a full import there, as it is with the switch off."""
    return database.execute(
        "SELECT 1 FROM agy_turn_authority WHERE agent_id = ? AND backend_session_id = ? LIMIT 1",
        (agent_id, backend_session_id)).fetchone() is not None


def _try_tail(backend, path, agent_id, backend_session_id, conf, started, *, diagnostic=None):
    from . import db, message_carry, message_writes
    key = _tail_key(path, agent_id, backend_session_id)
    revision = parser_version(backend)
    size = diagnostic.get("size") if diagnostic is not None else None

    def refuse(code, *, reason=None):
        return _refuse_tail(code, reason=reason, backend=backend, size=size)

    rules = carry_rules(conf)
    if message_carry.LIVE_ROWS in rules and _stream_owns_turns(db.conn(), agent_id,
                                                               backend_session_id):
        rules = rules - {message_carry.LIVE_ROWS}
    session_rows = message_carry.SESSION_ROWS in rules
    found, state, reason = _TAIL_STATES.observe(key, revision)
    # Invalidate before any attempt, including a failure after a partial batch
    # or external effect. Only successful committed progress re-admits carry.
    reason_token = _TAIL_STATES.consume(key, revision)
    if diagnostic is not None:
        diagnostic["token"] = reason_token
    restored = False
    if (not found or state is None) and reason in _RESTORABLE:
        # The frontier this process does not hold, as the last import that
        # published it left it in the store.
        stored, state = _stored_frontier(db.conn(), key, revision, agent_id,
                                         backend_session_id, path)
        restored = state is not None
        if stored is not None and diagnostic is not None:
            diagnostic["imported"] = stored
    elif found and state is not None and diagnostic is not None:
        diagnostic["imported"] = state.checkpoint
    if state is None:
        return refuse("no_frontier", reason=reason)
    cp = load_checkpoint(db.conn(), agent_id, backend_session_id, str(path))
    if cp != state.checkpoint:
        return refuse("checkpoint_changed")
    before = identity(path)
    if complete_size(path, before.size) != before.size:
        return refuse("partial_record")
    rows_now = None

    def recorded_rows_changed(applying=frozenset(), why=None) -> str:
        """What stands in the way of the stored record, the rows read once."""
        nonlocal rows_now
        if rows_now is None:
            rows_now = message_writes._prepare_import_prelude(
                db.conn(), agent_id, backend_session_id, [], raw_only=True,
                session_rows=session_rows)
        return message_carry.changed(
            state.inputs, rows_now, rules=applying, carry=cp.store_carry,
            boundary_seq=cp.boundary_seq, source_file=str(path),
            backend_session_id=backend_session_id, session_rows=session_rows, why=why)

    # The file is the one that import read, to its last byte (a checkpoint's
    # size can be ahead of its imported bytes: the file grew during that
    # parse), and the database rows it read are as they were: every one of
    # them, no rule applied. A row a rule lets through can still be for a
    # turn after the boundary, so with such a row the tail runs.
    unchanged = (restored and (before.size, before.mtime_ns) == (cp.size, cp.mtime_ns)
                 and cp.complete_size == before.size
                 and not recorded_rows_changed())
    if restored and not unchanged:
        # A frontier from the store: its rows are asked before anything of
        # the file is read. The checks below read its samples and the bytes
        # after the boundary, and the first tail from it every imported
        # byte: all wasted on a tail the rows refuse.
        why: dict = {}
        what = recorded_rows_changed(rules, why)
        if what:
            from .log import log
            log("transcriptCarryInputsChanged", _carry_line(what, why.get("how", "")))
            return refuse("carry_inputs_changed")
    # With ``tail_hashes_while_parsing`` a tail that is going to parse its
    # range takes both hashes of it from that parse: the one that proves the
    # earlier import's bytes from the boundary on (``tail_rewritten``, asked
    # after the parse instead of before it) and the one its checkpoint keeps.
    hashes_in_parse = bool(conf.get("tail_hashes_while_parsing")) and not unchanged
    checks = dict(now_ms=db.now_ms(), max_tail_bytes=int(conf["max_tail_bytes"]),
                  tail_hash_later=hashes_in_parse)
    reason = decide(cp, backend, path, before, audit_every=int(conf["audit_every"]),
                    audit_seconds=float(conf["audit_seconds"]), **checks)
    audited = False
    if reason == "audit" and unchanged and bool(conf.get("audit_unchanged_by_digest")):
        # Its audit is due and there is nothing to replay: the rows are proven
        # unchanged, so what an audit can still find is in the bytes. The
        # tail's other checks first, then every imported byte, read once.
        # Nothing is written, so this is not the audit a grown file gets;
        # that stays a full import and is what starts the clock again.
        reason = decide(cp, backend, path, before, audit_every=sys.maxsize,
                        audit_seconds=float("inf"), **checks)
        if not reason:
            imported_hash, _, hasher = _read_digest(path, cp.complete_size, before.size)
            if imported_hash != state.file_hash:
                _prefix_changed(path, "imported_bytes", cp)
                if diagnostic is not None:
                    diagnostic.pop("imported", None)      # said once; the full import follows
                return refuse("prefix_content_changed")
            state = replace(state, hasher=hasher)
            audited = True
    audit_by_digest = False
    # How many audits of this conversation were by digest since its last full
    # import. It is kept in the checkpoint, beside the parser's carry, and a
    # tail hands it on; a full import writes a new carry without it, so a full
    # import for any reason starts the count again. Every
    # ``audit_full_every``-th due audit stays the full import.
    audits_by_digest = int((cp.parser_carry or {}).get(_AUDITS_BY_DIGEST) or 0)
    if reason == "audit" and not unchanged and bool(conf.get("audit_grown_by_digest")) \
            and audits_by_digest + 1 < max(1, int(conf.get("audit_full_every") or 1)):
        # A grown file whose audit is due. What the audit is for is the bytes
        # a tail does not read again: are they still what was imported? That
        # is one read of the file and a comparison; replaying every turn to
        # find it out cost a parse of the whole file and every row of the
        # session. The tail's other checks first (a tail too large, a
        # rewritten head or sample, a reference outside the tail are still
        # full imports); then every imported byte is read below, a mismatch
        # is a full import as it always was, and the tail starts the audit's
        # clock again.
        reason = decide(cp, backend, path, before, audit_every=sys.maxsize,
                        audit_seconds=float("inf"), **checks)
        audit_by_digest = not reason
    if reason:
        if reason in ("inode", "shrink", "mtime_backwards", "prefix_rewritten", "tail_rewritten"):
            _prefix_changed(path, reason, cp)
            if diagnostic is not None:
                diagnostic.pop("imported", None)
        return refuse(reason)
    if unchanged:
        # There is nothing to import. What the process's own record of an
        # unchanged file says, which a Host start empties. (With other rows a
        # full import may rewrite some, so it runs, as it always did.)
        #
        # It stands after the checks above on purpose: a file is left alone
        # only where a tail from its checkpoint would be allowed. They have
        # compared the device and inode; a sub-agent child that appeared or
        # went while the parent was idle is noticed; and a file rewritten in
        # place and then idle is found once its audit is due, by the read
        # above or, with ``audit_unchanged_by_digest`` off, by a full import.
        _TAIL_STATES.put(key, revision, state)
        _tail_log("unchanged_file_audited" if audited else "unchanged_file",
                  backend=backend, size=size)
        return {"rows": cp.turn_count, "parse_s": 0.0,
                "store_s": time.perf_counter() - started, "shadow": "not_sampled_tail",
                "apply_mode": "unchanged", "parsed_bytes": 0,
                "_clarp_transcript_completion_v1": "completed-unchanged"}
    # Every imported byte is read again when the setting says so, when this
    # process has not read them yet (the first tail after a Host start), and
    # when the file was touched without growing. Otherwise the new bytes are
    # added to the running hash: the checks in ``decide`` (identity, size,
    # mtime, the head, the samples, the bytes after the boundary) stand for
    # the rest until the next audit, which is a full import.
    touched = before.size == cp.size and before.mtime_ns != cp.mtime_ns
    # With ``tail_keeps_frontier_when_grown``: the running hash as it stands
    # before this import's bytes are added, to add them a second time below.
    keep_when_grown = bool(conf.get("tail_keeps_frontier_when_grown"))
    earlier_hasher = None
    if bool(conf.get("digest_every_tail")) or state.hasher is None or touched or audit_by_digest:
        previous_hash, current_hash, hasher = _read_digest(path, cp.complete_size, before.size)
        if previous_hash != state.file_hash:
            _prefix_changed(path, "imported_bytes", cp)
            if diagnostic is not None:
                diagnostic.pop("imported", None)      # said once; the full import follows
            return refuse("prefix_content_changed")
    else:
        if keep_when_grown:
            earlier_hasher = state.hasher.copy()
        _, current_hash, hasher = _read_digest(path, cp.complete_size, before.size,
                                               digest=state.hasher)
    observed = {} if keep_when_grown or hashes_in_parse else None
    if hashes_in_parse:
        observed["mark"] = cp.complete_size - cp.boundary_offset
    turns = parse_range(backend, path, cp.boundary_offset, before.size, observed=observed)
    parsed = time.perf_counter()
    tail_hash = None
    grew = False            # the file only grew while it was imported
    after_parse = identity(path)
    if after_parse != before:
        # The file moved while it was parsed. When it only grew, the tail
        # can go on for the bytes it parsed, if those are still the file's:
        # the bytes the parser was handed hash as the same range read now,
        # and the bytes hashed before the parse hash the same read again
        # (those added to the running hash; all of them when this import
        # read them all). A full import has the same allowance,
        # ``growing_prefix``. Without both proofs, or when the file was
        # touched, rewritten or replaced, it is refused as before.
        if not (keep_when_grown and _only_grew(before, after_parse)):
            return refuse("file_moved_during_parse")
        tail_hash = range_hash(path, cp.boundary_offset, before.size)
        if observed.get("hash") != tail_hash or _read_digest(
                path, cp.complete_size, before.size, digest=earlier_hasher)[1] != current_hash:
            return refuse("file_moved_during_parse")
        grew = True
    parsed_hash = None      # the range's digest from the parse, when it is the file's
    if hashes_in_parse:
        # What the parser was handed is what a read of the range gives when
        # the file did not change while it was parsed: the same file, size,
        # modification and change time as when its reader opened it, and the
        # size this import began with (``_parsed_digests`` asks the same of a
        # full import). Otherwise the range is read, as it always was. Either
        # way the earlier import's part is compared here, after the parse:
        # bytes of it rewritten while this tail ran are found by this tail.
        stood = _parse_stood(observed, path, before)
        earlier_tail = observed.get("at_mark") if stood else None
        if earlier_tail is None:
            earlier_tail = range_hash(path, cp.boundary_offset, cp.complete_size)
        if earlier_tail != cp.tail_hash:
            _prefix_changed(path, "tail_rewritten", cp)
            if diagnostic is not None:
                diagnostic.pop("imported", None)
            return refuse("tail_rewritten")
        parsed_hash = observed.get("hash") if stood else None
    if tail_hash is None:
        tail_hash = parsed_hash or range_hash(path, cp.boundary_offset, before.size)
    next_cp = replace(cp, size=before.size, mtime_ns=before.mtime_ns,
                      complete_size=before.size, turn_count=cp.boundary_seq + len(turns),
                      tail_hash=tail_hash,
                      imports_since_audit=0 if audit_by_digest else cp.imports_since_audit + 1,
                      audited_at=db.now_ms() if audit_by_digest else cp.audited_at,
                      parser_carry={**cp.parser_carry, _AUDITS_BY_DIGEST: audits_by_digest + 1}
                      if audit_by_digest else cp.parser_carry,
                      updated_at=db.now_ms())
    recorded = None
    checkpoint_status = {}
    source_invalidated = False
    witness: dict = {}      # the rows the guard hashed, for the record published below

    def moved(current) -> bool:
        """Whether the file is no longer the one that was parsed. Growth is
        not that, with the switch: the checkpoint is for the bytes parsed,
        and the import is reported as not complete so that it comes again."""
        nonlocal grew
        if current == before:
            return False
        if keep_when_grown and _only_grew(before, current):
            grew = True
            return False
        return True

    published_under: list = []   # what the checkpoint was written under (head, turns, oracle)
    snapshot: list = []          # the message rows as they were at that commit

    def publish(database, captured):
        nonlocal recorded, source_invalidated
        try:
            current = identity(path)
        except OSError:
            source_invalidated = True
            raise
        if moved(current):
            source_invalidated = True
            return
        write_checkpoint(database, next_cp)
        # The message rows are read for the frontier AFTER this transaction
        # commits, not inside it (R119): here the head (one row, or one
        # statement), and the turn rows and oracle delegations, which move no
        # head and are read where they always were.
        published_under.append(commit_inputs(database, agent_id, backend_session_id, session_rows))

    def committed(database):
        # The import has committed: a failed read costs the frontier, never
        # the import's result.
        if not published_under:
            return
        try:
            snapshot.append(commit_snapshot(database, agent_id, backend_session_id,
                                            published_under[-1], session_rows))
        except Exception as exc:  # noqa: BLE001 - logged; the next tail goes without a frontier
            from .log import log_exception
            log_exception("transcriptFrontierSnapshotFail", exc)
            _tail_log("frontier_not_published_snapshot_failed", retention=True)
            snapshot.append(None)

    accepted = None
    if restored and reason in _YIELD_REFUSALS:
        # The refused tail's rows stand above the stored mark. They pass only
        # if this tail's plan writes each of them again exactly as it stands
        # (reproduced from the transcript, line for line); anything else is
        # refused and the import is full, as before.
        pending: dict = {}
        rows_now = message_writes._prepare_import_prelude(
            db.conn(), agent_id, backend_session_id, [], raw_only=True, session_rows=session_rows)
        what = message_carry.changed(
            state.inputs, rows_now, rules=rules, carry=cp.store_carry, boundary_seq=cp.boundary_seq,
            source_file=str(path), backend_session_id=backend_session_id, session_rows=session_rows,
            pending_tail=pending)
        if not what and pending:
            try:
                plan = message_writes.plan_transcript_tail(
                    agent_id=agent_id, backend_session_id=backend_session_id, source_file=str(path),
                    turns=turns, start_seq=cp.boundary_seq, carry=cp.store_carry)
            except message_writes._TailInvalidated as changed:
                # The plan runs the tail's pass, which refuses what a tail cannot
                # write (a parked positional row, an identity shift): full, as the
                # tail itself would refuse it below, never a failed import.
                from .log import log
                log("transcriptCarryInputsChanged", f"what={getattr(changed, 'what', 'unknown')}")
                return refuse("carry_inputs_changed")
            planned = {str(entry.get("id")) for entry in plan["out"]}
            touched = set(plan["written"]) | set(plan["updated"]) | set(plan["deleted"])
            if not pending["ids"] <= planned or pending["ids"] & touched:
                from .log import log
                log("transcriptCarryInputsChanged", "what=row_transcript_not_reproduced")
                return refuse("carry_inputs_changed")
            accepted = pending
    try:
        message_writes.store_transcript_tail(
            agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=str(path), turns=turns, start_seq=cp.boundary_seq,
            carry=cp.store_carry, expected_prelude=state.inputs, carry_rules=rules,
            carry_witness=witness,
            yield_after_first_write=bool(conf.get("tail_yield_after_first_write")),
            on_stored=publish, on_committed=committed, checkpoint_status=checkpoint_status,
            carry_accepted=accepted)
    except message_writes._TailInvalidated as changed:
        # Which rows stood in the way: what a further rule would have to cover.
        from .log import log
        log("transcriptCarryInputsChanged",
            _carry_line(getattr(changed, "what", "unknown"), getattr(changed, "how", "")))
        return refuse("carry_inputs_changed")
    if snapshot and snapshot[-1] is not None and not _checkpoint_refused(checkpoint_status) \
            and not source_invalidated:
        recorded = frontier_after_commit(next_cp, current_hash, published_under[-1], snapshot[-1],
                                         witness.get("base"), session_rows=session_rows)
    try:
        source_invalidated = source_invalidated or moved(identity(path))
    except OSError:
        source_invalidated = True
    if not _checkpoint_refused(checkpoint_status) and not source_invalidated:
        # Why no frontier, when there is none: the tail wrote nothing (the
        # stored pair still holds), or its rows could not be read after commit.
        unwritten = ("nothing_written" if not published_under
                     else "snapshot_missing" if not snapshot or snapshot[-1] is None else "")
        _cache_committed_tail(key, revision, next_cp, recorded, current_hash,
                              reason_token=reason_token, hasher=hasher, grown_ok=keep_when_grown,
                              unwritten=unwritten)
    else:
        _TAIL_STATES.note_refusal(key, revision, reason_token, _checkpoint_reason(checkpoint_status))
    # Include replacement during process-frontier publication, not just the
    # transaction callback. A refusal must not become a fresh completion.
    try:
        source_invalidated = source_invalidated or moved(identity(path))
    except OSError:
        source_invalidated = True
    result = {"rows": next_cp.turn_count, "parse_s": parsed - started,
              "store_s": time.perf_counter() - parsed, "shadow": "not_sampled_tail",
              "apply_mode": "tail", "parsed_bytes": before.size - cp.boundary_offset}
    if source_invalidated:
        token = _TAIL_STATES.consume(key, revision)
        _TAIL_STATES.note_refusal(key, revision, token, "publication_file_moved")
        _tail_log("deferred_tail_source_changed", backend=backend, size=size)
        result.update(apply_mode="deferred", _clarp_transcript_completion_v1="deferred")
    elif grew:
        # Imported and published up to the size that was parsed. The file is
        # longer now, so this is not a completion: the import comes again,
        # and finds its frontier.
        kept = _TAIL_STATES.peek(key)
        if kept is not None and kept[0] == revision and kept[1] is not None \
                and kept[1].checkpoint == next_cp:
            if audit_by_digest:
                _tail_log("audited_by_digest", retention=True)
            _tail_log("frontier_kept_source_grew", retention=True)
        _tail_log("deferred_tail_source_changed", backend=backend, size=size)
        result.update(apply_mode="deferred", _clarp_transcript_completion_v1="deferred")
    else:
        if audit_by_digest and not _checkpoint_refused(checkpoint_status):
            _tail_log("audited_by_digest", retention=True)
        _tail_log("tail_applied", backend=backend, size=size)
    return result


def _parse_stood(observed, path, before) -> bool:
    """Whether the file is still what the parser's reader found when it
    opened it, and that was the file this import began with: the same device
    and inode, the size ``before`` has, the same modification and change
    time. Then the bytes the parser was handed are the file's. False when it
    did not read the range in one piece."""
    opened = (observed or {}).get("opened")
    if opened is None:
        return False
    try:
        now = os.stat(path)
    except OSError:
        return False
    return opened == (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns, now.st_ctime_ns) \
        and opened[:4] == (before.device, before.inode, before.size, before.mtime_ns)


def _parsed_digests(parsed_read, parsed_evidence, before, earlier, path):
    """((digest at the earlier import's size, digest of the parsed bytes,
    the running hash), "saved") from the parse's own read, when a second
    read of the file would give the same: ``_read_digest`` without reading.
    (None, why) when it must be read; ``why`` is a fixed word for the
    journal: ``grew``, ``touched``, ``identity_changed`` (another file under
    the name, a shorter one, or none), ``earlier_import_moved`` (the earlier
    import's size is not the one the parse was told to mark),
    ``not_observed`` (the parse was not one whole observed read; with the
    switch off it never is, and the caller says ``switch_off``).

    The parser was handed bytes [0, before.size) and they were hashed as it
    took them (``_RangePath``). Those are the file's bytes when the file did
    not change while it was read: the same file, the same size, the same
    modification and change times as when it was opened. Size or mtime alone
    would let a rewrite in place that restores the mtime through; the change
    time cannot be set back. (A store through a memory map into a page that
    is already dirty moves neither time and is not seen here; the audit finds
    it, as it finds a rewrite that lands just after an import.) A file that
    grew or was touched meanwhile is read again, as it always was: growth
    cannot be told from a rewrite without reading."""
    if parsed_read is None or parsed_evidence is None or parsed_read.get("stat") is None:
        return None, "not_observed"
    try:
        now = os.stat(path)
    except OSError:
        return None, "identity_changed"
    size, mtime_ns, ctime_ns = parsed_read["stat"]
    if (now.st_dev, now.st_ino) != (before.device, before.inode) \
            or not before.size <= size <= now.st_size:
        return None, "identity_changed"
    if now.st_size != before.size:
        return None, "grew"
    if (now.st_mtime_ns, now.st_ctime_ns) != (mtime_ns, ctime_ns) or mtime_ns != before.mtime_ns:
        return None, "touched"
    current = parsed_evidence[1]
    if earlier is None:
        return (current, current, parsed_read["hash"].copy()), "saved"
    if parsed_read.get("mark") != earlier[0] or parsed_read.get("at_mark") is None:
        return None, "earlier_import_moved"
    return (parsed_read["at_mark"], current, parsed_read["hash"].copy()), "saved"


def _second_read_log(code, backend, size, *, read_bytes=None, cpu_ms=None):
    """One line for each full import that reaches its digest: whether the
    second read of the file was saved, or why it was made. A fixed word, a
    count, the backend and the size's bucket; no session, path or id. With
    them, what the full import has cost so far: the bytes it read (the parse,
    and the second read when it was made) and the CPU of its thread in ms,
    from the parse's start to the digest."""
    from .log import log
    detail = (f"code={code} count=1 backend={backend if backend in SUPPORTED_BACKENDS else 'other'}"
              f" size={_size_bucket(size)}")
    if read_bytes is not None:
        detail += f" bytes={int(read_bytes)}"
    if cpu_ms is not None:
        detail += f" cpu_ms={int(cpu_ms)}"
    log("transcriptFullDigest", detail)


def _digest_and_log(path, digests, second_read, earlier, before, backend, cpu_started):
    """The full import's digests (the parse's, or a second read of the file), and its
    one transcriptFullDigest line. The line is written even when the second read
    raises (a file gone or unreadable mid-read), then with what this import had read
    and spent by then: the parse's bytes, as the line was before the read on main."""
    read_bytes = before.size
    try:
        found = digests or _read_digest(path, earlier[0] if earlier else before.size, before.size)
        read_bytes = before.size * (1 if digests else 2)
        return found
    finally:
        _second_read_log(second_read, backend, before.size, read_bytes=read_bytes,
                         cpu_ms=None if cpu_started is None
                         else (_thread_time() - cpu_started) * 1000)


def _store_full_with_tail_checkpoint(backend, path, agent_id, backend_session_id,
                                     turns, before, conf, *, reason_token=None,
                                     parsed_evidence=None, imported=None, parsed_read=None,
                                     cpu_started=None):
    from . import db, message_carry, message_store, message_writes
    key = _tail_key(path, agent_id, backend_session_id)
    revision = parser_version(backend)
    session_rows = message_carry.SESSION_ROWS in carry_rules(conf)
    kwargs = {}
    checkpoint = None
    recorded = None
    current_hash = None
    checkpoint_status = {}
    refusal = "file_identity_unavailable"
    refusal_logged = False
    source_invalidated = False
    effects = ""
    hasher = None
    # Why a full import that found its boundary still wrote no checkpoint: a
    # fixed word for the journal (the reason the next import is full again).
    unwritten = ""
    try:
        observed = identity(path) if before is not None else None
        growing_prefix = (parsed_evidence is not None and before is not None
                          and parsed_evidence[0] == before.size
                          and parsed_evidence[2:] == (before.device, before.inode)
                          and (observed.device, observed.inode) == (before.device, before.inode)
                          and observed.size >= before.size
                          and observed.mtime_ns >= before.mtime_ns)
        if before is None:
            pass
        elif not turns:
            refusal = "empty_transcript"
        elif turns[0].get("role") != "user":
            refusal = "assistant_prefix"
        elif observed != before and not growing_prefix:
            source_invalidated = parsed_evidence is not None
            refusal = "file_moved_before_bootstrap"
        elif complete_size(path, before.size) != before.size:
            refusal = "partial_record"
        else:
            boundary, why = choose_boundary(
                backend, path, turns, turn_digests(turns), before, int(conf["max_tail_bytes"]),
                turns_only=bool(conf.get("boundary_skips_dropped_records")))
            if boundary is None:
                refusal = "no_safe_boundary_" + why
            elif (effects := _prefix_effects(turns[:boundary.seq])) == "open":
                refusal = "effect_bearing_prefix"
            else:
                # The same read tells whether the bytes an earlier import
                # read are still what they were (the audit of the bytes a
                # tail does not read again). The full import goes on either way.
                earlier = _imported_before(imported, before, agent_id, backend_session_id, path)
                digests, second_read = _parsed_digests(
                    parsed_read, parsed_evidence, before, earlier, path)
                if digests is None and not bool(conf.get("full_digest_while_parsing")):
                    second_read = "switch_off"
                previous, current_hash, hasher = _digest_and_log(
                    path, digests, second_read, earlier, before, backend, cpu_started)
                if earlier and previous != earlier[1]:
                    _prefix_changed(path, "imported_bytes", imported)
                # Compare against ACTUAL bytes delivered to the native parser,
                # not a digest created after the parser saw some older file.
                if parsed_evidence is not None and current_hash != parsed_evidence[1]:
                    source_invalidated = True
                    refusal = "parsed_prefix_changed"
                else:
                    final = identity(path)
                    if ((final.device, final.inode) != (before.device, before.inode)
                            or final.size < before.size or final.mtime_ns < before.mtime_ns
                            or (parsed_evidence is None and final != before)):
                        source_invalidated = parsed_evidence is not None
                        refusal = "file_moved_before_bootstrap"
                    else:
                        observed = final
                        refusal = ""
                        captured = {}

                        def publish(database, carry):
                            nonlocal checkpoint, recorded, source_invalidated, unwritten
                            if not carry:
                                unwritten = "no_capture"
                                return
                            try:
                                current = identity(path)
                            except OSError:
                                source_invalidated = parsed_evidence is not None
                                raise
                            if current != observed:
                                source_invalidated = parsed_evidence is not None
                                unwritten = ("grew_during_store" if _only_grew(observed, current)
                                             else "moved_during_store")
                                return
                            now = db.now_ms()
                            # size/mtime describe the observed file; complete_size
                            # and all hashes describe ONLY the parsed prefix.
                            checkpoint = Checkpoint(
                                agent_id, backend_session_id, str(path), backend, parser_version(backend),
                                observed.device, observed.inode, observed.size, observed.mtime_ns,
                                boundary.complete_size, boundary.offset, boundary.seq, len(turns),
                                boundary.prefix_hash, boundary.tail_hash, boundary.prefix_turns_hash,
                                boundary.parser_carry, dict(carry), 0, now, now)
                            write_checkpoint(database, checkpoint)
                            recorded = inputs_record(message_writes._prepare_import_prelude(
                                database, agent_id, backend_session_id, [], raw_only=True,
                                session_rows=session_rows), checkpoint, session_rows)
                            write_frontier(database, checkpoint, current_hash, recorded)
                            return True

                        def prepare_publication():
                            nonlocal observed, source_invalidated
                            current = identity(path)
                            if ((current.device, current.inode) != (before.device, before.inode)
                                    or current.size < before.size or current.mtime_ns < before.mtime_ns):
                                source_invalidated = parsed_evidence is not None
                                return False
                            _, digest = _tail_file_hashes(path, before.size, before.size)
                            if digest != current_hash:
                                source_invalidated = parsed_evidence is not None
                                return False
                            observed = identity(path)
                            valid = ((observed.device, observed.inode) == (before.device, before.inode)
                                     and observed.size >= before.size
                                     and observed.mtime_ns >= before.mtime_ns
                                     and (parsed_evidence is not None or observed == before))
                            if not valid:
                                source_invalidated = parsed_evidence is not None
                            return valid

                        kwargs = {"capture_at": boundary.seq, "captured": captured,
                                  "on_stored": publish, "recertify_checkpoint": prepare_publication,
                                  "carry_session_rows": session_rows}
                        if conf.get("carry_live_rows") and conf.get("carry_live_rows_by_words"):
                            kwargs["reply_words"] = True
    except OSError:
        _tail_log("bootstrap_file_io", retention=True)
        refusal_logged = True
        refusal = "bootstrap_file_io"
        kwargs = {}
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id=backend_session_id,
        source_file=str(path), turns=turns, checkpoint_status=checkpoint_status, **kwargs)
    if parsed_evidence is not None:
        # A staged rename can land after the callback's check and before its
        # transaction commits. Never turn those old-incarnation products into a
        # fresh completion; the next admission must parse the new source fully.
        try:
            after_store = identity(path)
            if ((after_store.device, after_store.inode) != (before.device, before.inode)
                    or after_store.size < before.size or after_store.mtime_ns < before.mtime_ns):
                source_invalidated = True
        except OSError:
            source_invalidated = True
    if _checkpoint_refused(checkpoint_status):
        _TAIL_STATES.note_refusal(key, revision, reason_token, _checkpoint_reason(checkpoint_status))
        return source_invalidated
    if refusal:
        reason = {"effect_bearing_prefix": "bootstrap_effect",
                  "file_moved_before_bootstrap": "bootstrap_file_moved",
                  "empty_transcript": "bootstrap_empty",
                  "assistant_prefix": "bootstrap_assistant_prefix",
                  "partial_record": "bootstrap_partial_record",
                  "bootstrap_file_io": "bootstrap_io"}.get(refusal, "bootstrap_refused")
        _TAIL_STATES.note_refusal(key, revision, reason_token, reason)
        if not refusal_logged:
            _tail_log(refusal, retention=True)
    elif checkpoint is None:
        _TAIL_STATES.note_refusal(key, revision, reason_token, "publication_not_written")
        _tail_log("checkpoint_not_written", retention=True, reason=unwritten or "not_published")
    else:
        if effects == "closed":
            # Counted apart: before this narrowing it was bootstrap_effect.
            _tail_log("checkpoint_closed_effects", retention=True)
        _cache_committed_tail(key, revision, checkpoint, recorded, current_hash,
                              reason_token=reason_token, hasher=hasher)
    return source_invalidated


def _imported_before(checkpoint, before, agent_id, backend_session_id, path):
    """(bytes, their digest) that an earlier import of this same file read,
    when the store still says: for the full import to compare with."""
    from . import db
    if (checkpoint is None or before is None
            or (checkpoint.device, checkpoint.inode) != (before.device, before.inode)
            or checkpoint.complete_size > before.size):
        return None
    current = load_checkpoint(db.conn(), agent_id, backend_session_id, str(path))
    stored = load_frontier(db.conn(), current) if current == checkpoint else None
    return (checkpoint.complete_size, stored["file_hash"]) if stored else None


def import_transcript(*, backend: str, path: pathlib.Path, agent_id: str,
                      backend_session_id: str,
                      parse: Callable[[pathlib.Path], list[dict]] | None = None,
                      _defer_full: bool = False,
                      ) -> dict[str, Any]:
    """Parse and store one transcript in full (authoritative), plus the
    shadow when the budget admits it. Returns the import's timings."""
    from . import backends, message_store
    started = time.perf_counter()
    path = pathlib.Path(path)
    implementation = backends.get(backend)
    method = implementation.parse_transcript if implementation is not None else None
    native_binding = (isinstance(method, types.MethodType)
                      and method.__self__ is implementation
                      and method.__func__ is type(implementation).parse_transcript)
    native_parse = native_binding and (parse is None or (
        isinstance(parse, types.MethodType) and parse.__self__ is implementation
        and parse.__func__ is type(implementation).parse_transcript))
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
        diagnostic = {}
        if tail_enabled:
            try:
                before = identity(path)
                diagnostic["size"] = before.size
            except OSError as exc:
                from .log import log_exception
                log_exception("transcriptTailFileSkip", exc)
                _tail_log("full_file_io", backend=backend)
        gate_native = False
        if _defer_full and tail_enabled:
            method = implementation.parse_transcript
            gate_native = (isinstance(method, types.MethodType)
                           and method.__func__ is type(implementation).parse_transcript)
        with _FullImportAttempt(gate_native, backend, path,
                                agent_id, backend_session_id, before) as admission:
            if admission.reason:
                _tail_log("deferred_full_" + admission.reason, backend=backend,
                          size=before.size if before else None)
                return {"rows": 0, "parse_s": 0.0, "store_s": 0.0,
                        "apply_mode": "deferred", "shadow": skip,
                        "_clarp_transcript_completion_v1": "deferred"}
            if tail_enabled and before is not None:
                try:
                    tail = _try_tail(backend, path, agent_id, backend_session_id, conf, started,
                                     diagnostic=diagnostic)
                except OSError as exc:
                    from .log import log_exception
                    log_exception("transcriptTailFileSkip", exc)
                    _tail_log("full_file_io", backend=backend, size=before.size)
                except Exception:
                    _tail_log("tail_failed", backend=backend, size=before.size)
                    raise
                else:
                    if tail is not None:
                        return tail
            if not tail_enabled:
                if not native_parse:
                    _tail_log("full_custom_parser", backend=backend)
                elif implementation is None or backend not in SUPPORTED_BACKENDS:
                    _tail_log("full_backend_unsupported", backend=backend)
                else:
                    _tail_log("full_backend_tail_disabled", backend=backend)
            admission.full_started = True
            parse_path = path
            if (tail_enabled and before is not None
                    and getattr(implementation, "supports_frozen_transcript_prefix", False)):
                parse_path = _RangePath(path, start=0, end=before.size)
                parse_path._full_read = {"hash": hashlib.blake2b(digest_size=16),
                                         "bytes": 0, "opens": 0, "file": None}
                if bool(conf.get("full_digest_while_parsing")):
                    # The full import also wants the digest of the bytes an
                    # earlier import read; say where they end before parsing.
                    earlier = _imported_before(diagnostic.get("imported"), before, agent_id,
                                               backend_session_id, path)
                    parse_path._full_read["mark"] = earlier[0] if earlier else None
            cpu_started = _thread_time()
            turns = parse(parse_path)
            parsed_evidence = None
            parsed_read = None
            if parse_path is not path:
                read = parse_path._full_read
                if (read["opens"] == 1 and read["bytes"] == before.size
                        and read["file"] == (before.device, before.inode)):
                    parsed_evidence = (before.size, read["hash"].hexdigest(), *read["file"])
                    parsed_read = read if "mark" in read else None
                else:
                    # Abnormal shrink/replacement/unrecognized parser read:
                    # retain the legacy complete parse/store response semantics.
                    turns = parse(path)
            parsed = time.perf_counter()
            if tail_enabled:
                invalidated = _store_full_with_tail_checkpoint(backend, path, agent_id, backend_session_id,
                                                 turns, before, conf,
                                                 reason_token=diagnostic.get("token"),
                                                 parsed_evidence=parsed_evidence,
                                                 imported=diagnostic.get("imported"),
                                                 parsed_read=parsed_read,
                                                 cpu_started=cpu_started)
                retained = _TAIL_STATES.peek(_tail_key(path, agent_id, backend_session_id))
                admission.retained = (retained is not None
                                      and retained[0] == parser_version(backend)
                                      and retained[1] is not None)
            else:
                invalidated = False
                message_store.store_transcript_turns(
                    agent_id=agent_id, backend_session_id=backend_session_id,
                    source_file=str(path), turns=turns)
            result = {"rows": len(turns), "parse_s": parsed - started,
                      "store_s": time.perf_counter() - parsed, "shadow": skip,
                      "apply_mode": "full", "parsed_bytes": before.size if before else None}
            if invalidated is True:
                result["_clarp_transcript_completion_v1"] = "deferred"
            return result
    _tail_log("full_shadow_admitted", backend=backend)
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
        cpu, wall = _thread_time(), time.perf_counter()
        try:
            return work()
        except Exception as exc:  # noqa: BLE001 - logged; the full import goes on
            from .log import log_exception
            log_exception("transcriptShadowFail", exc, detail=f"{self.agent_id} {step}")
            self.failed = True
            self.verdict = f"error:{step}"
            return None
        finally:
            self.cpu += _thread_time() - cpu
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
            try:
                self.plan = message_writes.plan_transcript_tail(
                    agent_id=self.agent_id, backend_session_id=self.backend_session_id,
                    source_file=str(self.path), turns=tail, start_seq=old.boundary_seq,
                    carry=old.store_carry)
            except message_writes._TailInvalidated as refused:
                # The tail would give up to a full read (R108's shift rules): no plan to compare.
                self.verdict = f"fallback:{getattr(refused, 'what', 'tail_invalidated')}"
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
