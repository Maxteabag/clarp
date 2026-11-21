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


class BodyValueNotText(ValueError):
    """A JSON body value that a route reads as text is a number, a list, an object or true (R146)."""

    def __init__(self, key: str):
        super().__init__(f"{key} must be text")
        self.key = key


def body_text(data: dict, key: str, *, strip: bool = True) -> str:
    """The text ``data[key]`` holds, "" when it is absent or empty. A value of another JSON type
    raises BodyValueNotText, which the route dispatch answers 400 '<key> must be text': before,
    such a value reached a string method and the caller got no answer, only a traceback in the
    Host's log (R146: 'a JSON value of the wrong type in a body reaches a string method')."""
    value = data.get(key)
    if not value:
        return ""
    if not isinstance(value, str):
        raise BodyValueNotText(str(key))
    return value.strip() if strip else value


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


def refused(responder, method: str, path: str) -> bool:
    """Answer 400 with the fixed sentence when an artifact or decision mutation carries a text
    that cannot be stored; True when it did. The body is read once and kept for the handler."""
    if method == "GET" or not (path in _FAMILIES or path.startswith(tuple(f + "/" for f in _FAMILIES))):
        return False
    data = responder.read_json()
    if not isinstance(data, dict) or not lone_surrogate(data) or _render_spec(path, data):
        return False
    from .log import log
    log("requestTextRefused", path.split("/", 2)[1])     # the family only: no id, no text
    responder.send_error(400, SENTENCE)
    return True
