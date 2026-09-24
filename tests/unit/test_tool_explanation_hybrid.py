"""Scripted templates, then Jev among known templates, then the language model.

Every input is synthetic and nothing here runs the described command: the
scripted path only parses metadata, `judgments._post` is a fake, and the model
is the `translate` hook. The real queue, cache, Janitor admission and SQLite
schema are used throughout.
"""
import time

import pytest

from lib import janitor_builtins, janitors, judgments, settings_store
from lib import tool_explanation_mappings as mappings
from lib import tool_explanation_templates as templates
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


def item(activity, ident="1", demand=None):
    return {"id": ident, "activity": activity, **({"demand_id": demand} if demand else {})}


def enable_jev(monkeypatch, answers):
    """Fake Jev. `answers(questions)` returns the per-question answer map."""
    monkeypatch.setattr(judgments, "api_key", lambda: "test-key")
    settings_store.set_bool(judgments.KEY_ENABLED, True)
    settings_store.set_bool("judgments.explanations", True)
    seen = []

    def post(body, key, seconds):
        seen.append(body)
        return {"answers": answers(body["questions"]), "usage": {}}
    monkeypatch.setattr(judgments, "_post", post)
    return seen


def pick(template_id, p=.93, argument=None):
    def answers(questions):
        out = {}
        for qid in questions:
            if qid.startswith("t_"):
                out[qid] = {"choice": template_id, "probabilities": {template_id: p}, "confidence": p}
            else:
                out[qid] = {"choice": argument, "probabilities": {argument: .9}, "confidence": .9}
        return out
    return answers


def settle(service, items, level=1, **kw):
    for _ in range(300):
        result = service.request(level, items, **kw)["items"]
        if all(entry["status"] != "pending" for entry in result):
            return result
        time.sleep(.01)
    pytest.fail("worker did not finish")


def set_sources(value):
    config = janitor_builtins.get_builtin("tool-explainer")
    config = janitors.configure(config["session"], config["revision"], options={"explanation_sources": value})
    janitors.set_enabled(config["session"], config["revision"], True)


# ---- scripted -------------------------------------------------------------

@pytest.mark.parametrize("case", templates.REGRESSION, ids=lambda c: str(c["activity"])[:60])
def test_regression_cases_match_or_abstain_exactly(case):
    text, route = templates.lookup(case["activity"], 1)
    if "abstain" in case:
        assert text is None and route["reason"] == case["abstain"]
    else:
        assert route["template_id"] == case["template"] and route["parameters"] == case["parameters"]
        for level in range(1, 5):
            rendered = templates.lookup(case["activity"], level)[0]
            assert rendered and len(rendered) <= 240


def test_every_template_renders_every_audience_without_parameters_or_with_them():
    for template_id, template in templates.TEMPLATES.items():
        for level in range(1, 5):
            full = {key: "src/app" if kind in {"directory", "path"} else "example.org" if kind == "host" else "main"
                    for key, kind in template["params"].items()}
            assert templates.render(template_id, full, level), (template_id, level)


def test_project_is_a_parameter_only_when_the_input_names_that_directory():
    assert templates.lookup(bash("ls -la ~/GIT/clarp"), 3)[0] == "Looks at which files are in the clarp folder."
    assert templates.lookup(bash("ls"), 3)[0] == "Looks at which files are in a folder."
    # Grandma never sees file or project names.
    assert "clarp" not in templates.lookup(bash("ls ~/GIT/clarp"), 4)[0]


def test_parameters_must_literally_come_from_the_input_and_pass_type_checks():
    activity = bash("ls ~/GIT/clarp")
    assert templates.validate("list_directory", {"directory": "~/GIT/clarp"}, activity)
    assert templates.validate("list_directory", {"directory": "~/GIT/other"}, activity) is None
    assert templates.validate("list_directory", {"directory": "a;b"}, bash("ls 'a;b'")) is None
    assert templates.validate("list_directory", {"unknown": "x"}, activity) is None
    assert templates.validate("no_such_template", {}, activity) is None
    assert templates.lookup(bash("ls $HOME/private"), 1) == (None, {"reason": "invalid_parameters", "action": "list", "signature": "ls"})


