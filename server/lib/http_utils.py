"""Small HTTP helpers shared by server handlers and tests."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SECRET_QUERY_KEYS = {"token", "auth", "access_token"}


def redact_query_secrets(path: str) -> str:
    """Return `path` with sensitive query values replaced by REDACTED."""
    parts = urlsplit(path)
    if not parts.query:
        return path
    pairs = []
    changed = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SECRET_QUERY_KEYS:
            pairs.append((key, "REDACTED"))
            changed = True
        else:
            pairs.append((key, value))
    if not changed:
        return path
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(pairs, doseq=True), parts.fragment))
