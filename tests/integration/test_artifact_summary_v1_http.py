"""representation=summary-v1 and item-route flat-v1 over the real HTTP handler.

summary-v1 is flat-v1 with exactly two omissions, each replaced by an explicit
marker: html_form ``content`` (content_omitted) and ``plan.goal.history``
(plan.goal.history_omitted). Everything else, the rows, their order, filters,
archived/pinned visibility and decision actions, is flat-v1's.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from lib import agents, artifacts, db, task_plans
from lib import task_goal_state as goals

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("artifact_summary_http_server", _ROOT / "server/server.py")
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

TOKEN = "isolated-artifact-summary-test"
NOW = 1_800_000_000_000
BIG = ("<html><body><p>é漢😀 report</p><img src=\"data:image/png;base64,"
       + "iVBORw0KGgoAAAANSUhEUgAA" * 44_000 + "\"></body></html>")   # ~1 MB


@pytest.fixture
def host(tmp_path, monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(db, "now_ms", lambda: clock["now"])
    agents.create_agent(persona="Theo", voice_id="V", cwd=str(tmp_path), session="theo")
    agent = agents.get_by_session("theo")
    agents.start_runtime(agent["agent_id"], "theo")
    agents.bind_backend_session(agent["agent_id"], "native-theo")
    ctx = SimpleNamespace(auth_token=TOKEN, stream=SimpleNamespace(broadcast=lambda _e: None))
    srv = server_module.ContextHTTPServer(("127.0.0.1", 0), server_module.Handler, ctx)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    world = SimpleNamespace(base=f"http://127.0.0.1:{srv.server_port}", clock=clock,
                            agent_id=agent["agent_id"], ids={})
    _populate(world)
    clock["now"] = NOW + 10_000                 # one frozen read time for every request
    try:
        yield world
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _tick(world):
    world.clock["now"] += 7
    return world.clock["now"]


def _form(content, title, version="1"):
    return artifacts.create(session="theo", type="html_form", title=title, payload={
        "content": content, "version": version,
        "answer_schema": {"type": "object", "properties": {}, "additionalProperties": False}})


def _populate(world):
    ids = world.ids
    _tick(world); ids["big"] = _form(BIG, "Big report")["artifact_id"]
    _tick(world); ids["small"] = _form("<p>Hi</p>", "Small form", version="3")["artifact_id"]
    _tick(world); old = _form("<p>old</p>", "Archived form")
    _tick(world)
    artifacts.archive(old["artifact_id"], archived=True, expected_updated_at=old["updated_at"])
    ids["archived_form"] = old["artifact_id"]
    for n in range(3):
        _tick(world)
        ids[f"doc{n}"] = artifacts.create(session="theo", type="document", title=f"Doc {n}",
                                          payload={"content": f"# Doc {n} ü"})["artifact_id"]
    _tick(world); artifacts.pin(ids["doc1"], pinned=True)
    _tick(world); artifacts.pin(ids["small"], pinned=True)
    _tick(world)
    decision = artifacts.create_decision(session="theo", title="Ship it?", question="Ship?",
                                         context="ctx")
    ids["decision"] = decision["artifact_id"]
    _tick(world)
    plan = task_plans.create(session="theo", title="Goal", items=[{"id": "a", "title": "A"}],
                             goal={"criteria": ["c"], "limits": "none", "enroll": True})
    for n in range(4):
        _tick(world)
        plan = goals.mutate(plan["plan_id"], revision=plan["revision"], action="checkpoint",
                            data={"progress": f"step {n} ✓", "next_work": "more",
                                  "continuation": {"kind": "timer", "due_at": NOW + 10**9}})
    ids["plan"] = plan["plan_id"]
    ids["plan_artifact"] = db.conn().execute(
        "SELECT artifact_id FROM artifacts WHERE type='plan' AND reference_id=?",
        (plan["plan_id"],)).fetchone()[0]


def _raw(world, path):
    request = urllib.request.Request(world.base + path,
                                     headers={"Authorization": "Bearer " + TOKEN})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get(world, path):
    status, body = _raw(world, path)
    return status, json.loads(body)


def _ids(result):
    return [row["artifact_id"] for row in result["artifacts"]]


def _q(**params):
    return urllib.parse.urlencode(params)


def _c1(text):
    return "c1:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _h1(history):
    return "h1:" + hashlib.sha256(json.dumps(
        history, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8", "surrogatepass")).hexdigest()


def _strip_summary(row):
    """flat-v1's row, given a summary-v1 row: put nothing back, just compare the rest."""
    row = dict(row)
    row.pop("content_omitted", None)
    if isinstance(row.get("plan"), dict) and isinstance(row["plan"].get("goal"), dict):
        plan = dict(row["plan"]); goal = dict(plan["goal"])
        goal.pop("history_omitted", None); plan["goal"] = goal; row["plan"] = plan
    return row


