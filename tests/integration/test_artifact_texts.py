"""A text no store can hold is refused for every artifact kind and every decision at one place,
with 400 and one fixed sentence; render_spec's own contract refusals still decide first (R79)."""
from __future__ import annotations

import pytest

from lib import db, render_spec
from tests.integration.test_attention_questions_http import _question, _request, host, server_module  # noqa: F401
from tests.integration.test_render_spec_http import envelope  # noqa: F401

HALF = "x\ud800"
REFUSED = (400, {"error": "A text in this request holds a character that cannot be stored."})


def _rows():
    return [db.conn().execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("artifacts", "form_submissions")]


def _document(**fields):
    return {"session": "theo", "type": "document", "title": "Notes", "payload": {"content": "hi"}, **fields}


@pytest.mark.parametrize("fields", [
    {"title": HALF}, {"summary": HALF}, {"reference_id": HALF}, {"session": HALF},
    {"payload": {"content": HALF}}, {"payload": {HALF: "a key"}},
])
def test_a_document_with_a_text_no_store_can_hold_is_refused(host, fields):
    before = _rows()
    assert _request(host, "/artifacts", _document(**fields)) == REFUSED
    assert _rows() == before


@pytest.mark.parametrize("fields", [{"title": HALF}, {"summary": HALF}, {"payload": {"content": HALF}}])
def test_an_update_with_such_a_text_is_refused_and_the_row_kept(host, fields):
    code, made = _request(host, "/artifacts", _document())
    ident = made["artifact"]["artifact_id"]
    assert _request(host, "/artifacts/" + ident, fields) == REFUSED
    assert db.conn().execute("SELECT title FROM artifacts WHERE artifact_id=?", (ident,)).fetchone()[0] == "Notes"


@pytest.mark.parametrize("field", ["title", "question", "context"])
def test_a_decision_with_such_a_text_is_refused(host, field):
    body = {"session": "theo", "title": "Layout", "question": "Which layout should I build?", field: HALF}
    before = _rows()
    assert _request(host, "/decisions", body) == REFUSED
    assert _rows() == before


def test_an_answer_with_such_a_text_is_refused(host):
    decision = _question(host)
    ident = decision.get("artifact_id") or decision.get("decision_id")
    assert _request(host, f"/decisions/{ident}/resolve", {"answer": HALF, "choice": "keep"}) == REFUSED


def test_render_spec_still_answers_with_its_own_field_refusal(host, envelope):
    assert _request(host, "/artifacts", {**envelope, "title": "Decide " + HALF}) == (
        400, {"error": render_spec.Refusal("invalid_json", "/title").triple()})
    assert _request(host, "/artifacts", envelope)[0] == 201
    body = {"version": "build-1", "submission_id": "a" * 31 + "\ud800",
            "answers": {"intent": "submit_decision", "state": {"choice": "send"}}}
    code, result = _request(host, "/artifacts/artifact-spec/submit", body,
                            headers={"X-Clarp-Client": "ios/12 contract=74"})
    assert (code, result["error"]["code"]) in ((400, "invalid_submission_id"), (403, "client_contract"))


def test_an_ordinary_text_still_passes(host):
    assert _request(host, "/artifacts", _document(title="Café 東京"))[0] == 201
