"""render_spec catalog version 2 (issue 673): version 1 plus chart, table and button. Each new rule
refuses with its own triple at its own path; version 1 is unchanged."""
import copy
import json
from pathlib import Path

import pytest

from lib import config, render_spec as spec, server_identity

ROOT = Path(__file__).parents[2]
CATALOGS = ROOT / "contract/catalogs"
EL = "/spec/elements"


@pytest.fixture
def form():
    return json.loads((ROOT / "tests/fixtures/render_spec/deploy-v2.json").read_text())


def _verdict(payload):
    try:
        spec.validate(payload)
    except spec.Refusal as exc:
        triple = exc.triple()
        return triple["code"], triple["path"]
    return "ok"


def _els(f):
    return f["spec"]["elements"]


def _series(f):
    return _els(f)["lat"]["props"]["series"]


def _points(n, start=0):
    return [{"x": start + i, "y": i} for i in range(n)]


def _add_chart(f, ident, props):
    _els(f)[ident] = {"type": "chart", "props": props, "children": []}
    _els(f)["root"]["children"].append(ident)


def _bar(f, points):
    _add_chart(f, "bars", {"kind": "bar", "series": [{"name": "s", "points": points}]})


def _pie(f, series):
    _add_chart(f, "share", {"kind": "pie", "series": series})


def _set(path, value):
    """A payload edit: set the value at a list of keys."""
    def edit(f):
        node = f
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return edit


def _drop_button(f):
    del _els(f)["hold"]
    _els(f)["row"]["children"].remove("hold")


def _cols(n):
    return [{"key": f"c{i}", "label": f"C{i}"} for i in range(n)]


LAT = f"{EL}/lat/props"
CMP = f"{EL}/cmp/props"
SHIP = f"{EL}/ship/props"