def test_read_search_edit_delete_network_and_compound_are_distinguished():
    kinds = {command: templates.classify(bash(command)).get("action") for command in [
        "cat notes.md", "rg -n TODO src", "sed -i 's/a/b/' app.py", "rm -rf build", "curl -s https://example.org/x",
        "ls && rm -rf build", "find . -name '*.pyc' -delete", "curl -d @data.json https://example.org", "git reset --hard"]}
    assert kinds == {"cat notes.md": "read", "rg -n TODO src": "search", "sed -i 's/a/b/' app.py": "edit",
                     "rm -rf build": "delete", "curl -s https://example.org/x": "network", "ls && rm -rf build": "compound",
                     "find . -name '*.pyc' -delete": "compound", "curl -d @data.json https://example.org": "network",
                     "git reset --hard": "unknown"}
    assert templates.classify(bash("rm -rf build"))["template_id"] == "delete_recursive"
    assert templates.classify(bash("rm build.log"))["template_id"] == "delete_path"
    assert "template_id" not in templates.classify(bash("git reset --hard"))


def test_benign_output_limits_and_cd_prefix_keep_a_single_command():
    assert templates.classify(bash("rg foo src 2>/dev/null | head -20"))["template_id"] == "search_text"
    assert templates.classify(bash("bash -lc 'cd /repo && git status'"))["template_id"] == "git_status"
    assert templates.classify(bash("rg foo > out.txt"))["reason"] == "compound"
    assert templates.classify(bash("echo $(cat secret)"))["reason"] == "compound"


def test_known_program_signature_masks_flag_values_that_could_hold_secrets():
    assert templates.classify(bash("cat -pHunter2Secret notes.md"))["signature"] == "cat -?"


def test_exact_match_bypasses_the_queue_and_model_and_reports_scripted():
    with ToolExplanations(translate=lambda *_: pytest.fail("scripted must not call the model"), debounce=.001) as service:
        plain = service.request(1, [item(bash("ls -la ~/GIT/clarp"))])["items"][0]
        assert plain == {"id": "1", "status": "ready", "text": "Lists files and subdirectories in `~/GIT/clarp`.",
                         "source": "scripted", "cached": False}
        debug = service.request(1, [item(bash("ls -la ~/GIT/clarp"))], include_provenance=True)["items"][0]
        assert debug["provenance"] == {"template_version": templates.VERSION, "template_id": "list_directory",
                                       "parameters": {"directory": "~/GIT/clarp"}, "confidence": 1.0}
    assert conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0


def test_scripted_honours_developer_level_release_and_model_only_configuration():
    calls = []
    with ToolExplanations(translate=lambda level, items: calls.append(items) or {i["id"]: "Model text." for i in items},
                          debounce=.001) as service:
        assert service.request(0, [item(bash("ls"))])["items"][0]["status"] == "disabled"
        released = service.request(1, [item(bash("ls"), demand="view-1")], release=["view-1"])["items"][0]
        assert released["status"] == "cancelled"
        set_sources(2)
        result = settle(service, [item(bash("ls"))])[0]
        assert result.get("source") == "llm", result
    assert result["text"] == "Model text." and len(calls) == 1


def test_mixed_batch_keeps_order_and_only_unknown_work_reaches_the_model():
    seen = []
    with ToolExplanations(translate=lambda level, items: seen.extend(items) or {i["id"]: "Runs the grocery script." for i in items},
                          debounce=.001) as service:
        result = settle(service, [item(bash("python task_47.py"), "a"), item(bash("git status"), "b")], include_provenance=True)
    assert [entry["id"] for entry in result] == ["a", "b"]
    assert [entry["source"] for entry in result] == ["llm", "scripted"]
    assert result[0]["provenance"]["fallback_reason"] == "script_run"
    assert [s["activity"]["command"] for s in seen] == ["python task_47.py"]


# ---- Jev ------------------------------------------------------------------

def test_paraphrased_program_maps_through_jev_without_the_model(monkeypatch):
    seen = enable_jev(monkeypatch, pick("list_directory"))
    with ToolExplanations(translate=lambda *_: pytest.fail("Jev answered"), debounce=.001) as service:
        first = settle(service, [item(bash("eza -la src"))], include_provenance=True)[0]
        again = service.request(1, [item(bash("eza -la src"))], include_provenance=True)["items"][0]
    # Jev never saw this program, so its pick is worded as the likely effect.
    assert first["text"] == "Likely lists files and subdirectories in `src`." and first["source"] == "jev"
    assert first["provenance"]["template_id"] == "list_directory"
    assert first["provenance"]["parameters"] == {"directory": "src"}
    assert first["provenance"]["confidence"] == .93 and first["provenance"]["fallback_reason"] == "unknown_program"
    assert again["cached"] is True and again["source"] == "jev" and again["provenance"]["parameters"] == {"directory": "src"}
    # Only read/list/search templates are offered, plus unknown, and scripts never leave.
    criteria = seen[0]["questions"]["t_1"]["criteria"]
    assert "unknown" in criteria and "delete_path" not in criteria and "git_push" not in criteria
    assert "scripts" not in seen[0]["state"]["activities"]["1"]
    # The cache persists which candidate was chosen, not the value itself.
    stored = conn().execute("SELECT provenance_json FROM tool_explanation_cache").fetchone()[0]
    assert "src" not in stored