def _flat_without_omitted(row):
    row = dict(row)
    if row.get("type") == "html_form":
        row.pop("content", None)
    if isinstance(row.get("plan"), dict) and isinstance(row["plan"].get("goal"), dict):
        plan = dict(row["plan"]); goal = dict(plan["goal"])
        goal.pop("history", None); plan["goal"] = goal; row["plan"] = plan
    return row


# --- parity with flat-v1 --------------------------------------------------------

FILTERS = [
    {}, {"order": "created"}, {"limit": 3}, {"limit": 2, "offset": 2},
    {"type": "html_form"}, {"type": "plan"}, {"type": "document"}, {"search": "doc"},
    {"session": "theo"}, {"created_from": NOW + 20}, {"created_to": NOW + 40},
]


@pytest.mark.parametrize("params", FILTERS, ids=lambda p: _q(**p) or "default")
def test_summary_lists_the_same_rows_as_flat_v1(host, params):
    _, flat = _get(host, "/artifacts?" + _q(representation="flat-v1", **params))
    status, summary = _get(host, "/artifacts?" + _q(representation="summary-v1", **params))
    assert status == 200
    assert _ids(summary) == _ids(flat)
    for s, f in zip(summary["artifacts"], flat["artifacts"]):
        assert _strip_summary(s) == _flat_without_omitted(f)


def test_summary_parity_with_an_agent_filter_includes_archived_and_pinned(host):
    params = {"agent_id": host.agent_id, "limit": 200}
    _, flat = _get(host, "/artifacts?" + _q(representation="flat-v1", **params))
    _, summary = _get(host, "/artifacts?" + _q(representation="summary-v1", **params))
    assert _ids(summary) == _ids(flat)
    rows = {r["artifact_id"]: r for r in summary["artifacts"]}
    assert rows[host.ids["archived_form"]]["archived_at"] is not None
    assert rows[host.ids["doc1"]]["pinned"] is True


def test_pinned_and_inbox_parity(host):
    _, flat = _get(host, "/pinned-artifacts?representation=flat-v1")
    status, summary = _get(host, "/pinned-artifacts?representation=summary-v1")
    assert status == 200 and _ids(summary) == _ids(flat) != []
    _, flat = _get(host, "/attention/inbox?representation=flat-v1")
    status, summary = _get(host, "/attention/inbox?representation=summary-v1")
    assert status == 200 and _ids(summary) == _ids(flat) != []
    for key in ("next_cursor", "total", "index_version"):
        assert summary[key] == flat[key]
    for s, f in zip(summary["artifacts"], flat["artifacts"]):
        assert _strip_summary(s) == _flat_without_omitted(f)


# --- the markers ------------------------------------------------------------------

def test_content_is_absent_and_marked_exactly(host):
    _, summary = _get(host, "/artifacts?representation=summary-v1&limit=200")
    rows = {r["artifact_id"]: r for r in summary["artifacts"]}
    for key, content, version in (("big", BIG, "1"), ("small", "<p>Hi</p>", "3")):
        row = rows[host.ids[key]]
        assert "content" not in row
        assert row["content_omitted"] == {
            "reason": "summary", "bytes": len(content.encode("utf-8")),
            "sha256": _c1(content), "version": version, "updated_at": row["updated_at"]}
    assert rows[host.ids["small"]]["content_omitted"]["sha256"] == \
        "c1:9c655e84ab91015eed7670dfbb369ffdc31f6efe04ef8bad2615cab6b9e63ca6"
    assert len(BIG.encode("utf-8")) > len(BIG)          # bytes, not characters
    assert rows[host.ids["doc0"]]["content"] == "# Doc 0 ü"   # only html_form omits


def test_plan_history_is_absent_and_marked_exactly(host):
    _, flat = _get(host, "/artifacts?representation=flat-v1&type=plan")
    _, summary = _get(host, "/artifacts?representation=summary-v1&type=plan")
    history = flat["artifacts"][0]["plan"]["goal"]["history"]
    goal = summary["artifacts"][0]["plan"]["goal"]
    assert "history" not in goal and len(history) >= 4
    assert goal["history_omitted"] == {
        "reason": "summary", "count": len(history), "last_at": history[-1]["at"],
        "digest": _h1(history)}
    assert goal["history_omitted"]["digest"].startswith("h1:")


