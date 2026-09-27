"""Which calls are put to Jev, with what options, and which picks are accepted.

The activities are the shapes the live Host sent Jev on 2026-09-27, when every
one of its answers fell back to the model: Codex rows whose tool name is a
clipped `/usr/bin/bash -lc` label, status rows, grouped exploration rows,
shell builtins, and unknown programs whose candidates included shell
punctuation and `$` expressions. Paths and names are replaced; no command is
run, `judgments._post` is a fake and the model is the `translate` hook.
"""
import json
import time

import pytest

from lib import janitor_builtins, judgments, settings_store
from lib import tool_explanation_learning as learning
from lib import tool_explanation_templates as templates
from lib.db import conn
from lib.tool_explanations import PROMPT_VERSION, ToolExplanations


@pytest.fixture(autouse=True)
def configured_explainer():
    janitor_builtins.ensure_builtins(cwd="/tmp")
    judgments.reset_breaker()
    yield
    judgments.reset_breaker()


def bash(command):
    return {"name": "Bash", "command": command, "input": {"command": command}}


def label(command):
    """A Codex row as it reaches the Host: only the clipped display label."""
    name = f'/usr/bin/bash -lc "{command}'[:templates.LABEL_CLIP]
    return {"name": name, "summary": f"Using {name}"}


def enable_jev(monkeypatch, answers):
    monkeypatch.setattr(judgments, "api_key", lambda: "test-key")
    settings_store.set_bool(judgments.KEY_ENABLED, True)
    settings_store.set_bool("judgments.explanations", True)
    seen = []

    def post(body, key, seconds):
        seen.append(body)
        return {"answers": answers(body["questions"]), "usage": {}}
    monkeypatch.setattr(judgments, "_post", post)
    return seen


def pick(choice, argument=None, p=.93):
    def answers(questions):
        out = {}
        for qid in questions:
            if qid.startswith("t_"):
                out[qid] = {"choice": choice, "probabilities": {choice: p}, "confidence": p}
            else:
                out[qid] = {"choice": argument, "probabilities": {argument: .9}, "confidence": .9}
        return out
    return answers


def questions_for(seen, command):
    """The questions Jev was asked about the activity running `command`, by kind."""
    for body in seen:
        for key, activity in body["state"]["activities"].items():
            if activity.get("command") == command:
                return {qid[0]: q for qid, q in body["questions"].items() if qid[2:] == key}
    pytest.fail(f"{command!r} was not put to Jev")


def settle(service, activities, level=2):
    items = [{"id": str(i), "activity": a} for i, a in enumerate(activities)]
    for _ in range(300):
        result = service.request(level, items, include_provenance=True)["items"]
        if all(entry["status"] != "pending" for entry in result):
            return result
        time.sleep(.01)
    pytest.fail("worker did not finish")


def model(level, items):
    return {i["id"]: "Model explains it." for i in items}


def reason(entry):
    return entry["provenance"].get("fallback_reason", "")


def ledger_reasons():
    return [row[0] for row in conn().execute("SELECT reason FROM tool_explanation_decisions ORDER BY id")]


# ---- not asked ------------------------------------------------------------

def test_clipped_shell_labels_are_not_put_to_jev(monkeypatch):
    seen = enable_jev(monkeypatch, pick("read_file"))
    rows = [label("tail -12 lab/relay-trace/socket-candidate/direct-" + "x" * 40),
            label("cat /home/u/dotfiles/skills/self-prompt/SKILL.md\ncat /home/u/dotfiles/skills/other.md"),
            label("sed -n '346,373p' /home/u/.local/share/app/current/lib/module_with_long_name.py"),
            label("python - <<'PY'\nimport sqlite3\nc=sqlite3.connect('file:/home/u/state.sqlite')")]
    assert all(len(row["name"]) == templates.LABEL_CLIP for row in rows)
    with ToolExplanations(translate=model, debounce=.001) as service:
        result = settle(service, rows)
    # Jev read_file picks on these were rejected as invalid parameters, or were
    # guesses about a command whose end was cut off.
    assert seen == []
    assert [entry["source"] for entry in result] == ["llm"] * 4
    assert {reason(entry) for entry in result} == {"truncated"}
    assert all(r.startswith("truncated") for r in ledger_reasons())