def test_jev_picks_the_argument_among_several_literals(monkeypatch):
    enable_jev(monkeypatch, pick("list_directory", argument="arg2"))
    with ToolExplanations(translate=lambda *_: pytest.fail("Jev answered"), debounce=.001) as service:
        result = settle(service, [item(bash("lsd --depth 2 docs"))], include_provenance=True)[0]
    assert result["text"] == "Likely lists files and subdirectories in `docs`."
    assert result["provenance"]["parameters"] == {"directory": "docs"}


@pytest.mark.parametrize("answers,reason", [
    (pick("unknown"), "jev_unknown"),
    (pick("list_directory", p=.55), "jev_low_confidence"),
    (pick("delete_path"), "jev_low_confidence"),            # not an offered template
    (pick("list_directory", argument="none"), "jev_invalid_parameters"),
])
def test_jev_abstention_falls_back_to_the_model(monkeypatch, answers, reason):
    enable_jev(monkeypatch, answers)
    with ToolExplanations(translate=lambda level, items: {i["id"]: "Model explains it." for i in items}, debounce=.001) as service:
        result = settle(service, [item(bash("lsd --depth 2 docs"))], include_provenance=True)[0]
    assert result["source"] == "llm" and result["provenance"]["fallback_reason"] == reason


def test_jev_unavailable_disabled_or_invalid_parameters_fall_back(monkeypatch):
    with ToolExplanations(translate=lambda level, items: {i["id"]: "Model." for i in items}, debounce=.001) as service:
        disabled = settle(service, [item(bash("eza src"))], include_provenance=True)[0]
        assert disabled["provenance"]["fallback_reason"] == "jev_disabled"

        def broken(*_):
            raise RuntimeError("provider down")
        enable_jev(monkeypatch, pick("list_directory"))
        monkeypatch.setattr(judgments, "_post", broken)
        down = settle(service, [item(bash("eza docs"))], include_provenance=True)[0]
        assert down["source"] == "llm" and down["provenance"]["fallback_reason"] == "jev_unavailable"

        enable_jev(monkeypatch, pick("list_directory"))
        unsafe = settle(service, [item(bash("eza 'a;b'"))], include_provenance=True)[0]
        assert unsafe["provenance"]["fallback_reason"] == "jev_invalid_parameters"


def test_compound_destructive_and_script_runs_never_reach_jev(monkeypatch):
    seen = enable_jev(monkeypatch, pick("list_directory"))
    commands = ["ls && rm -rf build", "shred -u secrets.txt", "python task_47.py", "sudo ls /root", "echo 'broken"]
    with ToolExplanations(translate=lambda level, items: {i["id"]: "Model." for i in items}, debounce=.001) as service:
        result = settle(service, [item(bash(c), str(i)) for i, c in enumerate(commands)], include_provenance=True)
    assert seen == []
    assert [r["provenance"]["fallback_reason"] for r in result] == ["compound", "mutating_program", "script_run", "privileged", "malformed"]


def test_scripted_only_configuration_skips_jev(monkeypatch):
    seen = enable_jev(monkeypatch, pick("list_directory"))
    set_sources(1)
    with ToolExplanations(translate=lambda level, items: {i["id"]: "Model." for i in items}, debounce=.001) as service:
        assert settle(service, [item(bash("eza src"))])[0]["source"] == "llm"
        assert service.request(1, [item(bash("ls src"))])["items"][0]["source"] == "scripted"
    assert seen == []


# ---- cache provenance and invalidation ------------------------------------

