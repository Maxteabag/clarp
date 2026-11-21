"""How a transcript range is read and parsed, and the parser carry a tail resumes from.

Part of what a checkpoint is valid for: this module is a parser source, hashed
by ``transcript_incremental.parser_version`` (``_PARSE_COMMON``), so a change to
it refuses every checkpoint once and the next import of each transcript is
full, as a change to a parser must. It lived in transcript_incremental.py,
which is a store source and changes often; moved here so that holds.

Nothing here touches the database.
"""
from __future__ import annotations

import hashlib
import io
import os
import pathlib
from typing import Any


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
        record = getattr(self, "_full_read", None)
        if record is None:
            raw = _Range(pathlib.Path(self), self.range_start, self.range_end)
        else:
            class ObservedRange(_Range):
                def readinto(self, buffer):
                    count = super().readinto(buffer)
                    if count:
                        view = memoryview(buffer)[:count]
                        mark = record.get("mark")
                        if mark is not None and record["bytes"] < mark <= record["bytes"] + count:
                            # The digest of the first ``mark`` bytes, on the way past.
                            cut = mark - record["bytes"]
                            record["hash"].update(view[:cut])
                            record["at_mark"] = record["hash"].hexdigest()
                            view = view[cut:]
                        record["hash"].update(view)
                        record["bytes"] += count
                    return count

            raw = ObservedRange(pathlib.Path(self), self.range_start, self.range_end)
            try:
                stat = os.fstat(raw._file.fileno())
                record["file"] = (stat.st_dev, stat.st_ino)
                record["stat"] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                record["opens"] += 1
                if record.get("mark") == 0:
                    record["at_mark"] = record["hash"].hexdigest()      # of no bytes
            except BaseException:
                raw.close()
                raise
        return io.TextIOWrapper(io.BufferedReader(raw), encoding=encoding,
                                errors=errors, newline=newline)


def parse_range(backend: str, path: pathlib.Path, start: int, end: int,
                *, observed: dict | None = None) -> list[dict]:
    """``observed``, when given, receives the digest of the bytes the parser
    was handed, as ``range_hash`` would take it: "hash" when the parser
    opened the range once and read all of it, else None. With "mark" in it
    (a count of bytes from ``start``) it also receives "at_mark", the digest
    of the range's first ``mark`` bytes taken on the way past, as
    ``range_hash(path, start, start + mark)`` would take it, and "opened",
    the file as the reader found it when it opened it: (device, inode, size,
    mtime_ns, ctime_ns). Both None with "hash"."""
    from . import backends
    ranged = _RangePath(path, start=start, end=end)
    if observed is not None:
        ranged._full_read = {"hash": hashlib.blake2b(digest_size=16), "bytes": 0, "opens": 0,
                             "file": None}
        if "mark" in observed:
            ranged._full_read["mark"] = observed["mark"]
    turns = backends.parse_turns(backend, ranged)
    if observed is not None:
        read = ranged._full_read
        whole = read["opens"] == 1 and read["bytes"] == max(0, end - start)
        observed["hash"] = read["hash"].hexdigest() if whole else None
        if "mark" in observed:
            observed["at_mark"] = read.get("at_mark") if whole else None
            observed["opened"] = (*read["file"], *read["stat"]) if whole else None
    return turns


# ---- the parser carry ------------------------------------------------------
# What the reference scans (transcript_incremental) need to know about the
# turns before the boundary.

def _claude_children(path: pathlib.Path, keys) -> list[list]:
    """The child transcript each sub-agent lookup key finds now, or None.

    A Claude sub-agent cell reads one thing outside the parent's bytes: the
    ``<parent>/subagents`` directory, looked up by the Agent call's tool_use
    id and then by agent id (claude_transcript._finish_subagent_cell)."""
    from .claude_transcript import _subagent_transcripts
    found = _subagent_transcripts(pathlib.Path(path))
    return [[key, str(found[key]) if key in found else None] for key in sorted(keys)]


def _claude_carry(prefix: list[dict], boundary_record: dict,
                  path: pathlib.Path | None = None) -> dict[str, Any]:
    pending, subagents, cells = [], [], []
    for turn in prefix:
        for tool in turn.get("tools") or []:
            if tool.get("status") not in ("ok", "error"):
                pending.append(str(tool.get("id") or ""))
        for cell in turn.get("display_cells") or []:
            if cell.get("kind") == "subagents":
                subagents.append(str(cell.get("id") or ""))
                cells.append(cell)
    carry = {"pending_tool_ids": pending, "subagent_ids": subagents}
    if cells and path is not None:
        # What the directory answered for each old cell, so that a tail is
        # refused once a child appears, goes or is bound to another call
        # (the parent's bytes do not change when that happens). None when
        # the directory already answers differently from what the parse saw.
        children = _claude_children(path, {str(key) for cell in cells
                                           for key in (cell.get("id"), cell.get("agent_id")) if key})
        found = dict(children)
        if any((found.get(str(cell.get("id") or "")) or found.get(str(cell.get("agent_id") or ""))
                or "") != (cell.get("transcript_path") or "") for cell in cells):
            children = None
        carry["subagent_children"] = children
    return carry


def _agy_carry(prefix: list[dict], boundary_record: dict,
               path: pathlib.Path | None = None) -> dict[str, Any]:
    step = boundary_record.get("step_index")
    return {"boundary_step": step if isinstance(step, int) else 0,
            "running_tools": sum(1 for turn in prefix for tool in turn.get("tools") or []
                                 if tool.get("status") == "running")}


_PARSER_CARRIES = {"claude": _claude_carry, "agy": _agy_carry}


def parser_carry(backend: str, prefix: list[dict], boundary_record: dict,
                 path: pathlib.Path | None = None) -> dict[str, Any]:
    carry = _PARSER_CARRIES.get(backend)
    return carry(prefix, boundary_record, path) if carry else {}
