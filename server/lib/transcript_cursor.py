"""Transcript-position bookkeeping for the TTS hooks.

`TranscriptStreamer` and `TranscriptWatcher` own `cursor_positions.position`.
The `UserPromptSubmit` hook (`pwa_source_flag.py`) calls `reset_spoken_first_all()`
so the next turn voices its initial chunk.
"""

from __future__ import annotations


class CursorStoreError(RuntimeError):
    """SQLite cursor state could not be read or persisted."""

    def __init__(self, operation: str, session_id: str, cause: BaseException):
        self.operation = operation
        self.session_id = session_id
        self.cause = cause
        super().__init__(f"{operation} failed for transcript cursor {session_id}: {cause}")


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
