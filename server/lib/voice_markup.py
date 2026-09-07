"""Single source of truth for voice-channel markup normalization.

Agents write spoken replies with markup that must NEVER reach a screen but is
either spoken or honoured by the TTS engine. Historically each surface (chat
preview, push notification, live-row compare, both clients) stripped it with its
own ad-hoc regexes, which drifted and leaked markup (e.g. push bodies showing
`<break .../>`). Everything server-side now funnels through here; the PWA
(`web/src/lib/render.js`) and native (`MarkdownParser.swift`) mirror
`clean_for_display`.

Markup vocabulary:
  <speak>…</speak>  voice-channel gate — markers removed, inner text kept.
  <vox>…</vox>      audio-only fillers (um/uh/like) — display: dropped entirely;
                    TTS: unwrapped so the words are spoken.
  <break/>          display: dropped; TTS: kept ONLY for engines that parse
                    SSML (Cartesia, ElevenLabs). Deepgram and custom adapters
                    that have not declared `ssml: true` in their manifest get
                    it stripped at the provider boundary via
                    strip_ssml_for_plain_tts, otherwise the tag is read aloud.
  <speed/> <volume/> <emotion/>  display + TTS: dropped; Cartesia does not
                    reliably honour them and leaked tags are worse than no tag.
"""
from __future__ import annotations

import re

# The authoritative server-side list of internal transport blocks. These are
# metadata, never conversation, so they are removed before persistence, API
# responses, previews, notifications, or TTS. Add new tags here only.
HIDDEN_BLOCK_TAGS = ("oai-mem-citation", "environment_context")
_HIDDEN_TAG_PATTERN = "|".join(re.escape(tag) for tag in HIDDEN_BLOCK_TAGS)
_HIDDEN_BLOCK_RE = re.compile(
    rf"<(?P<tag>{_HIDDEN_TAG_PATTERN})\b[^>]*>.*?</(?P=tag)>",
    re.DOTALL | re.IGNORECASE,
)
_HIDDEN_OPEN_TAIL_RE = re.compile(
    rf"<(?:{_HIDDEN_TAG_PATTERN})\b[^>]*>.*$", re.DOTALL | re.IGNORECASE
)

_SPEAK_TAG_RE = re.compile(r"</?speak\b[^>]*>", re.IGNORECASE)
_VOX_TAG_RE = re.compile(r"</?vox\b[^>]*>", re.IGNORECASE)
_VOX_BLOCK_RE = re.compile(r"<vox\b[^>]*>.*?</vox>", re.DOTALL | re.IGNORECASE)
_SSML_RE = re.compile(r"</?(?:break|speed|volume|emotion)\b[^>]*/?>", re.IGNORECASE)
_TTS_DROP_SSML_RE = re.compile(r"</?(?:speed|volume|emotion)\b[^>]*/?>", re.IGNORECASE)
# A <team>…</team> block is an agent's broadcast to its teammates — private to
# the team feed. The user never sees or hears it in their 1:1, so it is dropped
# wholesale (inner text and all) from both the display and the spoken paths.
_TEAM_BLOCK_RE = re.compile(r"<team\b[^>]*>.*?</team>", re.DOTALL | re.IGNORECASE)
_INLINE_WS_RE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,.;:!?])")
_VOX_SENTINEL = "\ue000"
_VOX_BOUNDARY_RE = re.compile(
    rf"[ \t]*[,;:—–-]?[ \t]*{_VOX_SENTINEL}"
    rf"[ \t]*[,.;:!?…—–-]?[ \t]*"
)
_LEADING_VOX_RE = re.compile(
    rf"(^[ \t]*|[.!?][ \t]+|\n[ \t]*){_VOX_SENTINEL}[ \t]*([a-z])"
)
TTS_CHUNK_MAX_CHARS = 1_800


def _drop_vox_for_display(text: str) -> str:
    marked = _VOX_BLOCK_RE.sub(_VOX_SENTINEL, text)
    marked = _VOX_BOUNDARY_RE.sub(f" {_VOX_SENTINEL} ", marked)

    def capitalize_after_boundary(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2).upper()}"

    marked = _LEADING_VOX_RE.sub(capitalize_after_boundary, marked)
    return marked.replace(_VOX_SENTINEL, " ")


