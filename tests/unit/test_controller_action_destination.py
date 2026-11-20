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


BASE = ("https://allowed.example.invalid/base",)
B = "https://allowed.example.invalid/base/"
# R139: a path read as a server might read it: the encoded dot, slash and backslash decoded once,
# a backslash read as a slash. Refused if a segment is then '.' or '..'.
DOT_SEGMENTS = {
    "a raw '..' under a prefix with a path": B + "../admin",
    "an encoded '..'": B + "%2e%2e/admin",
    "an encoded '..' in upper case": B + "%2E%2E/admin",
    "an encoded '..' in mixed case": B + "%2E%2e/admin",
    "a raw dot beside an encoded one": B + ".%2e/admin",
    "a raw '.'": B + "./admin",
    "an encoded '.'": B + "%2E/admin",
    "a '..' as the last segment": B + "x/..",
    "a '..' between encoded slashes": B + "a%2F..%2Fb",
    "a '..' between encoded slashes in lower case": B + "a%2f..%2fb",
    "a '..' between encoded backslashes": B + "a%5C..%5Cb",
    "an encoded '..' between encoded backslashes": B + "a%5c%2e%2e%5cb",
    "a '..' on a loopback default": "http://127.0.0.1:8765/api/../admin",
    "an encoded '..' on a loopback default": "http://localhost/%2e%2E/admin",
    "a '.' on a loopback default": "http://[::1]:9/a/./b",
}
BACKSLASHES = {
    "a raw backslash in the path": B + "a\\b",
    "a raw backslash around a '..'": B + "a\\..\\b",
    "a raw backslash in the query": B + "x?a=\\",
    "a raw backslash on a loopback default": "http://localhost/a\\b",
}
BRACKETED_NAMES = {
    "a future-version address in brackets": "http://[v1.localhost]/x",
    "an allowed name in future-version brackets": "https://[v7.allowed.example.invalid]/base/x",
    "an IPv4 address in brackets": "http://[127.0.0.1]/x",
}
WIDENED = {**DOT_SEGMENTS, **BACKSLASHES, **BRACKETED_NAMES}


@pytest.mark.parametrize("url", WIDENED.values(), ids=WIDENED.keys())
def test_a_dot_segment_a_backslash_or_a_bracketed_name_is_refused_at_creation_edit_and_run(url):
    """R139: judged on the raw path such a URL lay under the prefix, but a receiving server that
    resolves dot segments (or reads a backslash as a slash) serves a path outside it; a bracketed
    name is a host we do not understand. One rule, the loopback defaults included; the sender is
    never called."""
    assert url_destination.destination(url) is None and ca.sendable_url(url, BASE) is None
    with pytest.raises(ca.ActionError, match="allow"):
        ca.create_action({**ACTION, "url": url}, actor="test", allowed_urls=BASE)
    ca.create_action({**ACTION, "url": B + "x"}, actor="test", allowed_urls=BASE)
    with pytest.raises(ca.ActionError, match="allow"):
        ca.update_action("a1", {"url": url}, actor="test", allowed_urls=BASE)
    state = ca._load()
    state["actions"]["a1"]["url"] = url                                  # stored before the rule
    ca._save(state)
    result = ca.run("a1", actor="t", request_id="r1", allowed_urls=BASE, http_post=_never)
    assert result["ok"] is False and "allow" in result["error"]


@pytest.mark.parametrize("url", [u for u in WIDENED.values() if "?" not in u],
                         ids=[k for k, u in WIDENED.items() if "?" not in u])
def test_such_a_prefix_is_refused_at_configuration(url, monkeypatch):
    monkeypatch.setattr("lib.log.log", lambda *a, **k: None)
    assert url_destination.prefix(url) is None
    assert config._controller_action_urls([url]) == ()
    assert ca.sendable_url(B + "x", (url,)) is None                      # admits nothing


@pytest.mark.parametrize("path", [
    ".well-known", "a..b", "...", "%2e%2e%2e",
    "a%2Fb",                            # an encoded slash with no dot segment beside it
    "%252e%252e/admin",                 # double encoding: not a dot to a server that decodes once
    "a%252F..%252Fb",
])
def test_what_has_no_dot_segment_once_decoded_is_sent_byte_for_byte(path):
    assert ca.sendable_url(B + path, BASE) == B + path


def test_a_bracketed_name_is_refused_even_where_the_name_itself_is_allowed():
    """Judged by its labels it was the allowed name, and it was sent without its brackets."""
    allowed = ("https://v1.allowed.example.invalid",)
    assert ca.sendable_url("https://v1.allowed.example.invalid/x", allowed)
    url = "https://[v1.allowed.example.invalid]/x"
    assert ca.sendable_url(url, allowed) is None
    with pytest.raises(ca.ActionError, match="allow"):
        ca.create_action({**ACTION, "url": url}, actor="test", allowed_urls=allowed)
    assert url_destination.prefix(url) is None


def test_an_ipv6_address_in_brackets_is_still_a_host():
    assert ca.sendable_url("http://[::1]:9/x") == "http://[::1]:9/x"


@pytest.mark.parametrize("url", [
    "http://localhost.example.invalid:9/x", "http://localhostx:9/x",
    "http://127.0.0.1.example.invalid:9/x", "http://127.0.0.10:9/x",
    "http://[::1:2]:9/x",
])
def test_a_host_that_only_begins_with_a_loopback_default_is_refused(url):
    """The defaults admit any port of their own host; a host that merely begins with that text
    (a suffix label, a longer address) is another host."""
    assert ca.sendable_url(url) is None
    with pytest.raises(ca.ActionError, match="allow"):
        ca.create_action({**ACTION, "url": url}, actor="test")


@pytest.mark.parametrize("char", ["\t", "\r", "\n", "\x00", " "], ids=["tab", "CR", "LF", "NUL", "space"])
@pytest.mark.parametrize("where", ["host", "path"])
def test_a_control_character_or_whitespace_inside_the_url_is_refused_at_create(char, where):
    url = ("https://allowed" + char + ".example.invalid/x" if where == "host"
           else "https://allowed.example.invalid/x" + char + "y")
    assert url_destination.destination(url) is None
    with pytest.raises(ca.ActionError, match="allow"):
        ca.create_action({**ACTION, "url": url}, actor="test", allowed_urls=ALLOWED)