def test_model_cache_hit_keeps_producer_and_library_version_invalidates(monkeypatch):
    calls = []
    with ToolExplanations(translate=lambda level, items: calls.append(1) or {i["id"]: "Model." for i in items},
                          debounce=.001) as service:
        first = settle(service, [item(bash("python job.py"))])[0]
        hit = service.request(1, [item(bash("python job.py"))])["items"][0]
        assert (first["source"], first.get("cached")) == ("llm", True) and hit["cached"] is True and len(calls) == 1
        monkeypatch.setattr(templates, "VERSION", templates.VERSION + 1)
        assert service.request(1, [item(bash("python job.py"))])["items"][0]["status"] == "pending"


def test_legacy_cache_rows_report_the_model_as_producer():
    conn().execute("INSERT INTO tool_explanation_cache(cache_key,explanation,created_at,expires_at) VALUES('k','Old.',1,9999999999999)")
    assert conn().execute("SELECT source, provenance_json FROM tool_explanation_cache WHERE cache_key='k'").fetchone()[:] == ("llm", "{}")


# ---- learning -------------------------------------------------------------

def route_for(command):
    return templates.classify(bash(command))


def propose(command_template, directories=("a", "b", "c"), template_id="list_directory"):
    for directory in directories:
        command = command_template.format(directory)
        status = mappings.record(route_for(command), template_id, .95, bash(command))
    return status


def test_agreement_only_proposes_and_never_changes_an_explanation(monkeypatch):
    enable_jev(monkeypatch, pick("list_directory", p=.97))
    with ToolExplanations(translate=lambda *_: pytest.fail("Jev answered"), debounce=.001) as service:
        for directory in ["src", "docs", "tests", "server"]:
            result = settle(service, [item(bash(f"eza -la {directory}"))])[0]
            assert result["source"] == "jev" and result["text"].startswith("Likely ")
    signature = route_for("eza -la src")["signature"]
    assert [(row["signature"], row["status"]) for row in mappings.listing()] == [(signature, "proposed")]
    assert mappings.promoted() == {}


def test_vetted_approval_makes_a_rule_for_one_exact_shape_and_invalidates(monkeypatch):
    enable_jev(monkeypatch, pick("list_directory", p=.97))
    with ToolExplanations(translate=lambda *_: pytest.fail("Jev answered"), debounce=.001) as service:
        for directory in ["src", "docs", "tests"]:
            settle(service, [item(bash(f"eza -la {directory}"))])
        signature = route_for("eza -la src")["signature"]
        assert conn().execute("SELECT count(*) FROM tool_explanation_cache WHERE signature=?", (signature,)).fetchone()[0] == 3
        with pytest.raises(ValueError):
            mappings.approve(signature, "list_directory", reviewer="")
        with pytest.raises(ValueError):
            mappings.approve(signature, "find_files", reviewer="peter")
        mappings.approve(signature, "list_directory", reviewer="peter")
        assert mappings.promoted() == {signature: "list_directory"}
        assert conn().execute("SELECT count(*) FROM tool_explanation_cache WHERE signature=?", (signature,)).fetchone()[0] == 0
        seen = enable_jev(monkeypatch, pick("unknown"))
        vetted = service.request(1, [item(bash("eza -la server"))], include_provenance=True)["items"][0]
        assert seen == []
        assert vetted["source"] == "scripted" and vetted["provenance"]["learned"] is True
        assert vetted["text"] == "Lists files and subdirectories in `server`."
        # Another flag set, or a second positional, is another shape.
        assert service.request(1, [item(bash("eza --tree server"))])["items"][0]["status"] == "pending"
        assert service.request(1, [item(bash("eza -la server docs"))])["items"][0]["status"] == "pending"


def test_option_values_are_part_of_the_learned_identity():
    listing = route_for("filectl --mode=list /tmp/example")
    deleting = route_for("filectl --mode=delete /tmp/example")
    assert listing["candidates"] == deleting["candidates"] == ["/tmp/example"]
    assert listing["signature"] != deleting["signature"]
    assert propose("filectl --mode=list /tmp/{}") == "proposed"
    mappings.approve(listing["signature"], "list_directory", reviewer="peter")
    promoted = mappings.promoted()
    assert templates.lookup(bash("filectl --mode=list /tmp/example"), 2, promoted)[0] == "Looks at the files and folders inside example."
    text, route = templates.lookup(bash("filectl --mode=delete /tmp/example"), 2, promoted)
    assert text is None and route["reason"] == "unknown_program"
    # Subcommands and executable paths are identity too.
    assert route_for("filectl list /tmp/x")["signature"] != route_for("filectl delete /tmp/x")["signature"]
    assert route_for("./filectl --mode=list /tmp/x")["signature"] != route_for("/usr/bin/filectl --mode=list /tmp/x")["signature"]


