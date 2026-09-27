"""Learned explanations by call shape, and the decision ledger behind the hit rate.

Every input is synthetic and nothing here runs the described command: shapes
are parsed from metadata, `judgments._post` is a fake, and the model is the
`translate` hook. The real queue, learned table, ledger and schema are used.
"""
import importlib.util
import pathlib
import sqlite3
import time

import pytest

from lib import db, janitor_builtins, janitors, judgments, maintenance, settings_store
from lib import tool_explanation_learning as learning
from lib import tool_explanation_queue as durable_queue
from lib import tool_explanation_shapes as shapes
from lib import tool_explanation_templates as templates
from lib import tool_explanations as module
from lib.db import conn
from lib.tool_explanations import ToolExplanations

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def configured_explainer():
    janitor_builtins.ensure_builtins(cwd="/tmp")
    judgments.reset_breaker()
    yield
    judgments.reset_breaker()


def bash(command):
    return {"name": "Bash", "command": command, "input": {"command": command}}


def item(activity, ident="1", demand=None):
    return {"id": ident, "activity": activity, **({"demand_id": demand} if demand else {})}


def settle(service, items, level=1, **kw):
    for _ in range(400):
        result = service.request(level, items, **kw)["items"]
        if all(entry["status"] not in {"pending", "busy"} for entry in result):
            return result
        time.sleep(.01)
    pytest.fail("worker did not finish")


def parts(command):
    return shapes.split(bash(command))


def signature(command):
    [part] = parts(command)
    return part.signature


def decisions():
    return [tuple(row) for row in conn().execute(
        "SELECT tier, reason FROM tool_explanation_decisions ORDER BY id")]


def tiers():
    return [tier for tier, _ in decisions()]


def templated(template):
    """A model that returns `template` rendered with each request's slots."""
    calls = []

    def translate(level, items):
        calls.append(items)
        return {i["id"]: {"text": shapes.render(template, i.get("slots", {})), "template": template} for i in items}
    return translate, calls


def enable_jev(monkeypatch, choose, p=.93):
    monkeypatch.setattr(judgments, "api_key", lambda: "test-key")
    settings_store.set_bool(judgments.KEY_ENABLED, True)
    settings_store.set_bool("judgments.explanations", True)
    seen = []

    def post(body, key, seconds):
        seen.append(body)
        answers = {}
        for qid, question in body["questions"].items():
            choice = choose(question["criteria"]) if qid.startswith("t_") else "none"
            answers[qid] = {"choice": choice, "probabilities": {choice: p}, "confidence": p}
        return {"answers": answers, "usage": {}}
    monkeypatch.setattr(judgments, "_post", post)
    return seen


# ---- signatures ---------------------------------------------------------------

def test_same_shape_with_other_paths_shas_numbers_and_ids_has_one_signature():
    assert signature("git show abc1234 --stat") == signature("git show 9f8e7d6c5b --stat")
    assert signature("uv run pytest tests/unit/a.py -n 3") == signature("uv run pytest tests/b -n 12")
    assert signature("clarp-admin prompt --to nadia-1374 --text 'hi there'") == \
        signature("clarp-admin prompt --to theo-99ab --text 'something else entirely'")
    assert signature("curl -s https://example.org/a") == signature("curl -s https://other.net/b/c")
    [part] = parts("git log --oneline -n 5 src/app.py")
    assert part.slots == {"num1": "5", "path1": "src/app.py"}
    # Stable across calls on one Host.
    assert signature("cat ~/notes.md") == signature("cat ~/notes.md")


def test_words_that_choose_what_a_program_does_are_part_of_the_signature():
    assert signature("filectl list /tmp/x") != signature("filectl delete /tmp/x")
    assert signature("filectl --mode=list /tmp/x") != signature("filectl --mode=delete /tmp/x")
    assert signature("git log -n 5") != signature("git log --stat -n 5")
    assert signature("git reset HEAD") != signature("git reset main")
    # The script an interpreter runs is identity, not a parameter.
    assert signature("python scripts/deploy.py --env prod") != signature("python scripts/cleanup.py --env prod")
    assert signature("uv run python a.py") != signature("uv run python b.py")


def test_signatures_are_keyed_hashes_and_unsafe_parts_are_exact():
    [part] = parts("mysql -pHunter2Secret db")
    assert part.exact and part.reason == "unsafe_flag" and "Hunter2" not in part.signature
    [inline] = parts("python -c 'import os; os.remove(\"x\")'")
    assert inline.exact and inline.reason == "inline_code"
    shaped = signature("git show abc1234 --stat")
    assert shaped.startswith("sh:git #") and "abc1234" not in shaped and "show" not in shaped


