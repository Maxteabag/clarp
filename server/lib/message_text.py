"""Canonical injected-context delimiters and pure message text normalization."""
from __future__ import annotations

from .voice_markup import clean_for_display


def _strip_voice_markup(text: str | None) -> str:
    """Normalized, markup-free single-line form of a reply — used to compare a
    streamed turn (markup intact) against its durable copy when superseding the
    live row. Delegates to the canonical cleaner so the rules never drift."""
    return clean_for_display(text, oneline=True)


# The server appends a team-context block to the prompt the backend sees (see
# turn_dispatch._with_team_context). The backend's transcript copies the whole
# augmented prompt back, so on import we must strip the block from user turns —
# otherwise it leaks into the chat as if User typed it. Marker shared with
# turn_dispatch.
TEAM_CONTEXT_OPEN = "--- Clarp team context ---"
TEAM_CONTEXT_CLOSE = "--- End Clarp team context ---"
FALLBACK_CONTEXT_OPEN = "--- Clarp fallback context ---"
FALLBACK_CONTEXT_CLOSE = "--- End Clarp fallback context ---"
# The whole input of a listen-first initiative turn (group_calls.opening_header):
# imported, it is skipped, never a user row.
CALL_OPENING_OPEN = "<clarp-call-opening>"
CALL_OPENING_CLOSE = "</clarp-call-opening>"
# Clarp's record of a conversation the backend lost, opening its continuation.
RESTORED_CONTEXT_OPEN = "--- Clarp restored conversation ---"
RESTORED_CONTEXT_CLOSE = "--- End Clarp restored conversation ---"
# The id of a user row the client authored starts with this
# (message_import_common._client_message_id); transcript ids never do.
CLIENT_ROW_PREFIX = "u-"
# What a barge-in puts in front of the user's words in the prompt
# (turn_command._with_delivery_context): the replies they did not hear.
DELIVERY_CONTEXT_OPEN = "<clarp-delivery-context>"
DELIVERY_CONTEXT_CLOSE = "</clarp-delivery-context>"


def strip_injected_team_context(text: str) -> str:
    """Drop server-injected team-context blocks from a user turn.

    Older turns appended the block after the user's text; newer turns prepend it
    for prompt-cache friendliness. Preserve the real user text in either order.
    """
    if not text or TEAM_CONTEXT_OPEN not in text:
        return text
    out = text
    while TEAM_CONTEXT_OPEN in out:
        before, rest = out.split(TEAM_CONTEXT_OPEN, 1)
        if TEAM_CONTEXT_CLOSE not in rest:
            return before.rstrip()
        _, after = rest.split(TEAM_CONTEXT_CLOSE, 1)
        out = (before + after).strip()
    return out


def strip_injected_blocks(text: str) -> str:
    """Drop the server-injected blocks around a user turn: all but a delivery
    notice in front of the words, which the caller judges.

    A block appended without delimiters was imported as part of the user's own
    words, so the chat showed the user's message twice: once as they typed it
    and once several kilobytes long with the injection attached.
    """
    # The restored record is the outermost block and may quote the others
    # (an old reply about Clarp's own team context), so it goes first.
    text = _strip_block(text, RESTORED_CONTEXT_OPEN, RESTORED_CONTEXT_CLOSE)
    return _strip_block(strip_injected_team_context(text),
                        FALLBACK_CONTEXT_OPEN, FALLBACK_CONTEXT_CLOSE)


def strip_injected_context(text: str) -> str:
    """Drop every server-injected block from a user turn."""
    # The delivery notice sits directly in front of the words, inside the
    # other blocks: last, when they are gone.
    return strip_delivery_context(strip_injected_blocks(text))


def strip_delivery_context(text: str) -> str:
    """Drop the delivery notice a barge-in put in front of a user turn.

    The provider's transcript copies the whole prompt back. Imported with the
    notice, the copy no longer matched the row that holds the user's own
    words, and the chat showed those words and then the notice as a second
    message (issue 284). Only a notice at the very start is the server's: a
    message that mentions the tag further in is the user's text. So is a turn
    that is nothing but the notice: the Host never sends it without words
    behind it, and nothing is ever turned into an empty message.

    A user can also begin their own message with the notice (pasting it into
    a report about it). This function cannot tell; who calls it does: the
    import leaves a copy alone that matches the user's own row as it stands,
    and a row the client stored is never passed here for display."""
    if not text:
        return text
    body = text.lstrip()
    if not body.startswith(DELIVERY_CONTEXT_OPEN):
        return text
    # Nothing behind the closing tag, or no closing tag: not the Host's notice.
    return body.partition(DELIVERY_CONTEXT_CLOSE)[2].strip() or text


def _strip_block(text: str, opening: str, closing: str) -> str:
    if not text or opening not in text:
        return text
    out = text
    while opening in out:
        before, rest = out.split(opening, 1)
        if closing not in rest:
            return before.rstrip()
        _, after = rest.split(closing, 1)
        out = (before + after).strip()
    return out
