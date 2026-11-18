import hashlib
import json
from pathlib import Path

import pytest

from tests.integration.test_attention_questions_http import host, _request, server_module  # noqa: F401
from lib import db, render_spec
from tests.fixtures.render_spec.scenarios import WIRE


@pytest.fixture
def envelope():
    return {"session":"theo", "type":"render_spec", "artifact_id":"artifact-spec", "title":"Decide",
            "payload":json.loads((Path(__file__).parents[1]/"fixtures/render_spec/build-decision.json").read_text())}


def test_validate_only_has_no_persistent_or_published_effects(host, envelope):
    tables = ("artifacts", "form_submissions", "sse_events")
    counts = lambda: [db.conn().execute("SELECT count(*) FROM "+t).fetchone()[0] for t in tables]
    before = counts()
    for good in (True, False):
        if not good:
            envelope["payload"]["catalog"] = "sensitive-instance-value"
        code, response = _request(host, "/artifacts?validate_only=1", envelope)
        assert code == (200 if good else 400)
        if good:
            assert response == {"valid": True}
        else:
            assert response["error"] == render_spec.Refusal("unknown_catalog", "/catalog").triple()
            assert "sensitive-instance-value" not in str(response)
        assert counts() == before and host.deliveries == []


@pytest.mark.parametrize("header,capable", [(v["header"], v["capable"]) for v in WIRE["clients"]])
def test_client_contract_gates_spec_and_submission(host, envelope, monkeypatch, header, capable):
    from lib import server_identity
    monkeypatch.setattr(server_identity, "RENDER_SPEC_MIN_CLIENT", WIRE["development_minimum"])
    headers = {"X-Clarp-Client":header} if header else {}
    assert _request(host, "/artifacts", envelope)[0] == 201
    for rep in ("", "?representation=flat-v1", "?representation=summary-v1"):
        status, rows = _request(host, "/artifacts"+rep, headers=headers)
        assert status == 200
        [row] = [x for x in rows["artifacts"] if x["type"] == "render_spec"]
        assert "spec" not in row and "payload" not in row
        assert ("content_omitted" in row) is capable
    _, body = _request(host, "/artifacts/artifact-spec?representation=flat-v1", headers=headers)
    opened = body["artifact"]
    assert ("spec" in opened) is capable and "content_omitted" not in opened
    if capable:
        mark = row["content_omitted"]
        assert opened["content_identity"] == {k:mark[k] for k in ("version","sha256","bytes")}
        assert row["catalog_sha256"] == "c1:"+hashlib.sha256(render_spec.CATALOG_BYTES).hexdigest()
    data = {"version":"build-1", "submission_id":"a"*32,"answers":{"intent":"submit_decision","state":{"choice":"send"}}}
    status, result = _request(host, "/artifacts/artifact-spec/submit", data, headers=headers)
    assert status == (200 if capable else 403)
    if not capable:
        assert result["error"] == render_spec.Refusal("client_contract","").triple()


def test_catalog_bytes_and_hash(host, envelope, monkeypatch):
    import urllib.request
    from tests.integration.test_attention_questions_http import TOKEN
    from lib import server_identity

    monkeypatch.setattr(server_identity, "RENDER_SPEC_MIN_CLIENT", 74)
    request = urllib.request.Request(host.base + "/artifact-catalogs/clarp.form/1",
                                     headers={"Authorization":"Bearer " + TOKEN})
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
    assert raw == (Path(__file__).parents[2]/"contract/catalogs/clarp.form.v1.json").read_bytes()
    headers = {"X-Clarp-Client":"ios/12 contract=74"}
    code, published = _request(host, "/artifacts", envelope, headers=headers)
    assert code == 201 and published["artifact"]["catalog_sha256"] == "c1:"+hashlib.sha256(raw).hexdigest()


