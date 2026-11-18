"""How deep a JSON text nests, read from its bytes before it is parsed (issue 360, R73).

json.loads recurses once per level, so a body nested some ten thousand levels deep raises
RecursionError. The Host's reader refuses a body nested deeper than MAX_DEPTH levels with its
400 for invalid JSON, the Rust Host's bound, before json.loads sees it.

Cost: a body with no more than MAX_DEPTH opening brackets in all (nearly every request) is
answered by two byte counts. Otherwise the strings are cut out and the brackets summed, all in
C but the sum: 1.5 ms for 4 MiB that is one string, about 20 to 45 ms for 4 MiB of small
arrays or objects, about what json.loads itself takes for such a body.
"""
from __future__ import annotations

from itertools import accumulate

MAX_DEPTH = 512

_NOT_BRACKET = bytes(b for b in range(256) if b not in b"[]{}")
_STEP = [0] * 256
_STEP[ord("[")] = _STEP[ord("{")] = 1
_STEP[ord("]")] = _STEP[ord("}")] = -1


def too_deep(raw: bytes, limit: int = MAX_DEPTH) -> bool:
    """Whether arrays and objects in ``raw`` nest deeper than ``limit``."""
    if raw.count(b"[") + raw.count(b"{") <= limit:
        return False
    # In valid JSON a backslash stands only inside a string: without the escaped backslashes
    # and quotes, every other piece between quotes is outside the strings.
    outside = b"".join(raw.replace(b"\\\\", b"").replace(b'\\"', b"").split(b'"')[::2])
    brackets = outside.translate(None, _NOT_BRACKET)
    return max(accumulate(map(_STEP.__getitem__, brackets)), default=0) > limit