def test_decision_fields_are_identical_to_flat_v1(host):
    _, flat = _get(host, "/artifacts?representation=flat-v1&type=decision")
    _, summary = _get(host, "/artifacts?representation=summary-v1&type=decision")
    assert summary["artifacts"] == flat["artifacts"] != []
    assert summary["artifacts"][0]["decision"]["status"] == "pending"


# --- old clients and errors -------------------------------------------------------

@pytest.mark.parametrize("route", ["/artifacts?limit=200", "/pinned-artifacts", "/attention/inbox"])
def test_default_and_flat_v1_bodies_are_the_legacy_bodies(host, route):
    sep = "&" if "?" in route else "?"
    status, default = _raw(host, route)
    status_flat, flat = _raw(host, route + sep + "representation=flat-v1")
    assert status == status_flat == 200
    rows = json.loads(default)["artifacts"]
    # The legacy flat-v1 projection: the same rows with only `payload` removed.
    legacy_flat = {**json.loads(default),
                   "artifacts": [{k: v for k, v in r.items() if k != "payload"} for r in rows]}
    assert flat == json.dumps(legacy_flat).encode()


@pytest.mark.parametrize("route,error", [
    ("/artifacts", "unsupported artifact representation"),
    ("/pinned-artifacts", "unsupported artifact representation"),
    ("/attention/inbox", "invalid attention query"),
])
@pytest.mark.parametrize("value", ["summary-v2", "SUMMARY-V1", "bogus"])
def test_unknown_representations_keep_their_errors(host, route, error, value):
    status, body = _get(host, f"{route}?representation={value}")
    assert (status, body) == (400, {"error": error})


# --- P1: the item route ---------------------------------------------------------

def test_item_route_flat_v1_drops_only_payload(host):
    for key in ("big", "small", "plan_artifact", "decision", "doc0"):
        path = f"/artifacts/{host.ids[key]}"
        status, legacy = _raw(host, path)
        status_flat, flat = _raw(host, path + "?representation=flat-v1")
        assert status == status_flat == 200
        legacy_item = json.loads(legacy)["artifact"]
        flat_item = json.loads(flat)["artifact"]
        assert "payload" in legacy_item and "payload" not in flat_item
        assert flat_item == {k: v for k, v in legacy_item.items() if k != "payload"}
        assert flat == json.dumps({"artifact": flat_item}).encode()
        assert len(flat) < len(legacy)


@pytest.mark.parametrize("value", ["", "summary-v1", "bogus", "flat-v2", "FLAT-V1"])
def test_item_route_any_other_value_is_the_legacy_item(host, value):
    path = f"/artifacts/{host.ids['big']}"
    _, legacy = _raw(host, path)
    status, other = _raw(host, path + "?" + _q(representation=value))
    assert status == 200 and other == legacy


def test_item_route_hydration_matches_the_list_marker_in_both_shapes(host):
    _, summary = _get(host, "/artifacts?representation=summary-v1&type=html_form")
    marker = {r["artifact_id"]: r["content_omitted"] for r in summary["artifacts"]}
    for shape in ("", "?representation=flat-v1"):
        _, item = _get(host, f"/artifacts/{host.ids['big']}{shape}")
        content = item["artifact"]["content"]
        assert _c1(content) == marker[host.ids["big"]]["sha256"]
        assert len(content.encode("utf-8")) == marker[host.ids["big"]]["bytes"]
        assert item["artifact"]["updated_at"] == marker[host.ids["big"]]["updated_at"]


def test_item_route_404_is_unchanged(host):
    for shape in ("", "?representation=flat-v1"):
        assert _get(host, "/artifacts/no-such-artifact" + shape) == (
            404, {"error": "artifact not found"})


# --- size ---------------------------------------------------------------------------

def test_summary_is_smaller_by_the_omitted_fields(host):
    _, flat = _raw(host, "/artifacts?representation=flat-v1&limit=200")
    status, summary = _raw(host, "/artifacts?representation=summary-v1&limit=200")
    assert status == 200 and len(json.loads(summary)["artifacts"]) == len(json.loads(flat)["artifacts"])
    assert len(summary) < len(flat) - len(BIG.encode())    # the big body is gone
    print(f"\nSIZE flat-v1={len(flat)} summary-v1={len(summary)}")
