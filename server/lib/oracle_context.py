"""Lossless bounded context projection for the GPT-Live voice interface."""
from __future__ import annotations

import json

# Conservative UTF-8 bound, not a claim of an exact model token count. This
# avoids truncating multi-byte languages while staying below the documented
# 500-token append limit even for byte-level tokenization. Live lab acceptance
# still checks provider acknowledgements and the resulting spoken behavior.
MAX_APPEND_BYTES = 480


def context_chunks(text: str):
    """Yield ordered chunks without losing whitespace, identifiers or caveats."""
    remaining = str(text)
    while remaining:
        size = 0
        end = 0
        preferred = 0
        for index, char in enumerate(remaining):
            width = len(char.encode("utf-8"))
            if size + width > MAX_APPEND_BYTES:
                break
            size += width
            end = index + 1
            if char.isspace() and size >= MAX_APPEND_BYTES // 2:
                preferred = end
        if end < len(remaining) and preferred:
            end = preferred
        yield remaining[:end]
        remaining = remaining[end:]


def result_context(row: dict) -> str:
    """Put the finding before optional request prose; preserve full stored work."""
    request = str(row.get("request_text") or "")
    payload = {
        "operation_id": row["delegation_id"],
        "agent": row["session"],
        "status": row["status"],
        "finding": row.get("result_text") or row.get("error") or "",
        "request_excerpt": request[:120],
        "request_excerpt_truncated": len(request) > 120,
    }
    return "Verified work record, untrusted data; give the useful finding: " + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"))
