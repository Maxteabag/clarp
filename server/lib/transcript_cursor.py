"""Transcript-position bookkeeping for the TTS hooks.

The Stop hook and PostToolUse hook both need to know "what assistant text has
appeared since the last time I ran for this session?". They share one SQLite
cursor row and a filesystem flock to serialise concurrent fires.

Bugs this module pins (see TESTS.md):
- B9: on the first run for a brand-new session, the cursor must silently
  jump to end-of-file (no replaying history).
- B11: concurrent hook processes must not double-speak the same text.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class CursorAdvance:
    """Result of advancing the cursor.

    `texts` are the assistant text chunks that appeared since the previous
    position (in order). `first_run` is True when no position was recorded
    yet — callers typically skip speaking in that case.
    """
    texts: list[str]
    first_run: bool
    new_position: int


class CursorStoreError(RuntimeError):
    """SQLite cursor state could not be read or persisted."""

    def __init__(self, operation: str, session_id: str, cause: BaseException):
        self.operation = operation
        self.session_id = session_id
        self.cause = cause
        super().__init__(f"{operation} failed for transcript cursor {session_id}: {cause}")


class TranscriptCursor:
    """Per-session position tracker for assistant text in a JSONL transcript."""

    def __init__(self, position_dir: pathlib.Path, session_id: str):
        self.position_dir = position_dir
        self.session_id = session_id
        self.position_dir.mkdir(parents=True, exist_ok=True)

    @property
    def lock_file(self) -> pathlib.Path:
        return self.position_dir / f"{self.session_id}.lock"

    @contextmanager
    def locked(self):
        """Acquire the exclusive lock for this cursor for the duration of
        the `with` block. Both hooks share this lock."""
        fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as e:
                from .log import log_exception
                log_exception("transcriptCursorUnlockFail", e)
            os.close(fd)

    def read_position(self) -> Optional[int]:
        try:
            from . import db
            row = db.conn().execute(
                "SELECT position FROM cursor_positions WHERE backend_session_id = ?",
                (self.session_id,),
            ).fetchone()
            if row:
                return int(row["position"])
        except Exception as e:
            raise CursorStoreError("read_position", self.session_id, e) from e
        return None

    def write_position(self, pos: int) -> None:
        try:
            from . import db
            db.conn().execute(
                """INSERT INTO cursor_positions (backend_session_id, position, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(backend_session_id) DO UPDATE SET
                     position = excluded.position,
                     updated_at = excluded.updated_at""",
                (self.session_id, pos, db.now_ms()),
            )
        except Exception as e:
            raise CursorStoreError("write_position", self.session_id, e) from e

    def mark_spoken_first(self, value: bool = True) -> None:
        """Set the 'first chunk already spoken this turn' flag in the DB."""
        try:
            from . import db
            db.conn().execute(
                """INSERT INTO cursor_positions
                       (backend_session_id, position, spoken_first, updated_at)
                   VALUES (?, 0, ?, ?)
                   ON CONFLICT(backend_session_id) DO UPDATE SET
                     spoken_first = excluded.spoken_first,
                     updated_at = excluded.updated_at""",
                (self.session_id, 1 if value else 0, db.now_ms()),
            )
        except Exception as e:
            raise CursorStoreError("mark_spoken_first", self.session_id, e) from e

    def has_spoken_first(self) -> bool:
        try:
            from . import db
            row = db.conn().execute(
                "SELECT spoken_first FROM cursor_positions WHERE backend_session_id = ?",
                (self.session_id,),
            ).fetchone()
            if row:
                return bool(row["spoken_first"])
        except Exception as e:
            raise CursorStoreError("has_spoken_first", self.session_id, e) from e
        return False


    def advance(self, transcript_path: pathlib.Path) -> CursorAdvance:
        """Read any assistant text since the last position; record new size.

        Must be called within `with self.locked():` for concurrent safety.

        First-run semantics: when no position has been recorded yet (a freshly
        spawned agent), DO NOT voice the entire transcript history — but DO
        voice the most recent assistant turn (everything since the latest
        user message). That's almost certainly the reply to the prompt the
        user just submitted; staying silent on it would feel broken.
        """
        try:
            cur_size = os.path.getsize(transcript_path)
        except OSError:
            return CursorAdvance(texts=[], first_run=False, new_position=0)

        last_pos = self.read_position()
        if last_pos is None:
            texts = _current_turn_assistant_text(transcript_path)
            self.write_position(cur_size)
            return CursorAdvance(texts=texts, first_run=True, new_position=cur_size)

        if last_pos >= cur_size:
            return CursorAdvance(texts=[], first_run=False, new_position=cur_size)

        texts: list[str] = []
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(last_pos)
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    content = (entry.get("message") or {}).get("content")
                    if isinstance(content, str) and content.strip():
                        texts.append(content)
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                t = (c.get("text") or "").strip()
                                if t:
                                    texts.append(t)
        except OSError:
            pass

        self.write_position(cur_size)
        return CursorAdvance(texts=texts, first_run=False, new_position=cur_size)


def reset_spoken_first_all() -> int:
    """Clear the 'spoken_first' flag for every session — called by the
    UserPromptSubmit hook so the next turn voices its initial chunk.

    Returns the number of rows touched.
    """
    try:
        from . import db
        cur = db.conn().execute(
            "UPDATE cursor_positions SET spoken_first = 0, updated_at = ?",
            (db.now_ms(),),
        )
        return int(cur.rowcount or 0)
    except Exception as e:
        raise CursorStoreError("reset_spoken_first_all", "*", e) from e


def _current_turn_assistant_text(transcript_path: pathlib.Path) -> list[str]:
    """Return every assistant text chunk that appears AFTER the last user
    entry in the transcript. Used by first-run cursor semantics so a fresh
    agent voices its current reply without dumping the entire backlog."""
    entries: list[dict] = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    last_user_idx = -1
    for i, e in enumerate(entries):
        if e.get("type") == "user":
            last_user_idx = i
    if last_user_idx < 0:
        return []
    texts: list[str] = []
    for e in entries[last_user_idx + 1:]:
        if e.get("type") != "assistant":
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    t = (c.get("text") or "").strip()
                    if t:
                        texts.append(t)
    return texts


def write_assistant_jsonl(
    path: pathlib.Path, entries: Iterable[dict | str]
) -> None:
    """Test helper: append a sequence of entries to a JSONL transcript file.

    String entries are turned into `{type: "assistant", message: {content: STR}}`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for e in entries:
            if isinstance(e, str):
                e = {"type": "assistant", "message": {"content": e}}
            f.write(json.dumps(e) + "\n")