# (the edit, the verdict): every new rule at its limit and one step past it.
VECTORS = {
    "the example": (lambda f: None, "ok"),
    # chart
    "a line of 200 points": (_set(["spec", "elements", "lat", "props", "series"],
                                  [{"name": "a", "points": _points(200)}]), "ok"),
    "a line of 201 points": (_set(["spec", "elements", "lat", "props", "series"],
                                  [{"name": "a", "points": _points(201)}]), ("prop", f"{LAT}/series/0/points")),
    "600 points in all": (_set(["spec", "elements", "lat", "props", "series"],
                               [{"name": f"s{i}", "points": _points(200)} for i in range(3)]), "ok"),
    "601 points in all": (_set(["spec", "elements", "lat", "props", "series"],
                               [{"name": f"s{i}", "points": _points(200 if i < 3 else 1)} for i in range(4)]),
                          ("chart_points", f"{LAT}/series")),
    "seven series": (_set(["spec", "elements", "lat", "props", "series"],
                          [{"name": f"s{i}", "points": _points(1)} for i in range(7)]), ("prop", f"{LAT}/series")),
    "no series": (_set(["spec", "elements", "lat", "props", "series"], []), ("prop", f"{LAT}/series")),
    "a bar series of 60": (lambda f: _bar(f, _points(60)), "ok"),
    "a bar series of 61": (lambda f: _bar(f, _points(61)), ("chart_points", f"{EL}/bars/props/series/0/points")),
    "an unknown kind": (_set(["spec", "elements", "lat", "props", "kind"], "area"), ("prop", f"{LAT}/kind")),
    "a title of 121": (_set(["spec", "elements", "lat", "props", "title"], "t" * 121), ("prop", f"{LAT}/title")),
    "a unit of 13": (_set(["spec", "elements", "lat", "props", "unit"], "u" * 13), ("prop", f"{LAT}/unit")),
    "an x label of 61": (_set(["spec", "elements", "lat", "props", "x_label"], "x" * 61), ("prop", f"{LAT}/x_label")),
    "decimals 6": (_set(["spec", "elements", "lat", "props", "decimals"], 6), "ok"),
    "decimals 7": (_set(["spec", "elements", "lat", "props", "decimals"], 7), ("prop", f"{LAT}/decimals")),
    "negative decimals": (_set(["spec", "elements", "lat", "props", "decimals"], -1), ("prop", f"{LAT}/decimals")),
    "an empty series name": (_set(["spec", "elements", "lat", "props", "series", 0, "name"], ""),
                             ("prop", f"{LAT}/series/0/name")),
    "an x string of 61": (_set(["spec", "elements", "lat", "props", "series", 0, "points", 0, "x"], "x" * 61),
                          ("prop", f"{LAT}/series/0/points/0/x")),
    "an empty x string": (_set(["spec", "elements", "lat", "props", "series", 0, "points", 0, "x"], ""),
                          ("prop", f"{LAT}/series/0/points/0/x")),
    "a boolean x": (_set(["spec", "elements", "lat", "props", "series", 0, "points", 0, "x"], True),
                    ("prop", f"{LAT}/series/0/points/0/x")),
    "a null y": (_set(["spec", "elements", "lat", "props", "series", 0, "points", 0, "y"], None),
                 ("prop", f"{LAT}/series/0/points/0/y")),
    "a string y": (_set(["spec", "elements", "lat", "props", "series", 0, "points", 0, "y"], "1"),
                   ("prop", f"{LAT}/series/0/points/0/y")),
    "a point with another key": (_set(["spec", "elements", "lat", "props", "series", 0, "points", 0, "z"], 1),
                                 ("unknown_key", f"{LAT}/series/0/points/0/z")),
    "x mixed in one series": (_set(["spec", "elements", "lat", "props", "series", 0, "points", 1, "x"], 2),
                              ("chart_x_mixed", f"{LAT}/series/0/points/1/x")),
    "x mixed across series": (_set(["spec", "elements", "lat", "props", "series", 1, "points"], _points(2)),
                              ("chart_x_mixed", f"{LAT}/series/1/points/0/x")),
    "a string x twice in one series": (_set(["spec", "elements", "lat", "props", "series", 0, "points", 1, "x"], "Mon"),
                                       ("chart_x_duplicate", f"{LAT}/series/0/points/1/x")),
    "the same x in two series": (lambda f: None, "ok"),
    "numeric x not increasing": (_set(["spec", "elements", "lat", "props", "series"],
                                      [{"name": "a", "points": [{"x": 3, "y": 1}, {"x": 1, "y": 1}]}]),
                                 ("chart_x_order", f"{LAT}/series/0/points/1/x")),
    "numeric x twice": (_set(["spec", "elements", "lat", "props", "series"],
                             [{"name": "a", "points": [{"x": 1, "y": 1}, {"x": 1, "y": 1}]}]),
                        ("chart_x_duplicate", f"{LAT}/series/0/points/1/x")),
    "string x in any order": (_set(["spec", "elements", "lat", "props", "series"],
                                   [{"name": "a", "points": [{"x": "b", "y": 1}, {"x": "a", "y": 1}]}]), "ok"),
    "a pie of 12 with numeric x": (lambda f: _pie(f, [{"name": "p", "points": _points(12)}]), "ok"),
    "a pie of 13": (lambda f: _pie(f, [{"name": "p", "points": _points(13)}]),
                    ("chart_pie", f"{EL}/share/props/series/0/points")),
    "a pie of two series": (lambda f: _pie(f, [{"name": "p", "points": _points(1)},
                                               {"name": "q", "points": _points(1)}]),
                            ("chart_pie", f"{EL}/share/props/series")),
    "a pie slice below zero": (lambda f: _pie(f, [{"name": "p", "points": [{"x": "a", "y": 1}, {"x": "b", "y": -1}]}]),
                               ("chart_pie", f"{EL}/share/props/series/0/points/1/y")),
    "a chart with children": (_set(["spec", "elements", "lat", "children"], ["note"]),
                              ("element_tree", f"{EL}/lat/children")),
    # table
    "eight columns": (lambda f: _els(f)["cmp"]["props"].update(columns=_cols(8), rows=[{"c7": "x"}]), "ok"),
    "nine columns": (_set(["spec", "elements", "cmp", "props", "columns"], _cols(9)), ("prop", f"{CMP}/columns")),
    "no rows": (_set(["spec", "elements", "cmp", "props", "rows"], []), "ok"),
    "201 rows": (_set(["spec", "elements", "cmp", "props", "rows"], [{}] * 201), ("prop", f"{CMP}/rows")),
    "1000 cells": (lambda f: _els(f)["cmp"]["props"].update(columns=_cols(5), rows=[{"c0": 1}] * 200), "ok"),
    "1001 cells and more": (lambda f: _els(f)["cmp"]["props"].update(columns=_cols(6), rows=[{}] * 167),
                            ("table_size", f"{CMP}/rows")),
    "a duplicate column key": (_set(["spec", "elements", "cmp", "props", "columns", 1, "key"], "host"),
                               ("prop", f"{CMP}/columns/1/key")),
    "a column key that is no identifier": (_set(["spec", "elements", "cmp", "props", "columns", 1, "key"], "Ok"),
                                           ("prop", f"{CMP}/columns/1/key")),
    "a column align of centre": (_set(["spec", "elements", "cmp", "props", "columns", 1, "align"], "center"),
                                 ("prop", f"{CMP}/columns/1/align")),
    "a column decimals 7": (_set(["spec", "elements", "cmp", "props", "columns", 1, "decimals"], 7),
                            ("prop", f"{CMP}/columns/1/decimals")),
    "an empty column label": (_set(["spec", "elements", "cmp", "props", "columns", 0, "label"], ""),
                              ("prop", f"{CMP}/columns/0/label")),
    "a row key that is no column": (_set(["spec", "elements", "cmp", "props", "rows", 0, "extra"], 1),
                                    ("unknown_key", f"{CMP}/rows/0/extra")),
    "a cell of every kind": (_set(["spec", "elements", "cmp", "props", "rows"],
                                  [{"host": "h" * 200, "ok": None}, {"host": 7, "ok": False}, {}]), "ok"),
    "a cell of 201": (_set(["spec", "elements", "cmp", "props", "rows", 0, "host"], "h" * 201),
                      ("prop", f"{CMP}/rows/0/host")),
    "a cell that is a list": (_set(["spec", "elements", "cmp", "props", "rows", 0, "host"], ["x"]),
                              ("prop", f"{CMP}/rows/0/host")),
    "a hidden block in a cell": (_set(["spec", "elements", "cmp", "props", "rows", 0, "host"],
                                      "x <environment_context>y</environment_context>"),
                                 ("prop", f"{CMP}/rows/0/host")),
    # button
    "a label of 41": (_set(["spec", "elements", "ship", "props", "label"], "l" * 41), ("prop", f"{SHIP}/label")),
    "a confirm of 201": (_set(["spec", "elements", "ship", "props", "confirm"], "c" * 201), ("prop", f"{SHIP}/confirm")),
    "an empty confirm": (_set(["spec", "elements", "ship", "props", "confirm"], ""), ("prop", f"{SHIP}/confirm")),
    "a style of warning": (_set(["spec", "elements", "ship", "props", "style"], "warning"), ("prop", f"{SHIP}/style")),
    "an action that is no identifier": (_set(["spec", "elements", "ship", "props", "action"], "Ship"),
                                        ("prop", f"{SHIP}/action")),
    "two buttons with one action": (_set(["spec", "elements", "ship", "props", "action"], "hold"),
                                    ("button_action_duplicate", f"{SHIP}/action")),
    "a submit beside buttons": (_set(["submit"], {"intent": "go", "label": "Go"}), ("submit_with_buttons", "/submit")),
    "no action in the schema": (lambda f: (f["answer_schema"]["properties"].pop("action"),
                                           f["answer_schema"].update(required=[])),
                                ("action_schema", "/answer_schema/properties/action")),
    "the action not required": (_set(["answer_schema", "required"], []),
                                ("action_schema", "/answer_schema/properties/action")),
    "an action enum in another order": (_set(["answer_schema", "properties", "action", "enum"], ["hold", "ship"]), "ok"),
    "an action enum with an extra": (_set(["answer_schema", "properties", "action", "enum"], ["ship", "hold", "wait"]),
                                     ("action_schema", "/answer_schema/properties/action")),
    "an action enum missing one": (_drop_button, ("action_schema", "/answer_schema/properties/action")),
    "an action rule with a length": (_set(["answer_schema", "properties", "action", "maxLength"], 10),
                                     ("action_schema", "/answer_schema/properties/action")),
    "an element bound to the action": (lambda f: _els(f)["note"]["props"].update(value={"$bindState": "/action"}),
                                       ("action_bound", f"{EL}/note/props/value")),
    "a button with children": (_set(["spec", "elements", "ship", "children"], ["note"]),
                               ("element_tree", f"{EL}/ship/children")),
    # the payload
    "a fraction is still refused": (_set(["spec", "elements", "lat", "props", "series", 0, "points", 0, "y"], 12.5),
                                    ("number_form", f"{LAT}/series/0/points/0/y")),
    # An unknown version is judged by version 1's rules: its submit is required first.
    "catalog version 3": (_set(["catalog_version"], 3), ("missing_key", "/submit")),
    "catalog version 3 with a submit": (lambda f: f.update(catalog_version=3, submit={"intent": "go", "label": "Go"}),
                                        ("catalog_version", "/catalog_version")),
    "catalog version true": (_set(["catalog_version"], True), ("missing_key", "/submit")),
}


