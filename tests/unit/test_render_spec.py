"""Public contracts for native form publishing and one-time admission."""
import json
from pathlib import Path

import pytest

from lib import render_spec as spec


@pytest.fixture
def form():
    path = Path(__file__).parents[1] / "fixtures/render_spec/build-decision.json"
    return json.loads(path.read_text())


def refused(code, path):
    return pytest.raises(spec.Refusal, match="^" + code + "$", check=lambda e: e.path == path)


def test_spec_identity_covers_only_canonical_spec(form):
    assert spec.validate(form) is None
    identity = spec.content_identity(form["spec"], form["version"])
    assert identity["version"] == "build-1" and len(identity["sha256"]) == 67
    assert identity["bytes"] == len(spec.canonical(form["spec"]))
    changed = json.loads(json.dumps(form["spec"], sort_keys=True))
    assert spec.content_identity(changed, "build-1") == identity


def test_canonical_numbers_and_utf16_order():
    from tests.fixtures.render_spec.scenarios import WIRE
    for vector in WIRE["canonical"]:
        assert spec.canonical(vector["value"]).hex() == vector["utf8_hex"]
        assert spec.content_identity(vector["value"], "v1")["sha256"] == "c1:" + vector["sha256"]
    assert spec.CATALOG_SHA256 == "c1:" + WIRE["catalog_sha256"]
    for value in (1.0, 1e3, -0.0, 2**53, -(2**53)):
        with refused("number_form", "/n"):
            spec.canonical({"n": value})


@pytest.mark.parametrize("key", ["label", "option-value", "option-label"])
def test_choice_is_accessible_and_unselected_is_distinct_from_an_option(form, key):
    p = form["spec"]["elements"]["selection"]["props"]
    if key == "label":
        p["label"] = ""
    else:
        p["options"][0][key.split("-")[1]] = ""
    with pytest.raises(spec.Refusal) as failure:
        spec.validate(form)
    assert failure.value.code == "prop"


def test_validate_schema_before_accepting_state(form):
    form["answer_schema"]["properties"]["answer"]["pattern"] = "private-pattern"
    with refused("schema_keyword", "/answer_schema/properties/answer/pattern"):
        spec.validate(form)


@pytest.mark.parametrize("minimum,answer,accepted", [(1, "", False), (1, "ñ", True), (None, "", True)])
def test_required_text_and_minimum_length_have_separate_meanings(form, minimum, answer, accepted):
    form["answer_schema"]["required"].append("answer")
    if minimum is not None:
        form["answer_schema"]["properties"]["answer"]["minLength"] = minimum
    spec.validate(form)
    submission = {"version": "build-1", "submission_id": "1234567890abcdef",
                  "answers": {"intent": "submit_decision", "state": {"choice": "send", "answer": answer}}}
    if accepted:
        assert spec.admit("form", form, "ready", submission, None)["repeat"] is False
    else:
        with refused("state_value", "/answers/state/answer"):
            spec.admit("form", form, "ready", submission, None)


def test_same_id_retry_wins_over_completed_state(form):
    data = {"version": "build-1", "submission_id": "1234567890abcdef",
            "answers": {"intent": "submit_decision", "state": {"choice": "send"}}}
    admitted = spec.admit("form", form, "ready", data, None)
    prior = {"payload_hash": admitted["payload_hash"], "synthesize_audio": 1}
    assert spec.admit("form", form, "completed", data, prior)["repeat"]
    with refused("already_answered", ""):
        spec.admit("form", form, "completed", {**data, "submission_id": "fedcba0987654321"}, None)
    data["answers"]["state"]["choice"] = "review"
    with refused("reused_id", "/submission_id"):
        spec.admit("form", form, "completed", data, prior)


def test_legacy_projection_and_terminal_outbox(tmp_path, form):
    from lib import agents, artifacts, db, html_forms, render_spec_store
    agents.create_agent(persona="Synthetic", session="spec", voice_id="v", cwd=str(tmp_path))
    row = artifacts.create(session="spec", type="render_spec", title="Build", payload=form)
    ident = row["artifact_id"]
    for representation in ("", "summary-v1", "flat-v1"):
        old = artifacts.response_representation(row, representation)
        assert not {"spec", "payload", "content_omitted", "element_count", "answer_schema"} & old.keys()
        listing = artifacts.response_representation(row, representation, render_capable=True)
        opened = render_spec_store.open_current(ident)
        marker = listing["content_omitted"]
        assert marker["updated_at"] == row["updated_at"]
        assert opened["content_identity"] == {k:v for k,v in marker.items() if k != "updated_at"}
        assert opened["spec"] == form["spec"] and "content_omitted" not in opened
        assert "spec" not in listing and "content" not in listing
    body = {"version":"build-1", "answers":{"intent":"submit_decision","state":{"choice":"review"}},
            "submission_id":"a"*32}
    with refused("client_contract", ""):
        html_forms.submit(ident, body)
    receipt = html_forms.submit(ident, body, render_capable=True)
    assert html_forms.submit(ident, body, render_capable=True) == receipt
    assert artifacts.get(ident)["status"] == "completed"
    assert db.conn().execute("SELECT COUNT(*) FROM form_submissions").fetchone()[0] == 1
    [delivery] = html_forms.pending()
    assert "not approval to perform protected actions" in delivery["prompt"]
    html_forms.mark_delivered(body["submission_id"])
    opened = render_spec_store.open_current(ident)
    assert opened["submission"]["answers"] == body["answers"]
    assert opened["submission"]["delivery_status"] == "delivered"
    assert "submission" not in artifacts.get(ident)
    with refused("already_answered", ""):
        artifacts.update(ident, {"payload":form,"expected_version":"build-1","status":"active"})


