"""How deep a JSON text nests, read from its bytes before it is parsed (issue 360, R73).

json.loads recurses once per level, so a body nested deep enough raises RecursionError. The
Python Host's reader refuses a body nested deeper than MAX_DEPTH (512) levels with its 400 for
invalid JSON, on every route, before json.loads sees it. 512 is the Rust Host's bound for
render-spec bodies; its other routes parse with serde_json, whose own limit stops at 127 levels
today, a difference recorded on the Rust Host's side.

The count reads the bytes as UTF-8, as every client sends. A body json.loads would decode as
UTF-16 or UTF-32 is counted byte by byte and may be refused at 512 for brackets inside strings.

Cost: a body with no more than MAX_DEPTH opening brackets in all, nearly every request, is
answered by two byte counts. Otherwise the body is read in windows of WINDOW bytes: in each the
escaped backslashes and quotes are dropped, the pieces between quotes outside the strings kept
and their brackets summed, all in C but the sum. Whether a window ends inside a string carries
over to the next, and no window ends inside a run of backslashes, so the answer is the whole
body's while the extra memory stays a few times WINDOW, whatever the body's size. Measured
figures are in the PR that bounded it (#438).
"""
from __future__ import annotations

from itertools import accumulate

MAX_DEPTH = 512

WINDOW = 256 * 1024
_NOT_BRACKET = bytes(b for b in range(256) if b not in b"[]{}")
_STEP = [0] * 256
_STEP[ord("[")] = _STEP[ord("{")] = 1
_STEP[ord("]")] = _STEP[ord("}")] = -1
_BACKSLASH = 0x5C


def too_deep(raw: bytes, limit: int = MAX_DEPTH, window: int = WINDOW) -> bool:
    """Whether arrays and objects in ``raw`` nest deeper than ``limit``."""
    if raw.count(b"[") + raw.count(b"{") <= limit:
        return False
    depth, inside, pos, size = 0, False, 0, len(raw)
    while pos < size:
        end = min(size, pos + window)
        if end < size:
            # Never cut a run of backslashes: an escape is read whole, in one window.
            back = end
            while back > pos and raw[back - 1] == _BACKSLASH:
                back -= 1
            if back > pos:
                end = back
            else:
                while end < size and raw[end] == _BACKSLASH:
                    end += 1
                end = min(size, end + 1)        # and the byte the last backslash escapes
        # In valid JSON a backslash stands only inside a string: without the escaped backslashes
        # and quotes, every other piece between quotes is outside the strings.
        pieces = raw[pos:end].replace(b"\\\\", b"").replace(b'\\"', b"").split(b'"')
        outside = b"".join(pieces[1::2] if inside else pieces[::2])
        if len(pieces) % 2 == 0:
            inside = not inside
        brackets = outside.translate(None, _NOT_BRACKET)
        if brackets:
            if max(accumulate(map(_STEP.__getitem__, brackets), initial=depth)) > limit:
                return True
            depth += (brackets.count(b"[") + brackets.count(b"{")) - (brackets.count(b"]") + brackets.count(b"}"))
        pos = end
    return False
