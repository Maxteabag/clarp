"""A JSON value of the wrong type in a body reaches a string method (R146).

A route read a body value with (data.get(K) or "").strip(): a value that is true, a number, a
list or an object has no .strip(), so the handler raised before it answered, the caller got no
proper answer and the Host logged a traceback. 27 reads in 9 route files had that shape. Each now
reads through request_texts.body_text, and such a value is answered 400 '<key> must be text'; a
remote action that is not text is an unknown action (the Rust Host's answer)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    CLIENT_TIMEOUT_S, _model_catalog_never_probes_installed_clis, fake_ctx, running_server)

NOT_TEXT = [True, 1, 1.5, ["words"], {"words": 1}]


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=CLIENT_TIMEOUT_S) as reply:
            return reply.status, json.loads(reply.read() or b"{}")
    except urllib.error.HTTPError as refused:
        return refused.code, json.loads(refused.read() or b"{}")


@pytest.mark.parametrize("value", NOT_TEXT, ids=repr)
def test_a_remote_action_that_is_not_text_is_an_unknown_action(running_server, value):  # noqa: F811
    base, _ctx, _srv = running_server
    assert _post(base + "/remote-action", {"action": value}) == (400, {"error": "unknown action"})


@pytest.mark.parametrize("value", NOT_TEXT, ids=repr)
@pytest.mark.parametrize("path, key", [("/focus", "session"), ("/agent-llm", "session"),
                                       ("/agent-dreaming", "session")])
def test_a_body_value_that_is_not_text_is_answered_400(running_server, path, key, value):  # noqa: F811
    base, _ctx, _srv = running_server
    status, body = _post(base + path, {key: value})
    assert (status, body.get("error")) == (400, f"{key} must be text")


def test_body_text():
    from lib.request_texts import BodyValueNotText, body_text
    assert body_text({"k": "  words  "}, "k") == "words"
    assert body_text({"k": " a b "}, "k", strip=False) == " a b "
    assert body_text({}, "k") == "" and body_text({"k": None}, "k") == "" and body_text({"k": 0}, "k") == ""
    for value in NOT_TEXT:
        with pytest.raises(BodyValueNotText, match="k must be text"):
            body_text({"k": value}, "k")


def test_no_route_reads_a_body_value_with_a_string_method_unchecked():
    """The guard: the shape is gone from every route file and does not come back."""
    import pathlib
    import re
    routes = pathlib.Path(__file__).resolve().parents[2] / "server" / "lib" / "routes"
    shape = re.compile(r'(?<!str)\((data|body|payload)\.get\([^()]*\) or ""\)\.(strip|split|lower)\(')
    found = [f"{path.name}:{n}" for path in sorted(routes.glob("*.py"))
             for n, line in enumerate(path.read_text().splitlines(), 1) if shape.search(line)]
    assert not found, found
