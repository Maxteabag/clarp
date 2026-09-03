"""The single source of truth for voice-markup normalization. Every user-facing
surface (chat, chat-list preview, push notification) must strip identically; the
TTS path keeps only the audio tags the engine reliably honours."""
from __future__ import annotations

from lib.voice_markup import (
    clean_for_display,
    spoken_chunks_for_tts,
    spoken_for_tts,
    strip_hidden_blocks,
    strip_ssml_for_plain_tts,
)


def test_clean_strips_speak_markers_keeps_inner():
    assert clean_for_display("<speak>hello there</speak>") == "hello there"


def test_clean_drops_team_blocks_wholesale():
    # Team broadcasts are private to the team feed — never shown to the user.
    assert clean_for_display(
        "All done. <team>refactored the parser</team> Cheers."
    ) == "All done. Cheers."


def test_spoken_drops_team_blocks_wholesale():
    # ...and never spoken to the user either.
    assert "parser" not in spoken_for_tts(
        "All done. <team>refactored the parser</team> Cheers.")


def test_clean_drops_vox_fillers_wholesale():
    assert clean_for_display("So <vox>um,</vox> it's done") == "So it's done"


def test_clean_repairs_punctuation_and_capitalization_around_removed_fillers():
    raw = (
        "<speak><vox>Hmm</vox>, okay, let's try this naturally. "
        '<break time="350ms"/> I think it works. '
        "<vox>You know</vox>, the words stay the same.</speak>"
    )

    assert clean_for_display(raw) == (
        "Okay, let's try this naturally. I think it works. "
        "The words stay the same."
    )


def test_clean_repairs_inline_punctuation_around_removed_fillers():
    assert clean_for_display("I, <vox>um</vox>, think so.") == "I think so."


def test_clean_drops_ssml_tags():
    assert clean_for_display('wait <break time="350ms"/> for it') == "wait for it"
    assert clean_for_display('slow <speed ratio="0.85"/>down') == "slow down"
    assert clean_for_display('slow <speed ratio="0.85">down</speed>') == "slow down"


def test_clean_combined_oneline():
    raw = ('<speak>So <vox>um,</vox> <break time="300ms"/>the '
           '<speed ratio="1.1"/>build passed.</speak>')
    assert clean_for_display(raw, oneline=True) == "So the build passed."


def test_clean_preserves_newlines_unless_oneline():
    raw = "line one\n\nline two"
    assert clean_for_display(raw) == "line one\n\nline two"
    assert clean_for_display(raw, oneline=True) == "line one line two"


def test_clean_empty_and_none():
    assert clean_for_display("") == ""
    assert clean_for_display(None) == ""


def test_server_drops_internal_transport_blocks_from_every_output_path():
    for tag in ("oai-mem-citation", "environment_context"):
        raw = f"Visible reply.\n<{tag}><private>metadata</private></{tag}>"
        assert strip_hidden_blocks(raw) == "Visible reply.\n"
        assert clean_for_display(raw) == "Visible reply."
        assert spoken_for_tts(raw) == "Visible reply."


def test_server_hides_streaming_tail_and_requires_matching_close_tag():
    assert strip_hidden_blocks(
        "Visible.\n<oai-mem-citation><citation_entries>partial"
    ) == "Visible.\n"
    raw = (
        "Visible. <environment_context>private </oai-mem-citation>"
        "still private</environment_context>"
    )
    assert clean_for_display(raw) == "Visible."
    assert spoken_for_tts(raw) == "Visible."


def test_spoken_for_tts_unwraps_vox_keeps_breaks_drops_speed():
    raw = '<vox>um</vox> ready <break time="300ms"/><speed ratio="0.9">now</speed>'
    out = spoken_for_tts(raw)
    # <vox> tags gone but the word stays; only pause markup is preserved.
    assert "um ready" in out
    assert '<break time="300ms"/>' in out
    assert "<speed" not in out
    assert "</speed>" not in out
    assert "now" in out
    assert "<vox>" not in out and "</vox>" not in out


def test_strip_ssml_for_plain_tts_replaces_tags_with_a_space():
    # An engine that does not parse SSML would otherwise read the tag aloud
    # ("break time equals 300 milliseconds"); replacing with a space keeps the
    # neighbouring words apart (issue #14).
    assert strip_ssml_for_plain_tts('one<break time="300ms"/>two') == "one two"
    assert strip_ssml_for_plain_tts(
        'ready <break time="350ms"/> now, <speed ratio="0.9">go</speed>'
        '<volume value="+2dB">loud</volume><emotion name="calm"/>'
    ) == "ready now, go loud"
    assert strip_ssml_for_plain_tts("plain words") == "plain words"
    assert strip_ssml_for_plain_tts("") == ""
    assert strip_ssml_for_plain_tts(None) == ""


def test_spoken_for_tts_empty():
    assert spoken_for_tts("") == ""
    assert spoken_for_tts(None) == ""


def test_long_spoken_reply_is_chunked_without_truncation():
    text = " ".join(
        f"Sentence {index} contains enough words to exercise provider-safe splitting."
        for index in range(120)
    )
    chunks = spoken_chunks_for_tts(text, max_chars=500)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 500 for chunk in chunks)
    assert " ".join(chunks) == spoken_for_tts(text)


def test_long_spoken_reply_never_splits_inside_break_markup():
    text = (
        "A" * 75
        + ' <break time="350ms"/> '
        + "the reply continues after the pause without malformed markup."
    )
    chunks = spoken_chunks_for_tts(text, max_chars=100)
    assert all("<break" not in chunk or '<break time="350ms"/>' in chunk
               for chunk in chunks)
    assert " ".join(chunks) == spoken_for_tts(text)
