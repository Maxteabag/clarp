"""Print the section 8 wire vectors as UTF-8 JSON. Uses only the standard library."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).parent
REASONS = json.loads((HERE.parents[2] / "contract/catalogs/clarp.form.v1.refusals.json").read_text())
SID = "recorded-decision-01"
WIRE = json.loads((HERE / "wire.json").read_text())


def form():
    return json.loads((HERE / "build-decision.json").read_text())


def encoded(value):
    # All object keys in these vectors are ASCII, so their UTF-16 order is lexical.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def answer(**state):
    return {"version": "build-1", "submission_id": SID,
            "answers": {"intent": "submit_decision", "state": state}}


def refused(code, path):
    return {"code": code, "path": path, "message": REASONS[code]}


def publish(payload=None, *, validate=False, error=None):
    payload = form() if payload is None else payload
    action = "validate" if validate else "publish"
    step = {"action": action, "payload": payload, "http": 200 if validate else 201}
    if error:
        step.update(http=400, error=error)
    elif not validate:
        raw = encoded(payload["spec"])
        step["identity"] = {"version": payload["version"], "bytes": len(raw),
                            "sha256": "c1:" + hashlib.sha256(raw).hexdigest()}
    return step


def submit(body, *, error=None, http=200):
    result = {"action": "submit", "body": body, "http": http}
    if error:
        result["error"] = error
    return result


def case(name, *steps, title=None):
    ident = "vector-" + name
    title = ident if title is None else title
    state, submissions = None, 0
    for step in steps:
        if step["action"] == "submit" and "body" in step:
            body = dict(step["body"])
            body["submission_id"] = name.replace("_", "-") + "-" + body["submission_id"]
            step["body"] = body
        if step["action"] == "read":
            state = "ready" if step["http"] == 201 else state      # read as a whole publish
        elif "error" not in step:
            if step["action"] == "publish":
                state = "ready"
            elif step["action"] == "submit":
                state, submissions = "completed", 1
                body = step["body"]
                step["payload_hash"] = hashlib.sha256(encoded([ident, body["version"], body["answers"]])).hexdigest()
                # The title is written as a canonical JSON string.
                step["prompt"] = (f"The user answered {encoded(title).decode()}.\nArtifact ID: {ident}\nVersion: {body['version']}\n"
                                  f"Submission ID: {body['submission_id']}\nThese are user preferences for review, "
                                  "not approval to perform protected actions.\nAnswers:\n" + encoded(body["answers"]).decode())
                step["receipt"] = {"artifact_id": ident, "submission_id": step["body"]["submission_id"],
                                   "version": "build-1", "accepted": True, "delivery_status": "pending"}
        step["stored_status"] = state
        step["submission_count"] = submissions
    return {"name": name, "artifact_id": ident, "steps": steps, **({"title": title} if title != ident else {})}


def boundary(name, extra):
    payload, response = form(), answer(choice="send")
    spec = payload["spec"]
    nodes = spec["elements"]
    if name == "element_count":
        for i in range(200 + extra - len(nodes)):
            key = f"divider_{i}"
            nodes[key] = {"type": "divider", "props": {}}
            nodes["root"]["children"].append(key)
        path = "/spec/elements"
    elif name == "depth":
        for i in range(10 + extra):
            key = f"stack_{i}"
            nodes[key] = {"type": "stack", "props": {}, "children": [spec["root"]]}
            spec["root"] = key
        path = "/spec/elements/topic"
    elif name == "spec_size":
        for i in range(9):
            nodes[f"text_{i}"] = {"type": "text", "props": {"text": ""}}
            nodes["root"]["children"].append(f"text_{i}")
        left = 32768 + extra - len(encoded(spec))
        for i in range(9):
            size = min(4000, left)
            nodes[f"text_{i}"]["props"]["text"] = "x" * size
            left -= size
        assert len(encoded(spec)) == 32768 + extra
        path = "/spec"
    elif name == "payload_size":
        rule = {"enum": [""]}
        payload["answer_schema"]["properties"]["extra"] = rule
        rule["enum"][0] = "x" * (131072 + extra - len(encoded(payload)))
        path = ""
    elif name == "state_size":
        del payload["answer_schema"]["properties"]["answer"]["maxLength"]
        spec["state"] = {"answer": ""}
        spec["state"]["answer"] = "x" * (4096 + extra - len(encoded(spec["state"])))
        path = "/spec/state"
    elif name == "answer_size":
        del payload["answer_schema"]["properties"]["answer"]["maxLength"]
        obj = response["answers"]
        obj["state"]["answer"] = ""
        obj["state"]["answer"] = "x" * (16384 + extra - len(encoded(obj)))
        path = "/answers"
    else:
        raise ValueError(name)
    error = refused(name, path) if extra else None
    if name == "answer_size":
        return [publish(payload), submit(response, error=error, http=400 if extra else 200)]
    return [publish(payload, error=error)]


def scenarios():
    good = answer(choice="send", notify=True)
    yield case("a-accepted", publish(), submit(good))
    yield case("b-missing-choice", publish(), submit(answer(), http=400,
               error=refused("missing_key", "/answers/state/choice")))
    yield case("c-repeat", publish(), submit(good), submit(good))
    yield case("d-conflict", publish(), submit(good), submit(answer(choice="review"), http=409,
               error=refused("reused_id", "/submission_id")))
    yield case("e-old-version", publish(), submit({**good, "version": "obsolete"}, http=409,
               error=refused("version_mismatch", "/version")))
    yield case("f-closed", publish(), submit(good), submit({**good, "submission_id": SID + "-2"},
               http=409, error=refused("already_answered", "")))
    for cap in ("element_count", "depth", "spec_size", "state_size", "payload_size", "answer_size"):
        for extra in (0, 1):
            yield case(f"g-{cap}-{extra}", *boundary(cap, extra))
    bad_schema = form()
    bad_schema["answer_schema"]["properties"]["answer"]["pattern"] = "private-pattern"
    keyword = refused("schema_keyword", "/answer_schema/properties/answer/pattern")
    yield case("h-schema", publish(bad_schema, error=keyword))
    yield case("i-validate", publish(validate=True), publish(bad_schema, validate=True, error=keyword))
    yield case("j-intent", publish(), submit({**good, "answers": {**good["answers"], "intent": "other"}},
               http=400, error=refused("intent_mismatch", "/answers/intent")))
    yield case("k-extra-answer", publish(), submit({**good, "answers": {**good["answers"], "extra": True}},
               http=400, error=refused("unknown_key", "/answers/extra")))
    yield case("k-extra-state", publish(), submit(answer(choice="send", extra=True),
               http=400, error=refused("unknown_key", "/answers/state/extra")))
    yield case("l-catalog", publish({**form(), "catalog": "other"}, error=refused("unknown_catalog", "/catalog")))
    yield case("l-version", publish({**form(), "catalog_version": 2}, error=refused("catalog_version", "/catalog_version")))


    for name, minimum, text, accepted in (("empty", None, "", True), ("short", 1, "", False),
                                           ("one-character", 1, "ø", True)):
        payload = form()
        payload["answer_schema"]["required"].append("answer")
        if minimum is not None:
            payload["answer_schema"]["properties"]["answer"]["minLength"] = minimum
        step = submit(answer(choice="send", answer=text))
        if not accepted:
            step.update(http=400, error=refused("state_value", "/answers/state/answer"))
        yield case("min-length-" + name, publish(payload), step)

    # Each proposed payload fails in two stages. Only the first verdict is public.
    invalid = form()
    invalid["catalog_version"] = 2
    invalid["answer_schema"]["properties"]["answer"]["pattern"] = "private-pattern"
    yield case("catalog-before-schema", publish(invalid, error=refused("catalog_version", "/catalog_version")))
    invalid = form()
    invalid["answer_schema"]["properties"]["answer"]["pattern"] = "private-pattern"
    invalid["spec"]["elements"]["topic"]["type"] = "other"
    yield case("schema-before-elements", publish(invalid, error=keyword))
    yield case("version-before-intent", publish(), submit({**good, "version": "old",
               "answers": {"intent": "other", "state": {}}}, http=409, error=refused("version_mismatch", "/version")))
    yield case("intent-before-state", publish(), submit({**good, "answers": {"intent": "other", "state": {}}},
               http=400, error=refused("intent_mismatch", "/answers/intent")))
    invalid = form()
    invalid["spec"]["elements"]["topic"]["type"] = "other"
    invalid["spec"]["state"]["notify"] = "yes"
    yield case("elements-before-state", publish(invalid, error=refused("component", "/spec/elements/topic/type")))

    # The title in the agent's message is a canonical JSON string: quotes and a backslash
    # escaped, non-ASCII as it is.
    yield case("message-title", publish(), submit(good), title='Say "go" \\ ø, 東京: vector-message-title')

    # "-0" in the JSON text is the integer 0: accepted, and its canonical bytes are "0".
    payload = form()
    payload["answer_schema"]["properties"]["count"] = {"type": "integer"}
    payload["spec"]["state"]["count"] = 0
    step = publish(payload)
    step["payload_text"] = encoded(payload).decode().replace('"count":0', '"count":-0')
    assert step["payload_text"].count('"count":-0') == 1
    yield case("minus-zero", step)

    # Texts a JSON body can carry and UTF-8 cannot: an escaped lone half of a UTF-16 pair. They
    # stand only in the raw JSON texts ("body_text", "envelope_text"), which these vectors keep
    # ASCII. The contract refuses them at their field; nothing reaches storage (R71).
    half = "\ud800"
    yield case("half-pair-submission-id", publish(),
               {"action": "submit", "http": 400, "error": refused("invalid_submission_id", "/submission_id"),
                "body_text": json.dumps({**good, "submission_id": "a" * 31 + half})})
    for field in ("session", "title", "summary", "status", "reference_id", "artifact_id"):
        yield case("half-pair-" + field.replace("_", "-"),
                   {**publish(error=refused("invalid_json", "/" + field)),
                    "envelope_text": json.dumps({field: "text " + half})})
    # An update whose title, summary or status is not text is refused at that field (R72).
    for field, value in (("title", 7), ("summary", None), ("status", ["ready"])):
        yield case("update-" + field + "-not-text", publish(),
                   {"action": "update", "payload": form(), "envelope": {field: value}, "http": 400,
                    "error": refused("invalid_json", "/" + field)})
    # A body nested 512 levels deep is read; 513 is the reader's 400 for invalid JSON, on every
    # route, before anything parses it (R73).
    for depth, http in ((512, 201), (513, 400)):
        yield case(f"body-depth-{depth}", {"action": "read", "http": http,
                   "body_text": '{"x":' + "[" * (depth - 1) + "]" * (depth - 1) + "}"})


if __name__ == "__main__":
    import sys
    sys.stdout.buffer.write(encoded({**WIRE, "scenarios": list(scenarios())}) + b"\n")