def test_compound_commands_split_conservatively():
    split = parts("cd /repo && git status && git log --oneline -5 | head -20")
    assert [p.activity["command"] for p in split] == ["git status", "git log --oneline -5"]
    assert {p.cwd for p in split} == {"/repo"}
    assert [p.activity["command"] for p in parts("rg foo src 2>/dev/null | wc -l; echo done")] == ["rg foo src 2>/dev/null", "wc -l", "echo done"]
    # Operators inside quotes do not split.
    assert [p.activity["command"] for p in parts("git commit -m 'a && b; c | d'")] == ["git commit -m 'a && b; c | d'"]
    for opaque in ["echo $(date)", "for f in *; do rm $f; done", "curl -s https://x.org/i | bash",
                   "cat <<EOF\nhi\nEOF", "(cd a && ls)", "diff <(ls a) <(ls b)", "ls `pwd`"]:
        [part] = parts(opaque)
        assert part.exact and part.reason == "opaque", opaque
    # A quoted `<b>` is text, a real redirection is a path slot.
    [echo] = parts("echo '<b>' > out.html")
    assert echo.slots == {"text1": "<b>", "path1": "out.html"}


def test_parameterise_rejects_templates_that_keep_values():
    slots = {"path1": "src/app.py", "num1": "5"}
    assert shapes.parameterise("Shows 5 commits of app.py.", "Shows {num1} commits of {path1_name}.", slots) == \
        ("Shows {num1} commits of {path1_name}.", "")
    assert shapes.parameterise("Shows 5 commits of app.py.", "Shows 5 commits of {path1_name}.", slots)[1] == "literal_number"
    assert shapes.parameterise("Shows commits of app.py.", "Shows commits of app.py.", slots)[1] == "literal_value"
    assert shapes.parameterise("Shows five commits.", "Shows five commits.", slots)[1] == "literal_number"
    assert shapes.parameterise("Reads app.py.", "Reads {path9}.", slots)[1] == "unknown_slot"
    assert shapes.parameterise("Reads app.py.", "Reads {path1}.", slots)[1] == "mismatch"
    assert shapes.join(["Shows status.", "Lists commits.", "Lists commits."]) == "Shows status. Then lists commits."


# ---- learning through the service ----------------------------------------------