def test_identity_is_a_keyed_hash_without_raw_values():
    route = route_for("mysql --password=Hunter2Secret db")
    assert "Hunter2" not in route["signature"] and route["signature"].startswith("mysql #")
    assert propose("mysql --password=Hunter2Secret {}") == "proposed"
    assert "Hunter2" not in str(mappings.listing())


def test_repeating_one_activity_is_not_independent_evidence():
    activity = bash("eza -la src")
    for _ in range(5):
        assert mappings.record(route_for("eza -la src"), "list_directory", .95, activity) == "candidate"
    assert mappings.promoted() == {}


@pytest.mark.parametrize("template_id,confidence,command,status", [
    ("delete_path", .99, "eza -la src", "rejected:unsafe_action"),
    ("git_push", .99, "eza -la src", "rejected:unsafe_action"),
    ("invented_template", .99, "eza -la src", "rejected:unknown_template"),
    ("list_directory", .85, "eza -la src", "rejected:low_confidence"),
    ("list_directory", .99, "ls && eza", "rejected:ineligible_route"),
    ("list_directory", .99, "shred -u x", "rejected:ineligible_route"),
])
def test_invalid_learning_is_rejected_without_seeding_a_candidate(template_id, confidence, command, status):
    assert mappings.record(route_for(command), template_id, confidence, bash(command)) == status
    assert mappings.listing() == []


def test_disagreement_blocks_proposal_permanently():
    mappings.record(route_for("eza src"), "list_directory", .95, bash("eza src"))
    assert mappings.record(route_for("eza docs"), "find_files", .95, bash("eza docs")) == "conflicted"
    assert propose("eza {}") == "conflicted"
    with pytest.raises(ValueError):
        mappings.approve(route_for("eza src")["signature"], "list_directory", reviewer="peter")


def test_proposal_and_approval_require_the_regression_cases_to_still_pass():
    # `srm -r old-backups` is a shipped case that must stay unmapped.
    assert propose("srm -r {}", ("old-backups", "x", "y")) == "rejected"
    row = mappings.listing()[0]
    assert row["reason"].startswith("regression:")
    with pytest.raises(ValueError):
        mappings.approve(row["signature"], "list_directory", reviewer="peter")
    assert mappings.promoted() == {}


def test_manual_rejection_withdraws_rule_and_its_cached_explanations():
    signature = route_for("eza a")["signature"]
    propose("eza {}")
    mappings.approve(signature, "list_directory", reviewer="peter")
    conn().execute("INSERT INTO tool_explanation_cache(cache_key,explanation,created_at,expires_at,signature) VALUES('k','x',1,9999999999999,?)", (signature,))
    mappings.reject(signature)
    assert mappings.promoted() == {}
    assert conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 0


def test_paused_janitor_disables_the_synchronous_path():
    config = janitor_builtins.get_builtin("tool-explainer")
    with ToolExplanations(translate=lambda *_: pytest.fail("paused"), debounce=.001) as service:
        assert service.request(1, [item(bash("ls"))])["items"][0]["status"] == "ready"
        janitors.set_enabled(config["session"], config["revision"], False)
        assert service.request(1, [item(bash("ls"))])["items"][0]["status"] == "disabled"


def test_secret_looking_patterns_are_never_parameters():
    with ToolExplanations(translate=lambda level, items: {i["id"]: "Model." for i in items}, debounce=.001) as service:
        result = settle(service, [item(bash("rg 'password=hunter2' src"))], include_provenance=True)[0]
    assert result["source"] == "llm" and result["provenance"]["fallback_reason"] == "invalid_parameters"
    assert "hunter2" not in str(result)


def test_secrets_never_become_parameters_or_provenance():
    command = "curl -s -H 'Authorization: Bearer sk-abcdefghijklmnopqrstuv' https://api.example.org/v1"
    from lib.tool_explanations import normalize_activity
    activity = normalize_activity(bash(command))
    text, route = templates.lookup(activity, 1)
    assert text == "Fetches a web resource from api.example.org." and "sk-" not in str(route)
    with ToolExplanations(translate=lambda *_: pytest.fail("scripted"), debounce=.001) as service:
        debug = service.request(1, [item(bash(command))], include_provenance=True)["items"][0]
    assert "abcdefghij" not in str(debug)