def test_a_complete_shell_label_is_explained_as_the_command_it_names(monkeypatch):
    seen = enable_jev(monkeypatch, pick("unknown"))
    with ToolExplanations(translate=lambda *_: pytest.fail("scripted"), debounce=.001) as service:
        result = settle(service, [{"name": "/usr/bin/bash -lc 'cat docs/readme.md'",
                                   "summary": "Using /usr/bin/bash -lc 'cat docs/readme.md'"}])[0]
    assert result["source"] == "scripted" and result["text"] == "Reads readme.md to see what it contains."
    assert seen == []


def test_status_and_delegation_rows_are_not_put_to_jev(monkeypatch):
    seen = enable_jev(monkeypatch, pick("read_file"))
    rows = [{"name": "done", "summary": "Done"}, {"name": "idle", "summary": "Idle"},
            {"name": "running command", "summary": "Make the new-session hub lazy and run its test"},
            {"kind": "status", "summary": "background command"},
            {"kind": "subagents", "summary": "Map voice pipeline latency",
             "operations": ["Task: Read-only, very thorough.", "Type: Explore", "Mode: Foreground"]}]
    with ToolExplanations(translate=model, debounce=.001) as service:
        result = settle(service, rows)
    assert seen == []
    assert [reason(entry) for entry in result] == ["jev_no_target"] * 5


def test_shell_builtins_are_not_put_to_jev(monkeypatch):
    seen = enable_jev(monkeypatch, pick("read_file"))
    with ToolExplanations(translate=model, debounce=.001) as service:
        result = settle(service, [bash("export APP_SESSION=$PWA_SESSION"), bash("[ -f build.log ]"),
                                  bash("echo ready"), bash("cd src")])
    assert seen == []
    assert [reason(entry) for entry in result] == ["shell_builtin"] * 4


def test_a_single_exploration_read_is_a_template_and_several_go_to_the_model(monkeypatch):
    seen = enable_jev(monkeypatch, pick("read_file"))
    one = {"kind": "exploration", "operations": ["Read: /home/u/proj/docs/architecture/rebuild.md",
                                                 "Read: /home/u/proj/docs/architecture/rebuild.md"]}
    several = {"kind": "exploration", "operations": [
        "Read: /home/u/proj/docs/architecture/rebuild.md, /home/u/qa/state.json"]}
    search = {"kind": "exploration", "operations": ["Search: def schedule in server/lib"]}
    with ToolExplanations(translate=model, debounce=.001) as service:
        result = settle(service, [one, several, search])
    assert result[0]["source"] == "scripted" and result[0]["text"] == "Reads rebuild.md to see what it contains."
    assert result[0]["provenance"]["parameters"] == {"file": "/home/u/proj/docs/architecture/rebuild.md"}
    assert [reason(entry) for entry in result[1:]] == ["exploration", "exploration"]
    assert seen == []


# ---- what is offered ------------------------------------------------------

def test_shell_punctuation_variables_flags_and_free_text_are_never_candidates(monkeypatch):
    seen = enable_jev(monkeypatch, pick("list_directory", argument="arg2"))
    with ToolExplanations(translate=lambda *_: pytest.fail("Jev answered"), debounce=.001) as service:
        result = settle(service, [bash('lsd "$f" ] --depth 2 docs notes "a message with spaces"')], level=1)[0]
    options = questions_for(seen, 'lsd "$f" ] --depth 2 docs notes "a message with spaces"')["a"]["criteria"]
    assert options == {"arg1": "The argument `docs`", "arg2": "The argument `notes`",
                       "none": "It does not act on one of these arguments"}
    assert result["text"] == "Likely lists files and subdirectories in `notes`."
    # The stored index points into the call's own arguments, so the learned
    # rule fills the same position next time.
    assert result["provenance"]["parameters"] == {"directory": "notes"}


