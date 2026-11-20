"""A controller action is sent where its parsed URL goes, and only if that destination is allowed
(R133); a debounced request's answer is kept under its request id (R132)."""
from __future__ import annotations

import pytest

from lib import config, controller_actions as ca, url_destination

ALLOWED = ("https://allowed.example.invalid",)
HOOKS = ("https://allowed.example.invalid/hooks",)
ACTION = {"id": "a1", "title": "A", "type": "http_post", "body": {"k": "v"}}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(ca, "DEBOUNCE_MS", 0)
    ca._reset_runtime_state()


def _never(url, body, timeout):
    raise AssertionError(f"the sender was called for a refused destination: {url}")


REFUSED = {
    "user information before an allowed host": "https://allowed.example.invalid@other.example.invalid/x",
    "user information on the allowed host": "https://u:p@allowed.example.invalid/x",
    "a dot suffix": "https://allowed.example.invalid.other.example.invalid/x",
    "an encoded dot": "https://allowed.example.invalid%2eother.example.invalid/x",
    "an encoded at sign": "https://allowed.example.invalid%40other.example.invalid/x",
    "a port that is not a number": "https://allowed.example.invalid:abc/x",
    "a port followed by text": "https://allowed.example.invalid:443x/x",
    "another port than the prefix's": "https://allowed.example.invalid:8443/x",
    "an empty host": "https:///x",
    "a port and no host": "https://:443/x",
    "another IPv6 host": "http://[::2]/x",
    "an IPv6 literal that is not one": "http://[::1.example.invalid]/x",
    "another scheme": "ftp://allowed.example.invalid/x",
    "a space": "https://allowed.example.invalid /x",
}


@pytest.mark.parametrize("url", REFUSED.values(), ids=REFUSED.keys())
def test_a_destination_that_is_not_the_allowed_one_is_refused_when_created(url):
    assert ca.sendable_url(url, ALLOWED) is None
    with pytest.raises(ca.ActionError, match="allow"):
        ca.create_action({**ACTION, "url": url}, actor="test", allowed_urls=ALLOWED)


@pytest.mark.parametrize("url", REFUSED.values(), ids=REFUSED.keys())
def test_an_action_stored_before_the_rule_is_checked_again_and_never_sent(url):
    state = ca._load()
    state.setdefault("actions", {})["a1"] = {**ACTION, "enabled": True, "url": url}
    ca._save(state)
    result = ca.run("a1", actor="t", request_id="r1", allowed_urls=ALLOWED, http_post=_never)
    assert result["ok"] is False and "allow" in result["error"]


@pytest.mark.parametrize("url,sent", [
    ("https://allowed.example.invalid/x", "https://allowed.example.invalid/x"),
    ("https://allowed.example.invalid:443/x", "https://allowed.example.invalid/x"),
    ("HTTPS://ALLOWED.EXAMPLE.INVALID/x", "https://allowed.example.invalid/x"),  # the same host
    ("https://allowed.example.invalid/x?q=1#frag", "https://allowed.example.invalid/x?q=1"),
    ("http://127.0.0.1:8765/api", "http://127.0.0.1:8765/api"),    # the loopback defaults: any port
    ("http://localhost/x", "http://localhost/x"),
    ("http://[::1]:9/x", "http://[::1]:9/x"),
])
def test_an_allowed_destination_is_sent_as_parsed(url, sent):
    ca.create_action({**ACTION, "url": url}, actor="test", allowed_urls=ALLOWED)
    calls = []
    result = ca.run("a1", actor="t", request_id="r1", allowed_urls=ALLOWED,
                    http_post=lambda target, body, timeout: (calls.append(target), (200, ""))[1])
    assert result["ok"] is True and calls == [sent]


@pytest.mark.parametrize("url,admitted", [
    ("https://allowed.example.invalid/hooks", True),
    ("https://allowed.example.invalid/hooks/a", True),
    ("https://allowed.example.invalid/hooksx", False),
    ("https://allowed.example.invalid/", False),
])
def test_a_prefix_path_is_matched_on_a_path_boundary(url, admitted):
    assert (ca.sendable_url(url, HOOKS) is not None) is admitted


@pytest.mark.parametrize("text", [
    "https://u@allowed.example.invalid", "https://allowed.example.invalid?q=1",
    "https://allowed.example.invalid#f", "https://allowed.example.invalid:abc", "not a url", "https://",
])
def test_a_prefix_that_does_not_parse_plainly_is_refused_at_configuration(text, monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append(event))
    assert url_destination.prefix(text) is None
    assert config._controller_action_urls([text, "https://allowed.example.invalid"]) == (
        "https://allowed.example.invalid",)
    assert lines == ["config.controller_action_urls"]
    assert ca.sendable_url("https://allowed.example.invalid/x", (text,)) is None   # admits nothing


def test_a_debounced_request_repeated_after_the_window_is_its_first_answer(monkeypatch):
    """R132: A runs; B inside the window is answered debounced; B repeated after the window is
    that answer again with duplicate: true, and nothing runs a second time."""
    monkeypatch.setattr(ca, "DEBOUNCE_MS", 60_000)
    ca.create_action({**ACTION, "url": "http://127.0.0.1:8765/api"}, actor="test")
    calls = []

    def post(url, body, timeout):
        calls.append(url)
        return 200, ""
    first = ca.run("a1", actor="t", request_id="A", http_post=post)
    debounced = ca.run("a1", actor="t", request_id="B", http_post=post)
    monkeypatch.setattr(ca, "DEBOUNCE_MS", 0)                              # the window has passed
    again = ca.run("a1", actor="t", request_id="B", http_post=post)
    assert len(calls) == 1 and first["ok"] is True and debounced["debounced"] is True
    assert again == {**debounced, "duplicate": True}


@pytest.mark.parametrize("url", [
    "https://allowed.example.invalid%2eother.example.invalid/x",   # an encoded dot
    "https://allowed%40other.example.invalid/x",                     # an encoded at sign
    "https://allowed_example.invalid/x",                             # a character no host name has
    "https://allowed.example.invalid./x",                            # a trailing dot
    "https://-allowed.example.invalid/x",                            # a label that starts with a hyphen
])
def test_the_parser_gives_no_destination_for_a_host_that_is_not_a_plain_name(url):
    """Defence in depth under the comparison: such a host never equals an allowed one, and the
    parser does not give it a destination at all."""
    assert url_destination.destination(url) is None