@pytest.mark.parametrize("name", VECTORS)
def test_vectors(name, form):
    edit, verdict = VECTORS[name]
    payload = copy.deepcopy(form)
    edit(payload)
    assert _verdict(payload) == verdict


def test_a_form_without_buttons_still_needs_its_submit(form):
    payload = copy.deepcopy(form)
    for ident in ("ship", "hold"):
        del _els(payload)[ident]
    _els(payload)["row"]["children"] = []
    payload["answer_schema"] = {"type": "object", "properties": {"note": {"type": "string"}}}
    assert _verdict(payload) == ("missing_key", "/submit")
    payload["submit"] = {"intent": "save", "label": "Save"}
    assert _verdict(payload) == "ok"


def test_version_one_knows_none_of_the_new_components(form):
    form["catalog_version"] = 1
    form["submit"] = {"intent": "go", "label": "Go"}
    assert _verdict(form) == ("component", f"{EL}/cmp/type")


def test_version_one_still_requires_its_submit_first(form):
    form["catalog_version"] = 1
    assert _verdict(form) == ("missing_key", "/submit")


def test_the_v2_files_are_v1_plus_the_new_parts():
    v1, v2 = (json.loads((CATALOGS / f"clarp.form.v{n}.json").read_text()) for n in (1, 2))
    r1, r2 = (json.loads((CATALOGS / f"clarp.form.v{n}.refusals.json").read_text()) for n in (1, 2))
    assert v2["catalog_version"] == 2 and v2["catalog"] == v1["catalog"]
    for key in ("identifiers", "expression_shapes", "initial_ui"):
        assert v2[key] == v1[key]
    assert {k: v for k, v in v2["components"].items() if k in v1["components"]} == v1["components"]
    assert set(v2["components"]) - set(v1["components"]) == {"chart", "table", "button"}
    assert {k: v for k, v in r2.items() if k in r1} == r1
    assert set(r2) == spec.CODES_V2 and set(r1) == spec.CODES
    assert spec.catalog(2).sha256 != spec.catalog(1).sha256