def test_unset_threshold_ships_generic_even_for_high_contract(host, envelope):
    headers = {"X-Clarp-Client":"ios/12 contract=999"}
    _, result = _request(host, "/artifacts", envelope, headers=headers)
    row = result["artifact"]
    assert not {"payload","spec","content_identity","content_omitted","element_count"} & row.keys()
    _, response = _request(host, "/artifacts/artifact-spec", headers=headers)
    assert response["artifact"] == row
    status, refusal = _request(host, "/artifacts/artifact-spec/submit", {}, headers=headers)
    assert status == 403 and refusal["error"]["code"] == "client_contract"


def test_first_submission_announces_completion_and_worker_retries_same_prompt(host, envelope, monkeypatch):
    from lib import html_forms, server_identity

    monkeypatch.setattr(server_identity, "RENDER_SPEC_MIN_CLIENT", 74)
    headers = {"X-Clarp-Client": "ios/12 contract=74"}
    ident = envelope["artifact_id"]
    status, _ = _request(host, "/artifacts", envelope, headers=headers)
    assert status == 201
    published = [event for event in host.events if event.get("artifact_id") == ident]
    assert len(published) == 1
    host.events.clear()
    submission = {"submission_id": "recorded-answer-01", "version": "build-1",
                  "answers": {"intent": "submit_decision", "state": {"choice": "send"}}}
    path = f"/artifacts/{ident}/submit"
    receipt = {"submission_id": submission["submission_id"], "artifact_id": ident,
               "version": "build-1", "accepted": True, "delivery_status": "pending"}
    for _ in range(2):
        assert _request(host, path, submission, headers=headers) == (200, receipt)
    assert [event for event in host.events if event.get("artifact_id") == ident] == published
    assert set(published[0]) == {"type", "artifact_id", "agent_id", "session"}
    assert len(html_forms.pending()) == 1
    host.fail_delivery = True
    server_module._deliver_decision_rows(host.ctx)
    assert len(html_forms.pending()) == 1
    host.fail_delivery = False
    server_module._deliver_decision_rows(host.ctx)
    server_module._deliver_decision_rows(host.ctx)
    assert html_forms.pending() == []
    first, retry = host.deliveries
    assert retry["client_msg_id"] == first["client_msg_id"] == "html-form-recorded-answer-01"
    assert retry["text"] == first["text"]
    assert render_spec.canonical(submission["answers"]).decode() in first["text"]
    assert "preferences for review" in first["text"]
    assert _request(host, path, submission, headers=headers) == (200, {**receipt, "delivery_status": "delivered"})
    assert [event for event in host.events if event.get("artifact_id") == ident] == published


def _post_raw(host, path, raw):
    import urllib.error
    import urllib.request
    from tests.integration.test_attention_questions_http import TOKEN

    request = urllib.request.Request(host.base + path, data=raw, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN,
        "X-Clarp-Client": "ios/12 contract=74",
    })
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


@pytest.mark.parametrize("encoding,expected", [("utf-8", 200), ("utf-16", 400)])
def test_payload_limit_uses_canonical_bytes_over_real_transport(host, envelope, encoding, expected):
    limit = 131_072
    if encoding == "utf-8":
        raw = b" " * limit + json.dumps(envelope).encode()
        assert len(raw) > limit > len(render_spec.canonical(envelope["payload"]))
    else:
        envelope["payload"]["answer_schema"]["properties"]["extra"] = {"enum": ["字" * 44_000]}
        raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(encoding)
        assert len(raw) < limit < len(render_spec.canonical(envelope["payload"]))
    status, response = _post_raw(host, "/artifacts?validate_only=1", raw)
    assert status == expected
    if expected == 400:
        assert response["error"] == render_spec.Refusal("payload_size", "").triple()
    else:
        assert response == {"valid": True}
    assert host.events == [] and host.deliveries == []


@pytest.mark.parametrize("token", ["1.0", "1e0", "-0.0", "9007199254740992"])
def test_publish_rejects_number_spelling_before_catalog_validation(host, envelope, token):
    envelope["payload"]["catalog"] = "unsupported"
    envelope["payload"]["catalog_version"] = "REPLACE_NUMBER"
    encoded = json.dumps(envelope).replace('"REPLACE_NUMBER"', token).encode()
    status, response = _post_raw(host, "/artifacts?validate_only=1", encoded)
    assert status == 400 and response["error"] == render_spec.Refusal("number_form", "/catalog_version").triple()