def test_section_eight_pure_verdicts():
    import subprocess
    import sys
    generator = Path(__file__).parents[1] / "fixtures/render_spec/scenarios.py"
    vectors = json.loads(subprocess.check_output([sys.executable, str(generator)]))
    for case in vectors["scenarios"]:
        stored = None
        status = None
        prior = {}
        for step in case["steps"]:
            try:
                if step["action"] == "submit":
                    body = step["body"]
                    sid = body["submission_id"]
                    result = spec.admit(case["artifact_id"], stored, status, body, prior.get(sid))
                    assert result["repeat"] == (sid in prior)
                    assert spec.message_text({"title": case["artifact_id"], "artifact_id": case["artifact_id"]}, body) == step["prompt"]
                    if not result["repeat"]:
                        assert result["payload_hash"] == step["payload_hash"]
                        prior[sid] = {"payload_hash": result["payload_hash"], "synthesize_audio": True}
                    status = "completed"
                else:
                    spec.validate(step["payload"])
                    if step["action"] == "publish":
                        stored = step["payload"]
                        status = "ready"
                        assert spec.content_identity(stored["spec"], stored["version"]) == step["identity"]
            except spec.Refusal as exc:
                assert exc.triple() == step.get("error"), case["name"]
            else:
                assert "error" not in step, case["name"]
            assert status == step["stored_status"] and len(prior) == step["submission_count"], case["name"]


def test_submit_and_update_have_one_serial_outcome(tmp_path, form):
    import copy
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from lib import agents, artifacts, db, html_forms, render_spec_store

    agents.create_agent(persona="Builder", session="build", voice_id="v", cwd=str(tmp_path))
    ident = artifacts.create(type="render_spec", session="build", title="Build", payload=form)["artifact_id"]
    barrier = threading.Barrier(2)
    def start(fn):
        try:
            barrier.wait(timeout=5)
            fn()
            return "ok"
        except spec.Refusal as err:
            return err.code
        finally:
            db.close_local()

    replacement = {**copy.deepcopy(form), "version":"build-2"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        update = pool.submit(start, lambda: artifacts.update(ident,
            {"expected_version":"build-1","payload":replacement}))
        submit = pool.submit(start, lambda: html_forms.submit(ident,
            {"submission_id":"b"*32,"answers":{"intent":"submit_decision","state":{"choice":"review"}},
             "version":"build-1"}, render_capable=True))
        outcomes = update.result(timeout=10), submit.result(timeout=10)
    assert outcomes in {("ok", "version_mismatch"), ("already_answered", "ok")}
    row = render_spec_store.open_current(ident)
    count = db.conn().execute("SELECT count(*) FROM form_submissions WHERE artifact_id=?", (ident,)).fetchone()[0]
    assert (row["version"], row["status"], count) == (
        ("build-2","ready",0) if outcomes[0] == "ok" else ("build-1","completed",1))


def test_on_is_an_unknown_key_even_when_used_as_an_element_name(form):
    nodes = form["spec"]["elements"]
    nodes["on"] = nodes.pop("topic")
    nodes["root"]["children"][0] = "on"
    with refused("unknown_key", "/spec/elements/on"):
        spec.validate(form)


@pytest.mark.parametrize("schema", [
    {"type":"integer","minLength":1}, {"type":"string","minLength":-1},
    {"type":"string","minLength":True}, {"type":"string","minLength":2,"maxLength":1},
])
def test_schema_bounds_reject_invalid_minimums(form, schema):
    form["answer_schema"]["properties"]["answer"] = schema
    with pytest.raises(spec.Refusal) as error:
        spec.validate(form)
    assert error.value.code == "schema_shape"


@pytest.mark.parametrize("defect,code,path", [
    ("nested-card", "element_tree", "/spec/elements/card_inner"),
    ("shared-node", "element_tree", "/spec/elements/topic"),
    ("unreachable", "element_tree", "/spec/elements"),
    ("option-order", "expression", "/spec/elements/selection/props/options"),
    ("shared-binding", "expression", "/spec/elements/second_comment/props/value"),
    ("read-path", "expression", "/spec/elements/instructions/props/text"),
    ("boolean-as-level", "prop", "/spec/elements/topic/props/level"),
    ("unpaired-surrogate", "invalid_json", "/spec/elements/topic/props/text"),
])
def test_closed_tree_and_expression_contract(form, defect, code, path):
    import copy
    nodes = form["spec"]["elements"]
    children = nodes["root"]["children"]
    if defect == "nested-card":
        form["spec"]["root"] = "card_outer"
        nodes["card_outer"] = {"type": "card", "props": {}, "children": ["root"]}
        nodes["card_inner"] = {"type": "card", "props": {}}
        children.append("card_inner")
    elif defect == "shared-node":
        children.append("topic")
    elif defect == "unreachable":
        children.remove("topic")
    elif defect == "option-order":
        nodes["selection"]["props"]["options"].reverse()
    elif defect == "shared-binding":
        nodes["second_comment"] = copy.deepcopy(nodes["comment"])
        children.append("second_comment")
    elif defect == "read-path":
        nodes["instructions"]["props"]["text"] = {"$state": "/answer/more"}
    elif defect == "boolean-as-level":
        nodes["topic"]["props"]["level"] = True
    else:
        nodes["topic"]["props"]["text"] = chr(0xD800)
    with refused(code, path):
        spec.validate(form)
