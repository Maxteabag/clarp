"""A limited device may GET only what it needs to chat, speak and answer decisions; every other
GET, and every method not on a named list, is refused before routing (R123).

The GETs were a list of what is forbidden, so a new route was open to a limited device unless
someone added it. Now they are a list of what is allowed, as the POSTs already were. /server-info
is allowed, its answer trimmed to an allow-list of keys."""
from __future__ import annotations

import email.message
import pathlib
import re

import pytest

import server as host
from lib.routes import settings as settings_routes

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _handler(scope="limited"):
    handler = host.Handler.__new__(host.Handler)
    handler.headers = email.message.Message()
    handler._request_device_scope = scope
    return handler


ALLOWED = sorted(host.Handler._LIMITED_DEVICE_GET_EXACT) + [
    "/artifacts/a1", "/artifacts/a1/html", "/artifacts/a1/revisions", "/artifact-catalogs/clarp.form/1",
    "/artifact-catalogs/clarp.form/2",
    "/clips/c1/stream", "/clips/c1/playlist.m3u8", "/media/m1", "/audio/clip.mp3", "/avatars/a1",
    "/stt/stream", "/controller-actions/mappings", "/controller-actions/a1",
]


@pytest.mark.parametrize("path", ALLOWED)
def test_what_a_limited_app_needs_is_allowed(path):
    assert not _handler()._device_forbidden(path, "GET")


REFUSED = [
    "/dirs", "/launch-directories", "/favorite-paths", "/past-sessions", "/background-jobs",
    "/background-jobs/j1", "/agent-conversations", "/archived-agents", "/schedules",
    "/agent-portraits", "/paired-devices",
    "/backend-auth", "/settings", "/podcast-history", "/dreaming", "/janitor-runs/r1",
    "/a-path-no-route-serves", "/server-info/extra", "/statusx", "/logs", "/artifacts/a1/export",
]


@pytest.mark.parametrize("path", REFUSED)
def test_every_other_get_is_refused_for_a_limited_device_and_open_to_a_full_one(path):
    assert _handler()._device_forbidden(path, "GET")
    if path not in ("/dirs", "/launch-directories", "/favorite-paths", "/past-sessions"):
        assert not _handler("full")._device_forbidden(path, "GET")


def test_every_get_route_the_host_serves_is_either_on_the_list_or_refused():
    """The list is closed: a GET route added later is refused to a limited device until named."""
    source = (ROOT / "server" / "server.py").read_text() + (ROOT / "server" / "lib" / "routes" / "registry.py").read_text()
    paths = set(re.findall(r'"(/[a-z][a-z0-9/._-]*)":\s*"_handle', source))
    handler = _handler()
    named = {path for path in paths if not handler._device_forbidden(path, "GET")}
    assert named <= set(host.Handler._LIMITED_DEVICE_GET_EXACT) | {
        path for path in paths if path.startswith(host.Handler._LIMITED_DEVICE_GET_PREFIXES)}


@pytest.mark.parametrize("method", ["PATCH", "PUT", "DELETE", "HEAD", "OPTIONS", "TRACE"])
@pytest.mark.parametrize("path", ["/schedules/s1", "/agents/a1", "/artifacts/a1", "/send", "/log"])
def test_every_method_not_on_a_named_list_is_refused(method, path):
    assert _handler()._device_forbidden(path, method)


def test_a_limited_patch_is_refused_before_routing_on_any_path():
    handler = _handler()
    handler.path = "/agents/synthetic"
    handler._authorized = lambda: True
    denied, reached = [], []
    handler._json_error = lambda status, _message: denied.append(status)
    original = host.route_registry.dispatch
    host.route_registry.dispatch = lambda *a, **k: reached.append(a)
    try:
        handler.do_PATCH()
    finally:
        host.route_registry.dispatch = original
    assert denied == [403] and not reached


def test_posts_keep_their_own_list():
    handler = _handler()
    assert not handler._device_forbidden("/send", "POST")
    assert handler._device_forbidden("/agents", "POST")


FULL_INFO = {
    "server_id": "s", "name": "n", "deployment_mode": "native", "version": "1", "image_version": "",
    "clarp_version": "1", "min_app_version": 1, "capabilities": ["x"], "contract": {"host": 1},
    "default_cwd": "/synthetic/home", "client": {"status": "ok"},
    "local_connection": {"enabled": True, "port": 7683, "certificate_sha256": "ab", "service_type": "_clarps._tcp",
                         "urls": ["https://192.0.2.1:7683"], "error": "bind failed on /synthetic/socket"},
}


def test_a_limited_device_gets_exactly_the_listed_keys_of_server_info():
    limited = settings_routes.limited_server_info(dict(FULL_INFO))
    assert sorted(limited) == sorted(settings_routes.LIMITED_SERVER_INFO_KEYS)
    assert "default_cwd" not in limited
    assert sorted(limited["local_connection"]) == sorted(
        settings_routes.LIMITED_LOCAL_CONNECTION_KEYS + ("error",))
    assert limited["local_connection"]["error"] == "failed"
    assert {"server_id", "name"} <= set(limited)                      # the two iOS requires


def test_a_key_added_to_server_info_later_is_not_given_to_a_limited_device():
    assert "added_later" not in settings_routes.limited_server_info({**FULL_INFO, "added_later": "x"})
    assert "added_later" not in settings_routes.limited_server_info(
        {**FULL_INFO, "local_connection": {**FULL_INFO["local_connection"], "added_later": 1}})["local_connection"]


@pytest.mark.parametrize("scope,expect_cwd", [("limited", False), ("full", True)])
def test_the_route_trims_for_a_limited_device_only(scope, expect_cwd):
    handler = _handler(scope)
    handler._connection_server_info = lambda: {k: v for k, v in FULL_INFO.items() if k != "client"}
    sent = []
    handler._json_ok = sent.append
    handler._handle_server_info()
    assert ("default_cwd" in sent[0]) is expect_cwd
    assert (sent[0]["local_connection"]["error"] == "failed") is (not expect_cwd)
