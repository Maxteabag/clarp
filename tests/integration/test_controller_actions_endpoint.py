"""HTTP tests for controller custom actions: registry, mappings, run, auth."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from .test_avatar_settings_endpoint import running_server, _get, _post  # noqa: F401


@pytest.fixture
def scenario_server():
    """A stand-in for the leak-watch demo's /api/scenario that records POSTs."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received.append((self.path, json.loads(self.rfile.read(length))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", received
    server.shutdown()
    server.server_close()


def _device(scope="full"):
    from lib import device_pairing
    return device_pairing.exchange(device_pairing.issue(scope=scope)["code"])["token"]


def _call(base, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    else:
        headers["Authorization"] = "Bearer avatar-test-token"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _seed_demo(base, url):
    for clip, action_id in (("leak", "rk-demo-leak"), ("normal", "rk-demo-reset")):
        status, body = _call(base, "POST", "/controller-actions", {
            "id": action_id, "title": f"Demo {clip}", "type": "http_post",
            "url": url + "/api/scenario", "body": {"feed": "demo-cam-02", "clip": clip}})
        assert status == 201, body


def test_a_mapped_gesture_runs_the_demo_post_end_to_end(running_server, scenario_server):
    url, received = scenario_server
    _seed_demo(running_server, url)
    status, mapped = _call(running_server, "POST", "/controller-actions/mappings", {
        "device": "*", "button": "secondary", "gesture": "swipe-up",
        "action_id": "rk-demo-leak"})
    assert status == 200 and mapped["mapping"]["action_id"] == "rk-demo-leak"

    phone = _device()
    status, state = _call(running_server, "GET", "/controller-actions", token=phone)
    assert status == 200
    assert {a["id"] for a in state["actions"]} == {"rk-demo-leak", "rk-demo-reset"}
    assert state["mappings"] == [mapped["mapping"]]

    status, result = _call(running_server, "POST", "/controller-actions/run", {
        "action_id": "rk-demo-leak", "request_id": "g-1",
        "device": "flic-abc", "button": "secondary", "gesture": "swipe-up"}, token=phone)
    assert status == 200 and result["ok"] is True and result["status"] == 200
    assert received == [("/api/scenario", {"feed": "demo-cam-02", "clip": "leak"})]

    status, audit = _call(running_server, "GET", "/controller-actions/audit?limit=5")
    assert status == 200
    run = audit["entries"][0]
    assert run["event"] == "run" and run["ok"] is True
    assert run["actor"].startswith("device:") and run["source"]["gesture"] == "swipe-up"


def test_run_can_resolve_the_action_from_the_gesture(running_server, scenario_server):
    url, received = scenario_server
    _seed_demo(running_server, url)
    _call(running_server, "POST", "/controller-actions/mappings", {
        "device": "*", "button": "secondary", "gesture": "rotate-clockwise",
        "action_id": "rk-demo-reset"})
    status, result = _call(running_server, "POST", "/controller-actions/run", {
        "device": "flic-abc", "button": "secondary", "gesture": "rotate-clockwise"},
        token=_device())
    assert status == 200 and result["action_id"] == "rk-demo-reset"
    assert received == [("/api/scenario", {"feed": "demo-cam-02", "clip": "normal"})]
    status, result = _call(running_server, "POST", "/controller-actions/run", {
        "device": "flic-abc", "button": "primary", "gesture": "hold"}, token=_device())
    assert status == 404


def test_a_failing_target_is_a_reported_failure_not_a_server_error(running_server):
    _call(running_server, "POST", "/controller-actions", {
        "id": "dead", "title": "Dead", "type": "http_post",
        "url": "http://127.0.0.1:9/nothing", "body": {}})
    status, result = _call(running_server, "POST", "/controller-actions/run",
                           {"action_id": "dead"}, token=_device())
    assert status == 200 and result["ok"] is False and result["error"]


def test_only_the_local_administrator_defines_actions(running_server):
    action = {"id": "x", "title": "X", "type": "http_post",
              "url": "http://127.0.0.1:8765/api/scenario", "body": {}}
    phone = _device()
    assert _call(running_server, "POST", "/controller-actions", action, token=phone)[0] == 403
    assert _call(running_server, "POST", "/controller-actions", action)[0] == 201
    assert _call(running_server, "POST", "/controller-actions/update",
                 {"id": "x", "title": "Y"}, token=phone)[0] == 403
    assert _call(running_server, "POST", "/controller-actions/delete",
                 {"id": "x"}, token=phone)[0] == 403
    status, updated = _call(running_server, "POST", "/controller-actions/update",
                            {"id": "x", "title": "Y"})
    assert status == 200 and updated["action"]["title"] == "Y"
    # The phone may bind its own gestures to an existing action.
    status, _ = _call(running_server, "POST", "/controller-actions/mappings", {
        "device": "*", "button": "primary", "gesture": "hold", "action_id": "x"}, token=phone)
    assert status == 200
    status, deleted = _call(running_server, "POST", "/controller-actions/delete", {"id": "x"})
    assert status == 200 and deleted["mappings_removed"] == 1


def test_a_limited_device_can_run_but_not_remap(running_server, scenario_server):
    url, received = scenario_server
    _seed_demo(running_server, url)
    watch = _device("limited")
    assert _call(running_server, "POST", "/controller-actions/mappings", {
        "device": "*", "button": "primary", "gesture": "hold",
        "action_id": "rk-demo-leak"}, token=watch)[0] == 403
    status, result = _call(running_server, "POST", "/controller-actions/run",
                           {"action_id": "rk-demo-leak"}, token=watch)
    assert status == 200 and result["ok"] is True


def test_unauthenticated_requests_are_refused(running_server):
    for method, path, body in (("GET", "/controller-actions", None),
                               ("POST", "/controller-actions/run", {"action_id": "x"})):
        request = urllib.request.Request(
            running_server + path, method=method,
            data=json.dumps(body).encode() if body else None,
            headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 401


def test_bad_definitions_answer_400_and_unknown_ids_404(running_server):
    status, body = _call(running_server, "POST", "/controller-actions", {
        "id": "evil", "title": "Evil", "type": "http_post",
        "url": "https://example.com/x", "body": {}})
    assert status == 400 and "allow" in body["error"]
    assert _call(running_server, "POST", "/controller-actions",
                 {"id": "sh", "title": "Sh", "type": "shell", "command": "ls"})[0] == 400
    assert _call(running_server, "POST", "/controller-actions/run",
                 {"action_id": "missing"}, token=_device())[0] == 404


def test_clarp_admin_seeds_maps_and_runs_the_demo_actions(running_server, scenario_server,
                                                          tmp_path):
    """The demo contract as an agent would set it up, through the real CLI."""
    import os
    import pathlib
    import subprocess
    import sys
    url, received = scenario_server
    port = int(running_server.rsplit(":", 1)[1])
    config_dir = tmp_path / "admin-config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        f'[server]\nport = {port}\nauth_token = "avatar-test-token"\n')
    admin = pathlib.Path(__file__).resolve().parents[2] / "bin/clarp-admin.py"
    env = {**os.environ, "CLARP_CONFIG_DIR": str(config_dir)}
    env.pop("CLARP_PROVIDER_TURN", None)

    def cli(*args):
        done = subprocess.run([sys.executable, str(admin), "controller-action", *args],
                              env=env, text=True, capture_output=True, timeout=60)
        assert done.returncode == 0, done.stderr
        return done.stdout

    for clip, action_id in (("leak", "rk-demo-leak"), ("normal", "rk-demo-reset")):
        cli("add", action_id, "--title", f"Demo {clip}",
            "--http-post", url + "/api/scenario",
            "--body", json.dumps({"feed": "demo-cam-02", "clip": clip}))
    cli("map", "--button", "secondary", "--gesture", "swipe-up", "--action", "rk-demo-leak")
    cli("map", "--button", "secondary", "--gesture", "swipe-down", "--action", "rk-demo-reset")
    listing = cli("list")
    assert "secondary swipe-up" in listing and "rk-demo-reset" in listing
    # The phone side: a gesture names only device, button and gesture.
    status, result = _call(running_server, "POST", "/controller-actions/run", {
        "device": "flic-abc", "button": "secondary", "gesture": "swipe-up"}, token=_device())
    assert status == 200 and result["ok"] is True
    assert '"ok": true' in cli("run", "rk-demo-reset")
    assert received == [("/api/scenario", {"feed": "demo-cam-02", "clip": "leak"}),
                        ("/api/scenario", {"feed": "demo-cam-02", "clip": "normal"})]
    assert "rk-demo-reset" in cli("audit")
