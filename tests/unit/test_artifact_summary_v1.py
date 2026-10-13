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
    monkeypatch.setattr(artifacts._CONTENT_IDENTITY, "max_weight", 3_000)   # a few entries
    forms = [_form("<p>" + "x" * 90 + f"{n}</p>") for n in range(12)]
    for round_ in range(3):
        for form in forms[:6]:
            _rewrite(form["artifact_id"], f"<p>{round_}{'y' * 90}</p>")
        _summary(type="html_form")
        assert 1 <= len(artifacts._CONTENT_IDENTITY) <= 5
        assert artifacts._CONTENT_IDENTITY.weight <= 3_000
    monkeypatch.setattr(artifacts._HISTORY_DIGEST, "max_weight", 20_000)
    for n in range(5):
        _checkpoint(_goal_plan(), f"plan {n}")
    _summary(type="plan")
    assert len(artifacts._HISTORY_DIGEST) >= 1
    assert artifacts._HISTORY_DIGEST.weight <= 20_000


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


# --- budget counts UTF-8 bytes; caches weigh retained bytes -------------------------

def test_a_non_ascii_body_under_the_budget_in_chars_but_over_in_bytes_is_pending(theo):
    _form("😀" * 1000)                                # 1000 chars, 4000 UTF-8 bytes
    budget = artifacts.summary_budget(max_bytes=2500)
    [item] = [artifacts.response_representation(r, "summary-v1", budget=budget)
              for r in artifacts.list_artifacts(limit=200, type="html_form")]
    marker = item["content_omitted"]
    assert marker["sha256"] is None and marker["hash_pending"] is True
    assert marker["bytes"] is None and marker["size_unknown"] is True


def test_a_mostly_ascii_body_with_a_few_wide_chars_is_measured_exactly(theo):
    """No 4x-per-character guess: 2,000 ASCII chars and one emoji is 2,004
    bytes. Counting and hashing them costs 2 x 2,004, which a 4,100-byte budget
    covers (a 4x guess would claim 8,004), so the marker completes exactly."""
    content = "a" * 2000 + "😀"
    _form(content)
    budget = artifacts.summary_budget(max_bytes=4100)
    [item] = [artifacts.response_representation(r, "summary-v1", budget=budget)
              for r in artifacts.list_artifacts(limit=200, type="html_form")]
    assert item["content_omitted"]["bytes"] == 2004
    assert item["content_omitted"]["sha256"] == "c1:" + hashlib.sha256(
        content.encode()).hexdigest()
    assert budget.remaining == 4100 - 2 * 2004


def test_a_wide_history_under_the_budget_in_chars_but_over_in_bytes_is_pending(theo):
    plan = _checkpoint(_goal_plan(), "😀" * 400)
    goal_text = db.conn().execute("SELECT goal_json FROM task_plans WHERE plan_id=?",
                                  (plan["plan_id"],)).fetchone()[0]
    budget = artifacts.summary_budget(max_bytes=len(goal_text) + 10)   # chars fit
    [item] = [artifacts.response_representation(r, "summary-v1", budget=budget)
              for r in artifacts.list_artifacts(limit=200, type="plan")]
    marker = item["plan"]["goal"]["history_omitted"]
    assert marker["digest"] is None and marker["digest_pending"] is True


def test_the_history_cache_weighs_its_goal_text_key_in_bytes(theo, monkeypatch):
    plan = _checkpoint(_goal_plan(), "😀" * 2000)     # wide text: 4 bytes per char held
    goal_text = db.conn().execute("SELECT goal_json FROM task_plans WHERE plan_id=?",
                                  (plan["plan_id"],)).fetchone()[0]
    import sys
    assert len(goal_text) < 10_000 < sys.getsizeof(goal_text)
    monkeypatch.setattr(artifacts._HISTORY_DIGEST, "max_weight", 10_000)
    _summary(type="plan")
    assert len(artifacts._HISTORY_DIGEST) == 0          # too heavy to retain
    assert artifacts._HISTORY_DIGEST.weight == 0


def test_every_content_entry_weighs_its_retained_bytes(theo):
    row = _form("<p>ok</p>")
    db.conn().execute("UPDATE artifacts SET payload_json=? WHERE artifact_id=?", (
        json.dumps({**json.loads(db.conn().execute(
            "SELECT payload_json FROM artifacts WHERE artifact_id=?",
            (row["artifact_id"],)).fetchone()[0]), "content": "bad \ud800"}),
        row["artifact_id"]))
    _summary(type="html_form")                       # caches (None, None)
    assert len(artifacts._CONTENT_IDENTITY) == 1
    assert artifacts._CONTENT_IDENTITY.weight >= len(row["artifact_id"])


