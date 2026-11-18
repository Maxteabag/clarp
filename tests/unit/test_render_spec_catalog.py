"""The catalog file is the one source: loaded on first use, followed wherever it states a rule."""
import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib import render_spec as spec

ROOT = Path(__file__).parents[2]
CATALOGS = ROOT / "contract/catalogs"


@pytest.fixture
def form():
    return json.loads((ROOT / "tests/fixtures/render_spec/build-decision.json").read_text())


@pytest.fixture
def catalog_at(monkeypatch):
    """Point the module at another catalog folder; the cache is cleared around each use."""
    def use(folder):
        monkeypatch.setattr(spec, "_catalog_dir", lambda: Path(folder))
        spec._loaded.cache_clear()
    yield use
    spec._loaded.cache_clear()


def _verdict(payload):
    try:
        spec.validate(payload)
    except spec.Refusal as exc:
        return exc.triple()["code"], exc.triple()["path"]
    return "ok"


def _edited(tmp_path, catalog_at, edit):
    folder = tmp_path / "catalogs"
    shutil.copytree(CATALOGS, folder)
    data = json.loads((folder / "clarp.form.v1.json").read_text())
    edit(data)
    (folder / "clarp.form.v1.json").write_text(json.dumps(data))
    catalog_at(folder)


def _image_layout(target: Path):
    """Lay out the runtime stage's files as the Dockerfile's COPY lines from the build context do."""
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    stage = max(i for i, line in enumerate(lines) if line.startswith("FROM "))
    workdir = None
    for line in lines[stage:]:
        words = line.split()
        if words[:1] == ["WORKDIR"]:
            workdir = words[1]
        if words[:1] != ["COPY"] or any(w.startswith("--from") for w in words) or workdir != "/opt/clarp":
            continue
        *sources, dest = words[1:]
        if dest.startswith("/"):
            continue
        for source in sources:
            src = ROOT / source
            out = target / dest / src.name if dest.endswith("/") else target / dest
            out.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy(src, out)


def test_the_docker_image_layout_loads_the_catalog(tmp_path):
    _image_layout(tmp_path)
    probe = ("import sys; sys.path.insert(0, '.'); from lib import render_spec; "
             "print(render_spec.catalog().sha256)")
    out = subprocess.run([sys.executable, "-I", "-c", probe], cwd=tmp_path, capture_output=True, text=True)
    raw = (CATALOGS / "clarp.form.v1.json").read_bytes()
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "c1:" + hashlib.sha256(raw).hexdigest()


