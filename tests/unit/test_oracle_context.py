"""Characterization tests for lib.oracle_context (bounded Live context chunks).

Pins: chunking is lossless (the chunks concatenate back to the input), every
chunk fits the UTF-8 byte bound, multi-byte characters are never split, a
whitespace boundary is preferred once past half the budget, and the result
context envelope has a fixed key set with the finding first and the request
excerpt bounded at 120 characters.
"""
from __future__ import annotations

import json

import pytest

from lib import oracle_context
from lib.oracle_context import MAX_APPEND_BYTES, context_chunks, result_context


@pytest.mark.parametrize("text", [
    "",
    "short",
    "a" * MAX_APPEND_BYTES,
    "a" * (MAX_APPEND_BYTES + 1),
    "word " * 400,
    "ø" * 600,                      # 2-byte characters
    "日本語のテキスト" * 120,        # 3-byte characters
    "😀" * 300,                     # 4-byte characters
    "x\n\n  y\t z  " * 100,          # whitespace must survive verbatim
])
def test_chunks_are_lossless_and_bounded(text):
    chunks = list(context_chunks(text))
    assert "".join(chunks) == text
    assert all(chunks), "no empty chunk is ever yielded"
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= MAX_APPEND_BYTES


def test_empty_input_yields_nothing():
    assert list(context_chunks("")) == []


def test_input_within_budget_is_one_chunk():
    text = "a" * MAX_APPEND_BYTES
    assert list(context_chunks(text)) == [text]


def test_one_byte_over_budget_splits_into_two():
    text = "a" * (MAX_APPEND_BYTES + 1)
    assert list(context_chunks(text)) == ["a" * MAX_APPEND_BYTES, "a"]


def test_prefers_whitespace_split_after_half_budget():
    # 300 letters, a space, then 300 more: the cut lands right after the
    # space (which sits past MAX_APPEND_BYTES // 2) rather than at byte 480.
    text = "a" * 300 + " " + "b" * 300
    chunks = list(context_chunks(text))
    assert chunks[0] == "a" * 300 + " "
    assert chunks[1] == "b" * 300


def test_whitespace_before_half_budget_is_not_used_as_split():
    # The only space is at position 10 (< 240), so a hard split at the byte
    # bound is used instead of dragging the first chunk down to 11 chars.
    text = "a" * 10 + " " + "b" * 600
    chunks = list(context_chunks(text))
    assert len(chunks[0]) == MAX_APPEND_BYTES


def test_multibyte_characters_never_straddle_a_chunk():
    # 3-byte chars: 480 is divisible by 3, so use 4-byte emoji where 480 % 4
    # == 0 too; mix widths so the boundary falls mid-character somewhere.
    text = "aé" * 500
    for chunk in context_chunks(text):
        chunk.encode("utf-8")  # would raise if a surrogate half slipped in
        assert len(chunk.encode("utf-8")) <= MAX_APPEND_BYTES


def test_non_string_input_is_coerced():
    assert list(context_chunks(12345)) == ["12345"]


def _row(**overrides):
    row = {"delegation_id": "op-1", "session": "theo", "status": "done",
           "result_text": "The build passes.", "request_text": "check the build"}
    row.update(overrides)
    return row


def test_result_context_prefix_and_payload_shape():
    text = result_context(_row())
    prefix, _, encoded = text.partition("aloud: ")
    assert prefix.startswith("Work record, untrusted data")
    payload = json.loads(encoded)
    assert list(payload) == ["operation_id", "agent", "status", "finding",
                             "request_excerpt", "request_excerpt_truncated"]
    assert payload == {"operation_id": "op-1", "agent": "theo",
                       "status": "done", "finding": "The build passes.",
                       "request_excerpt": "check the build",
                       "request_excerpt_truncated": False}


def test_result_context_falls_back_to_error_then_empty():
    assert json.loads(result_context(_row(result_text="", error="boom"))
                      .split("aloud: ", 1)[1])["finding"] == "boom"
    assert json.loads(result_context(_row(result_text=None, error=None))
                      .split("aloud: ", 1)[1])["finding"] == ""


def test_result_context_truncates_request_at_120_but_keeps_full_finding():
    request = "r" * 200
    finding = "f" * 5000
    payload = json.loads(result_context(
        _row(request_text=request, result_text=finding)).split("aloud: ", 1)[1])
    assert payload["request_excerpt"] == "r" * 120
    assert payload["request_excerpt_truncated"] is True
    assert payload["finding"] == finding


def test_result_context_keeps_unicode_and_compact_json():
    text = result_context(_row(result_text="Ferdig – alt OK ✓"))
    assert "Ferdig – alt OK ✓" in text
    assert "\\u" not in text
    encoded = text.split("aloud: ", 1)[1]
    assert ", " not in encoded and ": " not in encoded


def test_result_context_missing_request_text_is_empty_excerpt():
    row = _row()
    del row["request_text"]
    payload = json.loads(result_context(row).split("aloud: ", 1)[1])
    assert payload["request_excerpt"] == ""
    assert payload["request_excerpt_truncated"] is False


def test_max_append_bytes_is_below_documented_token_limit():
    # The module promises to stay under the provider's 500-token append cap
    # even for byte-level tokenization.
    assert 0 < oracle_context.MAX_APPEND_BYTES < 500


def test_context_chunks_respects_a_smaller_budget_losslessly():
    text = "Ordet æ og ✓ " * 300
    chunks = list(context_chunks(text, max_bytes=100))
    assert "".join(chunks) == text
    assert all(len(chunk.encode()) <= 100 for chunk in chunks)