def strip_hidden_blocks(text: str | None) -> str:
    """Remove internal metadata blocks, including a streaming open tail."""
    if not text:
        return ""
    return _HIDDEN_OPEN_TAIL_RE.sub("", _HIDDEN_BLOCK_RE.sub("", text))


def clean_for_display(text: str | None, *, oneline: bool = False) -> str:
    """Strip ALL voice markup for anything shown to the user — chat, chat-list
    preview, push-notification body. Drops <vox> fillers wholesale, removes SSML
    tags, and unwraps <speak> markers (keeping their inner text).

    `oneline=True` collapses all whitespace to single spaces (previews / push
    bodies); otherwise newlines are preserved (markdown bodies) and only the
    intra-line gaps left by removed markup are tidied.
    """
    if not text:
        return ""
    s = strip_hidden_blocks(text)
    s = _TEAM_BLOCK_RE.sub("", s)       # team broadcasts: never shown to the user
    s = _SPEAK_TAG_RE.sub("", s)        # <speak> markers: gone, inner text kept
    s = _SSML_RE.sub("", s)             # <break>/<speed>/<volume>/<emotion>: gone
    s = _drop_vox_for_display(s)        # fillers + their conversational punctuation
    if oneline:
        s = " ".join(s.split())
    else:
        s = _INLINE_WS_RE.sub(" ", s).strip()
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", s)


def spoken_for_tts(text: str | None) -> str:
    """The spoken text prepared for the TTS engine: unwrap <vox> fillers (keep
    the words so they're voiced), keep <break> pause tags, and drop speed-like
    tags that Cartesia does not reliably honour. (<speak> extraction happens
    before this.)"""
    if not text:
        return ""
    s = strip_hidden_blocks(text)
    s = _TEAM_BLOCK_RE.sub("", s)       # team broadcasts: never spoken to the user
    s = _VOX_TAG_RE.sub("", s)
    s = _TTS_DROP_SSML_RE.sub("", s)
    s = _INLINE_WS_RE.sub(" ", s).strip()
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", s)


def strip_ssml_for_plain_tts(text: str | None) -> str:
    """Remove every SSML tag for an engine that does not parse SSML.

    Each tag becomes a single space, never the empty string: `one<break/>two`
    must reach the model as "one two", not "onetwo". Applied at the provider
    boundary (custom adapters without `ssml: true`, Deepgram) after the normal
    spoken-text pipeline has already kept <break> for SSML-capable engines.
    """
    if not text:
        return ""
    s = _SSML_RE.sub(" ", text)
    s = " ".join(s.split())
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", s)


def spoken_chunks_for_tts(
    text: str | None,
    *,
    max_chars: int = TTS_CHUNK_MAX_CHARS,
) -> list[str]:
    """Split spoken text without dropping content at provider request limits.

    Deepgram Aura accepts at most 2,000 characters per request. Keeping a small
    margin and splitting first on sentence boundaries makes every provider path
    safe while preserving queue order and natural prosody.
    """
    clean = spoken_for_tts(text)
    if not clean:
        return []
    limit = max(100, int(max_chars))
    protected = re.sub(
        r"<break\b[^>]*?/?>",
        lambda match: match.group(0).replace(" ", "\u00a0"),
        clean,
        flags=re.IGNORECASE,
    )
    sentences = re.split(r"(?<=[.!?…])\s+", protected)
    chunks: list[str] = []
    current = ""

    def append_piece(piece: str) -> None:
        nonlocal current
        piece = piece.strip()
        if not piece:
            return
        candidate = f"{current} {piece}".strip()
        if current and len(candidate) > limit:
            chunks.append(current)
            current = piece
        else:
            current = candidate

    for sentence in sentences:
        remaining = sentence.strip()
        while len(remaining) > limit:
            split_at = remaining.rfind(" ", 0, limit + 1)
            if split_at <= 0:
                split_at = limit
            append_piece(remaining[:split_at])
            if current:
                chunks.append(current)
                current = ""
            remaining = remaining[split_at:].strip()
        append_piece(remaining)
    if current:
        chunks.append(current)
    return [chunk.replace("\u00a0", " ") for chunk in chunks]