def test_section_eight_wire_vectors(host, monkeypatch):
    from lib import server_identity
    from tests.fixtures.render_spec.scenarios import scenarios

    monkeypatch.setattr(server_identity, "RENDER_SPEC_MIN_CLIENT", 74)
    headers = {"X-Clarp-Client": "ios/12 contract=74"}
    for case in scenarios():
        ident = case["artifact_id"]
        read_path = "/artifacts/" + ident
        for step in case["steps"]:
            before = len(host.events)
            if step["action"] == "submit":
                path, data = read_path + "/submit", step["body"]
            else:
                path = "/artifacts?validate_only=1" if step["action"] == "validate" else "/artifacts"
                data = {"type": "render_spec", "session": "theo", "artifact_id": ident,
                        "title": case.get("title", ident), "payload": step["payload"]}
            if "payload_text" in step:
                text = json.dumps({**data, "payload": "PAYLOAD"}).replace('"PAYLOAD"', step["payload_text"])
                code, result = _post_raw(host, path, text.encode())  # sends the capable header
            else:
                code, result = _request(host, path, data, headers=headers)
            assert code == step["http"], case["name"]
            if "error" in step:
                assert result == {"error": step["error"]}
                assert len(host.events) == before
            elif step["action"] == "validate":
                assert result == {"valid": True}
                assert len(host.events) == before
            elif step["action"] == "submit":
                assert result == step["receipt"]
                stored = db.conn().execute("SELECT payload_hash,prompt FROM form_submissions WHERE submission_id=?",
                                           (step["body"]["submission_id"],)).fetchone()
                assert tuple(stored) == (step["payload_hash"], step["prompt"])
            else:
                listing = _request(host, "/artifacts?search=" + ident, headers=headers)[1]["artifacts"]
                row = next(item for item in listing if item["artifact_id"] == ident)
                opened = _request(host, read_path, headers=headers)[1]["artifact"]
                assert row["content_omitted"] == {**step["identity"], "updated_at": opened["updated_at"]}
                assert opened["content_identity"] == step["identity"] and opened["spec"] == step["payload"]["spec"]
                assert "spec" not in row and "content_omitted" not in opened
            status = db.conn().execute("SELECT status FROM artifacts WHERE artifact_id=?", (ident,)).fetchone()
            assert (status[0] if status else None) == step["stored_status"], case["name"]
            n = db.conn().execute("SELECT count(*) FROM form_submissions WHERE artifact_id=?", (ident,)).fetchone()[0]
            assert n == step["submission_count"]
    assert host.deliveries == []


@pytest.mark.parametrize("capable", [False, True])
def test_mutations_search_pins_and_inbox_share_the_negotiated_row(host, envelope, monkeypatch, capable):
    from lib import server_identity

    monkeypatch.setattr(server_identity, "RENDER_SPEC_MIN_CLIENT", 74)
    headers = {"X-Clarp-Client": f"ios/12 contract={74 if capable else 73}"}
    ident = envelope["artifact_id"]
    path = "/artifacts/" + ident

    def summary(row):
        assert row["artifact_id"] == ident
        assert not {"spec", "answers", "answer_schema", "submission", "payload", "content_identity"} & row.keys()
        assert ("content_omitted" in row) == capable
        if capable:
            marker = row["content_omitted"]
            assert marker["updated_at"] == row["updated_at"]
            assert marker["sha256"] == render_spec.content_identity(envelope["payload"]["spec"], "build-1")["sha256"]

    def post(target, body, code=200):
        status, result = _request(host, target, body, headers=headers)
        assert status == code
        summary(result["artifact"])
        return result["artifact"]["updated_at"]

    post("/artifacts", envelope, 201)
    stamp = post(path + "/pin", {"pinned": True})
    for listing in ("/artifacts?search=Decide", "/pinned-artifacts", "/attention/inbox"):
        status, result = _request(host, listing, headers=headers)
        assert status == 200
        [row] = [row for row in result["artifacts"] if row["artifact_id"] == ident]
        summary(row)
    stamp = post(path + "/archive", {"archived": True, "expected_updated_at": stamp})
    post(path + "/discard", {"expected_updated_at": stamp})


