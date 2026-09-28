"""Jev reusing a similar learned explanation for a shape it has never seen.

Everything is synthetic: `judgments._post` is a fake Jev, the model is the
`translate` hook, and nothing described is run. The real queue, learned table,
ledger and schema are used.
"""
import sqlite3
import time

import pytest

from lib import db_migrations, janitor_builtins, judgments, settings_store
from lib import tool_explanation_learning as learning
from lib import tool_explanation_shapes as shapes
from lib.db import conn
from lib.tool_explanations import ToolExplanations


@pytest.fixture(autouse=True)
def configured_explainer():
    janitor_builtins.ensure_builtins(cwd="/tmp")
    judgments.reset_breaker()
    yield
    judgments.reset_breaker()


def bash(command):
    return {"name": "Bash", "command": command, "input": {"command": command}}


def item(activity, ident="1"):
    return {"id": ident, "activity": activity}


def settle(service, items, level=1):
    for _ in range(400):
        result = service.request(level, items, include_provenance=True)["items"]
        if all(entry["status"] not in {"pending", "busy"} for entry in result):
            return result
        time.sleep(.01)
    pytest.fail("worker did not finish")


def templated(template):
    calls = []

    def translate(level, items):
        calls.append(items)
        return {i["id"]: {"text": shapes.render(template, i.get("slots", {})), "template": template} for i in items}
    return translate, calls


def no_model(*_):
    pytest.fail("the model was asked")


def learn(command, template):
    translate, _ = templated(template)
    with ToolExplanations(translate=translate, debounce=.001) as service:
        settle(service, [item(bash(command))])
    [part] = shapes.split(bash(command))
    return part.signature


def fake_jev(monkeypatch, choose, p=.93):
    """Jev answering each question with `choose(criteria)` at probability `p`."""
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


def first_learned(criteria):
    return next((key for key in criteria if key.startswith("learned_")), "unknown")


def decisions():
    return [tuple(row) for row in conn().execute(
        "SELECT tier, reason, jev_confidence FROM tool_explanation_decisions ORDER BY id")]


def test_an_unseen_shape_reuses_a_similar_explanation_and_the_next_call_is_learned(monkeypatch):
    source = learn("git worktree list --porcelain", "Lists the git worktrees in machine-readable form.")
    # Needs a file name `git worktree list` does not have, so it is never offered.
    learn("git blame src/app.py", "Shows who last changed each line of {path1_name}.")
    conn().execute("DELETE FROM tool_explanation_decisions")
    seen = fake_jev(monkeypatch, lambda criteria: next(
        key for key, text in criteria.items() if "worktrees" in text))
    with ToolExplanations(translate=no_model, debounce=.001) as service:
        [first] = settle(service, [item(bash("git worktree list"))])
        assert first["text"] == "Likely lists the git worktrees in machine-readable form."
        assert first["source"] == "jev"
    # `git worktree` is not a template, so this is an unmapped git call, and it
    # was offered only learned explanations of git, same action first.
    offered = seen[0]["questions"]["t_1"]["criteria"]
    assert set(offered) == {"learned_1", "unknown"}
    assert "worktrees" in offered["learned_1"]
    assert offered["unknown"].startswith("None of these states exactly")
    [signature] = [p.signature for p in shapes.split(bash("git worktree list"))]
    row = conn().execute("SELECT producer, source_signature, program, parameterised, template_text FROM "
                         "tool_explanation_learned WHERE signature=?", (signature,)).fetchone()
    assert tuple(row) == ("jev", source, "git", 1, "Likely lists the git worktrees in machine-readable form.")

    # The same shape again is a tier-1 learned hit: no Jev, no model.
    monkeypatch.setattr(judgments, "_post", lambda *a: pytest.fail("Jev was asked again"))
    with ToolExplanations(translate=no_model, debounce=.001) as service:
        [again] = service.request(1, [item(bash("git worktree list"))])["items"]
    assert again["status"] == "ready" and again["cached"] is True
    assert again["text"] == "Likely lists the git worktrees in machine-readable form."
    tiers = decisions()
    assert tiers[0][0] == "jev" and tiers[0][1] == "jev_learned:same_action" and tiers[0][2] == pytest.approx(.93)
    assert tiers[-1][0] == "learned"