def test_an_unreadable_v2_file_refuses_only_v2_forms(tmp_path, monkeypatch, form):
    import shutil
    folder = tmp_path / "catalogs"
    shutil.copytree(CATALOGS, folder)
    (folder / "clarp.form.v2.json").write_text("{")
    monkeypatch.setattr(spec, "_catalog_dir", lambda: folder)
    spec._loaded.cache_clear()
    try:
        assert _verdict(form) == ("catalog_unavailable", "")
        v1 = json.loads((ROOT / "tests/fixtures/render_spec/build-decision.json").read_text())
        assert _verdict(v1) == "ok"
    finally:
        spec._loaded.cache_clear()


def _answer(state, intent="button"):
    return {"submission_id": "sub-0000000000000002", "version": "deploy-7",
            "answers": {"intent": intent, "state": state}}


def test_a_button_submission_is_admitted_like_v1(form):
    spec.validate(form)
    result = spec.admit("a", form, "ready", _answer({"action": "hold", "note": "wait"}), None)
    assert result["repeat"] is False and json.loads(result["answers_json"])["state"]["action"] == "hold"


@pytest.mark.parametrize("data,verdict", [
    (_answer({"action": "ship"}, intent="submit"), ("intent_mismatch", "/answers/intent")),
    (_answer({"note": "x"}), ("missing_key", "/answers/state/action")),
    (_answer({"action": "wait"}), ("state_value", "/answers/state/action")),
    (_answer({"action": "ship", "other": 1}), ("unknown_key", "/answers/state/other")),
])
def test_a_button_submission_is_refused_like_v1(form, data, verdict):
    with pytest.raises(spec.Refusal) as refused:
        spec.admit("a", form, "ready", data, None)
    assert (refused.value.code, refused.value.path) == verdict