def test_shadowed_payload_key_has_same_refusal_from_tool_publish_and_pure_validator(host, envelope, tmp_path):
    import os
    import subprocess
    import sys
    from urllib.parse import urlparse
    from tests.integration.test_attention_questions_http import TOKEN

    config_path = tmp_path / "client.toml"
    config_path.write_text(f'[server]\nbind_addr="127.0.0.1"\nport={urlparse(host.base).port}\nauth_token="{TOKEN}"\n')
    script = Path(__file__).parents[2] / "scripts/agent_artifacts.py"

    def validate(request_file):
        return subprocess.run(
            [sys.executable, str(script), "publish-render-spec", str(request_file), "--validate-only"],
            env={**os.environ, "CLAUDE_PWA_CONFIG": str(config_path)}, capture_output=True, timeout=10)

    # The duplicate has equal values: parsing and reserializing in the tool would incorrectly accept it.
    raw = json.dumps(envelope).replace('"catalog_version": 1', '"catalog_version": 1, "catalog_version": 1')
    payload = json.loads(raw, object_pairs_hook=render_spec.JsonObject)["payload"]
    request_file = tmp_path / "request.json"
    request_file.write_text(raw)
    expected = render_spec.Refusal("invalid_object", "").triple()
    with pytest.raises(render_spec.Refusal) as error:
        render_spec.validate(payload)
    assert error.value.triple() == expected
    for path in ("/artifacts", "/artifacts?validate_only=1"):
        assert _post_raw(host, path, raw.encode()) == (400, {"error": expected})
    failed = validate(request_file)
    assert failed.returncode == 1 and json.loads(failed.stderr) == {"error": expected}
    assert host.events == [] and host.deliveries == []
    request_file.write_text(json.dumps(envelope))
    valid = validate(request_file)
    assert valid.returncode == 0 and json.loads(valid.stdout) == {"valid": True}
    assert db.conn().execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("field,value,code,status", [
    ("session", "unknown-session", "not_open", 409),
    ("title", "", "missing_key", 400),
    ("status", "unknown-status", "not_open", 409),
])
def test_render_publish_and_validate_return_the_same_metadata_refusal(host, envelope, field, value, code, status):
    envelope[field] = value
    expected = {"error": render_spec.Refusal(code, "/" + field).triple()}
    for path in ("/artifacts?validate_only=1", "/artifacts"):
        assert _request(host, path, envelope) == (status, expected)
    assert not host.events and not host.deliveries


def test_update_requires_whole_fresh_revision_and_preserves_prior_row_on_refusal(host, envelope, monkeypatch):
    from lib import server_identity
    monkeypatch.setattr(server_identity, "RENDER_SPEC_MIN_CLIENT", 74)
    headers = {"X-Clarp-Client": "ios/1 contract=74"}
    path = "/artifacts/" + envelope["artifact_id"]
    assert _request(host, "/artifacts", envelope, headers=headers)[0] == 201
    previous = _request(host, path, headers=headers)
    new = {**envelope["payload"], "version": "build-2"}
    for body, code, pointer, http in [
        ({"payload_patch": {"version": "build-2"}}, "unknown_key", "/payload_patch", 400),
        ({"expected_version": "build-1"}, "missing_key", "/payload", 400),
        ({"payload": new}, "version_mismatch", "/expected_version", 409),
        ({"payload": new, "expected_version": "old"}, "version_mismatch", "/expected_version", 409),
        ({"payload": envelope["payload"], "expected_version": "build-1"}, "version_mismatch", "/expected_version", 409),
    ]:
        assert _request(host, path, body, headers=headers) == (http, {"error": render_spec.Refusal(code, pointer).triple()})
        assert _request(host, path, headers=headers) == previous
    assert _request(host, path, {"payload": new, "expected_version": "build-1"}, headers=headers)[0] == 200
    assert _request(host, path, headers=headers)[1]["artifact"]["version"] == "build-2"