def test_a_pick_is_rendered_only_with_this_calls_values_and_learned_with_placeholders(monkeypatch):
    learn("drivectl put docs/plan.pdf", "Uploads {path1_name} to the team drive.")
    fake_jev(monkeypatch, first_learned)
    with ToolExplanations(translate=no_model, debounce=.001) as service:
        [result] = settle(service, [item(bash("drivectl put --quiet notes/todo.md"))])
    assert result["text"] == "Likely uploads todo.md to the team drive."
    stored = [row[0] for row in conn().execute("SELECT template_text FROM tool_explanation_learned")]
    # Privacy: learned rows hold placeholders, never a value of either call.
    assert "Likely uploads {path1_name} to the team drive." in stored
    assert not any(value in text for text in stored for value in ("todo", "notes/", "plan.pdf"))
    ledger = " ".join(str(row) for row in conn().execute("SELECT * FROM tool_explanation_decisions"))
    assert "todo" not in ledger and "notes/" not in ledger


def test_candidates_this_call_cannot_fill_are_never_offered(monkeypatch):
    learn("docker logs web-1a2b", "Shows the logs of the {id1} container.")
    seen = fake_jev(monkeypatch, first_learned)
    translate, calls = templated("Lists running containers.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        [result] = settle(service, [item(bash("docker ps"))])
    # `docker ps` has no {id1}: nothing to offer, so Jev is not asked.
    assert seen == [] and len(calls) == 1 and result["source"] == "llm"
    assert decisions()[-1][:2] == ("llm", "mutating_program")


def test_any_program_in_the_table_may_be_offered_when_it_shares_words(monkeypatch):
    learn("service restart web-1a2b", "Restarts the {id1} service.")
    learn("drivectl put docs/plan.pdf", "Uploads {path1_name} to the team drive.")
    seen = fake_jev(monkeypatch, first_learned)
    with ToolExplanations(translate=no_model, debounce=.001) as service:
        [result] = settle(service, [item(bash("systemctl restart api-9f3c"))])
    # A mutating call of another program: offered the closest learned rows of the
    # whole table that it can fill, never a template.
    offered = seen[0]["questions"]["t_1"]["criteria"]
    assert offered == {"learned_1": "A call that does this: Restarts the api-9f3c service.",
                       "unknown": "None of these states exactly what the call does, or its effect is unclear"}
    assert result["text"] == "Likely restarts the api-9f3c service."
    assert decisions()[-1][:2] == ("jev", "jev_learned:lexical")


def test_exact_only_rows_are_offered_unless_they_state_values_the_call_lacks(monkeypatch):
    learn("sqlite3 state.db 'select count(*) from agents' | tee /var/tmp/a", "Counts agents in the state database.")
    learn("sqlite3 state.db 'select * from agents limit 100' | tee /var/tmp/b", "Shows the first 100 agents.")
    seen = fake_jev(monkeypatch, first_learned)
    with ToolExplanations(translate=no_model, debounce=.001) as service:
        [result] = settle(service, [item(bash("sqlite3 state.db 'select count(*) from agents where x' | tee /var/tmp/c"))])
    offered = list(seen[0]["questions"]["t_1"]["criteria"].values())
    assert offered[0] == "A `sqlite3` call that does this: Counts agents in the state database."
    assert not any("100" in text for text in offered)
    assert result["text"] == "Likely counts agents in the state database."
    # An exact part learns its one identical text, exactly.
    [part] = shapes.split(bash("sqlite3 state.db 'select count(*) from agents where x' | tee /var/tmp/c"))
    row = conn().execute("SELECT producer, parameterised, program FROM tool_explanation_learned WHERE signature=?",
                         (part.signature,)).fetchone()
    assert tuple(row) == ("jev", 0, "sqlite3")


def test_learned_options_come_before_the_built_in_templates(monkeypatch):
    learn("mycli show conf/config.yaml", "Shows the settings in {path1_name}.")
    seen = fake_jev(monkeypatch, lambda criteria: "unknown")
    translate, _ = templated("Shows {path1_name}.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        settle(service, [item(bash("mycli show --raw conf/other.yaml"))])
    keys = list(seen[0]["questions"]["t_1"]["criteria"])
    assert keys[0] == "learned_1" and "read_file" in keys and keys[-1] == "unknown"


def test_an_unfamiliar_program_with_no_template_or_learned_candidate_is_not_asked(monkeypatch):
    seen = fake_jev(monkeypatch, first_learned)
    translate, calls = templated("Pings {url1_host}.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        # A URL is not a usable file argument, so no template renders either.
        settle(service, [item(bash("pingctl https://example.org/x"))])
    assert seen == [] and len(calls) == 1
    assert decisions()[-1][:2] == ("llm", "jev_unsafe_arguments")


def test_an_unfamiliar_program_may_be_offered_another_programs_explanation_by_shared_words(monkeypatch):
    learn("widgetctl inventory export --format csv", "Exports the widget inventory as a CSV file.")
    seen = fake_jev(monkeypatch, lambda criteria: "unknown")
    translate, _ = templated("Exports the gadget inventory.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        settle(service, [item(bash("gadgetctl inventory export"))])
    offered = seen[0]["questions"]["t_1"]["criteria"]
    lexical = [text for key, text in offered.items() if key.startswith("learned_")]
    assert lexical == ["A call that does this: Exports the widget inventory as a CSV file."]
    assert decisions()[-1][:2] == ("llm", "jev_unknown")


def test_a_low_confidence_pick_answers_but_is_not_learned(monkeypatch):
    learn("git worktree list --porcelain", "Lists the git worktrees in machine-readable form.")
    fake_jev(monkeypatch, first_learned, p=.85)
    with ToolExplanations(translate=no_model, debounce=.001) as service:
        [result] = settle(service, [item(bash("git worktree list"))])
    assert result["source"] == "jev"
    assert conn().execute("SELECT count(*) FROM tool_explanation_learned WHERE producer='jev'").fetchone()[0] == 0


# ---- program of whole commands --------------------------------------------------

@pytest.mark.parametrize("command, program", [
    ("cd /x && python3 - <<'EOF'\nimport os\nEOF", "python3"),
    ("FOO=1 x=$(git rev-parse HEAD) && make build", "make"),
    ("for f in *.py; do wc -l $f; done", "wc"),
    ("export A=1; sqlite3 db.sqlite 'select 1' | tee out", "sqlite3"),
    ("echo $(date)", ""),
])
def test_an_opaque_command_names_its_first_real_program(command, program):
    [part] = shapes.split(bash(command))
    assert part.exact and part.program == program
    assert part.signature.startswith(f"x:{program or '?'} #")


def test_a_clipped_codex_label_names_its_program():
    label = '/usr/bin/bash -lc "sqlite3 ~/.local/share/clarp/state.sqlite \\"select count(*) from agents where'
    [part] = shapes.split({"name": label[:90]})
    assert part.exact and part.reason == "truncated" and part.program == "sqlite3"


def test_a_row_learned_under_the_old_programless_key_is_found_and_moved(monkeypatch):
    command = "cd /x && python3 - <<'EOF'\nprint(1)\nEOF"
    [part] = shapes.split(bash(command))
    [[legacy, current]] = part.legacy
    assert legacy.startswith("x:? #") and current == part.signature
    assert legacy.split(" #")[1] == part.signature.split(" #")[1]
    from lib import tool_explanations as module, tool_explanation_templates as templates
    learning.store(conn(), [{"signature": legacy, "level": 1, "template_text": "Prints a number.",
                             "parameterised": 0, "producer": "llm", "prompt_version": module.PROMPT_VERSION,
                             "templates_version": templates.VERSION}], 1)
    with ToolExplanations(translate=no_model, debounce=.001) as service:
        [result] = service.request(1, [item(bash(command))])["items"]
        assert result["text"] == "Prints a number."
    rows = [tuple(r) for r in conn().execute("SELECT signature, program, hits FROM tool_explanation_learned")]
    assert rows == [(part.signature, "python3", 1)]


def test_a_timeout_wrapped_call_is_its_programs_and_its_old_row_still_answers(monkeypatch):
    [part] = shapes.split(bash("timeout 600 dotnet test Api.Tests/Api.Tests.csproj -c Release"))
    assert part.program == "dotnet" and part.signature.startswith("sh:dotnet #")
    [[old_signature, new_signature], _] = part.legacy
    assert old_signature.startswith("sh:timeout #") and new_signature == part.signature
    from lib import tool_explanations as module, tool_explanation_templates as templates
    learning.store(conn(), [{"signature": old_signature, "level": 1, "program": "timeout",
                             "template_text": "Runs the tests in {path1_name} with a time limit.",
                             "slot_names": '["path1_name"]', "producer": "llm",
                             "prompt_version": module.PROMPT_VERSION, "templates_version": templates.VERSION}], 1)
    with ToolExplanations(translate=no_model, debounce=.001) as service:
        [result] = service.request(1, [item(bash("timeout 60 dotnet test Web.Tests/Web.Tests.csproj -c Release"))])["items"]
        assert result["text"] == "Runs the tests in Web.Tests.csproj with a time limit."
    assert [tuple(r) for r in conn().execute("SELECT signature, program FROM tool_explanation_learned")] == [
        (part.signature, "dotnet")]


def test_old_programless_rows_are_recovered_once_from_the_hosts_own_tool_rows():
    from lib import tool_explanations as module, tool_explanation_templates as templates
    command = "cd /x && python3 - <<'EOF'\nprint(1)\nEOF"
    [part] = shapes.split(bash(command))
    [[legacy, current]] = part.legacy
    learning.store(conn(), [{"signature": legacy, "level": 2, "template_text": "Prints a number.", "parameterised": 0,
                             "producer": "llm", "prompt_version": module.PROMPT_VERSION,
                             "templates_version": templates.VERSION},
                            {"signature": "x:? #unmatched", "level": 2, "template_text": "Something.",
                             "parameterised": 0, "producer": "llm", "prompt_version": module.PROMPT_VERSION,
                             "templates_version": templates.VERSION}], 1)
    import json
    conn().execute("INSERT INTO messages(message_id, agent_id, seq, text, updated_at, tools_json) "
                   "VALUES('m1', 'a1', 1, '', 5, ?)", (json.dumps([{"name": "Bash", "command": command}]),))
    assert module.recover_programs() == 1
    rows = {tuple(r) for r in conn().execute("SELECT signature, program FROM tool_explanation_learned")}
    assert rows == {(current, "python3"), ("x:? #unmatched", "")}
    # Only the key and program changed: no command text was written anywhere.
    assert "print(1)" not in str(list(conn().execute("SELECT * FROM tool_explanation_learned")))
    assert module.recover_programs() == 0


def test_backfill_names_the_program_a_signature_carries():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE tool_explanation_learned (signature TEXT, level INTEGER, template_text TEXT, "
                       "program TEXT NOT NULL DEFAULT '', PRIMARY KEY (signature, level))")
    for signature in ("sh:git #aa", "x:jq #bb", "x:? #cc", "tool:exploration #dd"):
        connection.execute("INSERT INTO tool_explanation_learned(signature, level, template_text) VALUES(?, 1, 't')",
                           (signature,))
    db_migrations._migrate_to_v99(connection)
    rows = dict(connection.execute("SELECT signature, program FROM tool_explanation_learned"))
    assert rows == {"sh:git #aa": "git", "x:jq #bb": "jq", "x:? #cc": "", "tool:exploration #dd": "tool:exploration"}
    assert "source_signature" in {r[1] for r in connection.execute("PRAGMA table_info(tool_explanation_learned)")}


def test_rank_orders_same_action_then_same_program_then_shared_words():
    def row(signature, program, action, text, slots=(), hits=0):
        return {"signature": signature, "program": program, "action": action, "template_text": text,
                "slot_names": list(slots), "hits": hits}
    rows = [row("a", "tool", "unknown", "Deletes the widget cache."),
            row("b", "tool", "execute", "Rebuilds the widget cache.", hits=9),
            row("c", "tool", "execute", "Uploads {path1_name}.", slots=["path1_name"]),
            row("d", "other", "execute", "Rebuilds widget cache indexes."),
            row("e", "other", "execute", "Prints the date."),
            row("f", "tool", "execute", "Rebuilds the widget cache.")]
    ranked = learning.rank(rows, program="tool", action="execute", tokens=learning.words("tool rebuild widget cache"))
    assert [r["signature"] for r in ranked] == ["b", "a", "d"]
    assert [r["similarity"] for r in ranked] == ["same_action", "same_program", "lexical"]
    ranked = learning.rank(rows, program="tool", action="execute", exclude={"b"}, available={"path1_name"})
    assert [r["signature"] for r in ranked] == ["c", "f", "a"]


def test_a_value_counts_as_present_only_as_a_whole_token():
    assert learning.foreign("Merges pull request 6.", "gh pr merge 16")
    assert not learning.foreign("Merges pull request 6.", "gh pr merge 6 --squash")
    assert learning.foreign("Stops the sub-agent oracle-mike-call.", "clarp-sub-agent stop oracle-mike")
    assert not learning.foreign("Shows machine-readable output of {path1_name}.", "tool --porcelain a/b")