def test_a_plan_without_its_goal_text_is_pending_and_never_reuses_a_digest(theo, hashes):
    plan = _checkpoint(_goal_plan(), "first")
    _summary(type="plan")                            # caches the real digest
    cached = len(artifacts._HISTORY_DIGEST)
    [row] = artifacts.list_artifacts(limit=200, type="plan")
    bare = {**row, "plan": dict(row["plan"])}        # a plain dict: no goal text
    hashes.clear()
    item = artifacts.response_representation(bare, "summary-v1",
                                             budget=artifacts.summary_budget())
    marker = item["plan"]["goal"]["history_omitted"]
    assert marker["digest"] is None and marker["digest_pending"] is True
    assert marker["count"] == len(row["plan"]["goal"]["history"])
    assert hashes == [] and len(artifacts._HISTORY_DIGEST) == cached


def test_over_budget_counting_work_is_charged_and_bounded(theo, monkeypatch):
    """Five wide rows, each over a 2,500-byte budget: the counting of the
    first spends the budget, the rest do no encoding at all, so the request's
    total UTF-8 encoding stays within the budget plus one chunk."""
    monkeypatch.setattr(artifacts, "_UTF8_COUNT_CHUNK", 100)      # 100 chars <= 400 bytes
    encoded = []
    real = getattr(artifacts, "_encode_utf8", None)

    def counting(text):
        data = real(text)
        encoded.append(len(data))
        return data

    monkeypatch.setattr(artifacts, "_encode_utf8", counting, raising=False)
    for _ in range(5):
        _form("😀" * 500)                                       # 500 chars, 2,000 bytes each
    budget = artifacts.summary_budget(max_bytes=2500)
    items = [artifacts.response_representation(r, "summary-v1", budget=budget)
             for r in artifacts.list_artifacts(limit=200, type="html_form")]
    # 2,000 bytes are counted for the first row, then hashing it needs
    # 2,000 more: over. Nothing after it may encode.
    assert budget.remaining == 0
    assert all(i["content_omitted"]["hash_pending"] for i in items)
    assert sum(encoded) <= 2500 + 400


def test_a_too_heavy_entry_is_not_stored_and_the_others_stay():
    from lib.revisioned_cache import RevisionedCache
    cache = RevisionedCache("test_heavy_entry", max_entries=10, max_weight=1000,
                            weight=lambda key, revision, value: len(value))
    try:
        cache.put("a", 1, "x" * 300)
        cache.put("b", 1, "y" * 300)
        cache.put("c", 1, "z" * 5000)                 # heavier than the whole bound
        assert cache.get("a", 1) == (True, "x" * 300)
        assert cache.get("b", 1) == (True, "y" * 300)
        assert cache.get("c", 1) == (False, None)
        assert cache.weight == 600
        cache.put("a", 2, "w" * 5000)                 # a stale entry is not kept either
        assert cache.get("a", 1) == (False, None) and cache.get("a", 2) == (False, None)
        assert cache.get("b", 1) == (True, "y" * 300) and cache.weight == 300
    finally:
        from lib import revisioned_cache
        revisioned_cache._REGISTRY.pop("test_heavy_entry", None)


# --- c1 hits need the exact content ---------------------------------------------------

def _report(content):
    return artifacts.create(session="theo", type="html_form", title="Report", payload={
        "content": content, "version": "1", "read_only": True})


def _c1_of(artifact_id):
    [item] = [i for i in _summary(type="html_form") if i["artifact_id"] == artifact_id]
    return item["content_omitted"]


def test_two_real_updates_on_a_frozen_clock_get_their_own_c1(theo, monkeypatch):
    monkeypatch.setattr(db, "now_ms", lambda: 1_900_000_000_000)
    row = _report("<p>zero</p>")
    _c1_of(row["artifact_id"])                                # cached
    first = artifacts.update(row["artifact_id"], {"payload_patch": {"content": "<p>one</p>"}})
    second = artifacts.update(row["artifact_id"], {"payload_patch": {"content": "<p>two</p>"}})
    assert second["updated_at"] > first["updated_at"] > row["updated_at"]  # MAX(now, +1)
    marker = _c1_of(row["artifact_id"])
    assert marker["sha256"] == "c1:" + hashlib.sha256(b"<p>two</p>").hexdigest()
    assert marker["updated_at"] == second["updated_at"]


