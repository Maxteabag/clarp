"""GET /live and GET /events?live= on the real server (docs/live-items.md
§4-5): the snapshot validates against contract/schemas/live.json, follows the
hub, and a live subscriber receives the hub's events while an old client
does not."""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from schema_check import validate  # noqa: E402
from lib import agents as agents_db  # noqa: E402
from lib import live_hub  # noqa: E402

SCHEMA = json.loads((REPO / "contract/schemas/live.json").read_text())


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _sse_lines(base, path, out, stop):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        for raw in response:
            if stop.is_set():
                return
            line = raw.decode().strip()
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))


def test_live_snapshot_and_subscription(core_server):
    base = core_server["base"]
    status, info = _get(base, "/server-info")
    assert "live_items" in info["capabilities"]["features"]
    assert _get(base, "/live?session=nobody")[0] == 404
    status, empty = _get(base, "/live?session=rachel")
    assert status == 200
    validate(empty, SCHEMA["$defs"]["snapshot"], SCHEMA)
    assert empty["items"] == [] and empty["activity"]["state"] == "idle"

    hub = live_hub.current()
    assert hub is not None and empty["epoch"] == hub.epoch
    agent_id = agents_db.get_by_session("rachel")["agent_id"]
    phone, old, stop = [], [], threading.Event()
    for path, out in (("/events?live=rachel", phone), ("/events", old)):
        threading.Thread(target=_sse_lines, args=(base, path, out, stop), daemon=True).start()
    time.sleep(0.5)
    hub.begin_turn(agent_id=agent_id, session="rachel", conv="conv-r", turn_id="tr-1")
    hub.message_text(agent_id, "cx:msg_1", "Hello")
    deadline = time.monotonic() + 5
    while len([e for e in phone if e.get("type") == "live"]) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    live = [e for e in phone if e.get("type") == "live"]
    assert [e["lseq"] for e in live] == [1, 2]
    for event in live:
        validate(event, SCHEMA["$defs"]["live-event"], SCHEMA)
    assert not any(e.get("type") == "live" for e in old)
    status, snapshot = _get(base, "/live?session=rachel")
    validate(snapshot, SCHEMA["$defs"]["snapshot"], SCHEMA)
    assert snapshot["lseq"] == 2 and snapshot["items"][0]["text"] == "Hello"


def _post(base, path, body):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_tool_explanation_setting_turns_the_explainer_off(core_server):
    base, ctx = core_server["base"], core_server["ctx"]
    calls = []

    class Recording:
        def request(self, level, items, **_kw):
            calls.append(items)
            return {"detail_level": level, "model": "m",
                    "items": [{"id": i["id"], "status": "ready", "text": "x"} for i in items]}

        def close(self):
            pass

    ctx.install_tool_explanations(Recording())
    assert _get(base, "/tool-explanations/settings") == (200, {"enabled": True, "detail_level": 2})
    assert _get(base, "/agents/snapshot")[1]["tool_explanations"] == {"enabled": True, "detail_level": 2}
    item = {"id": "1", "activity": {"name": "Bash", "command": "ls"}}
    status, body = _post(base, "/tool-explanations", {"session": "rachel", "detail_level": 3, "items": [item]})
    assert status == 200 and body["items"][0]["status"] == "ready" and len(calls) == 1

    assert _post(base, "/tool-explanations/settings", {"enabled": False}) == (
        200, {"enabled": False, "detail_level": 2})
    assert _post(base, "/tool-explanations/settings", {"detail_level": 7})[0] == 400
    status, body = _post(base, "/tool-explanations", {"session": "rachel", "detail_level": 3, "items": [item]})
    assert status == 200 and [i["status"] for i in body["items"]] == ["disabled"]
    assert len(calls) == 1
    assert _get(base, "/live?session=rachel")[1]["tool_explanations"]["enabled"] is False
    assert _get(base, "/agents/snapshot")[1]["tool_explanations"]["enabled"] is False


def test_a_quiet_stream_sends_a_heartbeat_event_clients_can_see(core_server, monkeypatch):
    import dataclasses

    from conftest import _srv_mod as server_module
    monkeypatch.setattr(server_module, "SERVER_TIMING", dataclasses.replace(
        server_module.SERVER_TIMING, sse_queue_timeout_sec=0.2))
    lines = []
    with urllib.request.urlopen(core_server["base"] + "/events", timeout=5) as response:
        deadline = time.monotonic() + 3
        for raw in response:
            lines.append(raw.decode().rstrip("\n"))
            if any(line.startswith("data: ") and '"heartbeat"' in line for line in lines) \
                    or time.monotonic() > deadline:
                break
    index = lines.index("event: heartbeat")
    beat = json.loads(lines[index + 1][len("data: "):])
    assert beat["type"] == "heartbeat" and isinstance(beat["ts"], int)
    # Not durable: no id line, so it never moves a client's resume cursor.
    assert not lines[index - 1].startswith("id: ")


def _delete(base, path, body):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_live_activity_tokens_register_and_receive_the_fleet_status(core_server, monkeypatch):
    from lib import apns

    base = core_server["base"]
    pushed = []
    monkeypatch.setattr(apns, "send_live_activity",
                        lambda payload, priority: pushed.append((payload, priority)))
    assert "live_activity_push" in _get(base, "/server-info")[1]["capabilities"]["features"]
    assert _post(base, "/devices/live-activity", {"token": "zz", "kind": "agents-working"})[0] == 400
    status, body = _post(base, "/devices/live-activity",
                         {"token": "ab" * 32, "activity_id": "act-1", "kind": "agents-working"})
    assert status == 200 and body == {"ok": True, "activity_id": "act-1"}

    hub = live_hub.current()
    agent_id = agents_db.get_by_session("rachel")["agent_id"]
    hub.begin_turn(agent_id=agent_id, session="rachel", conv="conv-r", turn_id="tr-la")
    deadline = time.monotonic() + 3
    while not pushed and time.monotonic() < deadline:
        time.sleep(0.05)
    state = pushed[-1][0]["aps"]["content-state"]
    assert state["working"] == 1 and state["lead"]["persona"] == "Rachel"

    assert _delete(base, "/devices/live-activity", {"activity_id": "act-1"}) == (
        200, {"ok": True, "removed": 1})


def test_post_stop_settles_the_live_turn(core_server):
    from lib import turn_lifecycle

    base = core_server["base"]
    hub = live_hub.current()
    agent_id = agents_db.get_by_session("rachel")["agent_id"]
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-stop")
    turn_lifecycle.record(agent_id, "tool")
    hub.begin_turn(agent_id=agent_id, session="rachel", conv="conv-r", turn_id="tr-stop")
    hub.tool_start(agent_id, "cx:call_1", name="sleep 30", call_id="call_1", category="exec",
                   label="sleep 30")
    status, body = _post(base, "/stop", {"session": "rachel"})
    assert status == 200, body
    deadline = time.monotonic() + 3
    snapshot = {}
    while time.monotonic() < deadline:
        snapshot = _get(base, "/live?session=rachel")[1]
        if snapshot["turn"] and snapshot["turn"]["status"] == "interrupted":
            break
        time.sleep(0.05)
    validate(snapshot, SCHEMA["$defs"]["snapshot"], SCHEMA)
    assert snapshot["turn"]["status"] == "interrupted" and snapshot["turn"]["worked_ms"] is not None
    assert snapshot["activity"]["state"] == "interrupted"
    assert [item["status"] for item in snapshot["items"]] == ["interrupted"]
