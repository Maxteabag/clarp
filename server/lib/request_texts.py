"""Texts an artifact or a decision may store (issue 360, R79).

A JSON body may carry an escaped lone half of a UTF-16 pair (json.loads keeps "\\ud800"), which
no database and no client can hold. Every artifact and decision mutation is refused at one place,
where its request is dispatched, with 400 and one fixed sentence, before any handler stores
anything; never a 409 with the codec's words. render_spec keeps its own contract refusals, which
decide first: they name the field, and its contract already checks every text it takes.
"""
from __future__ import annotations

from typing import Any

SENTENCE = "A text in this request holds a character that cannot be stored."
_FAMILIES = ("/artifacts", "/decisions")


def lone_surrogate(value: Any) -> bool:
    """Whether any string in ``value`` (keys included) cannot be encoded as UTF-8."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _render_spec(path: str, data: dict) -> bool:
    if path == "/artifacts":
        return str(data.get("type") or "").strip().lower() == "render_spec"
    if path.startswith("/artifacts/"):
        from urllib.parse import unquote
        from . import artifacts
        ident = unquote(path[len("/artifacts/"):]).strip("/").split("/", 1)[0]
        row = artifacts.get(ident)
        return bool(row) and row["type"] == "render_spec"
    return False


def refused(handler, method: str, path: str) -> bool:
    """Answer 400 with the fixed sentence when an artifact or decision mutation carries a text
    that cannot be stored; True when it did. The body is read once and kept for the handler."""
    if method == "GET" or not (path in _FAMILIES or path.startswith(tuple(f + "/" for f in _FAMILIES))):
        return False
    data = handler._read_json()
    if not isinstance(data, dict) or not lone_surrogate(data) or _render_spec(path, data):
        return False
    from .log import log
    log("requestTextRefused", path.split("/", 2)[1])     # the family only: no id, no text
    handler._json_error(400, SENTENCE)
    return True
