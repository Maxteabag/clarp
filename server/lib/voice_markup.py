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
  [laughing] [sigh] …  bracketed emotion tags that Gemini TTS acts out. Agents
                    write them inside <vox> (hidden like any filler); a bare
                    one inside <speak> is dropped from display too. TTS: kept
                    for Gemini, removed for every other engine at the provider
                    boundary via strip_emotion_tags, which would read them aloud.
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
_SSML_RE = re.compile(r"</?(?:break|speed|volume|emotion)\b[^>]*/?>", re.IGNORECASE)
# Lowercase words in square brackets, not a markdown link or reference:
# [laughing], [short pause]. Kept narrow so [x] checkboxes and [1] citations
# survive.
_EMOTION_TAG_RE = re.compile(r"\[[a-z][a-z' -]{2,30}\](?![(:])")
_SPEAK_REGION_RE = re.compile(r"(<speak\b[^>]*>)(.*?)(</speak>|$)",
                              re.DOTALL | re.IGNORECASE)
_TTS_DROP_SSML_RE = re.compile(r"</?(?:speed|volume|emotion)\b[^>]*/?>", re.IGNORECASE)
# A <team>…</team> block is an agent's broadcast to its teammates — private to
# the team feed. The user never sees or hears it in their 1:1, so it is dropped
# wholesale (inner text and all) from both the display and the spoken paths.
_TEAM_BLOCK_RE = re.compile(r"<team\b[^>]*>.*?</team>", re.DOTALL | re.IGNORECASE)
_INLINE_WS_RE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,.;:!?])")
_VOX_SENTINEL = "\ue000"
_VOX_CONTENT_RE = re.compile(r"<vox\b[^>]*>(.*?)</vox>", re.DOTALL | re.IGNORECASE)
_VOX_EDGE_RE = re.compile(r"^([\s,.;:!?…—–-]*)(.*?)([\s,.;:!?…—–-]*)$", re.DOTALL)
_VOX_RUN_RE = re.compile(rf"{_VOX_SENTINEL}(?:[ \t]*[,;:—–]?[ \t]*{_VOX_SENTINEL})+")
# What may sit between the start of a line and a filler that opens a sentence:
# list and quote markers, a heading, an opening bold or italic marker.
_LINE_OPENING_RE = re.compile(
    r"[ \t]*(?:(?:[-*+>]|\d+[.)]|#{1,6})[ \t]+)*(?:\*\*|__|\*|_)?[ \t]*")
_SOFT_MARKS = ",;:—–"
_END_MARKS = ".!?…"
_WORD_STOPS = "—–,;:!?…()[]\"“”<"
TTS_CHUNK_MAX_CHARS = 1_800


def _vox_marker(match: re.Match[str]) -> str:
    # Punctuation written inside the filler ("<vox>um,</vox>") belongs to the
    # sentence around it, so it moves outside the hidden part.
    lead, core, trail = _VOX_EDGE_RE.match(match.group(1)).groups()
    return lead + _VOX_SENTINEL + trail if core else lead + trail


def _capitalize_sentence(rest: str) -> str:
    # Only a plain lowercase word: "iOS", "npm" in backticks, file.py stay as written.
    end = 0
    while end < len(rest) and not rest[end].isspace() and rest[end] not in _WORD_STOPS:
        end += 1
    word = rest[:end].rstrip(".'’*_")
    if word and word[0].islower() and all(c.islower() or c in "'’-" for c in word):
        return rest[0].upper() + rest[1:]
    return rest


def _close_vox_gap(s: str, i: int) -> str:
    """Remove the filler marker at `i` with the punctuation that only it
    needed. Mirrored by voice-markup.js, desktop text.rs and iOS
    MarkdownParser; contract/fixtures/voice-display.json is the contract."""
    j = i
    while j > 0 and s[j - 1] in " \t":
        j -= 1
    m = i + 1
    while m < len(s) and s[m] in " \t":
        m += 1
    space = " " if j < i or m > i + 1 else ""
    right = s[m] if m < len(s) else ""
    after = m + 1
    if right and right in _SOFT_MARKS or (right == "-" and s[m + 1:m + 2] in ("", " ", "\t")):
        while after < len(s) and s[after] in " \t":
            after += 1
        kind = "soft"
    elif right and right in _END_MARKS:
        kind = "end-mark"
    elif right in ("", "\n"):
        kind = "line-end"
    else:
        kind = "word"
    left = s[j - 1] if j > 0 else ""
    line_start = s.rfind("\n", 0, i) + 1
    if _LINE_OPENING_RE.fullmatch(s, line_start, i) or (left and left in ".!?"):
        # The filler opened a sentence: its comma or stop goes with it and
        # the next word starts the sentence.
        if kind == "end-mark":
            while after < len(s) and s[after] in _END_MARKS + " \t":
                after += 1
        rest = s[after:] if kind in ("soft", "end-mark") else s[m:]
        sep = " " if j < i and rest[:1] not in ("", "\n") else ""
        return s[:j] + sep + _capitalize_sentence(rest)
    if left and left in _SOFT_MARKS or (left == "-" and s[j - 2:j - 1] in (" ", "\t")):
        if kind in ("end-mark", "line-end"):
            # A comma left dangling before a stop or a line end goes too.
            k = j - 1
            while k > 0 and s[k - 1] in " \t":
                k -= 1
            return s[:k] + s[m:]
        # The mark before the filler is the sentence's own; keep it and
        # drop the filler's second one.
        return s[:j] + " " + (s[after:] if kind == "soft" else s[m:])
    rest = s[after:] if kind == "soft" and right == "," else s[m:]
    sep = "" if rest[:1] in ("", "\n") or rest[0] in ",;:.!?…" else space
    return s[:j] + sep + rest


def _drop_vox_for_display(text: str) -> str:
    s = _VOX_CONTENT_RE.sub(_vox_marker, text)
    if _VOX_SENTINEL not in s:
        return s
    s = _VOX_RUN_RE.sub(_VOX_SENTINEL, s)
    while (i := s.find(_VOX_SENTINEL)) >= 0:
        s = _close_vox_gap(s, i)
    return s


def _drop_spoken_emotion_tags(text: str) -> str:
    if "[" not in text:
        return text
    return _SPEAK_REGION_RE.sub(
        lambda m: m.group(1) + _EMOTION_TAG_RE.sub(" ", m.group(2)) + m.group(3),
        text)


def strip_emotion_tags(text: str | None) -> str:
    """Remove bracketed emotion tags for a TTS engine that would read them
    aloud (everything but Gemini). Each becomes a space, then gaps are tidied."""
    if not text:
        return ""
    if "[" not in text:
        return text
    s = _EMOTION_TAG_RE.sub(" ", text)
    s = _INLINE_WS_RE.sub(" ", s).strip()
    s = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", s)
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", s)


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
    s = _drop_spoken_emotion_tags(s)    # bare [laughing] inside <speak>
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