def test_the_message_names_the_button_before_the_values(form):
    data = _answer({"action": "hold", "note": "wait"})
    text = spec.message_text({"title": "Deploy", "artifact_id": "x"},
                             {**data, "submission_id": data["submission_id"]}, form)
    assert 'Action: "Hold" (hold)\nAnswers:\n' in text
    assert text.endswith(spec.canonical(data["answers"]).decode())
    v1 = spec.message_text({"title": "Deploy", "artifact_id": "x"}, data, None)
    assert "Action:" not in v1


# ---- the gate ------------------------------------------------------------------------------------

def test_only_the_web_app_draws_v2_by_default():
    assert server_identity.RENDER_SPEC_V2_MIN_CONTRACT == {"web": 2}
    assert server_identity.render_spec_level("ios/2981 contract=999") == 1
    assert server_identity.render_spec_level("android/1 contract=999") == 1   # v1 from 41, v2 not yet
    assert server_identity.render_spec_level("android/1 contract=40") == 0
    assert server_identity.render_spec_level("web/0.5.3 contract=2") == 2
    assert server_identity.render_spec_level("web/0.5.3 contract=1") == 1
    assert server_identity.render_spec_level("Web/0.5.3 contract=2") == 0
    assert server_identity.render_spec_level("web/0.5.3 contract=2", {"web": 0}) == 0   # v2 needs v1
    assert server_identity.render_spec_level("web/0.5.3 contract=2", None, {"web": 0}) == 1


@pytest.mark.parametrize("header,v1,v2,level", [
    ("ios/1 contract=80", None, {"ios": 80}, 2),
    ("ios/1 contract=79", None, {"ios": 80}, 1),
    ("ios/1 contract=80", None, {"ios": 0}, 1),
    ("ios/1 contract=80", None, {"android": 1}, 1),            # unnamed: off
    ("ios/1 contract=80", {"ios": 0}, {"ios": 80}, 0),          # v1 off: none
    ("android/1 contract=40", {"android": 30}, {"android": 40}, 2),
    ("android/1 contract=40", None, {"android": 40}, 0),        # v2 needs v1
    ("Desktop/1 contract=5", {"Desktop": 1}, {"desktop": 5}, 1),  # the name exactly as sent
    (None, None, {"ios": 1}, 0),
])
def test_the_level_is_per_client(header, v1, v2, level):
    assert server_identity.render_spec_level(header, v1, v2) == level


def test_the_v2_table_is_read_by_the_one_rule(monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append(event))
    got = config._render_spec_v2_minimums({"render_spec_v2_min_contract": {"ios": 80, "web": "x"}})
    assert got == {"ios": 80} and lines == ["config.render_spec_v2_min_contract"]
    assert config._render_spec_v2_minimums({}) is None
    assert config._render_spec_v2_minimums({"render_spec_v2_min_contract": {}}) is None


def test_the_feature_has_its_own_contract():
    assert "render_spec_v2" in server_identity.FEATURES
    assert server_identity.FEATURE_CONTRACTS["render_spec_v2"] == 133  # introduced at 133; later bumps leave it


def test_a_v2_form_follows_the_v2_files_identifier_rule(tmp_path, monkeypatch, form):
    """Each version's identifiers come from its own file: a stricter rule in v2's file refuses a v2
    form and leaves a v1 form alone."""
    import shutil
    folder = tmp_path / "catalogs"
    shutil.copytree(CATALOGS, folder)
    data = json.loads((folder / "clarp.form.v2.json").read_text())
    data["identifiers"]["maxLength"] = 3
    (folder / "clarp.form.v2.json").write_text(json.dumps(data))
    monkeypatch.setattr(spec, "_catalog_dir", lambda: folder)
    spec._loaded.cache_clear()
    try:
        assert _verdict(form) == ("schema_shape", "/answer_schema/required")  # "action" is longer than 3
        v1 = json.loads((ROOT / "tests/fixtures/render_spec/build-decision.json").read_text())
        assert _verdict(v1) == "ok"
    finally:
        spec._loaded.cache_clear()