def test_importing_the_module_reads_no_catalog(tmp_path):
    """Without contract/ beside lib/ the module still imports: no other artifact kind depends on it."""
    shutil.copytree(ROOT / "server/lib", tmp_path / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    probe = ("import sys; sys.path.insert(0, '.'); from lib import render_spec\n"
             "assert render_spec.JsonObject([('a', 1)]) == {'a': 1}\n"
             "try:\n    render_spec.catalog()\nexcept render_spec.Refusal as exc:\n    print(exc.triple()['code'])")
    out = subprocess.run([sys.executable, "-I", "-c", probe], cwd=tmp_path, capture_output=True, text=True)
    assert (out.returncode, out.stdout.strip()) == (0, "catalog_unavailable"), out.stderr[-500:]


def test_a_missing_catalog_refuses_every_render_spec_verdict_with_one_triple(tmp_path, catalog_at, form):
    catalog_at(tmp_path / "missing")
    fixed = {"code": "catalog_unavailable", "path": "", "message": spec.CATALOG_UNAVAILABLE_SENTENCE}
    for call in (lambda: spec.validate(form),
                 lambda: spec.admit("a", form, "ready", _answer(), None),  # a valid submission
                 lambda: spec.catalog()):
        with pytest.raises(spec.Refusal) as refused:
            call()
        assert refused.value.triple() == fixed
    # Any other refusal raised while the catalog is unreadable is told as the same triple.
    assert spec.Refusal("not_open", "/session").triple() == fixed
    assert str(tmp_path) not in json.dumps(fixed)


def test_the_fixed_sentence_is_the_refusals_files_sentence():
    reasons = json.loads((CATALOGS / "clarp.form.v1.refusals.json").read_text())
    assert reasons["catalog_unavailable"] == spec.CATALOG_UNAVAILABLE_SENTENCE
    assert set(reasons) == spec.CODES


def _nest_card(f):
    f["spec"]["elements"]["outer"] = {"type": "card", "props": {}, "children": ["inner"]}
    f["spec"]["elements"]["inner"] = {"type": "card", "props": {}, "children": []}
    f["spec"]["elements"]["root"]["children"].append("outer")


def _long_id(f):
    f["spec"]["elements"]["divider_twelve"] = {"type": "divider", "props": {}}
    f["spec"]["elements"]["root"]["children"].append("divider_twelve")


def _upper_id(f):
    f["spec"]["elements"]["Qline"] = {"type": "divider", "props": {}}
    f["spec"]["elements"]["root"]["children"].append("Qline")


def _read_state(f):
    f["spec"]["elements"]["instructions"]["props"]["text"] = {"$state": "/answer"}


def _three_options(f):
    f["spec"]["elements"]["selection"]["props"]["options"] = f["spec"]["elements"]["selection"]["props"]["options"][:2]


def _extra_heading_prop(f):
    f["spec"]["elements"]["topic"]["props"]["tone"] = "calm"


def _level_three(f):
    f["spec"]["elements"]["topic"]["props"]["level"] = 3


def _components(edit):
    return lambda data: edit(data["components"])


# Each rule the file carries as data: (the edit to the file, the payload change, verdict before, verdict after).
FOLLOWED = {
    "identifier length": (lambda d: d["identifiers"].update(maxLength=10), _long_id,
                          "ok", ("element_id", "/spec/elements")),
    "identifier first characters": (lambda d: d["identifiers"].update(first=d["identifiers"]["first"] + "Q"),
                                    _upper_id, ("element_id", "/spec/elements"), "ok"),
    "identifier rest characters": (lambda d: d["identifiers"].update(rest=d["identifiers"]["rest"].replace("_", "")),
                                   _long_id, "ok", ("element_id", "/spec/elements")),
    "forbid_descendants": (_components(lambda c: c["card"].update(forbid_descendants=[])), _nest_card,
                           ("element_tree", "/spec/elements/inner"), "ok"),
    "reads": (_components(lambda c: c["text"].pop("reads")), _read_state,
              "ok", ("prop", "/spec/elements/instructions/props/text")),
    "bindings": (_components(lambda c: c["toggle"]["bindings"].update(value="string")), lambda f: None,
                 "ok", ("expression", "/spec/elements/notify/props/value")),
    "option bounds": (_components(lambda c: c["choice"]["props"]["properties"]["options"].update(minItems=3)),
                      _three_options, "ok", ("prop", "/spec/elements/selection/props/options")),
    "expression shape": (lambda d: d["expression_shapes"]["$state"]["properties"]["$state"].update(maxLength=3),
                         _read_state, "ok", ("expression", "/spec/elements/instructions/props/text")),
    "closed props": (_components(lambda c: c["heading"]["props"].update(additionalProperties=True)),
                     _extra_heading_prop, ("unknown_key", "/spec/elements/topic/props/tone"), "ok"),
    "maximum": (_components(lambda c: c["heading"]["props"]["properties"]["level"].update(maximum=2)),
                _level_three, "ok", ("prop", "/spec/elements/topic/props/level")),
    "string length": (_components(lambda c: c["heading"]["props"]["properties"]["text"].update(maxLength=3)),
                      lambda f: None, "ok", ("prop", "/spec/elements/topic/props/text")),
    "children": (_components(lambda c: c["stack"].update(children=False)), lambda f: None,
                 "ok", ("element_tree", "/spec/elements/root/children")),
    "a bound prop's shape is the $bindState shape": (
        _components(lambda c: c["text_field"]["props"]["properties"]["value"]["properties"]["$bindState"].update(maxLength=30)),
        lambda f: None, "ok", ("catalog_unavailable", "")),
    "const": (_components(lambda c: c["heading"]["props"]["properties"]["level"].update(const=2)), lambda f: None,
              "ok", ("prop", "/spec/elements/topic/props/level")),
    "reads use the shape they name": (_components(lambda c: c["text"].update(reads={"text": "$bindState"})), _read_state,
                                      "ok", ("expression", "/spec/elements/instructions/props/text")),
    "keywords the catalog may use": (lambda d: d["schema_keywords"].remove("maxLength"), lambda f: None,
                                     "ok", ("catalog_unavailable", "")),
    "a keyword the code does not know": (lambda d: d["schema_keywords"].append("pattern"), lambda f: None,
                                         "ok", ("catalog_unavailable", "")),
}


@pytest.mark.parametrize("rule", FOLLOWED)
def test_the_validator_follows_the_catalog_file(rule, tmp_path, catalog_at, form):
    edit, change, before, after = FOLLOWED[rule]
    payload = copy.deepcopy(form)
    change(payload)
    catalog_at(CATALOGS)
    assert _verdict(copy.deepcopy(payload)) == before
    _edited(tmp_path, catalog_at, edit)
    assert _verdict(copy.deepcopy(payload)) == after


# ---- pointers name structural keys, indexes and valid identifiers only ---------------------------

def test_an_invalid_element_id_sorting_before_its_parent_is_not_named(form):
    form["spec"]["elements"]["aZZ-bad"] = {"type": "divider", "props": {}}
    form["spec"]["elements"]["root"]["children"].append("aZZ-bad")
    with pytest.raises(spec.Refusal) as refused:
        spec.validate(form)
    assert (refused.value.code, refused.value.path) == ("element_id", "/spec/elements")


@pytest.mark.parametrize("key", ["BadKey", "a" * 41, "dash-key", "ø"])
def test_an_invalid_answer_schema_property_key_is_not_named(form, key):
    form["answer_schema"]["properties"][key] = {"type": "string"}
    with pytest.raises(spec.Refusal) as refused:
        spec.validate(form)
    assert (refused.value.code, refused.value.path) == ("schema_shape", "/answer_schema/properties")


def test_an_unknown_key_that_is_a_valid_identifier_is_named(form):
    form["spec"]["state"]["mystery"] = "x"
    with pytest.raises(spec.Refusal) as refused:
        spec.validate(form)
    assert (refused.value.code, refused.value.path) == ("unknown_key", "/spec/state/mystery")


def _answer(**changes):
    body = {"submission_id": "sub-0000000000000001", "version": "build-1",
            "answers": {"intent": "submit_decision", "state": {"choice": "send"}}}
    body.update(changes)
    return body


def test_an_uppercase_key_in_the_answer_state_is_not_named(form):
    with pytest.raises(spec.Refusal) as refused:
        spec.admit("a", form, "ready", _answer(answers={"intent": "submit_decision",
                                                        "state": {"choice": "send", "SECRET": 1}}), None)
    assert (refused.value.code, refused.value.path) == ("unknown_key", "/answers/state")


@pytest.mark.parametrize("version", [1.5, 2, None, {"Secret": 1.5}, ["v"]])
def test_a_non_string_submission_version_is_refused_before_anything_hashes_it(form, version):
    with pytest.raises(spec.Refusal) as refused:
        spec.admit("a", form, "ready", _answer(version=version), None)
    assert (refused.value.code, refused.value.path) == ("invalid_version", "/version")


# ---- the stages and the sanitising the earlier tests left open -----------------------------------

def test_a_hidden_block_in_a_text_prop_is_refused(form):
    form["spec"]["elements"]["topic"]["props"]["text"] = "Build <environment_context>x</environment_context>"
    with pytest.raises(spec.Refusal) as refused:
        spec.validate(form)
    assert (refused.value.code, refused.value.path) == ("prop", "/spec/elements/topic/props/text")


def test_minus_zero_is_the_integer_zero():
    value = json.loads('{"n": -0}', object_pairs_hook=spec.JsonObject)
    assert spec.canonical(value) == b'{"n":0}'
    with pytest.raises(spec.Refusal) as refused:
        spec.canonical(json.loads('{"n": -0.0}'))
    assert refused.value.code == "number_form"


def test_message_text_writes_the_title_as_a_canonical_json_string():
    text = spec.message_text({"title": 'A "b" \\ ø\n', "artifact_id": "x"},
                             {"version": "v", "submission_id": "s", "answers": {}})
    assert text.startswith('The user answered "A \\"b\\" \\\\ ø\\n".\n')


def test_a_failed_status_update_leaves_no_submission(tmp_path, form):
    """The submission row and the completed status are one transaction."""
    import sqlite3
    from lib import agents, artifacts, db, render_spec_store
    agents.create_agent(persona="Synthetic", session="build", voice_id="v", cwd=str(tmp_path))
    ident = artifacts.create(type="render_spec", session="build", title="Build", payload=form)["artifact_id"]
    con = db.conn()
    con.execute("CREATE TEMP TRIGGER refuse_completion BEFORE UPDATE OF status ON artifacts "
                "WHEN NEW.status = 'completed' BEGIN SELECT RAISE(ABORT, 'refused'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            render_spec_store.submit(ident, _answer(), capable=True)
    finally:
        con.execute("DROP TRIGGER refuse_completion")
    assert con.execute("SELECT count(*) FROM form_submissions WHERE artifact_id=?", (ident,)).fetchone()[0] == 0
    assert con.execute("SELECT status FROM artifacts WHERE artifact_id=?", (ident,)).fetchone()[0] == "ready"
    assert render_spec_store.submit(ident, _answer(), capable=True)["accepted"] is True


def test_a_failure_after_the_completion_leaves_no_submission_and_the_spec_open(tmp_path, form, monkeypatch):
    """The second kill point: both writes done, the transaction not yet committed."""
    from lib import agents, artifacts, db, html_forms, render_spec_store
    agents.create_agent(persona="Synthetic", session="build", voice_id="v", cwd=str(tmp_path))
    ident = artifacts.create(type="render_spec", session="build", title="Build", payload=form)["artifact_id"]

    def killed(_row):
        raise RuntimeError("killed after the status update")
    monkeypatch.setattr(html_forms, "receipt", killed)
    with pytest.raises(RuntimeError, match="killed after the status update"):
        render_spec_store.submit(ident, _answer(), capable=True)
    monkeypatch.undo()
    con = db.conn()
    assert con.execute("SELECT count(*) FROM form_submissions WHERE artifact_id=?", (ident,)).fetchone()[0] == 0
    assert con.execute("SELECT status FROM artifacts WHERE artifact_id=?", (ident,)).fetchone()[0] == "ready"
    assert render_spec_store.submit(ident, _answer(), capable=True)["accepted"] is True
    assert render_spec_store.submit(ident, _answer(), capable=True)["accepted"] is True   # the retry's receipt


def test_the_owner_functions_refuse_to_run_outside_a_transaction():
    from lib import artifacts, db, html_forms
    con = db.conn()
    assert not con.in_transaction
    for call in (lambda: artifacts.complete_in(con, "x", now=1, updated_at=1),
                 lambda: artifacts.replace_render_spec_in(con, "x", title="t", summary="", status="ready",
                                                          reference="", payload_json="{}", now=1),
                 lambda: html_forms.record_submission_in(con, submission_id="s", artifact_id="x", version="v",
                                                         session="s", answers_json="{}", payload_hash="h",
                                                         prompt="p", created_at=1, synthesize_audio=1)):
        with pytest.raises(RuntimeError, match="inside the caller's transaction"):
            call()
    assert con.execute("SELECT count(*) FROM form_submissions").fetchone()[0] == 0


def test_a_refusal_raised_before_any_catalog_read_says_the_catalog_is_unavailable(tmp_path, catalog_at):
    """The guard in Refusal.triple is live (R74 kept it): a refusal that reads no catalog itself,
    such as not_open for an unknown session, still answers catalog_unavailable without one."""
    catalog_at(tmp_path / "missing")
    assert spec.Refusal("not_open", "/session").triple() == {
        "code": "catalog_unavailable", "path": "", "message": spec.CATALOG_UNAVAILABLE_SENTENCE}
