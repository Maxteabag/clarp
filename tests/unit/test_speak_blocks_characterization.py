"""S7 step 1d, G0: how the two voice-owned <speak> extractors read a reply
today, pinned before they share voice_markup.extract_speak_blocks. Their
behaviour differs and stays different here: transcript_streamer takes only a
bare <speak> tag, message_audio also one with attributes. Making them alike
is VP-12 (a fix), not this refactor."""
from __future__ import annotations

import pytest

from lib.voice_markup import extract_speak_blocks

CASES = {
    "plain": "Hello <speak>one</speak> bye",
    "two blocks": "<speak>one</speak> middle <speak>two</speak>",
    "multiline": "<speak>line one\nline two</speak>",
    "uppercase tag": "<SPEAK>loud</SPEAK>",
    "mixed case": "<Speak>mixed</sPeAk>",
    "attributes": '<speak lang="en">with attr</speak>',
    "space before close": "<speak >spaced</speak>",
    "unclosed": "<speak>never closed",
    "speaker tag": "<speaker>not speech</speaker>",
    "nested": "<speak>a <speak>b</speak> c</speak>",
    "empty": "<speak></speak>",
    "padded": "<speak>  padded  </speak>",
    "self closing": "<speak/> nothing",
    "attributes and plain": '<speak voice="x">first</speak><speak>second</speak>',
}

# What each extractor returns today (the inner text, unstripped).
STREAMER = {
    "plain": ["one"], "two blocks": ["one", "two"], "multiline": ["line one\nline two"],
    "uppercase tag": ["loud"], "mixed case": ["mixed"], "attributes": [],
    "space before close": [], "unclosed": [], "speaker tag": [], "nested": ["a <speak>b"],
    "empty": [""], "padded": ["  padded  "], "self closing": [],
    "attributes and plain": ["second"],
}
MESSAGE_AUDIO = {
    **STREAMER,
    "attributes": ["with attr"], "space before close": ["spaced"],
    "attributes and plain": ["first", "second"],
}


# The mode each reader passes (transcript_streamer._on_text, message_audio).
def streamer_blocks(text):
    return extract_speak_blocks(text, attributes=False)


def message_audio_blocks(text):
    return extract_speak_blocks(text, attributes=True)


@pytest.mark.parametrize("name", sorted(CASES))
def test_transcript_streamer_reads_bare_speak_blocks(name):
    assert streamer_blocks(CASES[name]) == STREAMER[name]


@pytest.mark.parametrize("name", sorted(CASES))
def test_message_audio_reads_speak_blocks_with_or_without_attributes(name):
    assert message_audio_blocks(CASES[name]) == MESSAGE_AUDIO[name]