# ---- a missing catalog refuses render_spec alone ------------------------------------------------

def _masked(value, prefix):
    """A response with its clock values masked and this run's id prefix replaced."""
    def mask(v):
        if isinstance(v, dict):
            return {k: "T" if (k.endswith("_at") or k in ("revision", "event_id")) and isinstance(x, int)
                    else mask(x) for k, x in v.items()}
        return [mask(x) for x in v] if isinstance(v, list) else v
    return mask(json.loads(json.dumps(value).replace(prefix, "P")))


def html_form_route_answers(host, prefix):
    """Every html_form route an agent or the app uses, in order, with its answers."""
    ident = prefix + "-form"
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "additionalProperties": False}
    form = {"session": "theo", "type": "html_form", "artifact_id": ident, "title": "Form " + prefix,
            "payload": {"content": "<p>Hi</p>", "version": "1", "answer_schema": schema}}
    out = []

    def call(path, body=None):
        status, answer = _request(host, path, body, headers={"X-Clarp-Client": "ios/12 contract=74"})
        out.append([path.replace(prefix, "P"), status, _masked(answer, prefix)])
        return answer

    call("/artifacts", form)
    call("/artifacts?validate_only=1", {**form, "artifact_id": ident + "-v"})
    call("/artifacts/" + ident, {"payload": {**form["payload"], "version": "2"}, "expected_version": "1"})
    call("/artifacts/" + ident, {"title": "Form " + prefix + " renamed"})
    row = call("/artifacts/" + ident).get("artifact") or {}
    call("/artifacts?type=html_form&search=" + prefix)
    call("/artifacts/" + ident + "/submit", {"submission_id": prefix + "-submission-0001",
                                            "version": row.get("version", "2"), "answers": {"ok": True}})
    call("/artifacts/" + ident + "/pin", {"pinned": True})
    row = call("/artifacts/" + ident).get("artifact") or {}
    call("/artifacts/" + ident + "/archive", {"archived": True, "expected_updated_at": row.get("updated_at")})
    other = call("/artifacts", {**form, "artifact_id": ident + "-2"}).get("artifact") or {}
    call("/artifacts/" + ident + "-2/discard", {"expected_updated_at": other.get("updated_at")})
    call("/artifacts?search=" + prefix)
    return out


def test_a_missing_catalog_changes_no_html_form_route_and_refuses_render_spec(host, envelope, monkeypatch,
                                                                            tmp_path, capfd):
    from lib import server_identity
    monkeypatch.setattr(server_identity, "RENDER_SPEC_MIN_CLIENT", 74)
    capable = {"X-Clarp-Client": "ios/12 contract=74"}
    assert _request(host, "/artifacts", envelope, headers=capable)[0] == 201
    present = html_form_route_answers(host, "aaa")
    monkeypatch.setattr(render_spec, "_catalog_dir", lambda: tmp_path / "missing")
    render_spec._loaded.cache_clear()
    try:
        capfd.readouterr()
        assert html_form_route_answers(host, "bbb") == present
        fixed = {"error": {"code": "catalog_unavailable", "path": "", "message": render_spec.CATALOG_UNAVAILABLE_SENTENCE}}
        answer = {"submission_id": "sub-0000000000000001", "version": "build-1",
                  "answers": {"intent": "submit_decision", "state": {"choice": "send"}}}
        refused = [_request(host, "/artifacts", {**envelope, "artifact_id": "other"}, headers=capable),
                   _request(host, "/artifacts?validate_only=1", envelope, headers=capable),
                   _request(host, "/artifacts/artifact-spec", headers=capable),
                   _request(host, "/artifacts/artifact-spec/submit", answer, headers=capable),
                   _request(host, "/artifacts/artifact-spec", {"payload": envelope["payload"],
                                                               "expected_version": "build-1"}, headers=capable),
                   _request(host, "/artifact-catalogs/clarp.form/1", headers=capable)]
        assert refused == [(503, fixed)] * 6
        lines = [line for line in capfd.readouterr().err.splitlines() if "renderSpec" in line]
        assert len(lines) == 6 and all(line.endswith("renderSpecRefused code=catalog_unavailable path=")
                                       for line in lines)
        assert str(tmp_path) not in "\n".join(lines)
        # A client below the threshold still reads the plain row, as it reads every list row.
        assert _request(host, "/artifacts/artifact-spec")[0] == 200
    finally:
        render_spec._loaded.cache_clear()