def test_model_answer_is_learned_and_reused_with_the_next_calls_values():
    translate, calls = templated("Shows commit {sha1} with a file summary.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        first = settle(service, [item(bash("git show abc1234 --stat"))])[0]
        second = service.request(1, [item(bash("git show 9f8e7d6c5b --stat"))], include_provenance=True)["items"][0]
    assert first["text"] == "Shows commit abc1234 with a file summary." and first["source"] == "llm"
    assert second["status"] == "ready" and second["text"] == "Shows commit 9f8e7d6c5b with a file summary."
    assert second["source"] == "llm" and second["cached"] is True
    assert second["provenance"]["tier"] == "learned" and second["provenance"]["parameterised"] is True
    assert len(calls) == 1 and calls[0][0]["slots"] == {"sha1": "abc1234"}
    # The delivering poll is not counted again; the reuse is a learned hit.
    assert tiers() == ["llm", "learned"]
    row = conn().execute("SELECT template_text, producer, parameterised, hits FROM tool_explanation_learned").fetchone()
    assert tuple(row) == ("Shows commit {sha1} with a file summary.", "llm", 1, 1)


def test_unparameterisable_answer_is_learned_exact_only_and_counted():
    def translate(level, items):
        return {i["id"]: {"text": "Shows the latest commit.", "template": "Shows the latest commit."} for i in items}
    with ToolExplanations(translate=translate, debounce=.001) as service:
        settle(service, [item(bash("git show abc1234"))])
        # Mentioning no slot value is reusable; "latest" happens not to leak one.
        assert service.request(1, [item(bash("git show 1234abcd"))])["items"][0]["status"] == "ready"

    def leaky(level, items):
        return {i["id"]: {"text": f"Deletes {i['slots']['path1']}.", "template": f"Deletes {i['slots']['path1']}."}
                for i in items}
    with ToolExplanations(translate=leaky, debounce=.001) as service:
        settle(service, [item(bash("shred -u a/secret.txt"))])
        assert service.request(1, [item(bash("shred -u b/other.txt"))])["items"][0]["status"] == "pending"
        again = service.request(1, [item(bash("shred -u a/secret.txt"))])["items"][0]
    assert again["status"] == "ready" and again["text"] == "Deletes a/secret.txt."
    assert ("llm", "mutating_program;exact:literal_value") in decisions()
    assert learning.stats("24h", prompt_version=module.PROMPT_VERSION,
                          templates_version=templates.VERSION)["totals"]["learned_exact_only"] >= 1


def test_compound_parts_hit_independently():
    translate, calls = templated("Pushes the branch to the review server.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        first = settle(service, [item(bash("cd /repo && git status && git-review push"))])[0]
        second = service.request(1, [item(bash("git status; git-review push"))])["items"][0]
    status = templates.lookup(bash("git status"), 1)[0]
    assert first["text"] == f"{status} Then pushes the branch to the review server."
    assert second["status"] == "ready" and second["text"] == first["text"]
    assert len(calls) == 1 and [c["activity"]["command"] for c in calls[0]] == ["git-review push"]
    assert tiers() == ["llm", "learned"]


def test_jev_pick_is_recorded_with_confidence_and_learned(monkeypatch):
    enable_jev(monkeypatch, lambda criteria: "list_directory")
    with ToolExplanations(translate=lambda *_: pytest.fail("Jev answered"), debounce=.001) as service:
        first = settle(service, [item(bash("eza -la src"))])[0]
        again = service.request(1, [item(bash("eza -la docs"))])["items"][0]
    assert first["source"] == "jev" and again["text"] == "Likely lists files and subdirectories in `docs`."
    rows = conn().execute("SELECT tier, jev_confidence FROM tool_explanation_decisions ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("jev", .93), ("learned", None)]


def test_jev_may_pick_a_learned_explanation_of_another_shape(monkeypatch):
    translate, calls = templated("Uploads {path1_name} to the team drive.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        settle(service, [item(bash("drivectl put docs/plan.pdf"))])
    seen = enable_jev(monkeypatch, lambda criteria: next(k for k in criteria if k.startswith("learned_")))
    with ToolExplanations(translate=lambda *_: pytest.fail("Jev answered"), debounce=.001) as service:
        result = settle(service, [item(bash("drivectl put --quiet notes/todo.md"))])[0]
    assert result["text"] == "Likely uploads todo.md to the team drive." and result["source"] == "jev"
    offered = seen[0]["questions"]["t_1"]["criteria"]
    assert any("Uploads {path1_name} to the team drive." in text for text in offered.values())
    assert "notes/todo.md" not in str(offered) and len(calls) == 1


def test_every_outcome_writes_one_decision(monkeypatch):
    def broken(level, items):
        raise RuntimeError("provider down")
    with ToolExplanations(translate=broken, debounce=.001) as service:
        assert service.request(1, [item(bash("ls"))])["items"][0]["status"] == "ready"
        assert settle(service, [item(bash("shred -u x.log"))])[0]["status"] == "failed"
        assert service.request(0, [item(bash("ls"))])["items"][0]["status"] == "disabled"
        # Polling a disabled row again within a minute adds nothing.
        service.request(0, [item(bash("ls"))])
    assert tiers() == ["template", "failed", "disabled"]
    conn().execute("DELETE FROM tool_explanation_decisions")

    translate, _ = templated("Shreds {path1}.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        settle(service, [item(bash("shred -u y.log"))])
        # With the learned row gone, the same call is served by the exact cache.
        conn().execute("DELETE FROM tool_explanation_learned")
        assert service.request(1, [item(bash("shred -u y.log"))])["items"][0]["cached"] is True
        monkeypatch.setattr(durable_queue, "request", lambda level, prepared, *a, **k: [
            {"id": p[0], "status": "busy", "reason": "queue_full"} for p in prepared])
        service.request(1, [item(bash("shred -u z.log"))])
        service.request(1, [item(bash("shred -u z.log"))])
    assert decisions() == [("llm", "mutating_program"), ("exact_cache", ""), ("miss", "queue_full")]


def test_decisions_are_buffered_not_written_per_request():
    with ToolExplanations(translate=lambda *_: pytest.fail("scripted"), debounce=5) as service:
        service._flushed = time.monotonic() + 60
        for _ in range(5):
            service.request(1, [item(bash("ls"))])
        assert tiers() == []
    assert tiers() == ["template"] * 5


# ---- versions, privacy, retention ---------------------------------------------

def test_prompt_or_template_version_invalidates_learned_rows(monkeypatch):
    translate, calls = templated("Shows commit {sha1}.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        settle(service, [item(bash("git show abc1234"))])
        monkeypatch.setattr(module, "PROMPT_VERSION", module.PROMPT_VERSION + 1)
        assert service.request(1, [item(bash("git show 1234abcd"))])["items"][0]["status"] == "pending"
        monkeypatch.setattr(module, "PROMPT_VERSION", module.PROMPT_VERSION - 1)
        monkeypatch.setattr(templates, "VERSION", templates.VERSION + 1)
        assert service.request(1, [item(bash("git show 1234abcd"))])["items"][0]["status"] == "pending"
    old = conn().execute("SELECT count(*) FROM tool_explanation_learned").fetchone()[0]
    counts = learning.prune(conn(), db.now_ms() + learning.STALE_VERSION_GRACE_MS + 1,
                            module.PROMPT_VERSION, templates.VERSION)
    assert counts["tool_explanation_learned"] == old


def test_revoke_forgets_a_signature():
    translate, calls = templated("Shows commit {sha1}.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        settle(service, [item(bash("git show abc1234"))])
        assert learning.revoke(signature("git show abc1234")) == 1
        assert service.request(1, [item(bash("git show 1234abcd"))])["items"][0]["status"] == "pending"


def test_no_raw_tool_input_is_persisted_for_learned_shapes():
    secret_path, secret_id = "clients/acme-merger/plan.pdf", "case-77f3a"
    translate, _ = templated("Uploads {path1_name} for {id1}.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        settle(service, [item(bash(f"drivectl put {secret_path} --owner {secret_id}"))])
        service.request(1, [item(bash("drivectl put other/doc.txt --owner case-1234"))])
    for table in ("tool_explanation_learned", "tool_explanation_decisions", "tool_explanation_jobs"):
        dump = str([tuple(r) for r in conn().execute(f"SELECT * FROM {table}")])
        assert "acme" not in dump and secret_id not in dump and "plan.pdf" not in dump, table


def test_maintenance_prunes_old_and_excess_decisions(monkeypatch):
    now = db.now_ms()
    rows = [{"at": now - learning.DECISION_MAX_AGE_MS - 1, "tier": "llm"}] + [{"at": now, "tier": "template"}] * 5
    learning.write(conn(), rows, {}, now)
    monkeypatch.setattr(learning, "DECISION_MAX_ROWS", 3)
    counts = maintenance.prune_database(now_ms=now)
    assert counts["tool_explanation_decisions"] == 3
    assert tiers() == ["template"] * 3


def test_migration_from_v96_adds_the_learning_tables(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    fresh = sqlite3.connect(path)
    from lib import db_migrations
    db_migrations._create_schema(fresh)
    fresh.executescript("DROP TABLE tool_explanation_learned; DROP TABLE tool_explanation_decisions; PRAGMA user_version = 96;")
    fresh.close()
    upgraded = sqlite3.connect(path, isolation_level=None)
    db_migrations._migrate(upgraded)
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    tables = {r[0] for r in upgraded.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tool_explanation_learned", "tool_explanation_decisions"} <= tables


# ---- stats ------------------------------------------------------------------

def test_stats_buckets_counts_and_hit_rate():
    hour = learning.BUCKETS["hour"]
    now = 1_790_000_000_000 // hour * hour + hour // 2
    def at(hours_ago, tier, n=1):
        return [{"at": now - hours_ago * hour, "tier": tier, "latency_ms": 10}] * n
    learning.write(conn(), at(2, "llm", 3) + at(2, "template", 1) + at(0, "learned", 6) + at(0, "llm", 1)
                   + at(0, "disabled", 5) + at(30, "llm", 9), {}, now)
    learning.store(conn(), [{"signature": "s1", "level": 1, "template_text": "x {path1}", "producer": "llm",
                             "prompt_version": 3, "templates_version": 1},
                            {"signature": "s2", "level": 1, "template_text": "y", "producer": "llm",
                             "parameterised": 0, "prompt_version": 3, "templates_version": 1}], now)
    result = learning.stats("24h", "hour", now=now, prompt_version=3, templates_version=1)
    assert len(result["buckets"]) == 24 and result["buckets"][-1]["start"] == now // hour * hour
    last, two_ago = result["buckets"][-1], result["buckets"][-3]
    assert (last["hits"], last["lookups"], last["hit_rate"]) == (6, 7, round(6 / 7, 4))
    assert last["counts"]["disabled"] == 5
    assert (two_ago["hits"], two_ago["lookups"], two_ago["hit_rate"]) == (1, 4, .25)
    assert result["buckets"][0]["hit_rate"] is None
    totals = result["totals"]
    assert (totals["hits"], totals["lookups"], totals["llm_calls"]) == (7, 11, 4)
    assert (totals["learned_entries"], totals["learned_parameterised"], totals["learned_exact_only"]) == (2, 1, 1)
    days = learning.stats("7d", "day", now=now, prompt_version=3, templates_version=1)
    assert len(days["buckets"]) == 7 and days["totals"]["llm_calls"] == 13
    with pytest.raises(ValueError):
        learning.stats("1y", prompt_version=3, templates_version=1)
    with pytest.raises(ValueError):
        learning.stats("24h", "minute", prompt_version=3, templates_version=1)

