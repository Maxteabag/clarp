"""summary-v1 markers, their bounded caches and the hash budget (in process)."""
from __future__ import annotations

import hashlib
import json

import pytest

from lib import agents, artifacts, db, server_identity, task_plans
from lib import task_goal_state as goals

VECTORS = [
    ("", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("<p>Hi</p>", 9, "9c655e84ab91015eed7670dfbb369ffdc31f6efe04ef8bad2615cab6b9e63ca6"),
    ("é漢😀", 9, "3ea0588400cb4af7b15d0edd7dc2fe1e359a94ab6822abe8c535f4281f330a20"),
    ("line1\nline2\t\"q\" \\ /", 19, "689426335045576c85a1ac7696a1146993c503c57b489f7b2fb2e581bac1ca1b"),
    ("<img src=\"data:image/png;base64,iVBORw0KGgo=\">", 46,
     "9af61be3754e69b51d792026d0f7364a2bef6f29389257192c59f443e6a643d8"),
]


@pytest.fixture
def theo(tmp_path):
    agents.create_agent(persona="Theo", voice_id="V", cwd=str(tmp_path), session="theo")
    agent = agents.get_by_session("theo")
    agents.start_runtime(agent["agent_id"], "theo")
    agents.bind_backend_session(agent["agent_id"], "native-theo")
    return agent


def _form(content, version="1"):
    return artifacts.create(session="theo", type="html_form", title="F", payload={
        "content": content, "version": version,
        "answer_schema": {"type": "object", "properties": {}, "additionalProperties": False}})


def _rewrite(artifact_id, content):
    """What the one payload writer does (artifacts._apply_update): new
    payload_json and updated_at=MAX(now, updated_at+1). html_forms refuse
    artifacts.update, so the test writes the row the same way."""
    con = db.conn()
    payload = json.loads(con.execute("SELECT payload_json FROM artifacts WHERE artifact_id=?",
                                     (artifact_id,)).fetchone()[0])
    con.execute("UPDATE artifacts SET payload_json=?, updated_at=MAX(?, updated_at+1) "
                "WHERE artifact_id=?",
                (json.dumps({**payload, "content": content}), db.now_ms(), artifact_id))


def _summary(**query):
    budget = artifacts.summary_budget()
    return [artifacts.response_representation(row, "summary-v1", budget=budget)
            for row in artifacts.list_artifacts(limit=200, **query)]


@pytest.fixture
def hashes(monkeypatch):
    calls = []
    real = artifacts._sha256

    def counted(data):
        calls.append(len(data))
        return real(data)

    monkeypatch.setattr(artifacts, "_sha256", counted)
    return calls


# --- content identity -------------------------------------------------------------

@pytest.mark.parametrize("text,size,hexdigest", VECTORS)
def test_the_c1_vectors(text, size, hexdigest):
    assert artifacts.content_identity(text) == (size, "c1:" + hexdigest)


@pytest.mark.parametrize("text,size,hexdigest", VECTORS)
def test_the_c1_vectors_reach_the_list_marker(theo, text, size, hexdigest):
    row = _form(text or "x")
    if not text:
        _rewrite(row["artifact_id"], text)      # creation rejects empty content
    [item] = [i for i in _summary(type="html_form") if i["artifact_id"] == row["artifact_id"]]
    assert item["content_omitted"]["bytes"] == size
    assert item["content_omitted"]["sha256"] == "c1:" + hexdigest


def test_a_lone_surrogate_takes_the_pending_path(theo):
    row = _form("<p>ok</p>")
    # Stored as a JSON escape (SQLite cannot hold a lone surrogate as text).
    db.conn().execute("UPDATE artifacts SET payload_json=? WHERE artifact_id=?", (
        json.dumps({**json.loads(db.conn().execute(
            "SELECT payload_json FROM artifacts WHERE artifact_id=?",
            (row["artifact_id"],)).fetchone()[0]), "content": "bad \ud800 text"}),
        row["artifact_id"]))
    [item] = _summary(type="html_form")
    marker = item["content_omitted"]
    assert marker["sha256"] is None and marker["hash_pending"] is True
    assert marker["bytes"] is None and marker["size_unknown"] is True
    assert "content" not in item


def test_an_unchanged_rebuild_hashes_nothing(theo, hashes):
    _form("<p>one</p>"); _form("<p>two</p>")
    plan = _goal_plan()
    _checkpoint(plan, "x")
    _summary()
    assert hashes                                   # the first build hashes
    hashes.clear()
    _summary()
    _summary()
    assert hashes == []                             # all hits


def test_a_content_change_rehashes_only_that_item(theo, hashes):
    first, second = _form("<p>one</p>"), _form("<p>two</p>")
    _summary(type="html_form")
    hashes.clear()
    _rewrite(second["artifact_id"], "<p>2!</p>")
    items = {i["artifact_id"]: i for i in _summary(type="html_form")}
    assert hashes == [len("<p>2!</p>")]
    assert items[second["artifact_id"]]["content_omitted"]["sha256"] == \
        "c1:" + hashlib.sha256(b"<p>2!</p>").hexdigest()


# --- plan history digest ------------------------------------------------------------

def _goal_plan():
    return task_plans.create(session="theo", title="Goal", items=[{"id": "a", "title": "A"}],
                             goal={"criteria": ["c"], "limits": "none", "enroll": True})


def _checkpoint(plan, text):
    plan = task_plans.get(plan["plan_id"])
    return goals.mutate(plan["plan_id"], revision=plan["revision"], action="checkpoint",
                        data={"progress": text, "next_work": "more",
                              "continuation": {"kind": "timer", "due_at": db.now_ms() + 10**9}})


def _history_marker():
    [item] = _summary(type="plan")
    assert "history" not in item["plan"]["goal"]
    return item["plan"]["goal"]["history_omitted"]


def _save_goal(plan_id, edit):
    """task_plans._goal_save only: no revision or updated_at bump."""
    con = db.conn()
    goal = json.loads(con.execute("SELECT goal_json FROM task_plans WHERE plan_id=?",
                                  (plan_id,)).fetchone()[0])
    edit(goal["history"])
    before = con.execute("SELECT revision,updated_at FROM task_plans WHERE plan_id=?",
                         (plan_id,)).fetchone()
    task_plans._goal_save(con, plan_id, goal)
    after = con.execute("SELECT revision,updated_at FROM task_plans WHERE plan_id=?",
                        (plan_id,)).fetchone()
    assert tuple(before) == tuple(after)


def test_the_digest_is_h1_over_the_canonical_history(theo):
    plan = _checkpoint(_goal_plan(), "first ✓")
    marker = _history_marker()
    history = task_plans.get(plan["plan_id"])["goal"]["history"]
    assert marker == {"reason": "summary", "count": len(history), "last_at": history[-1]["at"],
                      "digest": "h1:" + hashlib.sha256(json.dumps(
                          history, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                      ).encode("utf-8", "surrogatepass")).hexdigest()}


def test_a_goal_save_only_append_changes_the_digest(theo):
    plan = _checkpoint(_goal_plan(), "first")
    before = _history_marker()
    _save_goal(plan["plan_id"], lambda h: h.append({**h[-1], "kind": "wake_claim"}))
    after = _history_marker()
    assert after["count"] == before["count"] + 1 and after["digest"] != before["digest"]


def test_a_same_length_replacement_changes_the_digest(theo):
    plan = _checkpoint(_goal_plan(), "first")
    before = _history_marker()

    def replace_kind(history):
        old = history[-1]["kind"]
        history[-1]["kind"] = "".join("x" if c != "x" else "y" for c in old)  # same length

    _save_goal(plan["plan_id"], replace_kind)
    after = _history_marker()
    assert after["count"] == before["count"] and after["digest"] != before["digest"]


def test_a_same_count_edit_changes_the_digest(theo):
    plan = _checkpoint(_goal_plan(), "first")
    before = _history_marker()
    _save_goal(plan["plan_id"], lambda h: h[-1].update(at=h[-1]["at"] + 1))
    after = _history_marker()
    assert after["count"] == before["count"] and after["digest"] != before["digest"]


def test_an_unchanged_goal_is_a_cache_hit_without_rehash(theo, hashes):
    _checkpoint(_goal_plan(), "first")
    first = _history_marker()
    hashes.clear()
    assert _history_marker() == first
    assert hashes == []


# --- bounds and budget ----------------------------------------------------------------

def test_the_caches_stay_bounded_under_churn(theo, monkeypatch):
    monkeypatch.setattr(artifacts._CONTENT_IDENTITY, "max_entries", 5)
    monkeypatch.setattr(artifacts._CONTENT_IDENTITY, "max_weight", 400)
    forms = [_form("<p>" + "x" * 90 + f"{n}</p>") for n in range(12)]
    for round_ in range(3):
        for form in forms[:6]:
            _rewrite(form["artifact_id"], f"<p>{round_}{'y' * 90}</p>")
        _summary(type="html_form")
        assert len(artifacts._CONTENT_IDENTITY) <= 5
        assert artifacts._CONTENT_IDENTITY.weight <= 400
    monkeypatch.setattr(artifacts._HISTORY_DIGEST, "max_weight", 2_000)
    for n in range(5):
        _checkpoint(_goal_plan(), f"plan {n}")
    _summary(type="plan")
    assert artifacts._HISTORY_DIGEST.weight <= 2_000


def test_over_budget_takes_the_pending_paths(theo):
    _form("<p>" + "z" * 5000 + "</p>")
    _checkpoint(_goal_plan(), "first")
    budget = artifacts.summary_budget(max_bytes=10)
    items = [artifacts.response_representation(row, "summary-v1", budget=budget)
             for row in artifacts.list_artifacts(limit=200)]
    form = next(i for i in items if i["type"] == "html_form")
    plan = next(i for i in items if i["type"] == "plan")
    assert form["content_omitted"]["sha256"] is None and form["content_omitted"]["hash_pending"]
    assert form["content_omitted"]["bytes"] is None and form["content_omitted"]["size_unknown"]
    marker = plan["plan"]["goal"]["history_omitted"]
    assert marker["digest"] is None and marker["digest_pending"] is True
    assert marker["count"] >= 1


# --- capability -----------------------------------------------------------------------

def test_the_feature_and_contract_are_advertised():
    assert "artifact_summary_v1" in server_identity.capabilities()["features"]
    contract = server_identity.contract()
    assert contract["features"]["artifact_summary_v1"] == 1
    assert contract["host"] == 63