def test_invalid_keys_reach_no_response_log_or_telemetry(host, envelope, monkeypatch, capfd):
    """A caller's key that fails the identifier rule is never named: not in the pointer, on stderr or in telemetry."""
    from lib import log as log_module, server_identity
    monkeypatch.setattr(server_identity, "RENDER_SPEC_MIN_CLIENT", 74)
    rows = []
    monkeypatch.setattr(log_module, "_eventlog_emit", lambda event, level, detail: rows.append([event, detail]))
    capable = {"X-Clarp-Client": "ios/12 contract=74"}
    canary = "QzxCanary"
    assert _request(host, "/artifacts", envelope, headers=capable)[0] == 201
    answers = []
    for change in (lambda p: (p["spec"]["elements"].update({"a" + canary + "-x": {"type": "divider", "props": {}}}),
                              p["spec"]["elements"]["root"]["children"].append("a" + canary + "-x")),
                   lambda p: p["answer_schema"]["properties"].update({canary: {"type": "string"}}),
                   lambda p: p["spec"]["state"].update({canary: 1}),
                   lambda p: p.update({canary: 1})):
        payload = json.loads(json.dumps(envelope["payload"]))
        change(payload)
        answers.append(_request(host, "/artifacts", {**envelope, "artifact_id": "x", "payload": payload},
                                headers=capable))
    base = {"submission_id": "sub-0000000000000002", "version": "build-1",
            "answers": {"intent": "submit_decision", "state": {"choice": "send"}}}
    answers.append(_request(host, "/artifacts/artifact-spec/submit",
                            {**base, "answers": {"intent": "submit_decision", "state": {"choice": "send", canary: 1}}},
                            headers=capable))
    answers.append(_request(host, "/artifacts/artifact-spec/submit", {**base, "version": {canary: 1.5}},
                            headers=capable))
    assert [a[1]["error"]["code"] for a in answers] == ["element_id", "schema_shape", "unknown_key", "unknown_key",
                                                         "unknown_key", "invalid_version"]
    assert [a[1]["error"]["path"] for a in answers] == ["/spec/elements", "/answer_schema/properties", "/spec/state",
                                                         "", "/answers/state", "/version"]
    err = capfd.readouterr().err
    assert canary.lower() not in (json.dumps(answers) + err + json.dumps(rows)).lower()
    assert sum("renderSpecRefused" in line for line in err.splitlines()) == 6
    assert sum(event == "renderSpecRefused" for event, _ in rows) == 6


def test_an_agent_withdraws_an_unanswered_spec_by_discarding_it(host, envelope, monkeypatch):
    """Archive only files it away: it can still be answered. Discard withdraws it."""
    from lib import server_identity
    monkeypatch.setattr(server_identity, "RENDER_SPEC_MIN_CLIENT", 74)
    capable = {"X-Clarp-Client": "ios/12 contract=74"}
    answer = {"version": "build-1", "answers": {"intent": "submit_decision", "state": {"choice": "send"}}}
    for ident, action in (("kept", "archive"), ("gone", "discard")):
        row = _request(host, "/artifacts", {**envelope, "artifact_id": ident}, headers=capable)[1]["artifact"]
        body = {"expected_updated_at": row["updated_at"], **({"archived": True} if action == "archive" else {})}
        assert _request(host, f"/artifacts/{ident}/{action}", body, headers=capable)[0] == 200
    assert _request(host, "/artifacts/kept/submit", {**answer, "submission_id": "sub-kept-000000000001"},
                    headers=capable)[0] == 200
    assert _request(host, "/artifacts/gone/submit", {**answer, "submission_id": "sub-gone-000000000001"},
                    headers=capable) == (409, {"error": render_spec.Refusal("not_open", "").triple()})
