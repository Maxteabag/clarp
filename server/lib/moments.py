"""One reader of a moment written as ISO 8601 text with an explicit UTC offset (R104).

Countdown publishing and Updates each read ``target_at`` with their own parser, and what
``datetime.fromisoformat`` accepts changed between Python versions: on 3.14 an hour of 24
reads as the next midnight. Both now read it here, where hours run 00 to 23 and 24 is refused
whatever the interpreter accepts.
"""
from __future__ import annotations

import re
from datetime import datetime

# Date, then hours 00-23, minutes and seconds 00-59, optional fraction, then Z or an offset.
_MOMENT = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def read_moment(text: object) -> datetime:
    """The moment ``text`` names, timezone-aware. ValueError when it is not ISO 8601 with
    an offset, has an hour of 24 or any other out-of-range part, or names no real date."""
    if not isinstance(text, str) or not _MOMENT.fullmatch(text):
        raise ValueError("not an ISO 8601 timestamp with offset")
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("offset required")
    return moment