def test_concurrent_real_updates_show_the_final_content(theo, monkeypatch):
    import threading
    monkeypatch.setattr(db, "now_ms", lambda: 1_900_000_000_000)
    row = _report("<p>start</p>")
    _c1_of(row["artifact_id"])
    errors = []

    def write(text):
        try:
            artifacts.update(row["artifact_id"], {"payload_patch": {"content": text}})
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            db.close_local()

    threads = [threading.Thread(target=write, args=(f"<p>thread {n}</p>",)) for n in (1, 2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == []
    final = artifacts.get(row["artifact_id"])["content"]
    assert final in ("<p>thread 1</p>", "<p>thread 2</p>")
    assert _c1_of(row["artifact_id"])["sha256"] == "c1:" + hashlib.sha256(final.encode()).hexdigest()


def test_direct_sql_content_change_with_the_same_tuple_is_never_served_stale(theo, hashes):
    row = _form("<p>original</p>")
    before = _c1_of(row["artifact_id"])
    payload = json.loads(db.conn().execute(
        "SELECT payload_json FROM artifacts WHERE artifact_id=?", (row["artifact_id"],)).fetchone()[0])
    db.conn().execute("UPDATE artifacts SET payload_json=? WHERE artifact_id=?",
                      (json.dumps({**payload, "content": "<p>replaced</p>"}), row["artifact_id"]))
    after = _c1_of(row["artifact_id"])
    assert (after["updated_at"], after["version"]) == (before["updated_at"], before["version"])
    assert after["sha256"] == "c1:" + hashlib.sha256(b"<p>replaced</p>").hexdigest()


def test_a_resident_content_entry_weighs_its_content(theo):
    import sys
    content = "<p>" + "x" * 50_000 + "</p>"
    _form(content)
    _summary(type="html_form")
    assert len(artifacts._CONTENT_IDENTITY) == 1
    assert artifacts._CONTENT_IDENTITY.weight >= sys.getsizeof(content)


def test_an_overweight_content_entry_is_not_cached_but_still_marked(theo, monkeypatch, hashes):
    monkeypatch.setattr(artifacts._CONTENT_IDENTITY, "max_weight", 20_000)
    content = "<p>" + "y" * 50_000 + "</p>"
    _form(content)
    marker = _summary(type="html_form")[0]["content_omitted"]
    assert marker["sha256"] == "c1:" + hashlib.sha256(content.encode()).hexdigest()
    assert len(artifacts._CONTENT_IDENTITY) == 0
    hashes.clear()
    assert _summary(type="html_form")[0]["content_omitted"] == marker   # recomputed, not cached
    assert hashes == [len(content)]


# --- scan-resistant admission for summary requests (Option A) -------------------------

def _request(**query):
    """One summary request as the handlers make it."""
    rows = artifacts.list_artifacts(limit=200, **query)
    open_request = getattr(artifacts, "summary_request", None)
    if open_request is None:                       # before Option A: no protection
        budget = artifacts.summary_budget()
        return [artifacts.response_representation(r, "summary-v1", budget=budget) for r in rows]
    with open_request() as budget:
        return [artifacts.response_representation(r, "summary-v1", budget=budget) for r in rows]


def _corpus_over_the_bound(monkeypatch, forms=6, fit=3):
    """`forms` html_forms of equal size; the content cache holds `fit` of them."""
    import sys
    bodies = [f"<p>{n}</p>" + "z" * 20_000 for n in range(forms)]
    rows = [_form(b) for b in bodies]
    revision = (rows[0]["updated_at"], "1", bodies[0])
    one = artifacts._retained_bytes(rows[0]["artifact_id"], revision, (len(bodies[0]), "c1:" + "0" * 64))
    monkeypatch.setattr(artifacts._CONTENT_IDENTITY, "max_weight", one * fit + one // 2)
    return rows


def test_a_corpus_over_the_bound_keeps_a_stable_resident_share(theo, monkeypatch, hashes):
    _corpus_over_the_bound(monkeypatch)
    _request(type="html_form")                     # cold: all 6 hashed
    for _ in range(3):
        hashes.clear()
        _request(type="html_form")
        assert len(hashes) == 3                     # only the excess, every list
        assert len(artifacts._CONTENT_IDENTITY) == 3


def test_churn_rehashes_exactly_the_changed_items(theo, monkeypatch, hashes):
    rows = _corpus_over_the_bound(monkeypatch)
    _request(type="html_form"); _request(type="html_form")
    resident = {k for k in artifacts._CONTENT_IDENTITY._entries}
    changed = next(r for r in rows if r["artifact_id"] in resident)
    _rewrite(changed["artifact_id"], "<p>changed</p>" + "z" * 20_000)
    hashes.clear()
    items = {i["artifact_id"]: i for i in _request(type="html_form")}
    assert len(hashes) == 3 + 1                     # the excess plus the changed one
    assert items[changed["artifact_id"]]["content_omitted"]["sha256"] == "c1:" + hashlib.sha256(
        ("<p>changed</p>" + "z" * 20_000).encode()).hexdigest()
    assert resident == {k for k in artifacts._CONTENT_IDENTITY._entries}


def test_a_resident_entry_with_changed_content_gets_a_fresh_c1(theo, monkeypatch):
    rows = _corpus_over_the_bound(monkeypatch)
    _request(type="html_form")
    resident = next(r for r in rows if r["artifact_id"] in artifacts._CONTENT_IDENTITY._entries)
    payload = json.loads(db.conn().execute("SELECT payload_json FROM artifacts WHERE artifact_id=?",
                                           (resident["artifact_id"],)).fetchone()[0])
    db.conn().execute("UPDATE artifacts SET payload_json=? WHERE artifact_id=?",   # same tuple
                      (json.dumps({**payload, "content": "<p>swapped</p>"}), resident["artifact_id"]))
    items = {i["artifact_id"]: i for i in _request(type="html_form")}
    assert items[resident["artifact_id"]]["content_omitted"]["sha256"] == \
        "c1:" + hashlib.sha256(b"<p>swapped</p>").hexdigest()


def test_an_overweight_entry_is_not_admitted_and_evicts_nothing(theo, monkeypatch, hashes):
    _corpus_over_the_bound(monkeypatch)
    _request(type="html_form")
    before = dict(artifacts._CONTENT_IDENTITY._entries)
    big = _form("<p>big</p>" + "w" * 200_000)
    hashes.clear()
    items = {i["artifact_id"]: i for i in _request(type="html_form")}
    assert items[big["artifact_id"]]["content_omitted"]["sha256"] == "c1:" + hashlib.sha256(
        ("<p>big</p>" + "w" * 200_000).encode()).hexdigest()
    assert big["artifact_id"] not in artifacts._CONTENT_IDENTITY._entries
    assert dict(artifacts._CONTENT_IDENTITY._entries) == before


def test_concurrent_requests_release_their_protection(theo, monkeypatch):
    _corpus_over_the_bound(monkeypatch)
    open_request = getattr(artifacts, "summary_request", None)
    assert open_request is not None
    rows = artifacts.list_artifacts(limit=200, type="html_form")
    with open_request() as first:
        for r in rows[:3]:
            artifacts.response_representation(r, "summary-v1", budget=first)
        with open_request() as second:              # interleaved: disjoint sets
            for r in rows[3:]:
                artifacts.response_representation(r, "summary-v1", budget=second)
        assert artifacts._CONTENT_IDENTITY.weight <= artifacts._CONTENT_IDENTITY.max_weight
    assert artifacts._CONTENT_IDENTITY._pins == {}      # all released
    newcomer = _form("<p>new</p>" + "n" * 20_000)       # no starvation: a later request admits
    _request(type="html_form")
    assert newcomer["artifact_id"] in artifacts._CONTENT_IDENTITY._entries


def test_an_error_mid_request_releases_the_protection(theo, monkeypatch):
    _corpus_over_the_bound(monkeypatch)
    open_request = getattr(artifacts, "summary_request", None)
    assert open_request is not None
    rows = artifacts.list_artifacts(limit=200, type="html_form")
    with pytest.raises(RuntimeError):
        with open_request() as budget:
            artifacts.response_representation(rows[0], "summary-v1", budget=budget)
            raise RuntimeError("client went away")
    assert artifacts._CONTENT_IDENTITY._pins == {}
    assert artifacts._HISTORY_DIGEST._pins == {}


def test_other_revisioned_cache_users_keep_plain_lru():
    from lib.revisioned_cache import RevisionedCache
    from lib import revisioned_cache
    cache = RevisionedCache("test_plain_lru", max_entries=3)
    try:
        for n in range(5):                              # a sequential scan evicts the oldest
            cache.put(f"k{n}", 1, n)
        assert list(cache._entries) == ["k2", "k3", "k4"]
        assert cache.get("k0", 1) == (False, None)
    finally:
        revisioned_cache._REGISTRY.pop("test_plain_lru", None)


def test_pending_rows_over_the_budget_hydrate_correctly(theo):
    content = "<p>pending é</p>" + "q" * 5_000
    row = _form(content)
    budget = artifacts.summary_budget(max_bytes=10)
    [item] = [artifacts.response_representation(r, "summary-v1", budget=budget)
              for r in artifacts.list_artifacts(limit=200, type="html_form")]
    assert item["content_omitted"]["hash_pending"] is True
    hydrated = artifacts.get(row["artifact_id"])["content"]
    [later] = _request(type="html_form")
    assert later["content_omitted"]["sha256"] == "c1:" + hashlib.sha256(hydrated.encode()).hexdigest()
