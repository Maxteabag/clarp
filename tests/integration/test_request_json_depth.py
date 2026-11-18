"""The body reader bounds how deep a JSON body nests, on every route (issue 360, R73).

A body nested deeper than 512 levels is refused with the reader's 400 for invalid JSON before
json.loads recurses; 512 is read; and RecursionError never leaves the reader, even past the bound."""
from __future__ import annotations

import json

import pytest

from tests.integration.test_attention_questions_http import host, server_module  # noqa: F401
from tests.integration.test_render_spec_http import _post_raw


def _decision(depth):
    body = {"session": "theo", "title": "Layout", "question": "Which layout?", "response_type": "free_text",
            "x": "NESTED"}
    return json.dumps(body).replace('"NESTED"', "[" * (depth - 1) + "]" * (depth - 1)).encode()


@pytest.mark.parametrize("depth,refused", [(512, False), (513, True), (10_000, True)])
def test_a_body_nested_past_512_is_the_readers_400_on_another_route_too(host, depth, refused):
    code, result = _post_raw(host, "/decisions", _decision(depth))
    if refused:
        assert (code, result) == (400, {"error": "json object required"}), result
    else:
        assert code != 400 or result != {"error": "json object required"}, result


def test_recursion_never_leaves_the_reader_even_past_the_bound(host, monkeypatch):
    monkeypatch.setattr(server_module.json_depth, "too_deep", lambda raw, limit=512: False)
    try:
        code, result = _post_raw(host, "/decisions", _decision(100_000))
    except Exception as exc:  # a dropped connection or a body that is not JSON
        pytest.fail(f"the reader let an error out: {exc!r}")
    assert (code, result) == (400, {"error": "json object required"}), result