def test_only_templates_that_can_render_from_the_call_are_offered(monkeypatch):
    seen = enable_jev(monkeypatch, pick("unknown"))
    with ToolExplanations(translate=model, debounce=.001) as service:
        settle(service, [bash("date -Is"), bash("tokei src"), bash("jq . package.json")], level=2)
    no_argument = set(questions_for(seen, "date -Is")["t"]["criteria"])
    with_argument = set(questions_for(seen, "tokei src")["t"]["criteria"])
    # Nothing to read or search: only what is true of the current directory
    # or needs no value. A search needs a pattern this call cannot supply.
    assert "read_file" not in no_argument and "search_text" not in no_argument
    assert {"list_directory", "find_files", "show_current_directory", "unknown"} <= no_argument
    assert "read_file" in with_argument and "search_text" not in with_argument
    # `.` cannot name a file at this audience, but `package.json` can.
    assert "read_file" in questions_for(seen, "jq . package.json")["t"]["criteria"]
    assert not {"delete_path", "git_push", "run_tests"} & (no_argument | with_argument)


def test_learned_options_belong_to_their_own_program(monkeypatch):
    learning.store(conn(), [{"signature": "sh:tig #seeded", "level": 2, "template_text": "Shows the history of {path1_name}.",
                             "slot_names": json.dumps(["path1_name"]), "program": "tig", "producer": "llm",
                             "prompt_version": PROMPT_VERSION, "templates_version": templates.VERSION}], 0)
    def answers(questions):
        return {qid: {"choice": choice, "probabilities": {choice: .93}, "confidence": .93}
                for qid, q in questions.items() for choice in ["learned_1" if "learned_1" in q["criteria"] else "unknown"]}
    seen = enable_jev(monkeypatch, answers)
    with ToolExplanations(translate=model, debounce=.001) as service:
        result = settle(service, [bash("tig blame docs/plan.md"), bash("tokei docs/plan.md")])
    assert questions_for(seen, "tig blame docs/plan.md")["t"]["criteria"]["learned_1"].startswith("A `tig` call")
    assert not any(k.startswith("learned_") for k in questions_for(seen, "tokei docs/plan.md")["t"]["criteria"])
    assert result[0]["source"] == "jev" and result[0]["text"] == "Likely shows the history of plan.md."


# ---- accepted where it used to be rejected -------------------------------

def test_a_template_without_a_path_is_accepted_when_the_call_has_arguments(monkeypatch):
    # Before: the lone argument was forced on git_status, which takes none,
    # and the pick was rejected as invalid parameters.
    seen = enable_jev(monkeypatch, pick("git_status"))
    with ToolExplanations(translate=lambda *_: pytest.fail("Jev answered"), debounce=.001) as service:
        result = settle(service, [bash("tig status")])[0]
    assert "a" not in questions_for(seen, "tig status")
    assert result["source"] == "jev" and result["text"] == "Likely checks which files have changed in the repository."
    assert "parameters" in result["provenance"] and result["provenance"]["parameters"] == {}


def test_an_unknown_tool_naming_a_file_is_read_at_every_audience(monkeypatch):
    enable_jev(monkeypatch, pick("read_file"))
    tool = {"name": "mcp__fs__read_text_file", "file_path": "docs/plan.md"}
    with ToolExplanations(translate=lambda *_: pytest.fail("Jev answered"), debounce=.001) as service:
        texts = [settle(service, [tool], level=level)[0]["text"] for level in (1, 2)]
    assert texts == ["Likely reads `docs/plan.md` without changing it.", "Likely reads plan.md to see what it contains."]


def test_a_pick_never_carries_a_parameter_the_call_does_not_contain(monkeypatch):
    enable_jev(monkeypatch, pick("list_directory", argument="arg9"))
    with ToolExplanations(translate=model, debounce=.001) as service:
        result = settle(service, [bash("lsd docs notes")], level=1)[0]
    assert result["source"] == "llm" and reason(result) == "jev_no_argument"
