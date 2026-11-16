"""Characterization of settings and ops routes no other test reaches over HTTP.

Written before the S3 move of the settings and ops handlers (REFACTOR.md):
pins statuses, error messages and payload shapes of server.py's HTTP layer
as it is today. The library rules behind them have their own unit tests.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server)


def _call(base: str, method: str, path: str, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_judgments_settings_and_decisions(running_server):
    base, _ctx, _srv = running_server
    from lib import judgments

    status, body = _call(base, "GET", "/judgments/settings")
    assert status == 200
    assert body["sites"] == list(judgments.SITES)
    assert body["defaults"] == {"enabled": False, "timeout_ms": judgments.DEFAULT_TIMEOUT_MS}
    assert body["limits"] == {"timeout_ms": [100, judgments.MAX_TIMEOUT_MS]}
    assert body["settings"] == judgments.status()

    assert _call(base, "POST", "/judgments/settings", raw=b"[]") == (400, {"error": "bad json"})
    status, body = _call(base, "POST", "/judgments/settings", {"enabled": "yes"})
    assert status == 400 and set(body) == {"error"}
    status, body = _call(base, "POST", "/judgments/settings", {"enabled": True})
    assert status == 200 and body["ok"] is True and body["settings"]["enabled"] is True

    assert _call(base, "GET", "/judgments/decisions?site=nowhere") == (
        400, {"error": "unknown site"})
    assert _call(base, "GET", "/judgments/decisions?site=router&limit=x") == (
        200, {"decisions": []})


def test_personalities_settings_round_trip(running_server):
    base, _ctx, _srv = running_server
    status, before = _call(base, "GET", "/personalities/settings")
    assert status == 200 and set(before) == {"settings"}
    assert _call(base, "POST", "/personalities/settings", raw=b"{") == (400, {"error": "bad json"})
    status, after = _call(base, "POST", "/personalities/settings", {})
    assert status == 200 and after == {"ok": True, "settings": before["settings"]}


def test_orchestrator_addressing_and_decisions(running_server):
    base, _ctx, _srv = running_server
    from lib import addressing

    modes = list(addressing.MODES)
    assert _call(base, "POST", "/orchestrator/addressing", raw=b"{") == (
        400, {"error": "bad json"})
    assert _call(base, "POST", "/orchestrator/addressing", {"mode": "psychic"}) == (
        400, {"error": "unknown addressing mode", "modes": modes})
    assert _call(base, "POST", "/orchestrator/addressing", {"mode": addressing.OFF}) == (
        200, {"ok": True, "mode": addressing.OFF, "modes": modes})
    assert _call(base, "GET", "/orchestrator/decisions?limit=x") == (200, {"decisions": []})


def test_relay_status_without_a_relay(running_server):
    base, _ctx, srv = running_server
    srv.relay = None
    assert _call(base, "GET", "/network/relay") == (
        200, {"enabled": False, "state": "disabled"})


def test_dirs_lists_matching_visible_subdirectories(running_server, tmp_path):
    base, _ctx, _srv = running_server
    root = tmp_path / "browse"
    for name in ("alpha", "Almond", "beta", ".hidden"):
        (root / name).mkdir(parents=True)
    (root / "afile").write_text("x")
    # A path that ends in a slash lists what is in that directory (it used to
    # complete the name "dir" in its parent: the recovered pathlib.Path had
    # dropped the slash before the handler looked for it).
    query = urllib.parse.urlencode({"path": f"{root}/"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(root), "matches": [str(root / "Almond"), str(root / "alpha"), str(root / "beta")]})
    # Without the slash it is still the name being completed, in its parent.
    query = urllib.parse.urlencode({"path": f"{root}"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(tmp_path), "matches": [str(root)]})
    # A directory that is not there, asked for with a slash: nothing, and its own base.
    query = urllib.parse.urlencode({"path": f"{root}/missing/"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(root / "missing"), "matches": []})
    query = urllib.parse.urlencode({"path": f"{root}/al"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(root), "matches": [str(root / "Almond"), str(root / "alpha")]})
    query = urllib.parse.urlencode({"path": f"{root}/b"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(root), "matches": [str(root / "beta")]})
    query = urllib.parse.urlencode({"path": f"{root}/missing/x"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(root / "missing"), "matches": []})
    assert _call(base, "GET", "/dirs?path=/")[1]["base"] == "/"


def test_dirs_says_nothing_of_what_is_not_there_and_never_lists_a_relative_path(
        running_server, tmp_path, monkeypatch):
    base, _ctx, _srv = running_server
    from lib.routes import settings as settings_routes
    logged = []
    monkeypatch.setattr(settings_routes, "log_exception",
                        lambda event, error, **more: logged.append((event, type(error).__name__)))
    root = tmp_path / "browse"
    (root / "alpha").mkdir(parents=True)
    (root / "afile").write_text("x")
    # Typing a path that is not there yet, or that is a file: no match, and no exception line.
    for path, its_base in ((f"{root}/missing/", str(root / "missing")), (f"{root}/afile/", str(root / "afile")),
                           (f"{root}/missing/x", str(root / "missing"))):
        query = urllib.parse.urlencode({"path": path})
        assert _call(base, "GET", f"/dirs?{query}") == (200, {"base": its_base, "matches": []})
    assert logged == []
    # A relative path is never listed from the inside: not the Host's own working directory.
    for path in ("./", "../", "relative/"):
        query = urllib.parse.urlencode({"path": path})
        status, body = _call(base, "GET", f"/dirs?{query}")
        assert (status, body["matches"]) == (200, [])
    assert _call(base, "GET", "/dirs?" + urllib.parse.urlencode({"path": "./"}))[1]["base"] == "/"
    # A path the Host may not look at is answered, with nothing in it, and said once.
    from lib import launch_paths
    def refuse(_raw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(launch_paths, "recover_user_path", refuse)
    query = urllib.parse.urlencode({"path": "/proc/1/root/"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {"base": "/proc/1/root/", "matches": []})
    assert logged == [("dirsPathRefused", "PermissionError")]


@pytest.mark.parametrize("suffix", ["", "/", "/projects/"])
def test_dirs_answers_when_a_tilde_user_does_not_exist(running_server, monkeypatch, suffix):
    pwd = pytest.importorskip("pwd")
    username = "clarp-no-such-user-7182"
    with pytest.raises(KeyError):
        pwd.getpwnam(username)
    base, _ctx, _srv = running_server
    from lib.routes import settings as settings_routes

    logged = []
    monkeypatch.setattr(settings_routes, "log_exception",
                        lambda event, error, **more: logged.append((event, type(error).__name__)))
    path = f"~{username}{suffix}"
    query = urllib.parse.urlencode({"path": path})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {"base": path, "matches": []})
    assert logged == []


@pytest.mark.parametrize("path", ["~abc\0/", "~abc\0/projects/"])
def test_dirs_answers_a_malformed_tilde_username_quietly(running_server, monkeypatch, path):
    base, _ctx, _srv = running_server
    from lib.routes import settings as settings_routes

    logged = []
    monkeypatch.setattr(settings_routes, "log_exception",
                        lambda *args, **kwargs: logged.append(args))
    query = urllib.parse.urlencode({"path": path})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {"base": path, "matches": []})
    assert logged == []


@pytest.mark.parametrize("error_type", [LookupError, ValueError])
@pytest.mark.parametrize("failure_site", ["recovery", "named-expansion"])
def test_dirs_does_not_swallow_an_unrelated_recovery_error(monkeypatch, error_type, failure_site):
    from lib import launch_paths
    from lib.routes import settings as settings_routes

    failure = error_type("unrelated recovery defect")
    def broken(_raw):
        raise failure
    path = "/projects/"
    if failure_site == "recovery":
        monkeypatch.setattr(launch_paths, "recover_user_path", broken)
    else:
        import pathlib
        path = "~typed-user/"
        original_expanduser = pathlib.Path.expanduser
        def broken_expansion(candidate):
            if str(candidate) == "~typed-user":
                raise failure
            return original_expanduser(candidate)
        monkeypatch.setattr(pathlib.Path, "expanduser", broken_expansion)
    responses = []
    handler = SimpleNamespace(_query=lambda: {"path": [path]},
                              _send=lambda *args: responses.append(args))
    with pytest.raises(error_type) as caught:
        settings_routes.SettingsRoutes._handle_dirs(handler)
    assert caught.value is failure
    assert responses == []


def test_message_tool_details_requires_a_known_message(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "GET", "/message-tool-details?session=claude") == (
        400, {"error": "session and message_id required"})
    assert _call(base, "GET", "/message-tool-details?session=nobody&message_id=m1") == (
        404, {"error": "message not found"})
    assert _call(base, "GET", "/message-tool-details?session=claude&message_id=m1") == (
        404, {"error": "message not found"})


@pytest.mark.parametrize("input_path", ["absolute", "~", "~/projects/"])
def test_dirs_logs_a_host_home_failure(running_server, tmp_path, monkeypatch, input_path):
    import pathlib
    from lib.routes import settings as settings_routes

    base, _ctx, _srv = running_server
    path = str(tmp_path) + "/" if input_path == "absolute" else input_path
    failure = RuntimeError("Host account has no home")
    logged = []
    monkeypatch.setattr(settings_routes, "log_exception",
                        lambda event, error, **more: logged.append((event, error)))
    def no_home(cls):
        raise failure
    query = urllib.parse.urlencode({"path": path})
    with monkeypatch.context() as home_failure:
        home_failure.setattr(pathlib.Path, "home", classmethod(no_home))
        assert _call(base, "GET", f"/dirs?{query}") == (200, {"base": path, "matches": []})
    assert logged == [("dirsPathRefused", failure)]


@pytest.mark.parametrize("configured_root", [False, True], ids=["ordinary", "workspace"])
@pytest.mark.parametrize("suffix", ["", "/", "/x"])
def test_dirs_preserves_a_named_home_ending_in_space(
        running_server, tmp_path, monkeypatch, configured_root, suffix):
    pwd = pytest.importorskip("pwd")
    base, _ctx, _srv = running_server
    actual_home = tmp_path / "named home "
    sibling = tmp_path / "named home"
    (actual_home / "x-real-home").mkdir(parents=True)
    (sibling / "x-trimmed-home").mkdir(parents=True)
    original_lookup = pwd.getpwnam
    def lookup(username):
        if username == "clarp-space-home":
            return SimpleNamespace(pw_dir=str(actual_home))
        return original_lookup(username)
    monkeypatch.setattr(pwd, "getpwnam", lookup)
    if configured_root:
        monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
        monkeypatch.setenv("CLARP_WORKSPACE_ROOT", str(tmp_path))
    else:
        monkeypatch.delenv("CLARP_DEPLOYMENT_MODE", raising=False)
    path = "~clarp-space-home" + suffix
    expected = ({"base": str(tmp_path), "matches": [str(actual_home)]} if suffix == "" else
                {"base": str(actual_home), "matches": [str(actual_home / "x-real-home")]})
    query = urllib.parse.urlencode({"path": path})
    assert _call(base, "GET", f"/dirs?{query}") == (200, expected)


@pytest.mark.parametrize("path", ["~", "~/"])
def test_dirs_classifier_bypasses_own_home_without_expanding(monkeypatch, path):
    import pathlib
    from lib.routes import settings as settings_routes

    def unexpected_expansion(_path):
        pytest.fail("The named-user classifier must not expand the Host's own home")
    monkeypatch.setattr(pathlib.Path, "expanduser", unexpected_expansion)
    assert settings_routes._named_user_miss(path) is False
